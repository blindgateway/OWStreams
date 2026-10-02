#include "playbackcontroller.h"

#include <QAudioOutput>
#include <QMediaMetaData>
#include <QMediaPlayer>
#include <QMediaTimeRange>
#include <QTimer>
#include <QVideoSink>

#include "apiclient.h"
#include "apierror.h"

namespace {

/// Seconds, for the API, from a millisecond position.
///
/// Divided rather than truncated on purpose: a truncated position is up to a
/// second *behind* where the viewer is, and "continue watching" that starts a
/// second early every time is exactly the sort of thing that makes a resume
/// feel subtly wrong.
double secondsFromMs(qint64 ms)
{
    return double(ms) / 1000.0;
}

/// The name to show for one track.
///
/// This Qt has no `QMediaPlayer::audioTrackType`: the track lists are lists of
/// `QMediaMetaData` holding whatever the demuxer managed to parse.  A matroska
/// file tags its tracks with a language; an MPEG-TS does not, and then there
/// is nothing to invent but the ordinal.  The language is what a picker
/// actually shows -- "English", "Commentary" -- so it is preferred over the
/// container's own title, and the ordinal is the last resort rather than the
/// first, which is the order that produces useful labels on the formats people
/// actually have.
QString trackName(const QMediaMetaData &meta, int index)
{
    const QString language = meta.stringValue(QMediaMetaData::Language).trimmed();
    if (!language.isEmpty())
        return language;
    const QString titled = meta.stringValue(QMediaMetaData::Title).trimmed();
    if (!titled.isEmpty())
        return titled;
    return QObject::tr("Track %1").arg(index + 1);
}

}  // namespace

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

PlaybackController::PlaybackController(ApiClient *api, QObject *parent)
    : PlaybackController(api, new QMediaPlayer(), new QAudioOutput(), parent)
{
    // Parented here rather than left to the base constructor: an audio output
    // that outlives the controller is a device still being written to after
    // the screen that owned it has gone.
    m_player->setParent(this);
    m_audioOutput->setParent(this);
    m_player->setAudioOutput(m_audioOutput);
}

PlaybackController::PlaybackController(ApiClient *api, QMediaPlayer *player,
                                       QAudioOutput *audioOutput, QObject *parent)
    : QObject(parent),
      m_api(api),
      m_controlsTimer(new QTimer(this))
{
    // Adopted, not owned: a test makes these and destroys them.
    m_player = player;
    m_audioOutput = audioOutput;
    if (m_audioOutput) {
        m_audioOutput->setVolume(float(m_volume));
        m_audioOutput->setMuted(m_muted);
    }

    if (m_player) {
        connect(m_player, &QMediaPlayer::mediaStatusChanged,
                this, &PlaybackController::onMediaStatusChanged);
        connect(m_player, &QMediaPlayer::playbackStateChanged,
                this, &PlaybackController::onPlaybackStateChanged);
        connect(m_player, &QMediaPlayer::positionChanged,
                this, &PlaybackController::onPositionChanged);
        connect(m_player, &QMediaPlayer::durationChanged,
                this, &PlaybackController::onDurationChanged);
        connect(m_player, &QMediaPlayer::seekableChanged,
                this, &PlaybackController::onSeekableChanged);
        connect(m_player, &QMediaPlayer::bufferProgressChanged,
                this, [this](float) { refreshBufferedDuration(); });
        connect(m_player, &QMediaPlayer::errorOccurred, this,
                [this](QMediaPlayer::Error, const QString &) { onPlayerError(); });
        connect(m_player, &QMediaPlayer::tracksChanged,
                this, &PlaybackController::onTracksChanged);
        connect(m_player, &QMediaPlayer::activeTracksChanged,
                this, &PlaybackController::onActiveTracksChanged);

        // A backend may report a rate other than 1.0 -- a remembered
        // preference, a platform hint.  The pause button means "normal speed",
        // so the rate is stated here rather than inherited.
        m_player->setPlaybackRate(1.0);
    }

    if (m_api) {
        connect(m_api, &ApiClient::playbackStarted,
                this, &PlaybackController::onPlaybackStarted);
        // Every API failure is funnelled through one signal tagged with the
        // operation.  Only `playback.start` is fatal to the player: a dropped
        // progress report loses one position, and treating that as an error
        // would tear down a film that is playing perfectly well over it.
        connect(m_api, &ApiClient::failed, this,
                [this](const QString &operation, const ApiError &error) {
                    if (operation != QLatin1String("playback.start"))
                        return;
                    if (error.kind == ApiErrorKind::Gone) {
                        // 410 is not "something went wrong".  The server has
                        // already recorded that this title's file is not there,
                        // so the message says so: it is the difference between
                        // a person re-scanning their library and a person
                        // filing a bug.
                        failWith(error.message.isEmpty()
                                     ? tr("This title's file is missing from the server.")
                                     : error.message);
                        return;
                    }
                    if (!error.message.isEmpty()) {
                        failWith(error.message);
                        return;
                    }
                    failWith(tr("The server could not start this title."));
                });
    }

    // Three seconds of no input and the controls go away -- but only while
    // something is actually playing.  Hiding them during a stall, or before
    // the first frame, is how a player ends up looking broken while loading.
    m_controlsTimer->setSingleShot(true);
    m_controlsTimer->setInterval(kControlsHideDelayMs);
    connect(m_controlsTimer, &QTimer::timeout, this,
            &PlaybackController::onControlsTimeout);
}

PlaybackController::~PlaybackController() = default;

void PlaybackController::setVideoOutput(QVideoSink *sink)
{
    if (m_videoOutput == sink)
        return;
    // Detach before dropping the reference.  A sink destroyed while still
    // attached leaves the backend writing frames into freed memory, and the
    // QML side destroys the item -- and with it the sink -- first when a
    // Loader swaps the screen out.
    if (m_player && m_videoOutput)
        m_player->setVideoSink(nullptr);
    m_videoOutput = sink;
    if (m_player && m_videoOutput)
        m_player->setVideoSink(m_videoOutput);
}

// ---------------------------------------------------------------------------
// Opening
// ---------------------------------------------------------------------------

void PlaybackController::open(int mediaId, const QVariantMap &media)
{
    // Opening a title is also closing the previous one, through the same path:
    // the old session gets finished and its position recorded before a new
    // token is minted, rather than whenever the caller remembered to do it.
    close();

    if (m_mediaId != mediaId) {
        m_mediaId = mediaId;
        emit mediaIdChanged();
    }
    const QString newTitle = media.value(QStringLiteral("title")).toString();
    if (m_title != newTitle) {
        m_title = newTitle;
        emit titleChanged();
    }

    // The same authenticated artwork route every rail uses.  `artworkUrl` is
    // the one place that route's shape is written down, and it answers empty
    // for a title with no poster key -- so the `Image` is never pointed at a
    // URL that is guaranteed to 404 while the film loads.
    const bool hasPoster =
        !media.value(QStringLiteral("poster_key")).toString().isEmpty();
    const QString newPoster = m_api
        ? m_api->artworkUrl(mediaId, QStringLiteral("poster"), hasPoster).toString()
        : QString();
    if (m_posterUrl != newPoster) {
        m_posterUrl = newPoster;
        emit posterUrlChanged();
    }

    // Resume is the server's decision as much as ours: it honours the offset
    // that is sent, but sending the stored position keeps the server's history
    // row and this player agreeing on where the film starts even when the
    // client was closed with the film paused somewhere unusual.
    double resumeSeconds = 0.0;
    const QVariant progress = media.value(QStringLiteral("progress"));
    if (progress.canConvert<QVariantMap>()) {
        const QVariant stored =
            progress.toMap().value(QStringLiteral("position_seconds"));
        if (stored.isValid())
            resumeSeconds = stored.toDouble();
    }

    setState(Preparing);
    if (m_api)
        m_api->startPlayback(mediaId, resumeSeconds);
}

void PlaybackController::onPlaybackStarted(int mediaId, const QString &streamUrl,
                                           double startOffset, int expiresInSeconds)
{
    // A token for a title the player has since left behind is stale news: the
    // screen is not showing it, and honouring it would start audio in a room
    // nobody asked for.
    if (mediaId != m_mediaId)
        return;
    Q_UNUSED(expiresInSeconds)

    const QUrl url(streamUrl);
    if (url.isEmpty()) {
        failWith(tr("The server did not return a playable source."));
        return;
    }

    m_source = url;
    emit sourceChanged();

    // The resume seek is *remembered*, not applied.  `setPosition` into a
    // source the backend has not opened yet is ignored by every backend there
    // is, and `onDurationChanged` applies it the moment the file's length is
    // known.  Seeking here instead would resume at zero, which is the bug this
    // ordering exists to prevent.
    m_pendingSeekMs = qint64(startOffset * 1000.0);
    if (m_pendingSeekMs < 0)
        m_pendingSeekMs = 0;

    setState(Buffering);
    if (m_player) {
        m_player->setPlaybackRate(1.0);
        m_player->setSource(url);
    }
    play();
}

void PlaybackController::applyPendingSeek()
{
    if (m_pendingSeekMs < 0 || !m_player)
        return;
    const qint64 target = m_pendingSeekMs;
    m_pendingSeekMs = -1;
    m_player->setPosition(target);
    if (m_position != target) {
        m_position = target;
        emit positionChanged();
    }
    // The progress clock runs from the resume point, not from zero.  Fifteen
    // seconds of *watching* is the interval, and the part of the film watched
    // before this session began is not part of it.
    m_lastReportedPosition = target;
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

void PlaybackController::play()
{
    if (!m_player)
        return;
    // Refusing to play out of an error state is what makes the screen's retry
    // button the only way back: a second `play()` over a broken source would
    // put the player into a state where the picture is frozen and the state
    // says "playing".
    if (m_state == Error)
        return;
    m_player->play();
}

void PlaybackController::pause()
{
    if (!m_player)
        return;
    // Flushed before the pause, not after: what gets reported should be the
    // position still visible on the screen, not wherever the backend lands a
    // few milliseconds later.
    if (m_isPlaying || m_state == Playing || m_state == Buffering)
        flushProgress(false);
    m_player->pause();
}

void PlaybackController::togglePlay()
{
    if (!m_player) {
        // With no backend the state machine *is* the behaviour, and a test that
        // calls `togglePlay()` twice must see it return to where it started
        // rather than crash on a null pointer.
        setState(m_state == Playing ? Paused : Playing);
        return;
    }
    if (m_isPlaying || m_state == Playing || m_state == Buffering)
        pause();
    else
        play();
}

void PlaybackController::stop()
{
    if (m_player) {
        if (m_isPlaying || m_state == Playing || m_state == Buffering)
            flushProgress(false);
        m_player->stop();
    }
    setState(Stopped);
}

void PlaybackController::close()
{
    // Deliberately first, and deliberately *before* the player is stopped:
    // stopping rewinds the backend, so a close that stopped first would report
    // position zero and erase the viewer's history.
    // Only a real session gets closed.  `open()` calls this first, and
    // closing a title that never started would tell the server somebody
    // watched nothing at position zero.
    const bool hadSession = !m_source.isEmpty();
    flushProgress(true);
    if (hadSession)
        emit playbackSessionClosed();

    if (m_player) {
        m_player->stop();
        m_player->setSource(QUrl());
    }

    m_pendingSeekMs = -1;
    m_lastReportedPosition = 0;
    if (!m_source.isEmpty()) {
        m_source = QUrl();
        emit sourceChanged();
    }
    if (m_position != 0) {
        m_position = 0;
        emit positionChanged();
    }
    if (m_duration != 0) {
        m_duration = 0;
        emit durationChanged();
    }
    if (m_bufferedDuration != 0) {
        m_bufferedDuration = 0;
        emit bufferedDurationChanged();
    }
    if (m_seekable) {
        m_seekable = false;
        emit seekableChanged();
    }
    if (m_isPlaying) {
        m_isPlaying = false;
        emit isPlayingChanged();
    }
    if (!m_errorMessage.isEmpty()) {
        m_errorMessage.clear();
        emit errorMessageChanged();
    }
    setControlsVisible(true);
    setState(Idle);
}

void PlaybackController::requestNext()
{
    emit nextRequested();
}

void PlaybackController::requestPrevious()
{
    emit previousRequested();
}

// ---------------------------------------------------------------------------
// Seeking
// ---------------------------------------------------------------------------

void PlaybackController::seek(qint64 positionMs)
{
    // Clamped rather than trusted.  A scrubber can be dragged past the end and
    // a relative seek can run off either side; a backend handed a position
    // outside the file either clamps it itself -- slowly, having already shown
    // the wrong time -- or refuses the seek outright.
    if (positionMs < 0)
        positionMs = 0;
    if (m_duration > 0 && positionMs > m_duration)
        positionMs = m_duration;

    if (m_position != positionMs) {
        m_position = positionMs;
        emit positionChanged();
    }

    // A seek is a statement about where the viewer is now, so it counts as
    // progress: someone who skips forward ten minutes and then closes the app
    // should not come back to where they started watching.
    maybeReportProgress();

    if (m_player && m_seekable)
        m_player->setPosition(positionMs);
}

void PlaybackController::seekRelative(qint64 deltaMs)
{
    seek(m_position + deltaMs);
}

// ---------------------------------------------------------------------------
// Volume
// ---------------------------------------------------------------------------

void PlaybackController::setVolume(double value)
{
    if (value < 0.0)
        value = 0.0;
    if (value > 1.0)
        value = 1.0;
    if (m_volume == value)
        return;
    m_volume = value;
    if (m_audioOutput)
        m_audioOutput->setVolume(float(value));
    emit volumeChanged();

    // Turning the volume up unmutes, because a slider reading 60% while the
    // mute toggle says "muted" is a picture and a state that disagree about
    // whether there is sound.  Coming back *down* to zero does not mute:
    // dragging to zero and back up is a thing people do on purpose, and a
    // player that mutes itself there is a player that surprises you.
    if (value > 0.0 && m_muted)
        setMuted(false);
}

void PlaybackController::setMuted(bool muted)
{
    if (m_muted == muted)
        return;
    m_muted = muted;
    if (m_audioOutput)
        m_audioOutput->setMuted(muted);
    emit mutedChanged();
}

void PlaybackController::toggleMuted()
{
    setMuted(!m_muted);
}

// ---------------------------------------------------------------------------
// Tracks
// ---------------------------------------------------------------------------

void PlaybackController::onTracksChanged()
{
    QStringList audioNames;
    QStringList subtitleNames;
    if (m_player) {
        const QList<QMediaMetaData> audioTracks = m_player->audioTracks();
        for (int i = 0; i < audioTracks.size(); ++i)
            audioNames << trackName(audioTracks.at(i), i);
        const QList<QMediaMetaData> subtitleTracks = m_player->subtitleTracks();
        for (int i = 0; i < subtitleTracks.size(); ++i)
            subtitleNames << trackName(subtitleTracks.at(i), i);
    }
    m_audioTrackNames = audioNames;
    m_subtitleTrackNames = subtitleNames;

    if (m_player) {
        m_audioTrackIndex = m_player->activeAudioTrack();
        m_subtitleTrackIndex = m_player->activeSubtitleTrack();
    } else {
        m_audioTrackIndex = -1;
        m_subtitleTrackIndex = -1;
    }
    // A backend that reports one implicit track and no active index is playing
    // track zero, and a picker that says "off" while audio comes out of it is
    // worse than no picker at all.
    if (m_audioTrackIndex < 0 && !audioNames.isEmpty())
        m_audioTrackIndex = 0;

    emit tracksChanged();
    emit audioTrackIndexChanged();
    emit subtitleEnabledChanged();
}

void PlaybackController::onActiveTracksChanged()
{
    if (!m_player)
        return;
    const int audio = m_player->activeAudioTrack();
    const int subtitles = m_player->activeSubtitleTrack();
    if (m_audioTrackIndex != audio) {
        m_audioTrackIndex = audio;
        emit audioTrackIndexChanged();
    }
    if (m_subtitleTrackIndex != subtitles) {
        m_subtitleTrackIndex = subtitles;
        emit subtitleEnabledChanged();
    }
}

void PlaybackController::setAudioTrack(int index)
{
    // An out-of-range index is recorded rather than applied.  Ignoring it
    // silently would leave the picker showing a selection that is not playing,
    // which is the specific failure this avoids.
    if (index < 0 || index >= m_audioTrackNames.size()) {
        if (m_audioTrackIndex != index) {
            m_audioTrackIndex = index;
            emit audioTrackIndexChanged();
        }
        return;
    }
    if (m_player)
        m_player->setActiveAudioTrack(index);
    if (m_audioTrackIndex != index) {
        m_audioTrackIndex = index;
        emit audioTrackIndexChanged();
    }
    emit tracksChanged();
}

void PlaybackController::cycleAudioTrack()
{
    const int count = m_audioTrackNames.size();
    if (count <= 0)
        return;
    setAudioTrack((m_audioTrackIndex + 1) % count);
}

void PlaybackController::toggleSubtitles()
{
    // Off -> first -> second -> ... -> off.  A `subtitleTrackIndex` of -1 *is*
    // "off", so there is no second boolean that can disagree with it -- which
    // is what lets `subtitleEnabled` be derived rather than stored.
    if (m_subtitleTrackNames.isEmpty())
        return;
    if (m_subtitleTrackIndex < 0)
        setSubtitleTrack(0);
    else if (m_subtitleTrackIndex + 1 >= m_subtitleTrackNames.size())
        setSubtitleTrack(-1);
    else
        setSubtitleTrack(m_subtitleTrackIndex + 1);
}

void PlaybackController::cycleSubtitleTrack()
{
    if (m_subtitleTrackNames.isEmpty())
        return;
    if (m_subtitleTrackIndex + 1 >= m_subtitleTrackNames.size())
        setSubtitleTrack(-1);
    else
        setSubtitleTrack(m_subtitleTrackIndex + 1);
}

void PlaybackController::setSubtitleTrack(int index)
{
    if (index >= m_subtitleTrackNames.size())
        index = -1;
    if (m_player)
        m_player->setActiveSubtitleTrack(index);
    if (m_subtitleTrackIndex != index) {
        m_subtitleTrackIndex = index;
        emit subtitleEnabledChanged();
    }
    emit tracksChanged();
}

// ---------------------------------------------------------------------------
// Progress
// ---------------------------------------------------------------------------

void PlaybackController::maybeReportProgress()
{
    // Never with nothing loaded.  A position for a title that has not opened
    // writes a zero into the server's history row, and a zero that arrives
    // after a real session has already ended would be worse than sending
    // nothing at all.
    if (m_api == nullptr || m_mediaId == 0 || m_source.isEmpty())
        return;
    // Measured in *playback position*, not wall clock.  A player left paused in
    // the background for an hour has not watched an hour and must not spend
    // fifteen-second intervals saying so; a film at 1.5x reaches the interval
    // in ten seconds of real time and reports more often, which is the correct
    // reading of the cost.
    if (m_position - m_lastReportedPosition < kProgressReportIntervalMs)
        return;
    flushProgress(false);
}

void PlaybackController::flushProgress(bool complete)
{
    if (m_api == nullptr || m_mediaId == 0 || m_source.isEmpty())
        return;

    const qint64 position = m_position;
    m_lastReportedPosition = position;
    if (complete)
        m_api->finishPlayback(m_mediaId, secondsFromMs(position));
    else
        m_api->reportProgress(m_mediaId, secondsFromMs(position),
                              secondsFromMs(m_duration));
    emit progressReported(position, m_duration);
}

// ---------------------------------------------------------------------------
// Backend callbacks
// ---------------------------------------------------------------------------

void PlaybackController::onPositionChanged(qint64 position)
{
    if (m_position == position)
        return;
    m_position = position;
    emit positionChanged();
    // This is the signal that fires sixty times a second; the interval test
    // inside is what keeps that from being sixty requests a second.
    maybeReportProgress();
}

void PlaybackController::onDurationChanged(qint64 duration)
{
    if (m_duration == duration)
        return;
    m_duration = duration;
    emit durationChanged();
    // Applied here rather than at `setSource` time: a backend cannot seek into
    // a source whose length it has not reported yet.
    applyPendingSeek();
}

void PlaybackController::onSeekableChanged(bool seekable)
{
    if (m_seekable == seekable)
        return;
    m_seekable = seekable;
    emit seekableChanged();
}

void PlaybackController::onPlaybackStateChanged()
{
    if (!m_player)
        return;
    const bool playing = m_player->playbackState() == QMediaPlayer::PlayingState;
    if (m_isPlaying != playing) {
        m_isPlaying = playing;
        emit isPlayingChanged();
    }
    if (playing) {
        setState(Playing);
    } else if (m_player->playbackState() == QMediaPlayer::PausedState) {
        setState(Paused);
    } else if (m_player->playbackState() == QMediaPlayer::StoppedState
               && m_state != Error) {
        setState(Stopped);
    }
}

void PlaybackController::onMediaStatusChanged()
{
    if (!m_player)
        return;
    switch (m_player->mediaStatus()) {
    case QMediaPlayer::LoadingMedia:
        setState(Buffering);
        break;
    case QMediaPlayer::BufferingMedia:
    case QMediaPlayer::StalledMedia:
        // A stall is buffering, not an error.  The screen shows a spinner for
        // one and a message with a retry button for the other, and treating a
        // two-second underrun as a failure would tear down a film that was
        // about to carry on playing by itself.
        setState(Buffering);
        break;
    case QMediaPlayer::LoadedMedia:
    case QMediaPlayer::BufferedMedia:
        // An error outranks a loaded source: a backend that says the media is
        // fine *and* that it could not be played is reporting the error later,
        // not earlier.
        if (m_state != Error)
            setState(m_isPlaying ? Playing : (m_position > 0 ? Paused : Buffering));
        break;
    case QMediaPlayer::EndOfMedia:
        flushProgress(true);
        setState(Stopped);
        emit finished();
        break;
    case QMediaPlayer::InvalidMedia:
        onPlayerError();
        break;
    case QMediaPlayer::NoMedia:
        break;
    }
}

void PlaybackController::refreshBufferedDuration()
{
    if (!m_player)
        return;
    // `QMediaTimeRange` is in the same unit as `position()` -- milliseconds.
    // Multiplying by 1000 here made a buffered range that started at one second
    // read as sixteen minutes, so the scrubber drew a buffer bar that ran past
    // the end of the film.
    const qint64 buffered = m_player->bufferedTimeRange().latestTime();
    if (m_bufferedDuration == buffered)
        return;
    m_bufferedDuration = buffered;
    emit bufferedDurationChanged();
}

void PlaybackController::onPlayerError()
{
    if (!m_player)
        return;
    // The backend's own string first, because it is specific ("HTTP 410") and
    // the fallbacks below are for the cases where it said nothing at all.
    // They are chosen per error enum rather than being one string, because
    // "media error" is not something a person can act on.
    QString message = m_player->errorString();
    if (message.isEmpty()) {
        switch (m_player->error()) {
        case QMediaPlayer::ResourceError:
            message = tr("The stream stopped. Check the connection and try again.");
            break;
        case QMediaPlayer::FormatError:
            message = tr("This file's format cannot be played on this device.");
            break;
        case QMediaPlayer::NetworkError:
            message = tr("The connection to the server dropped.");
            break;
        case QMediaPlayer::AccessDeniedError:
            message = tr("The server refused the playback token for this title.");
            break;
        case QMediaPlayer::NoError:
            message = tr("Playback failed.");
            break;
        }
    }
    failWith(message);
}

void PlaybackController::failWith(const QString &message)
{
    setErrorMessage(message);
    setState(Error);
    emit errorOccurred(message);
}

void PlaybackController::setState(State state)
{
    if (m_state == state)
        return;
    m_state = state;
    emit stateChanged();
    if (state == Playing) {
        armControlsTimer();
    } else if (state != Buffering && state != Preparing) {
        // Anything that is not playing and not waiting on the network puts the
        // controls back, because they are what a viewer needs in order to fix
        // whatever just happened.
        m_controlsTimer->stop();
        setControlsVisible(true);
    }
}

void PlaybackController::setErrorMessage(const QString &message)
{
    if (m_errorMessage == message)
        return;
    m_errorMessage = message;
    emit errorMessageChanged();
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------

void PlaybackController::setControlsVisible(bool visible)
{
    if (m_controlsVisible == visible)
        return;
    m_controlsVisible = visible;
    emit controlsVisibleChanged();
    // Bringing them back restarts the clock, so "no input for three seconds"
    // is measured from the last time they were asked for rather than from
    // whenever the previous hide happened to land.
    if (visible)
        armControlsTimer();
}

void PlaybackController::armControlsTimer()
{
    // Armed only while something is playing.  A viewer who pauses gets the
    // controls and keeps them, which is what a paused screen wants: there is
    // nothing to hide them for.
    if (m_state == Playing)
        m_controlsTimer->start();
}

void PlaybackController::onControlsTimeout()
{
    if (m_state == Playing)
        setControlsVisible(false);
}
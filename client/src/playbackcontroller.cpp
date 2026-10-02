#include "playbackcontroller.h"

#include <QAudioOutput>
#include <QMetaEnum>
#include <QMediaMetaData>
#include <QMediaPlayer>
#include <QMediaTimeRange>
#include <QTimer>

#include "apiclient.h"
#include "apierror.h"

namespace {

constexpr double kVolumeStep = 0.05;
constexpr qint64 kSeekStepMs = 10 * 1000;

/// Seconds, for the API, from a millisecond position.
///
/// Rounded rather than truncated: a truncated position is up to a second
/// *behind* where the viewer is, and "continue watching" that starts a second
/// early every time is the kind of thing that makes a resume feel wrong.
double secondsFromMs(qint64 ms)
{
    return double(ms) / 1000.0;
}

/// The name to show for a track, when the container gave us one.
///
/// A matroska file names its tracks; an MPEG-TS does not, and there is nothing
/// to invent but the ordinal.  `QMediaMetaData::stringValue` is the accessor
/// for whatever the backend managed to parse, and it is safe on a metadata
/// entry with nothing in it.
QString trackName(const QMediaMetaData &meta, int index)
{
    const QString named = meta.stringValue(QMediaMetaData::Title).trimmed();
    if (!named.isEmpty())
        return named;

    // Two pass: the language tag is what a picker actually shows ("English",
    // "Commentary"), so it wins over the ordinal, and the two are combined when
    // both exist rather than one replacing the other.
    const QString language = meta.stringValue(QMediaMetaData::Language).trimmed();
    const QString kind = meta.stringValue(QMediaMetaData::Genre).trimmed();
    if (!language.isEmpty() && !kind.isEmpty() && language != kind)
        return language + QLatin1String(" · ") + kind;
    if (!language.isEmpty())
        return language;
    if (!kind.isEmpty())
        return kind;

    return QObject::tr("Track %1").arg(index + 1);
}

}  // namespace

// ---------------------------------------------------------------------------
// Construction
// ---------------------------------------------------------------------------

PlaybackController::PlaybackController(ApiClient *api, QObject *parent)
    : PlaybackController(api, new QMediaPlayer(), new QAudioOutput(), parent)
{
    m_player->setAudioOutput(m_audioOutput);
    setVideoOutput(nullptr);
}

PlaybackController::PlaybackController(ApiClient *api, QMediaPlayer *player,
                                       QAudioOutput *audioOutput, QObject *parent)
    : QObject(parent),
      m_api(api),
      m_controlsTimer(new QTimer(this))
{
    adopt(player, audioOutput);
    connectPlayer();

    // Three seconds of no input and the controls go away -- but only while
    // something is actually playing.  Hiding them during a stall, or before
    // the first frame, is how a player ends up looking broken when it is
    // loading.
    m_controlsTimer->setSingleShot(true);
    m_controlsTimer->setInterval(kControlsHideDelayMs);
    connect(m_controlsTimer, &QTimer::timeout, this,
            &PlaybackController::onControlsTimeout);
}

PlaybackController::~PlaybackController() = default;

void PlaybackController::adopt(QMediaPlayer *player, QAudioOutput *audioOutput)
{
    m_player = player;
    m_audioOutput = audioOutput;
    if (m_audioOutput) {
        m_audioOutput->setVolume(float(m_volume));
        m_audioOutput->setMuted(m_muted);
    }
}

void PlaybackController::connectPlayer()
{
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
        connect(m_player, &QMediaPlayer::errorOccurred,
                this, [this](QMediaPlayer::Error, const QString &) { onPlayerError(); });
        connect(m_player, &QMediaPlayer::tracksChanged,
                this, &PlaybackController::onTracksChanged);
        connect(m_player, &QMediaPlayer::activeTracksChanged,
                this, &PlaybackController::onActiveTracksChanged);

        // A backend is free to report a rate other than 1.0 (a remembered
        // preference, a platform hint); "play at normal speed" is what the
        // pause button means, so it is stated rather than inherited.
        m_player->setPlaybackRate(1.0);
    }

    if (m_api) {
        connect(m_api, &ApiClient::playbackStarted,
                this, &PlaybackController::onPlaybackStarted);
        // Every API failure is funnelled through one signal, tagged with the
        // operation.  Only `playback.start` is fatal to the player: a failed
        // progress report is a lost position, not a lost film, and treating it
        // as an error would tear down a working screen over one dropped PUT.
        connect(m_api, &ApiClient::failed, this, [this](const QString &operation,
                                                       const ApiError &error) {
            if (operation != QLatin1String("playback.start"))
                return;
            if (error.kind == ApiErrorKind::Gone) {
                // 410 is not "something went wrong": the server has already
                // recorded that this title's file is not there.  Saying so is
                // the difference between a person re-scanning the library and
                // a person filing a bug.
                setErrorMessage(error.message.isEmpty()
                                    ? tr("This title's file is missing from the server.")
                                    : error.message);
            } else if (!error.message.isEmpty()) {
                setErrorMessage(error.message);
            } else {
                setErrorMessage(tr("The server could not start this title."));
            }
            setState(Error);
            emit errorOccurred(m_errorMessage);
        });
    }
}

void PlaybackController::setVideoOutput(QObject *item)
{
    if (m_videoOutput == item)
        return;
    // Detach before dropping the reference: a `VideoOutput` destroyed while
    // still attached leaves the backend holding a dangling sink.
    if (m_player && m_videoOutput)
        m_player->setVideoOutput(nullptr);
    m_videoOutput = item;
    if (m_player && m_videoOutput)
        m_player->setVideoOutput(m_videoOutput);
}

// ---------------------------------------------------------------------------
// Opening
// ---------------------------------------------------------------------------

void PlaybackController::open(int mediaId, const QVariantMap &media)
{
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

    // The poster is the same authenticated artwork route every rail uses, and
    // `Image` reaches it with no token of its own, so the URL is assembled here
    // from what the API already knows rather than being passed down as state.
    const QVariant posterKey = media.value(QStringLiteral("poster_key"));
    QString newPoster;
    if (m_api && !posterKey.toString().isEmpty()) {
        newPoster = m_api->baseUrl() + QStringLiteral("/api/v1/media/")
                    + QString::number(mediaId) + QStringLiteral("/poster");
    }
    if (m_posterUrl != newPoster) {
        m_posterUrl = newPoster;
        emit posterUrlChanged();
    }

    // Resume is the server's decision as much as ours: it also honours the
    // offset we send, but sending the stored position means the server's
    // history row and this player agree on where the film starts even if the
    // client was closed while the film was paused somewhere unusual.
    double resumeSeconds = 0.0;
    const QVariant progress = media.value(QStringLiteral("progress"));
    if (progress.canConvert<QVariantMap>()) {
        const QVariantMap progressMap = progress.toMap();
        const QVariant stored = progressMap.value(QStringLiteral("position_seconds"));
        if (stored.isValid())
            resumeSeconds = stored.toDouble();
    }

    m_hasReported = false;
    m_lastReportedPosition = 0;
    setState(Preparing);
    if (m_api)
        m_api->startPlayback(mediaId, resumeSeconds);
}

void PlaybackController::onPlaybackStarted(int mediaId, const QString &streamUrl,
                                           double startOffset, int expiresInSeconds)
{
    // A token for a title the player has since left behind is stale news: the
    // screen will not be showing it, and opening it would start audio nobody
    // asked for.
    if (mediaId != m_mediaId)
        return;
    Q_UNUSED(expiresInSeconds)

    const QUrl url(streamUrl);
    if (!url.isValid() || url.isEmpty()) {
        setErrorMessage(tr("The server did not return a playable source."));
        setState(Error);
        emit errorOccurred(m_errorMessage);
        return;
    }

    m_source = url;
    emit sourceChanged();

    // Seek *before* play.  `setPosition` on a source that has not been opened
    // yet is a no-op in every backend, which is exactly what happens if the
    // seek follows the play; `onDurationChanged` applies it again once the
    // backend knows how long the file is.
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
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

void PlaybackController::play()
{
    if (!m_player)
        return;
    if (m_state == Error)
        return;
    m_player->play();
}

void PlaybackController::pause()
{
    if (!m_player)
        return;
    // The flush happens *before* the pause so the reported position is the one
    // the viewer can still see on the screen, rather than whatever the backend
    // lands on a few milliseconds later.
    if (m_isPlaying || m_state == Playing || m_state == Buffering)
        flushProgress(false);
    m_player->pause();
}

void PlaybackController::togglePlay()
{
    if (!m_player) {
        // Still a valid transition: with no backend the state machine is the
        // whole of the behaviour, and a test that drives `togglePlay()` twice
        // must see it return to where it started rather than crash.
        setState(m_state == Playing ? Paused : Playing);
        return;
    }
    if (m_isPlaying || m_state == Playing) {
        pause();
    } else {
        play();
    }
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
    // Ordered deliberately: the position is read from the backend *before* it
    // is stopped, because stopping rewinds it.  A close that reports zero is a
    // close that erases the viewer's history.
    flushProgress(true);
    if (m_player) {
        m_player->stop();
        m_player->setSource({});
    }
    setState(Idle);
    if (!m_source.isEmpty()) {
        m_source = QUrl();
        emit sourceChanged();
    }
    m_position = 0;
    m_duration = 0;
    m_bufferedDuration = 0;
    m_pendingSeekMs = -1;
    m_seekable = false;
    m_isPlaying = false;
    setControlsVisible(true);
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
    if (positionMs < 0)
        positionMs = 0;
    if (m_duration > 0 && positionMs > m_duration)
        positionMs = m_duration;

    m_position = positionMs;
    emit positionChanged();

    // A seek is a statement about where the viewer is now, so it counts as
    // progress: someone who skips forward for ten minutes and then closes the
    // app should not resume from where they started watching.
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
    if (qFuzzyCompare(m_volume, value))
        return;
    m_volume = value;
    if (m_audioOutput)
        m_audioOutput->setVolume(float(value));
    emit volumeChanged();

    // Dragging a volume slider to zero is a mute, and leaving it there while
    // the unmuted icon shows means the picture and the state disagree.  Coming
    // back off zero does not unmute: that is what the mute toggle is for.
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
    QStringList audio;
    QStringList subtitles;
    if (m_player) {
        const QList<QMediaMetaData> audioTracks = m_player->audioTracks();
        for (int i = 0; i < audioTracks.size(); ++i)
            audio << trackName(audioTracks.at(i), i);
        const QList<QMediaMetaData> subtitleTracks = m_player->subtitleTracks();
        for (int i = 0; i < subtitleTracks.size(); ++i)
            subtitles << trackName(subtitleTracks.at(i), i);
    }
    m_audioTrackNames = audio;
    m_subtitleTrackNames = subtitles;

    // The backend tells us which track it chose; when it did not (a format that
    // carries one implicit track), the first one is what is playing and saying
    // "off" would be a lie.
    if (m_player) {
        const int activeAudio = m_player->activeAudioTrack();
        m_audioTrackIndex = activeAudio >= 0 ? activeAudio : (audio.isEmpty() ? -1 : 0);
        const int activeSubtitle = m_player->activeSubtitleTrack();
        m_subtitleTrackIndex = activeSubtitle;
    } else {
        m_audioTrackIndex = audio.isEmpty() ? -1 : 0;
        m_subtitleTrackIndex = -1;
    }
    emit tracksChanged();
    emit audioTrackIndexChanged();
    emit subtitleTrackIndexChanged();
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
        emit subtitleTrackIndexChanged();
    }
}

void PlaybackController::setAudioTrack(int index)
{
    if (m_audioTrackNames.isEmpty())
        return;
    if (index < 0 || index >= m_audioTrackNames.size()) {
        if (m_audioTrackIndex != index) {
            m_audioTrackIndex = index;
            emit audioTrackIndexChanged();
            emit subtitleTrackIndexChanged();
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
    // Off -> first -> ... -> off.  `subtitleTrackIndex` of -1 *is* "off", so
    // the cycle is the whole truth about subtitles and there is no second
    // boolean that can disagree with it.
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
        emit subtitleTrackIndexChanged();
    }
    emit tracksChanged();
}

// ---------------------------------------------------------------------------
// Progress
// ---------------------------------------------------------------------------

void PlaybackController::maybeReportProgress()
{
    // Never report with nothing loaded: a position of zero for a title that
    // has not opened writes a zero into the server's history row and marks
    // nothing watched, which is worse than reporting nothing at all.
    if (m_mediaId == 0 || m_source.isEmpty())
        return;
    if (!m_hasReported) {
        flushProgress(false);
        return;
    }
    if (m_position - m_lastReportedPosition < kProgressReportIntervalMs)
        return;
    flushProgress(false);
}

void PlaybackController::flushProgress(bool complete)
{
    if (!m_api || m_mediaId == 0 || m_source.isEmpty())
        return;

    const qint64 position = m_position;
    m_lastReportedPosition = position;
    m_hasReported = true;
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
    // Throttled by the interval, not called unconditionally: this is the one
    // signal that fires sixty times a second.
    maybeReportProgress();
}

void PlaybackController::onDurationChanged(qint64 duration)
{
    if (m_duration == duration)
        return;
    m_duration = duration;
    emit durationChanged();
    // The resume seek is applied here rather than at `setSource` time,
    // because a backend ignores a seek into a source whose length it does not
    // know yet, and resuming at zero because the seek was too early is exactly
    // the bug this ordering exists to prevent.
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
        setControlsVisible(true);
        setState(Playing);
    } else if (m_player->playbackState() == QMediaPlayer::PausedState) {
        setState(Paused);
        setControlsVisible(true);
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
        // two-second underrun as a failure would tear down a film that is
        // about to carry on playing.
        setState(Buffering);
        break;
    case QMediaPlayer::LoadedMedia:
    case QMediaPlayer::BufferedMedia:
        if (m_state == Error)
            break;
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
    const QMediaTimeRange range = m_player->bufferedTimeRange();
    const qint64 buffered = range.latestTime() * 1000;
    if (m_bufferedDuration == buffered)
        return;
    m_bufferedDuration = buffered;
    emit bufferedDurationChanged();
}

void PlaybackController::onPlayerError()
{
    if (!m_player)
        return;
    const QString backendMessage = m_player->errorString();
    QString message = backendMessage;
    if (message.isEmpty()) {
        switch (m_player->error()) {
        case QMediaPlayer::ResourceError:
            // Either the backend cannot reach the server any more, or the file
            // went missing under it; both are worth naming as a connection
            // problem rather than "media error".
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
    if (state == Playing)
        armControlsTimer();
    else if (state != Buffering && state != Preparing) {
        // Anything that is not playing and not waiting on the network puts the
        // controls back: they are what the viewer needs to fix it.
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
    if (visible)
        armControlsTimer();
}

void PlaybackController::armControlsTimer()
{
    // Only ever armed while something is playing.  `setControlsVisible(true)`
    // from a paused player therefore leaves the controls up, which is what the
    // paused screen wants.
    if (m_state == Playing || m_state == Buffering)
        m_controlsTimer->start();
}

void PlaybackController::onControlsTimeout()
{
    if (m_state == Playing)
        setControlsVisible(false);
}
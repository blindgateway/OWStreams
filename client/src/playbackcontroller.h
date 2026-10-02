#pragma once

#include <QObject>
#include <QStringList>
#include <QUrl>
#include <QVariantMap>

class ApiClient;
class QAudioOutput;
class QMediaPlayer;
class QTimer;

/// Everything the player screen shows and everything it can ask for.
///
/// The split from the rest of the client is deliberate.  Navigation, library
/// state and the session live in `AppController`; the *media pipeline* lives
/// here, because it is the one part of the client with a lifecycle that is not
/// the window's: a title is opened, streams for an hour, and is closed, and
/// every one of those moments has a server-side consequence (a minted playback
/// token, a recorded position, a closed session).
///
/// Three properties of this class are load-bearing:
///
/// * **It never dereferences a media object it was not given.**  `QMediaPlayer`
///   needs a window and a platform plugin; a state machine and a progress
///   throttle do not.  Constructed with null media objects the whole class
///   still runs, which is the only way the resume and reporting logic can be
///   tested on a build machine with no display.
/// * **Progress reporting is throttled here, not in the API client.**  A
///   two-hour film reported on every position tick is four hundred requests and
///   the whole rate-limit budget of the `api` bucket with it.  Fifteen seconds
///   of *playback* -- not wall clock, so a paused player reports nothing -- is
///   the interval, and the last position is always flushed.
/// * **The source is a URL the server minted, never a path.**  `open()` asks
///   for a short-lived, title-scoped playback token; the bytes come back over
///   HTTP with Range, and the media backend attaches the token itself (see
///   `CredentialAccessManager`).  There is no second authentication path here,
///   because there must not be.
class PlaybackController : public QObject
{
    Q_OBJECT
    Q_PROPERTY(int mediaId READ mediaId NOTIFY mediaIdChanged)
    Q_PROPERTY(QString title READ title NOTIFY titleChanged)
    Q_PROPERTY(QString posterUrl READ posterUrl NOTIFY posterUrlChanged)
    Q_PROPERTY(QUrl source READ source NOTIFY sourceChanged)
    Q_PROPERTY(State state READ state NOTIFY stateChanged)
    Q_PROPERTY(QString errorMessage READ errorMessage NOTIFY errorMessageChanged)
    Q_PROPERTY(qint64 position READ position NOTIFY positionChanged)
    Q_PROPERTY(qint64 duration READ duration NOTIFY durationChanged)
    Q_PROPERTY(qint64 bufferedDuration READ bufferedDuration NOTIFY bufferedDurationChanged)
    Q_PROPERTY(bool seekable READ isSeekable NOTIFY seekableChanged)
    Q_PROPERTY(bool isPlaying READ isPlaying NOTIFY isPlayingChanged)
    Q_PROPERTY(bool controlsVisible READ controlsVisible NOTIFY controlsVisibleChanged)
    Q_PROPERTY(double volume READ volume NOTIFY volumeChanged)
    Q_PROPERTY(bool muted READ isMuted NOTIFY mutedChanged)
    Q_PROPERTY(bool subtitleEnabled READ subtitleEnabled NOTIFY subtitleEnabledChanged)
    Q_PROPERTY(int audioTrackIndex READ audioTrackIndex NOTIFY audioTrackIndexChanged)
    Q_PROPERTY(int subtitleTrackIndex READ subtitleTrackIndex NOTIFY subtitleTrackIndexChanged)
    Q_PROPERTY(QStringList audioTrackNames READ audioTrackNames NOTIFY tracksChanged)
    Q_PROPERTY(QStringList subtitleTrackNames READ subtitleTrackNames NOTIFY tracksChanged)
    Q_PROPERTY(bool audioTracksAvailable READ audioTracksAvailable NOTIFY tracksChanged)
    Q_PROPERTY(bool subtitleTracksAvailable READ subtitleTracksAvailable NOTIFY tracksChanged)

public:
    /// The seven things the player can be doing, in the order the screen cares
    /// about them.  `Buffering` is deliberately its own state and not a flavour
    /// of `Playing`: the screen shows a spinner for one and nothing for the
    /// other, and a rail that confuses them reads as broken.
    enum State {
        Idle,       ///< nothing open
        Preparing,  ///< token requested, no source yet
        Buffering,  ///< source set, the backend is filling its buffer
        Playing,
        Paused,
        Stopped,    ///< the backend has been stopped; the position is kept
        Error,
    };
    Q_ENUM(State)

    /// How much *playback* between two progress reports.
    ///
    /// Measured in position rather than wall clock on purpose: a player left
    /// paused in the background for an hour has not watched an hour and must
    /// not spend fifteen-second intervals saying so.
    static constexpr qint64 kProgressReportIntervalMs = 15 * 1000;

    /// How long the controls stay up after the last interaction.
    static constexpr int kControlsHideDelayMs = 3000;

    /// The real thing: creates and owns its own media objects.
    explicit PlaybackController(ApiClient *api, QObject *parent = nullptr);

    /// The headless one.  Whatever is passed is adopted (and deleted with us);
    /// `nullptr` for either means *there is no such object*, and every path
    /// through this class then behaves as a pure state machine.
    PlaybackController(ApiClient *api, QMediaPlayer *player, QAudioOutput *audioOutput,
                      QObject *parent = nullptr);

    ~PlaybackController() override;

    int mediaId() const { return m_mediaId; }
    QString title() const { return m_title; }
    QString posterUrl() const { return m_posterUrl; }
    QUrl source() const { return m_source; }
    State state() const { return m_state; }
    QString errorMessage() const { return m_errorMessage; }
    qint64 position() const { return m_position; }
    qint64 duration() const { return m_duration; }
    qint64 bufferedDuration() const { return m_bufferedDuration; }
    bool isSeekable() const { return m_seekable; }
    bool isPlaying() const { return m_isPlaying; }
    bool controlsVisible() const { return m_controlsVisible; }
    double volume() const { return m_volume; }
    bool isMuted() const { return m_muted; }
    bool subtitleEnabled() const { return m_subtitleTrackIndex >= 0; }
    int audioTrackIndex() const { return m_audioTrackIndex; }
    int subtitleTrackIndex() const { return m_subtitleTrackIndex; }
    QStringList audioTrackNames() const { return m_audioTrackNames; }
    QStringList subtitleTrackNames() const { return m_subtitleTrackNames; }
    bool audioTracksAvailable() const { return !m_audioTrackNames.isEmpty(); }
    bool subtitleTracksAvailable() const { return !m_subtitleTrackNames.isEmpty(); }

    /// Hand the controller the `VideoOutput` its frames belong in.
    ///
    /// Not a property and not a slot, because it is wiring rather than state:
    /// the player screen calls this once from `Component.onCompleted` and once
    /// with `nullptr` on teardown.  A `QMediaPlayer` paints into a
    /// `QVideoSink`, and `VideoOutput` owns exactly the one we want.
    void setVideoOutput(QObject *item);

    /// Ask the shell to advance or step back the queue.
    ///
    /// The queue is not this object's business -- it does not know what is next,
    /// and inventing a "playlist" here would be a second source of truth for
    /// something the library already owns.  So the request goes out and whoever
    /// is assembling the player decides what happens, which is why these are
    /// methods that *emit* signals rather than methods that navigate.
    void requestNext();
    void requestPrevious();

public slots:
    /// Open a title.  The map is the server's media object; the two fields that
    /// matter are `title` and `progress.position_seconds`, the second of which
    /// is what makes "continue watching" work.
    void open(int mediaId, const QVariantMap &media);

    void play();
    void pause();
    void togglePlay();
    void stop();

    /// End the session: the final position goes to the server, the backend
    /// stops, and the screen is asked to go away.
    void close();

    void seek(qint64 positionMs);
    void seekRelative(qint64 deltaMs);
    void setVolume(double value);
    void setMuted(bool muted);
    void toggleMuted();
    void toggleSubtitles();
    void setAudioTrack(int index);
    /// -1 turns subtitles off.  `toggleSubtitles` is the same walk without
    /// the intermediate steps, and both go through here.
    void setSubtitleTrack(int index);
    void cycleAudioTrack();
    void cycleSubtitleTrack();
    void setControlsVisible(bool visible);

signals:
    void stateChanged();
    void tracksChanged();
    void errorOccurred(const QString &message);
    /// The screen should navigate away; the shell owns where "back" goes.
    void closeRequested();
    void nextRequested();
    void previousRequested();
    /// Every position this controller has told the server about.
    void progressReported(qint64 positionMs, qint64 durationMs);
    /// The title played to its end.
    void finished();

    // Property notifications.  A player that does not notify is a player whose
    // scrubber does not move, so each of these exists even though the signals
    // above are the contract the rest of the app depends on.
    void mediaIdChanged();
    void titleChanged();
    void posterUrlChanged();
    void sourceChanged();
    void errorMessageChanged();
    void positionChanged();
    void durationChanged();
    void bufferedDurationChanged();
    void seekableChanged();
    void isPlayingChanged();
    void controlsVisibleChanged();
    void volumeChanged();
    void mutedChanged();
    void subtitleEnabledChanged();
    void audioTrackIndexChanged();
    void subtitleTrackIndexChanged();

private:
    // -- wiring ---------------------------------------------------------
    void adopt(QMediaPlayer *player, QAudioOutput *audioOutput);
    void connectPlayer();
    void setState(State state);
    void setErrorMessage(const QString &message);

    // -- media backend --------------------------------------------------
    void onPlaybackStarted(int mediaId, const QString &streamUrl, double startOffset,
                           int expiresInSeconds);
    void onMediaStatusChanged();
    void onPlaybackStateChanged();
    void onPositionChanged(qint64 position);
    void onDurationChanged(qint64 duration);
    void onSeekableChanged(bool seekable);
    void onPlayerError();
    void onTracksChanged();
    void onActiveTracksChanged();

    void refreshBufferedDuration();
    void applyPendingSeek();

    // -- progress -------------------------------------------------------
    /// Tell the server where we are, if `interval` of playback has passed
    /// since the last time.  Called from both the position tick and `seek()`,
    /// so a jump is recorded too.
    void maybeReportProgress();
    /// Send the position now, regardless of the interval.  `complete` closes
    /// the session rather than merely recording progress.
    void flushProgress(bool complete);

    // -- controls -------------------------------------------------------
    void armControlsTimer();
    void onControlsTimeout();

    ApiClient *m_api;
    QMediaPlayer *m_player = nullptr;
    QAudioOutput *m_audioOutput = nullptr;
    QObject *m_videoOutput = nullptr;
    QTimer *m_controlsTimer = nullptr;

    int m_mediaId = 0;
    QString m_title;
    QString m_posterUrl;
    QUrl m_source;
    QString m_errorMessage;
    State m_state = Idle;

    qint64 m_position = 0;
    qint64 m_duration = 0;
    qint64 m_bufferedDuration = 0;
    qint64 m_lastReportedPosition = 0;
    qint64 m_pendingSeekMs = -1;
    double m_volume = 1.0;
    bool m_seekable = false;
    bool m_isPlaying = false;
    bool m_muted = false;
    bool m_controlsVisible = true;
    bool m_hasReported = false;

    int m_audioTrackIndex = -1;
    int m_subtitleTrackIndex = -1;
    QStringList m_audioTrackNames;
    QStringList m_subtitleTrackNames;
};
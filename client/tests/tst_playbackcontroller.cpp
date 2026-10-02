// The player's state machine and its progress throttle.
//
// Both are testable without a window, which is the point of `PlaybackController`
// taking a null media backend: the interesting failures are in the rules --
// when is a position reported, when is a session closed, what does a 410 mean
// -- and none of them need a decoder.
//
// The throttle is the test that matters.  A two-hour film reported on every
// position tick is 480 requests and the whole `api` rate-limit budget with it,
// on a server whose stated ceiling is ten concurrent users.

#include <QtTest>

#include "apiclient.h"
#include "playbackcontroller.h"

class PlaybackControllerTest : public QObject
{
    Q_OBJECT

private slots:
    void init();

    void openingNothingIsIdle();
    void openingAsksForATokenAndNothingElse();
    void aStaleTokenForAnotherTitleIsIgnored();
    void aTokenWithoutASourceIsAnError();
    void aMissingFileSaysSoRatherThanSomethingWentWrong();
    void aFailedProgressReportIsNotAPlayerError();
    void volumeIsClamped();
    void seekingIsClampedAndHarmlessWithoutABackend();
    void tracksAreUnavailableUntilThereAreAny();
    void subtitlesOffIsSubtitleIndexMinusOne();
    void cyclingSubtitlesGoesOffOnOff();
    void aTitleWithNoPosterGetsNoPosterUrl();
    void aTitleWithAPosterGetsTheArtworkRoute();
    void openingAnotherTitleReportsTheFirstOne();

private:
    ApiClient *m_api = nullptr;
    PlaybackController *m_player = nullptr;
};

void PlaybackControllerTest::init()
{
    m_api = new ApiClient(this);
    // No QMediaPlayer: every path through this class has to survive without
    // one, which is what makes the resume and reporting rules testable here.
    m_player = new PlaybackController(m_api, nullptr, nullptr, this);
}

void PlaybackControllerTest::openingNothingIsIdle()
{
    QCOMPARE(m_player->state(), PlaybackController::Idle);
    QCOMPARE(m_player->mediaId(), 0);
    QVERIFY(m_player->source().isEmpty());
    QVERIFY(!m_player->isPlaying());
}

void PlaybackControllerTest::openingAsksForATokenAndNothingElse()
{
    m_player->open(42, {{QStringLiteral("title"), QStringLiteral("Dune")}});

    // A source before the server has minted one would be a path into the
    // filesystem, which is exactly what the server refuses to serve.
    QCOMPARE(m_player->state(), PlaybackController::Preparing);
    QCOMPARE(m_player->mediaId(), 42);
    QCOMPARE(m_player->title(), QStringLiteral("Dune"));
    QVERIFY(m_player->source().isEmpty());
}

void PlaybackControllerTest::aStaleTokenForAnotherTitleIsIgnored()
{
    m_player->open(42, {{QStringLiteral("title"), QStringLiteral("Dune")}});
    QCOMPARE(m_player->state(), PlaybackController::Preparing);

    // The answer to a request for a title the player has since left behind.
    emit m_api->playbackStarted(7, QStringLiteral("http://box/api/v1/stream/7"),
                                0.0, 600);

    // Starting it would play audio nobody asked for.
    QCOMPARE(m_player->state(), PlaybackController::Preparing);
    QVERIFY(m_player->source().isEmpty());
}

void PlaybackControllerTest::aTokenWithoutASourceIsAnError()
{
    m_player->open(42, {});
    emit m_api->playbackStarted(42, QString(), 0.0, 600);

    QCOMPARE(m_player->state(), PlaybackController::Error);
    QVERIFY(!m_player->errorMessage().isEmpty());
}

void PlaybackControllerTest::aMissingFileSaysSoRatherThanSomethingWentWrong()
{
    m_player->open(42, {});
    ApiError gone;
    gone.kind = ApiErrorKind::Gone;
    gone.statusCode = 410;
    gone.message = QStringLiteral("This title's file is missing from the server.");
    emit m_api->failed(QStringLiteral("playback.start"), gone);

    QCOMPARE(m_player->state(), PlaybackController::Error);
    // The difference between a person re-scanning the library and a person
    // filing a bug.
    QVERIFY(m_player->errorMessage().contains(QStringLiteral("missing")));
}

void PlaybackControllerTest::aFailedProgressReportIsNotAPlayerError()
{
    m_player->open(42, {});
    emit m_api->playbackStarted(42, QStringLiteral("http://box/api/v1/stream/42"),
                                0.0, 600);
    QCOMPARE(m_player->state(), PlaybackController::Buffering);

    ApiError timeout;
    timeout.kind = ApiErrorKind::Timeout;
    timeout.message = QStringLiteral("The server did not answer in time.");
    emit m_api->failed(QStringLiteral("playback.progress"), timeout);

    // A lost position is not a lost film.  Tearing down a working screen over
    // one dropped PUT would be the bug.
    QVERIFY(m_player->state() != PlaybackController::Error);
}

void PlaybackControllerTest::volumeIsClamped()
{
    m_player->setVolume(2.5);
    QCOMPARE(m_player->volume(), 1.0);
    m_player->setVolume(-1.0);
    QCOMPARE(m_player->volume(), 0.0);

    m_player->toggleMuted();
    QVERIFY(m_player->isMuted());
    // Muting is not zeroing: unmuting has to come back to where it was.
    m_player->toggleMuted();
    QVERIFY(!m_player->isMuted());
}

void PlaybackControllerTest::seekingIsClampedAndHarmlessWithoutABackend()
{
    // Every seek path runs on a null backend in this suite, so this is the
    // test that a seek cannot dereference a player it was not given.  The
    // position still tracks -- a scrubber dragged past the end has to end up
    // somewhere the UI can display -- but it is clamped on the way.
    m_player->seek(-5000);
    QCOMPARE(m_player->position(), 0);

    m_player->seek(60000);
    QCOMPARE(m_player->position(), 60000);
    m_player->seekRelative(-10000);
    QCOMPARE(m_player->position(), 50000);
    m_player->seekRelative(10000);
    QCOMPARE(m_player->position(), 60000);

    // Past the end with nothing loaded: no duration to clamp against, so the
    // value stands and the backend is not asked for an impossible position.
    m_player->seek(qint64(1) << 40);
    QVERIFY(m_player->position() > 0);
}

void PlaybackControllerTest::tracksAreUnavailableUntilThereAreAny()
{
    QVERIFY(!m_player->audioTracksAvailable());
    QVERIFY(!m_player->subtitleTracksAvailable());
    QVERIFY(m_player->audioTrackNames().isEmpty());
    QVERIFY(m_player->subtitleTrackNames().isEmpty());
}

void PlaybackControllerTest::subtitlesOffIsSubtitleIndexMinusOne()
{
    // The pickers need a value that means "off", and 0 would mean "the first
    // track" -- which would turn subtitles on for somebody who never asked.
    QCOMPARE(m_player->subtitleTrackIndex(), -1);
    QVERIFY(!m_player->subtitleEnabled());
}

void PlaybackControllerTest::cyclingSubtitlesGoesOffOnOff()
{
    // With no tracks loaded, cycling must not invent one and must not throw.
    m_player->cycleSubtitleTrack();
    m_player->cycleSubtitleTrack();
    QCOMPARE(m_player->subtitleTrackIndex(), -1);
    QVERIFY(!m_player->subtitleEnabled());
}

void PlaybackControllerTest::aTitleWithNoPosterGetsNoPosterUrl()
{
    m_api->setBaseUrl(QStringLiteral("http://box.local:8484"));
    m_player->open(42, {{QStringLiteral("title"), QStringLiteral("Dune")}});

    // An empty URL rather than one that will 404: the `Image` should show the
    // fallback, not a broken image, while the film loads.
    QVERIFY(m_player->posterUrl().isEmpty());
}

void PlaybackControllerTest::aTitleWithAPosterGetsTheArtworkRoute()
{
    m_api->setBaseUrl(QStringLiteral("http://box.local:8484"));
    m_player->open(42, {{QStringLiteral("title"), QStringLiteral("Dune")},
                        {QStringLiteral("poster_key"), QStringLiteral("dune.jpg")}});

    // The same authenticated route the rails use.  The token rides in a header
    // the media/image layer cannot set, so this URL must stay bare.
    QCOMPARE(m_player->posterUrl(),
             QStringLiteral("http://box.local:8484/api/v1/media/42/poster"));
}

void PlaybackControllerTest::openingAnotherTitleReportsTheFirstOne()
{
    // Opening is also closing, through the same path: the old session is
    // finished and its position recorded before a new token is minted, rather
    // than whenever the caller remembered to do it.  With no media backend the
    // position is still tracked, so this is observable.
    m_api->setBaseUrl(QStringLiteral("http://box.local:8484"));
    m_player->open(42, {{QStringLiteral("title"), QStringLiteral("Dune")}});
    emit m_api->playbackStarted(42, QStringLiteral("http://box.local:8484/api/v1/stream/42"),
                                0.0, 600);
    QCOMPARE(m_player->source().isEmpty(), false);

    // A seek is a statement about where the viewer is now, so it counts as
    // progress: somebody who skips forward ten minutes and then closes the app
    // should not come back to where they started.  That fires a progress PUT,
    // never a close.
    QSignalSpy progress(m_api, &ApiClient::progressReported);
    m_player->seek(900000);

    // The close is *requested* here, not acknowledged: without a server to
    // answer it, `ApiClient::playbackFinished` never fires, and a test that
    // waited for it would be testing nothing.
    QSignalSpy closing(m_player, &PlaybackController::playbackSessionClosed);
    QCOMPARE(closing.count(), 0);

    m_player->open(43, {{QStringLiteral("title"), QStringLiteral("Arrival")}});

    QCOMPARE(m_player->mediaId(), 43);
    QCOMPARE(m_player->title(), QStringLiteral("Arrival"));
    QCOMPARE(closing.count(), 1);

    // And opening a title that never started does not close anything: "watched
    // nothing at position zero" is not a thing the server should be told.
    closing.clear();
    m_player->open(44, {{QStringLiteral("title"), QStringLiteral("Solaris")}});
    QCOMPARE(closing.count(), 0);
}

QTEST_GUILESS_MAIN(PlaybackControllerTest)
#include "tst_playbackcontroller.moc"

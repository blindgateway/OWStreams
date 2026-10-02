// What the client puts on the wire.
//
// Every other client suite checks what the code *decides*; this one checks
// what it actually *sends*, against a real loopback HTTP server that records
// the bytes.  That distinction is not academic.  The end-to-end suite found
// seven defects that none of the deciding-suites could see, and the two worst
// were here:
//
// * every request that carried an id in its body was answered 422, because the
//   routes take ids as path parameters and their bodies are `extra="forbid"`.
//   Favourites, ratings, progress reports and playback start all silently did
//   nothing, and the favourite rail still updated, because `handle` reports the
//   *intent* rather than the server's answer;
// * `playbackStarted` announced an empty stream URL, because the media id was
//   stamped onto the client one line *after* the URL was built from it -- and
//   `streamUrl` returns empty for any id that is not the current one.  The
//   player read that as "the server did not return a playable source".
//
// Both look identical from the outside: a request arrives, a 2xx comes back,
// and nothing is wrong.  Only the request itself says otherwise, so the
// assertions here are about bytes.

#include <QtTest>

#include <QHostAddress>
#include <QVector>
#include <functional>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSignalSpy>

#include "apiclient.h"
#include "credentialaccessmanager.h"
#include "fakeserver.h"

namespace {

const QByteArray kHealth =
    R"({"status":"ok","database":"ok","version":"1.0.0","api_version":"v1"})";

const QByteArray kTokenPair =
    R"({"access_token":"ssat.access","refresh_token":"ssrt.refresh",)"
    R"("token_type":"Bearer","expires_in":900,"session_id":"session-1",)"
    R"("refresh_expires_at":"2027-01-01T00:00:00+00:00",)"
    R"("must_change_password":false,)"
    R"("user":{"id":1,"username":"phuree","display_name":"Phuree",)"
    R"("is_admin":true}})";

const QByteArray kPlaybackToken =
    R"({"playback_token":"mpt.token","expires_in":600,)"
    R"("stream_url":"/api/v1/stream/7","session_id":"play-1",)"
    R"("start_offset":0.0})";

const QByteArray kFinished = R"({"ok":true,"detail":"Playback recorded."})";

}  // namespace

class ApiClientTest : public QObject
{
    Q_OBJECT

private slots:
    void initTestCase();
    void init();

    // -- what goes out ------------------------------------------------------
    void startingPlaybackStampsTheIdBeforeBuildingTheUrl();
    void aStartWithNoTokenMintsNoStreamUrl();
    void finishingPlaybackIsPostedWithNoBody();
    void everyIdTravelsInThePathNotTheBody();
    void theProfileHeaderReachesTheProfileScopedRoots();
    void aFailedRequestIsReportedNotSilentlySwallowed();

private:
    FakeServer m_server;
    ApiClient *m_api = nullptr;

    QString url() const
    {
        return QStringLiteral("http://127.0.0.1:%1").arg(m_server.serverPort());
    }

    /// A signed-in client with a profile selected.
    void signedIn();

    /// Wait for one request to reach the fake server, then return its index.
    /// `expect` is the request line, so the wait cannot pass on some other
    /// request that happened to arrive first.
    int waitForRequest(const QByteArray &expect, int timeoutMs = 5000,
                       int fromIndex = 0);
};

void ApiClientTest::initTestCase()
{
    QVERIFY(m_server.listen(QHostAddress::LocalHost));
}

void ApiClientTest::init()
{
    m_server.setSilent(false);
    m_server.setAnswers({
        {QStringLiteral("/api/health"), answer(200, kHealth)},
        {QStringLiteral("/api/v1/auth/login"), answer(200, kTokenPair)},
        {QStringLiteral("/api/v1/media/7/playback"),
         answer(200, kPlaybackToken)},
        {QStringLiteral("/api/v1/media/7/playback/finish"),
         answer(200, kFinished)},
        {QStringLiteral("/api/v1/media/7/progress"),
         answer(200, R"({"profile_id":1,"media_id":7,"position_seconds":30.0,)"
                    R"("duration_seconds":120.0,"percent":25.0,"completed":false})")},
        {QStringLiteral("/api/v1/library/favorites/7"),
         answer(200, R"({"ok":true})")},
        {QStringLiteral("/api/v1/library/media/7/rating"),
         answer(200, R"({"media_id":7,"score":8.0})")},
        {QStringLiteral("/api/v1/library/history/7"),
         answer(200, R"({"ok":true})")},
    });

    // Fresh client, fresh request log.  Both, together: an index into a log
    // that still holds the previous test's requests reads *that* test's
    // request -- which is why a header sent here kept being reported absent,
    // three rounds after it was demonstrably on the wire.
    m_server.clearRequests();
    delete m_api;
    m_api = new ApiClient(this);
    m_api->setBaseUrl(url());
}

void ApiClientTest::signedIn()
{
    m_api->setSession(QStringLiteral("ssat.access"), QStringLiteral("ssrt.refresh"),
                      900);
    // Deliberately *not* the id in the paths below (7): `waitForRequest`
    // returns an *index* into the recorded list, and with 7 that index would
    // be 0 -- so the assertion would read the empty request at -1 rather than
    // the one just made, and report a missing header for a header that was
    // sent.  The marker id keeps the index nonzero.
    m_api->setProfileId(3);
}

int ApiClientTest::waitForRequest(const QByteArray &expect, int timeoutMs,
                                  int fromIndex)
{
    QElapsedTimer timer;
    timer.start();
    while (timer.elapsed() < timeoutMs) {
        const int at = m_server.indexOfRequest(expect, fromIndex);
        if (at >= 0)
            return at;
        QTest::qWait(20);
    }
    return -1;
}

void ApiClientTest::startingPlaybackStampsTheIdBeforeBuildingTheUrl()
{
    signedIn();
    QSignalSpy started(m_api, &ApiClient::playbackStarted);
    m_api->startPlayback(7, 0.0);
    QVERIFY(started.wait(5000));

    // The URL the player is handed.  It must be absolute and complete: an
    // empty one is reported to the person watching as "the server did not
    // return a playable source", which is a lie -- the server returned a
    // perfectly good token.
    const QString stream = started.at(0).at(1).toString();
    QCOMPARE(stream, url() + QStringLiteral("/api/v1/stream/7"));

    // And it must not carry the playback token in the query string, where it
    // would end up in proxy logs and error messages.
    QVERIFY(!stream.contains(QStringLiteral("token")));
    QCOMPARE(started.at(0).at(0).toInt(), 7);
}

void ApiClientTest::aStartWithNoTokenMintsNoStreamUrl()
{
    signedIn();
    // A title the current playback session is not for: the server may have
    // answered for something else entirely.
    QSignalSpy started(m_api, &ApiClient::playbackStarted);
    m_api->startPlayback(7, 0.0);
    QVERIFY(started.wait(5000));

    // Asking for a *different* title while one is open must not hand back the
    // open one's URL, or the player would stream the wrong film.
    QCOMPARE(m_api->streamUrl(8), QUrl());
    QCOMPARE(m_api->streamUrl(7),
             QUrl(url() + QStringLiteral("/api/v1/stream/7")));
}

void ApiClientTest::finishingPlaybackIsPostedWithNoBody()
{
    signedIn();
    QSignalSpy finished(m_api, &ApiClient::playbackFinished);
    m_api->finishPlayback(7, 120.0, QStringLiteral("play-1"));
    QVERIFY(finished.wait(5000));

    const int at = waitForRequest("POST /api/v1/media/7/playback/finish");
    QVERIFY(at >= 0);
    const QByteArray request = m_server.requests().at(at);
    QVERIFY(request.contains("?position_seconds=120.000"));
    QVERIFY(request.contains("session_id=play-1"));
    // The route takes no body.  Sending one is not merely untidy: `StrictModel`
    // rejects unknown fields, so a `media_id` here is a 422 and the title is
    // never marked watched.
    QCOMPARE(m_server.lastBody(), QByteArray());

    // And the answer announces the title it closed, which it can only do by
    // having remembered the id: the trailing path segment here is the verb.
    QCOMPARE(finished.at(0).at(0).value<ApiClient::FinishedPlayback>().mediaId, 7);
}

void ApiClientTest::everyIdTravelsInThePathNotTheBody()
{
    signedIn();

    m_api->reportProgress(7, 30.0, 120.0);
    QVERIFY(waitForRequest("PUT /api/v1/media/7/progress") >= 0);
    // Compared as JSON, not as text: `QJsonDocument` writes a whole number as
    // `30`, and a byte-for-byte comparison against `30.0` would be asserting on
    // the serialiser rather than on the request.
    const QJsonObject sent =
        QJsonDocument::fromJson(m_server.lastBody()).object();
    QCOMPARE(sent.value(QStringLiteral("position_seconds")).toDouble(), 30.0);
    QCOMPARE(sent.value(QStringLiteral("duration_seconds")).toDouble(), 120.0);

    m_api->setFavorite(7, true);
    QVERIFY(waitForRequest("PUT /api/v1/library/favorites/7") >= 0);
    QCOMPARE(m_server.lastBody(), QByteArray());

    m_api->rateMedia(7, 8.0);
    QVERIFY(waitForRequest("PUT /api/v1/library/media/7/rating") >= 0);
    QCOMPARE(QJsonDocument::fromJson(m_server.lastBody())
                 .object().value(QStringLiteral("score")).toDouble(), 8.0);

    // None of these bodies may carry `media_id`, `profile_id` or `session_id`.
    // The one that does is refused with 422 by a model that forbids extra
    // fields -- and the client never sees an error, because `handle` reports
    // what was asked for rather than what came back.
    for (const QByteArray &request : m_server.requests()) {
        const QByteArray body = request.mid(request.indexOf("\r\n\r\n") + 4);
        QVERIFY2(!body.contains("media_id"), body.constData());
        QVERIFY2(!body.contains("profile_id"), body.constData());
        QVERIFY2(!body.contains("session_id"), body.constData());
    }
}

void ApiClientTest::theProfileHeaderReachesTheProfileScopedRoots()
{
    signedIn();

    m_server.setAnswers({
        {QStringLiteral("/api/v1/library/favorites"),
         answer(200, R"({"items":[],"total":0})")},
    });

    m_api->loadFavorites();
    // The client has to *hold* the profile, not merely have been asked to: the
    // header is written from `m_profileId`, so a `setProfileId` that never took
    // would send every request with `X-Profile-Id: 0` and the server would
    // refuse all of it.  Which is why the header's absence below and its being
    // zero look identical from here -- hence this assertion.
    QCOMPARE(m_api->profileId(), 3);
    QCOMPARE(m_api->baseUrl(), url());

    // The manager attaches credentials only to the artwork and stream routes,
    // which `QQuickImage` and the media backend fetch by URL and cannot give a
    // header.  `ApiClient::send` writes the profile header for everything else.
    // Stating both halves is the point: they were once one rule kept in two
    // places, and the halves disagreed.
    QVERIFY(!m_api->wouldAuthenticate(
        url() + QStringLiteral("/api/v1/library/favorites")));
    QVERIFY(m_api->wouldAuthenticate(
        url() + QStringLiteral("/api/v1/media/7/poster")));
    QVERIFY(m_api->wouldAuthenticate(
        url() + QStringLiteral("/api/v1/stream/7")));

    // Every request the client builds under a profile-scoped root, read.
    // Each is asserted against *its own* recorded request: an assertion that
    // reads whichever request arrived last reads the one *after* the one it
    // meant, and reports a missing header for a header that was sent.  The wait
    // is driven from the fake server's own signal rather than by polling, for
    // the same reason -- see the note on `requestArrived`.
    QSignalSpy arrived(&m_server, &FakeServer::requestArrived);
    const QVector<QPair<QByteArray, std::function<void()>>> reads = {
        {"GET /api/v1/library/favorites", [this] { m_api->loadFavorites(); }},
        {"GET /api/v1/library/history", [this] { m_api->loadHistory(); }},
        {"GET /api/v1/library/home", [this] { m_api->loadHome(20); }},
    };
    for (const auto &read : reads) {
        const int before = arrived.count();
        read.second();
        QTRY_VERIFY_WITH_TIMEOUT(arrived.count() > before, 5000);
        const int at = arrived.at(arrived.count() - 1).at(0).toInt() - 1;
        QVERIFY2(at >= 0 && m_server.requestLine(at).startsWith(read.first),
                 qPrintable(QStringLiteral("expected %1, got %2")
                                .arg(QString::fromLatin1(read.first),
                                     QString::fromLatin1(
                                         m_server.requestLine(at)))));
        QVERIFY2(m_server.requestHeader(at, "x-profile-id") == QByteArray("3"),
                 qPrintable(QString::fromLatin1(m_server.requestLine(at))
                            + QStringLiteral(" had:\n")
                            + QString::fromLatin1(
                                  m_server.headers(at))));
    }

    m_api->setFavorite(7, true);
    const int written = waitForRequest("PUT /api/v1/library/favorites/7");
    QVERIFY(written >= 0);
    QCOMPARE(m_server.requestHeader(written, "x-profile-id"), QByteArray("3"));

    // Signing out has to take the profile with it.  A header naming a profile
    // the caller no longer holds is answered 404 by the server, so leaving it
    // on would turn every later request into a silent no-op.
    // One more request, and one more signal, so the assertion below is about
    // the request the sign-out produced rather than about whichever one
    // happened to arrive last.
    const int beforeSignOut = arrived.count();
    m_api->clearSession();
    m_api->loadFavorites();
    QTRY_VERIFY_WITH_TIMEOUT(arrived.count() > beforeSignOut, 5000);
    const int afterSignOut =
        arrived.at(arrived.count() - 1).at(0).toInt() - 1;
    QVERIFY2(afterSignOut != written,
             "the last request to arrive is the one before the sign-out");
    // `X-Profile-Id: 0`, not no header at all.  Omitting it is what makes the
    // server fall back to the account's first profile, so the client has to
    // keep saying "no profile" rather than stop saying anything.
    QVERIFY2(m_server.requestHeader(afterSignOut, "x-profile-id") == QByteArray("0"),
             qPrintable(QStringLiteral("profileId() is %1; #%2 of %3 said:\n%4")
                            .arg(m_api->profileId())
                            .arg(afterSignOut)
                            .arg(m_server.requests().size())
                            .arg(m_api->profileId())
                            .arg(QString::fromLatin1(m_server.headers(afterSignOut)))));
}

void ApiClientTest::aFailedRequestIsReportedNotSilentlySwallowed()
{
    signedIn();
    m_server.setAnswers({
        {QStringLiteral("/api/v1/library/favorites/7"),
         answer(422, R"({"error":"validation_error",)"
                    R"("message":"Unexpected field.","request_id":"abc"})")},
    });

    // The favourite is reported as *changed* the moment the client asks, so a
    // refused request has to produce a `failed` the screen can hear -- and this
    // pins the other half of that contract: the route the client builds really
    // is one the server can answer.
    QSignalSpy failed(m_api, &ApiClient::failed);
    m_api->setFavorite(7, true);
    QVERIFY(waitForRequest("PUT /api/v1/library/favorites/7") >= 0);
    QVERIFY(failed.wait(5000));
    QCOMPARE(failed.at(0).at(0).toString(), QStringLiteral("library.favorite.put"));
    QCOMPARE(failed.at(0).at(1).value<ApiError>().statusCode, 422);
}

QTEST_GUILESS_MAIN(ApiClientTest)
#include "tst_apiclient.moc"
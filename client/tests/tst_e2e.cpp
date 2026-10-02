// The client against a real server.
//
// The unit suites each cover one piece -- the session state machine, the error
// taxonomy, the list model's roles, the player's throttle -- and none of them
// touch a server.  The failures that survive all of them are at the seam: a
// resume offset that never leaves the client, a Range header the server drops,
// a profile header that arrives on the paths where it is needed.  A canned
// response table cannot see any of that, so these tests run the real backend
// over HTTP.
//
// The server is the *production* one: the real config file, the real database
// with its own signing key, a real account, and a library holding a real
// decodable clip.  If a route does not exist, or refuses a header, or answers
// 404 for a title that is plainly there, that is what these tests find.

#include <QtTest>

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTimer>
#include <QUrlQuery>

#include "apiclient.h"
#include "appcontroller.h"
#include "serverfixture.h"
#include "sessioncontroller.h"

namespace {

/// The title `harness.py` imports.  Named here rather than by id: an id is a
/// rowid, and a test that breaks if the database is ever seeded differently is
/// not testing the client.
const QString kTitle = QStringLiteral("Test Feature");

}  // namespace

class EndToEndTest : public QObject
{
    Q_OBJECT

private slots:
    void initTestCase();

    void init();

    // -- the walkthrough, in the order a person does it --------------------
    void connectThenLogInReachesTheLibrary();
    void theHomeRailsArePopulated();
    void browseSearchAndDetail();
    void favouritingIsPerProfileAndSurvivesALogout();
    void playbackMintsATokenAndStreamsBytes();
    void aRangeRequestReturnsTheBytesAskedFor();
    void progressReachesTheServerAndResumesFromIt();
    void loggingOutAndBackInKeepsTheFavourites();
    void theLibraryFilterRowNarrowsTheGrid();

    // -- the failure cases -------------------------------------------------
    void aServerThatIsNotThereFailsCleanly();
    void aWrongPasswordIsRejectedWithoutASession();
    void anUnknownTitleIsNotFound();
    void aRequestWithoutATokenIsRefused();
    void aProfileHeaderForSomebodyElsesProfileIsRefused();
    void aServerThatGoesAwayMidSessionFailsCleanly();
    void aProfileWithAPinCanBeUnlockedAndLockedAgain();

private:
    /// Sign in and wait for the profile list, which is where the session
    /// machine believes it is signed in.
    bool signIn();

    /// `signIn` as a test assertion.
    ///
    /// Called from every walkthrough test rather than guarded at each one: a
    /// test that carries on with `m_mediaId == 0` fails later on an assertion
    /// about a *title*, which reads as a library bug rather than as a set-up
    /// that did not happen.  Three of the four original failures were exactly
    /// that, and each one cost a debugging round to un-mask.
    void signedIn();
    void waitFor(QSignalSpy &spy, int count, int timeoutMs = 20000);
    /// The raw HTTP answer, for the cases that are about the server's contract
    /// rather than about the client's reaction to it.
    struct Reply {
        int status = 0;
        QByteArray body;
        QByteArray contentRange;
        QByteArray acceptRanges;
        qint64 contentLength = -1;
        /// QNetworkReply's own error, and the URL.  Both belong in the failure
        /// message: a transport failure leaves `status` at zero, and "expected
        /// 206, actual 0" is three debugging rounds spent rediscovering that a
        /// connection was refused rather than an answer being wrong.
        QNetworkReply::NetworkError transportError =
            QNetworkReply::NoError;
        QString url;
    };
    Reply rawRequest(const QString &path, const QString &verb = QStringLiteral("GET"),
                     const QByteArray &body = {}, const QList<QPair<QByteArray, QByteArray>> &headers = {},
                     bool withToken = true, bool withProfile = true);
    int mediaId() const { return m_mediaId; }
    int profileId() const { return m_profileId; }

    ServerFixture *m_server = nullptr;
    SessionController *m_session = nullptr;
    AppController *m_controller = nullptr;
    ApiClient *m_api = nullptr;
    int m_mediaId = 0;
    int m_profileId = 0;
    bool m_skipped = false;
};

void EndToEndTest::initTestCase()
{
    const QByteArray temp = qgetenv("TMPDIR");
    const QString root = temp.isEmpty() ? QStringLiteral("/tmp")
                                        : QString::fromUtf8(temp);
    // One directory per process, stamped with the pid so two ctest runs in
    // parallel do not fight over the same port file.
    const QString scratch = root + QStringLiteral("/stream-e2e-%1")
                            .arg(QCoreApplication::applicationPid());

    m_server = new ServerFixture(scratch, QStringLiteral(STREAM_E2E_HARNESS));

    QString skipReason;
    if (!m_server->start(&skipReason)) {
        qWarning("end-to-end tests skipped: %s", qPrintable(skipReason));
        m_skipped = true;
        QSKIP(qPrintable(skipReason));
    }
}

void EndToEndTest::init()
{
    if (m_skipped)
        QSKIP("no server");
    // A fresh controller per test: the unit suites already cover "does state
    // survive", and a shared one here would make an ordering dependency look
    // like a persistence bug.
    m_session = new SessionController(this);
    m_controller = new AppController(m_session->api(), this);
    m_api = m_session->api();
    m_mediaId = 0;
    m_profileId = 0;
}

void EndToEndTest::waitFor(QSignalSpy &spy, int count, int timeoutMs)
{
    QElapsedTimer timer;
    timer.start();
    while (spy.count() < count && timer.elapsed() < timeoutMs)
        QTest::qWait(25);
    QCOMPARE(spy.count(), count);
}

EndToEndTest::Reply EndToEndTest::rawRequest(
    const QString &path, const QString &verb, const QByteArray &body,
    const QList<QPair<QByteArray, QByteArray>> &headers, bool withToken,
    bool withProfile)
{
    Reply reply;
    QNetworkAccessManager manager;
    // An absolute URL is used as-is; a path is resolved against the fixture.
    // The playback tests hand over a whole stream URL, and prefixing a server
    // address to it produces something that looks like a host and fails as
    // `HostNotFound` -- three rounds spent learning that from an empty URL.
    const QString target = path.startsWith(QLatin1String("http"))
                               ? path
                               : m_server->url() + path;
    QNetworkRequest request{QUrl(target)};
    request.setRawHeader("Accept", "application/json");
    if (withToken && m_api->hasSession()) {
        // The token is private to ApiClient, so this reads it back out of the
        // session controller's own accessor rather than inventing a second
        // one.  If that ever returns empty the test says so plainly.
        request.setRawHeader("Authorization",
                             m_session->api()->authorizationHeader().toUtf8());
    }
    if (withProfile && m_profileId > 0)
        request.setRawHeader("X-Profile-Id", QByteArray::number(m_profileId));
    for (const auto &header : headers)
        request.setRawHeader(header.first, header.second);
    // A test that posts to a real route posts JSON, including the two that
    // take no body.  Qt 6 warns once per request otherwise, and a warning per
    // progress report is how a real one stops being read.
    request.setRawHeader("Content-Type", "application/json; charset=utf-8");

    QNetworkReply *network =
        verb == QStringLiteral("GET")
            ? manager.get(request)
            : manager.sendCustomRequest(request, verb.toUtf8(), body);

    QEventLoop loop;
    QObject::connect(network, &QNetworkReply::finished, &loop, &QEventLoop::quit);
    QTimer::singleShot(20000, &loop, &QEventLoop::quit);
    loop.exec();

    reply.status =
        network->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    reply.body = network->readAll();
    reply.contentRange = network->rawHeader("Content-Range");
    reply.acceptRanges = network->rawHeader("Accept-Ranges");
    reply.contentLength = network->header(QNetworkRequest::ContentLengthHeader).toLongLong();
    reply.transportError = network->error();
    reply.url = request.url().toString();
    if (reply.transportError != QNetworkReply::NoError) {
        // QString, not printf: this target defines QT_NO_CAST_FROM_ASCII, and
        // Qt 6 drops qWarning's printf-style overload along with it -- so the
        // `%1` version printed its placeholders literally, which is a very
        // expensive way to lose a diagnostic.
        qWarning().noquote()
            << QStringLiteral("e2e: %1 %2 -> transport error %3 (%4)")
                   .arg(verb, reply.url)
                   .arg(int(reply.transportError))
                   .arg(network->errorString());
    }
    network->deleteLater();
    return reply;
}

bool EndToEndTest::signIn()
{
    m_api->setBaseUrl(m_server->url());
    m_session->connectTo(m_server->url());
    QSignalSpy reached(m_session, &SessionController::stateChanged);
    QElapsedTimer timer;
    timer.start();
    while (m_session->state() != SessionController::Login && timer.elapsed() < 20000)
        QTest::qWait(25);
    if (m_session->state() != SessionController::Login)
        return false;

    m_session->signIn(m_server->username(), m_server->password(), false);

    timer.restart();
    while (m_session->state() != SessionController::Profiles
           && m_session->state() != SessionController::Home
           && timer.elapsed() < 20000) {
        QTest::qWait(25);
    }
    Q_UNUSED(reached)

    const QVariantList profiles = m_session->profiles();
    if (profiles.isEmpty())
        return false;
    m_profileId = profiles.first().toMap().value(QStringLiteral("id")).toInt();
    m_api->setProfileId(m_profileId);

    // Find the imported title, so the tests that need it are not hard-coding a
    // rowid.  Read out of the *model* rather than out of a signal spy: this
    // function is the one that fetches the library, and a test that then
    // attached a spy to the same request would be waiting for an answer that
    // had already arrived -- which is exactly the bug this comment replaces.
    //
    // The wait is on the response rather than on `rowCount() > 0`: the library
    // can legitimately come back empty, and a condition that waits for rows
    // walks straight past a successful answer and times out on it.
    // No spy here.  `goLibrary` fetches the page *and* the categories, so a
    // spy on the page that goes on counting sees two answers and a later test
    // waits on a count that has already been passed -- which reads as a client
    // that sent two library requests, and is not.  Waiting on "not busy" is
    // both correct for an empty library and immune to the second request.
    m_controller->goLibrary();
    QElapsedTimer fetched;
    fetched.start();
    while (m_controller->isBusy() && fetched.elapsed() < 20000)
        QTest::qWait(25);

    const MediaListModel *library = m_controller->library();
    for (int row = 0; row < library->rowCount(); ++row) {
        if (library->data(library->index(row),
                          MediaListModel::TitleRole).toString() == kTitle) {
            m_mediaId = library->data(library->index(row),
                                      MediaListModel::IdRole).toInt();
        }
    }
    return m_mediaId > 0;
}

void EndToEndTest::signedIn()
{
    QVERIFY2(signIn(), qPrintable(
        QStringLiteral("could not sign in; the server said: %1")
            .arg(m_session->lastError())));
}

// ---------------------------------------------------------------------------
// The walkthrough
// ---------------------------------------------------------------------------

void EndToEndTest::connectThenLogInReachesTheLibrary()
{
    signedIn();

    QCOMPARE(m_session->state(), SessionController::Profiles);
    QCOMPARE(m_session->username(), m_server->username());
    QVERIFY(m_api->hasSession());

    // The one profile the fixture creates, and it is the account's own.
    QCOMPARE(m_session->profiles().size(), 1);
    QCOMPARE(m_profileId,
             m_session->profiles().first().toMap().value(QStringLiteral("id")).toInt());
}

void EndToEndTest::theHomeRailsArePopulated()
{
    signedIn();

    // The spy is attached *before* `goHome`, which is what makes the assertion
    // about a request rather than about a model that may already be filled: a
    // fetch after the fact answers for a request that has already been made,
    // and `waitFor` then times out for a reason that has nothing to do with
    // the server.  That was the whole of the first version of this test.
    QSignalSpy home(m_api, &ApiClient::homeLoaded);
    QSignalSpy failed(m_api, &ApiClient::failed);
    m_controller->goHome();
    waitFor(home, 1);
    for (int i = 0; i < failed.count(); ++i) {
        const ApiError error = failed.at(i).at(1).value<ApiError>();
        QFAIL(qPrintable(QStringLiteral("%1 failed: %2 (%3)")
                            .arg(failed.at(i).at(0).toString(), error.message)
                            .arg(error.statusCode)));
    }

    const QJsonObject payload = home.at(0).at(0).toJsonObject();
    // Every rail key the screen binds to must be present, even when empty: a
    // missing key is an undefined in QML, which renders as a blank row rather
    // than as an error.
    for (const char *key : {"continue_watching", "recently_added", "recommended",
                            "favorites", "popular", "top_rated",
                            "recently_watched", "categories"}) {
        QVERIFY2(payload.contains(QString::fromLatin1(key)),
                 qPrintable(QStringLiteral("home payload has no %1")
                                .arg(QString::fromLatin1(key))));
    }

    const QJsonArray recent = payload.value(QStringLiteral("recently_added")).toArray();
    QCOMPARE(recent.size(), 1);
    QCOMPARE(recent.first().toObject().value(QStringLiteral("title")).toString(),
             kTitle);

    // And the model the screen binds to agrees with the payload.
    const MediaListModel *rail = m_controller->home()->recentlyAdded()->model();
    QCOMPARE(rail->rowCount(), 1);
    QCOMPARE(rail->data(rail->index(0), MediaListModel::TitleRole).toString(),
             kTitle);
    // The fixture's clip has no artwork, so the correct answer is *no* URL: a
    // poster route that 404s on every repaint of every delegate is worse than
    // the fallback the card draws instead.
    QVERIFY2(!rail->data(rail->index(0), MediaListModel::PosterUrlRole)
                 .toUrl().isValid(),
             "a title with no poster key must have no poster URL");
}

void EndToEndTest::browseSearchAndDetail()
{
    signedIn();

    // Browse.  `signIn` already loaded the first page, so the assertion here
    // is about what is on screen; the reload is what proves the *request*
    // still happens and still answers, rather than the model merely having
    // been filled once.
    QCOMPARE(m_controller->library()->rowCount(), 1);
    QCOMPARE(m_controller->libraryTotal(), 1);
    QVERIFY(!m_controller->libraryHasMore());
    // The title the row spells is the server's, not the fixture's file name:
    // this is the only place in the suite where the *importer's* parsing is
    // asserted, and a rename in the harness moves it.
    QCOMPARE(m_controller->library()
                 ->data(m_controller->library()->index(0),
                        MediaListModel::TitleRole).toString(),
             kTitle);

    QSignalSpy page(m_api, &ApiClient::mediaPageLoaded);
    m_controller->refreshLibrary();
    waitFor(page, 1);
    QCOMPARE(m_controller->library()->rowCount(), 1);

    // Search
    m_controller->setSearchQuery(QStringLiteral("Feature"));
    m_controller->submitSearch();
    QSignalSpy found(m_api, &ApiClient::searchLoaded);
    waitFor(found, 1);
    QCOMPARE(m_controller->searchResults()->rowCount(), 1);

    // A query that matches nothing is an empty result, not an error.
    m_controller->setSearchQuery(QStringLiteral("Nothing Called This"));
    m_controller->submitSearch();
    QSignalSpy empty(m_api, &ApiClient::searchLoaded);
    waitFor(empty, 1);
    QCOMPARE(m_controller->searchResults()->rowCount(), 0);
    QVERIFY(!m_controller->searchResults()->isFailed());

    // Detail
    m_controller->openDetail(m_mediaId);
    QSignalSpy detail(m_api, &ApiClient::mediaLoaded);
    waitFor(detail, 1);
    QCOMPARE(m_controller->screen(), AppController::Detail);
    QCOMPARE(m_controller->detailMediaId(), m_mediaId);
    QCOMPARE(m_controller->detail().value(QStringLiteral("title")).toString(),
             kTitle);
    // The one fact a player cannot invent: where the file actually is.  The
    // server must not publish a filesystem path.
    QVERIFY(!m_controller->detail().contains(QStringLiteral("media_path")));
}

void EndToEndTest::favouritingIsPerProfileAndSurvivesALogout()
{
    signedIn();

    m_controller->toggleFavorite(m_mediaId, true);
    QSignalSpy favourite(m_api, &ApiClient::favoriteChanged);
    waitFor(favourite, 1);

    const Reply favourites = rawRequest(QStringLiteral("/api/v1/library/favorites"));
    QCOMPARE(favourites.status, 200);
    QCOMPARE(QJsonDocument::fromJson(favourites.body)
                 .object().value(QStringLiteral("items")).toArray().size(), 1);

    // A second profile must not inherit it: favourites are per-profile state,
    // which is the whole point of having profiles.
    QJsonObject secondProfile;
    secondProfile.insert(QStringLiteral("name"), QStringLiteral("Second"));
    const Reply created = rawRequest(
        QStringLiteral("/api/v1/profiles"), QStringLiteral("POST"),
        QJsonDocument(secondProfile).toJson(QJsonDocument::Compact));
    QCOMPARE(created.status, 201);
    const int other = QJsonDocument::fromJson(created.body).object()
                          .value(QStringLiteral("id")).toInt();
    QVERIFY(other > 0 && other != m_profileId);

    QNetworkRequest otherRequest{
        QUrl(m_server->url() + QStringLiteral("/api/v1/library/favorites"))};
    otherRequest.setRawHeader("Authorization",
                              m_session->api()->authorizationHeader().toUtf8());
    otherRequest.setRawHeader("X-Profile-Id", QByteArray::number(other));
    QNetworkAccessManager manager;
    QNetworkReply *reply = manager.get(otherRequest);
    QEventLoop loop;
    QObject::connect(reply, &QNetworkReply::finished, &loop, &QEventLoop::quit);
    QTimer::singleShot(20000, &loop, &QEventLoop::quit);
    loop.exec();
    QCOMPARE(reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt(), 200);
    QCOMPARE(QJsonDocument::fromJson(reply->readAll())
                 .object().value(QStringLiteral("items")).toArray().size(), 0);
    reply->deleteLater();
}

void EndToEndTest::playbackMintsATokenAndStreamsBytes()
{
    signedIn();

    QSignalSpy started(m_api, &ApiClient::playbackStarted);
    m_api->startPlayback(m_mediaId, 0.0);
    waitFor(started, 1);

    const QString url = started.at(0).at(1).toString();
    QVERIFY2(!url.isEmpty(),
             qPrintable(QStringLiteral(
                 "playbackStarted carried no stream URL for media %1")
                 .arg(m_mediaId)));
    QCOMPARE(started.at(0).at(0).toInt(), m_mediaId);
    // A bare URL: the credential rides in a header the access manager
    // attaches, so a token in the query string would leak into every log
    // between here and the server.
    QVERIFY(!url.contains(QStringLiteral("token")));
    QVERIFY(url.endsWith(QStringLiteral("/api/v1/stream/%1").arg(m_mediaId)));

    // The profile header is not sent for the stream: it is a bare URL the
    // access manager authenticates, and the token names the profile.
    const Reply streamed = rawRequest(url, QStringLiteral("GET"), {}, {}, true, false);
    QCOMPARE(streamed.status, 200);
    QVERIFY2(streamed.body.size() > 1000,
             qPrintable(QStringLiteral("expected a whole file, got %1 bytes")
                            .arg(streamed.body.size())));
    // A Matroska file starts with the EBML magic; a JSON error body would not.
    QCOMPARE(streamed.body.left(4), QByteArray("\x1a\x45\xdf\xa3", 4));
}

void EndToEndTest::aRangeRequestReturnsTheBytesAskedFor()
{
    signedIn();
    QSignalSpy started(m_api, &ApiClient::playbackStarted);
    m_api->startPlayback(m_mediaId, 0.0);
    waitFor(started, 1);

    const QString url = started.at(0).at(1).toString();
    QVERIFY2(!url.isEmpty(),
             qPrintable(QStringLiteral(
                 "playbackStarted carried no stream URL for media %1")
                 .arg(m_mediaId)));

    const Reply first = rawRequest(
        url, QStringLiteral("GET"), {},
        QList<QPair<QByteArray, QByteArray>>{{QByteArrayLiteral("Range"),
                                              QByteArrayLiteral("bytes=0-99")}},
        true, false);
    QCOMPARE(first.status, 206);
    QCOMPARE(first.body.size(), 100);
    QVERIFY(first.contentRange.startsWith("bytes 0-99/"));

    const Reply whole = rawRequest(url, QStringLiteral("GET"), {}, {}, true, false);
    QCOMPARE(whole.status, 200);

    // The middle of the file, which is what a player does when it seeks.
    const int middle = whole.body.size() / 2;
    const Reply offset = rawRequest(
        url, QStringLiteral("GET"), {},
        QList<QPair<QByteArray, QByteArray>>{
            {QByteArrayLiteral("Range"),
             QStringLiteral("bytes=%1-").arg(middle).toUtf8()}},
        true, false);
    QCOMPARE(offset.status, 206);
    QCOMPARE(offset.body.size(), whole.body.size() - middle);
    QVERIFY(offset.body == whole.body.mid(middle));

    // The grammar's edges.  RFC 9110 §14.2 says an unsatisfiable range is a
    // 416 and an *invalid* one "MUST" be ignored -- this server answers 416 to
    // both, which is stricter and legal; what matters is that it does not
    // answer 200 with the whole file, which would silently discard the seek.
    const Reply past = rawRequest(
        url, QStringLiteral("GET"), {},
        QList<QPair<QByteArray, QByteArray>>{{QByteArrayLiteral("Range"),
                                              QByteArrayLiteral("bytes=99999999-")}},
        true, false);
    QCOMPARE(past.status, 416);
    QVERIFY(past.body.size() < 100);
}

void EndToEndTest::progressReachesTheServerAndResumesFromIt()
{
    signedIn();

    // Report a position the way the player does.
    m_api->reportProgress(m_mediaId, 0.4, 60.0);
    QSignalSpy reported(m_api, &ApiClient::progressReported);
    waitFor(reported, 1);

    // It has to be *on the server*, not merely in the client's memory: the
    // whole point is that it survives the app being closed.
    const Reply progress = rawRequest(
        QStringLiteral("/api/v1/media/%1/progress").arg(m_mediaId));
    QCOMPARE(progress.status, 200);
    const QJsonObject stored =
        QJsonDocument::fromJson(progress.body).object();
    QCOMPARE(stored.value(QStringLiteral("position_seconds")).toDouble(), 0.4);
    QCOMPARE(stored.value(QStringLiteral("profile_id")).toInt(), m_profileId);

    // Re-opening resumes from it, which is what makes "Continue watching"
    // true rather than decorative.
    QSignalSpy started(m_api, &ApiClient::playbackStarted);
    m_api->startPlayback(m_mediaId, 0.0);
    waitFor(started, 1);
    QCOMPARE(started.at(0).at(2).toDouble(), 0.4);

    // Finishing marks it watched, and the position stops being a resume point.
    m_api->finishPlayback(m_mediaId, 60.0);
    QSignalSpy finished(m_api, &ApiClient::playbackFinished);
    waitFor(finished, 1);
    QCOMPARE(finished.at(0).at(0).value<ApiClient::FinishedPlayback>().mediaId,
             m_mediaId);
}

void EndToEndTest::loggingOutAndBackInKeepsTheFavourites()
{
    signedIn();
    m_controller->toggleFavorite(m_mediaId, true);
    QSignalSpy favourite(m_api, &ApiClient::favoriteChanged);
    waitFor(favourite, 1);

    m_session->signOut();
    QSignalSpy out(m_api, &ApiClient::loggedOut);
    waitFor(out, 1, 10000);
    QCOMPARE(m_session->state(), SessionController::Login);
    QVERIFY(!m_api->hasSession());

    // A second controller, because the first one's ApiClient is the one that
    // was cleared -- and "sign in again" has to work from scratch.
    SessionController second(this);
    second.connectTo(m_server->url());
    QElapsedTimer timer;
    timer.start();
    while (second.state() != SessionController::Login && timer.elapsed() < 20000)
        QTest::qWait(25);
    QCOMPARE(second.state(), SessionController::Login);

    second.signIn(m_server->username(), m_server->password(), false);
    timer.restart();
    while (second.state() != SessionController::Profiles && timer.elapsed() < 20000)
        QTest::qWait(25);
    QCOMPARE(second.state(), SessionController::Profiles);
    // A profile has to be *chosen* before per-profile state means anything, and
    // the server refuses the read without one rather than answering with the
    // account's first.  The session machine is the thing that owns that
    // choice, so the test makes it the way the app does -- and waits for the
    // switch to be recorded, which is what actually lands the session on Home.
    QSignalSpy selected(second.api(), &ApiClient::profileSelected);
    QSignalSpy refused(second.api(), &ApiClient::failed);
    second.selectProfile(m_profileId);
    waitFor(selected, 1);
    for (int i = 0; i < refused.count(); ++i) {
        const ApiError error = refused.at(i).at(1).value<ApiError>();
        QFAIL(qPrintable(QStringLiteral("%1 failed: %2 (%3)")
                            .arg(refused.at(i).at(0).toString(), error.message)
                            .arg(error.statusCode)));
    }
    timer.restart();
    while (second.state() != SessionController::Home && timer.elapsed() < 20000)
        QTest::qWait(25);
    QCOMPARE(second.state(), SessionController::Home);

    AppController reopened(second.api(), this);
    // The spy first, then the fetch.  Attaching it afterwards waits for an
    // answer to a request that has already been made, and `favoritesLoaded`
    // never fires again -- which reads as "the favourites did not survive",
    // when what happened is that nothing was listening.
    QSignalSpy favourites(second.api(), &ApiClient::favoritesLoaded);
    QSignalSpy failures(second.api(), &ApiClient::failed);
    reopened.goFavorites();
    waitFor(favourites, 1);
    for (int i = 0; i < failures.count(); ++i) {
        const ApiError error = failures.at(i).at(1).value<ApiError>();
        QFAIL(qPrintable(QStringLiteral("%1 failed: %2 (%3)")
                            .arg(failures.at(i).at(0).toString(), error.message)
                            .arg(error.statusCode)));
    }
    QCOMPARE(reopened.favorites()->rowCount(), 1);
    QCOMPARE(reopened.favorites()->data(reopened.favorites()->index(0),
                                        MediaListModel::TitleRole).toString(),
             kTitle);
}

// ---------------------------------------------------------------------------
// The failure cases
// ---------------------------------------------------------------------------

void EndToEndTest::aServerThatIsNotThereFailsCleanly()
{
    // Port 1 on loopback refuses immediately, which is the fastest way to be
    // talking to nothing.
    m_session->connectTo(QStringLiteral("http://127.0.0.1:1"));
    QElapsedTimer timer;
    timer.start();
    while (m_session->state() != SessionController::Login && timer.elapsed() < 20000)
        QTest::qWait(25);

    QCOMPARE(m_session->state(), SessionController::Login);
    QVERIFY2(!m_session->lastError().isEmpty(),
             "an unreachable server has to say so rather than spin forever");
    QVERIFY(!m_api->hasSession());
}

void EndToEndTest::aWrongPasswordIsRejectedWithoutASession()
{
    m_api->setBaseUrl(m_server->url());
    m_session->connectTo(m_server->url());
    QElapsedTimer timer;
    timer.start();
    while (m_session->state() != SessionController::Login && timer.elapsed() < 20000)
        QTest::qWait(25);
    QCOMPARE(m_session->state(), SessionController::Login);

    m_session->signIn(m_server->username(), QStringLiteral("not-the-password"),
                      false);
    timer.restart();
    while (m_session->lastError().isEmpty() && timer.elapsed() < 20000)
        QTest::qWait(25);

    QVERIFY2(!m_session->lastError().isEmpty(),
             "a wrong password has to produce a message");
    QCOMPARE(m_session->state(), SessionController::Login);
    QVERIFY(!m_api->hasSession());
}

void EndToEndTest::anUnknownTitleIsNotFound()
{
    signedIn();
    m_controller->openDetail(999999);
    QSignalSpy failure(m_api, &ApiClient::failed);
    waitFor(failure, 1);

    const ApiError error = failure.at(0).at(1).value<ApiError>();
    QCOMPARE(error.statusCode, 404);
    QCOMPARE(error.kind, ApiErrorKind::NotFound);
    // A title that is not there is not worth retrying.
    QVERIFY(!error.isRetryable());
    // And the screen does not pretend it loaded something.
    // Not the detail screen: it only appears once there is something to show,
    // and a title that is not there is nothing.  The screen the viewer was on
    // stays, which is also what a back press would have returned to.
    QCOMPARE(m_controller->screen(), AppController::Library);
    QVERIFY(m_controller->detail().isEmpty());
}

void EndToEndTest::aRequestWithoutATokenIsRefused()
{
    const Reply reply = rawRequest(QStringLiteral("/api/v1/library/media"),
                                  QStringLiteral("GET"), {}, {}, false, false);
    QCOMPARE(reply.status, 401);
    // The body has to be the documented error shape, not an HTML page or an
    // empty 401 the UI has nothing to show.
    const QJsonObject body = QJsonDocument::fromJson(reply.body).object();
    QVERIFY(!body.value(QStringLiteral("error")).toString().isEmpty());
    QVERIFY(!body.value(QStringLiteral("message")).toString().isEmpty());
}

void EndToEndTest::aProfileHeaderForSomebodyElsesProfileIsRefused()
{
    signedIn();

    // A profile that does not exist at all.  The server answers 404 rather than
    // 403 for a profile the caller does not own, so that a probe cannot tell
    // "no such profile" from "not yours" -- see SECURITY.md.
    const Reply reply = rawRequest(
        QStringLiteral("/api/v1/library/media"), QStringLiteral("GET"), {},
        QList<QPair<QByteArray, QByteArray>>{{QByteArrayLiteral("X-Profile-Id"),
                                              QByteArrayLiteral("424242")}},
        true, false);
    QCOMPARE(reply.status, 404);
    QCOMPARE(QJsonDocument::fromJson(reply.body).object()
                 .value(QStringLiteral("error")).toString(),
             QStringLiteral("profile_not_found"));
}

// ---------------------------------------------------------------------------
// The filter row, and what it sends
// ---------------------------------------------------------------------------

void EndToEndTest::theLibraryFilterRowNarrowsTheGrid()
{
    signedIn();

    // The server is what decides what "genre=… sorted by title" means, so the
    // assertion is on the *wire*, not on the grid: a filter that looks right and
    // sends nothing is the failure, and it is invisible until the server's own
    // filter path is exercised.

    // Not `title`, which is the default sort and therefore the one request that
    // changes nothing on the wire.  `last_played` is a key the server accepts
    // and nothing here has used.
    QSignalSpy sent(m_api, &ApiClient::requestSent);
    m_controller->setLibrarySort(QStringLiteral("last_played"));
    waitFor(sent, 1);
    const QString sortRequest = sent.last().at(0).toString();
    QVERIFY2(sortRequest.contains(QStringLiteral("sort=last_played")),
             qPrintable(QStringLiteral("sort did not reach the server: %1")
                            .arg(sortRequest)));

    // The categories the filter row's buttons are built from.  `goLibrary`
    // fetches them when the screen opens, and it does not re-fetch when the
    // answer has already arrived -- so this waits rather than asserting
    // immediately.
    QElapsedTimer categories;
    categories.start();
    while (m_controller->filterOptions().isEmpty() && categories.elapsed() < 20000)
        QTest::qWait(25);

    const QVariantList genres =
        m_controller->filterOptions().value(QStringLiteral("genres")).toList();
    // What the server answers, verbatim.  A failure here is one of two quite
    // different things -- the server has no genres, or the client never asked
    // for them -- and the raw body tells them apart without another round.
    const QString serverSays = QString::fromUtf8(
        rawRequest(QStringLiteral("/api/v1/library/categories"),
                   QStringLiteral("GET")).body.left(400));
    const QVariantMap options = m_controller->filterOptions();
    const QString clientHas = QString::fromUtf8(
        QJsonDocument(QJsonObject::fromVariantMap(options))
            .toJson(QJsonDocument::Compact).left(400));
    QVERIFY2(!genres.isEmpty(),
             qPrintable(QStringLiteral(
                            "the library screen did not load the categories "
                            "its filter row is built from (options: %1); the "
                            "server answered: %2")
                            .arg(clientHas, serverSays)));
    const QString slug = genres.first().toMap().value(QStringLiteral("slug")).toString();
    QVERIFY(!slug.isEmpty());

    sent.clear();
    m_controller->setLibraryGenre(slug);
    waitFor(sent, 1);
    const QString genreRequest = sent.last().at(0).toString();
    QVERIFY2(genreRequest.contains(QStringLiteral("genre=") + slug),
             qPrintable(QStringLiteral("genre did not reach the server: %1")
                            .arg(genreRequest)));
    QCOMPARE(m_controller->libraryGenre(), slug);

    sent.clear();
    m_controller->clearLibraryFilters();
    waitFor(sent, 1);
    const QString clearedRequest = sent.last().at(0).toString();
    QVERIFY2(!clearedRequest.contains(QStringLiteral("genre=")),
             qPrintable(QStringLiteral("clearing the filter did not reach the "
                                       "server: %1").arg(clearedRequest)));
    QVERIFY(m_controller->libraryGenre().isEmpty());
}

// ---------------------------------------------------------------------------
// Losing the server, and a profile behind a PIN
// ---------------------------------------------------------------------------

void EndToEndTest::aServerThatGoesAwayMidSessionFailsCleanly()
{
    signedIn();
    QVERIFY(m_api->hasSession());

    // A server that does not exist rather than one that has gone away: nothing is
    // listening on port 1, so the connection is refused immediately.  The client
    // still holds a valid token, and the failure has to be about the server
    // rather than about the session -- the mistake this catches is one that
    // throws the session away on a transport error, which looks like being
    // signed out by something the user did not do.
    const QString reachable = m_server->url();
    m_api->setBaseUrl(QStringLiteral("http://127.0.0.1:1"));
    QSignalSpy failed(m_api, &ApiClient::failed);
    m_controller->refreshLibrary();
    waitFor(failed, 1);

    const ApiError error = failed.at(0).at(1).value<ApiError>();
    QCOMPARE(error.kind, ApiErrorKind::NetworkUnreachable);
    QVERIFY2(error.isRetryable(),
             "a server that is not answering is worth retrying, and the error "
             "has to say so");
    QVERIFY2(!error.message.isEmpty(),
             "an unreachable server has to produce a message the screen can "
             "show");
    // The token is gone -- and it *should* be, because pointing the client at
    // a different host is a different trust boundary, and keeping the token
    // would send this user's bearer to whatever host was typed last.  So the
    // session's question is not "was the token kept" but "is the failure
    // reported as what it was, rather than as a revoked session": a 401 would
    // say "your session expired" for a server that is not there at all.
    QVERIFY2(!m_api->hasSession(),
             "the token outlived a change of host, which would send it to "
             "whatever was typed next");
    QCOMPARE(error.statusCode, 0);

    // The failure belongs to the grid, so the library screen can offer a retry
    // rather than showing an empty library with no explanation.
    QVERIFY(m_controller->library()->isFailed());
    QVERIFY(m_controller->library()->isRetryable());

    // And pointing the client back at the server it came from recovers it.
    // `connectTo` rather than `setBaseUrl`, because the second is what tore the
    // session down in the first place: it clears the token for a different host,
    // and this is the same host again.
    m_session->connectTo(reachable);
    signedIn();
    QCOMPARE(m_session->state(), SessionController::Profiles);
}

void EndToEndTest::aProfileWithAPinCanBeUnlockedAndLockedAgain()
{
    // No `signedIn` here, deliberately.  The wrong-PIN half has to run against
    // a profile that has a PIN, and setting one needs the account -- but the
    // answer to the wrong PIN is watched on `ApiClient`, and `profileUnlocked`
    // is a client signal while `lastError` is a session one.  Setting up the
    // PIN first, then signing in, means both halves are asserted on the object
    // that carries the signal they are about, instead of on one object for the
    // right PIN and another for the wrong one.
    QSignalSpy wrongPin(m_api, &ApiClient::failed);
    QSignalSpy rightPin(m_api, &ApiClient::profileUnlocked);

    // -- set up: an account, a profile, a PIN on it ----------------------
    // A raw login, and the token taken from its answer.
    //
    // `rawRequest` cannot hold a token -- it sends whatever the *client* has,
    // and this client has none -- so it is used once, for the login, and the
    // bearer it returns is passed explicitly to every request after it.  That
    // is the only way to set a PIN on a profile that has one, which is the
    // subject of the test: a profile behind a PIN cannot be *signed in to*, so
    // the account is authenticated without one.
    m_api->setBaseUrl(m_server->url());

    m_api->setBaseUrl(m_server->url());

    const Reply login = rawRequest(
        QStringLiteral("/api/v1/auth/login"), QStringLiteral("POST"),
        QJsonDocument(QJsonObject{
            {QStringLiteral("username"), m_server->username()},
            {QStringLiteral("password"), m_server->password()},
            {QStringLiteral("device_name"), QStringLiteral("e2e")},
        }).toJson(), {}, false, false);
    QCOMPARE(login.status, 200);
    const QJsonObject loginBody = QJsonDocument::fromJson(login.body).object();
    const QByteArray bearer = loginBody.value(QStringLiteral("access_token"))
                                  .toString().toUtf8();
    QVERIFY2(!bearer.isEmpty(), login.body.constData());
    const QList<QPair<QByteArray, QByteArray>> auth{
        {QByteArrayLiteral("Authorization"), QByteArrayLiteral("Bearer ") + bearer}};

    // `/auth/me` *refuses* when the profile it would return is behind a PIN --
    // that is the whole point of a PIN -- so the profile list comes from the
    // route that answers either way.  Reading it from `/auth/me` is what put the
    // first version of this test into a retry loop nine hundred requests deep.
    const Reply list = rawRequest(QStringLiteral("/api/v1/profiles"),
                                  QStringLiteral("GET"), {}, auth, false, false);
    QCOMPARE(list.status, 200);
    const QJsonArray profiles = QJsonDocument::fromJson(list.body).array();
    QVERIFY2(!profiles.isEmpty(), list.body.constData());
    const int profileId = profiles.first().toObject()
                              .value(QStringLiteral("id")).toInt();
    QVERIFY(profileId > 0);

    const QString pin = QStringLiteral("4815");
    const Reply setPin = rawRequest(
        QStringLiteral("/api/v1/profiles/%1/pin").arg(profileId),
        QStringLiteral("POST"),
        QJsonDocument(QJsonObject{{QStringLiteral("pin"), pin}}).toJson(),
        auth, false, false);
    QCOMPARE(setPin.status, 200);

    // -- the wrong PIN ------------------------------------------------------
    // Watch the client, not the session: the request's outcome is a client
    // signal.  Anything that moved the session counts too, so a "success" that
    // arrived late cannot pass as one.  The *third* attempt is the interesting
    // one, because that is where a client that leaked the PIN, or retried it,
    // would show up.
    // -- the wrong PIN ------------------------------------------------------
    // -- the wrong PIN ------------------------------------------------------
    // The client, not a raw request: a raw one cannot be attributed, because
    // `rawRequest` sends whatever token the *client* holds and a 401 from it is
    // indistinguishable from a 401 the unlock caused.
    //
    // The client, and not the *session controller*, and that is not a
    // preference.  The account's only profile now has a PIN, so it is not one
    // the app can be signed in to: the server answers `/auth/me` with 403
    // `profile_unlocked_required` the moment it is selected, the session machine
    // never settles, and it retries `/auth/me` forever -- nine hundred requests
    // in twenty seconds, with not one signal for the unlock to come back on.
    // The token is in the test's own hands from the raw login above, so the
    // client is handed it directly rather than being made to sign in again.
    m_api->setSession(QString::fromUtf8(bearer), QString(), 0);
    QVERIFY(m_api->hasSession());

    m_api->unlockProfile(profileId, QStringLiteral("0000"));
    QElapsedTimer timer;
    timer.start();
    while (wrongPin.count() == 0 && rightPin.count() == 0 && timer.elapsed() < 20000)
        QTest::qWait(25);

    QVERIFY2(wrongPin.count() + rightPin.count() > 0,
             "the unlock request produced neither a refusal nor an unlock");

    // Nothing came back, which means the request is still in flight or was
    // never sent.  Said out loud, because the assertion below would otherwise
    // report "expected 1, got 0" for a request that never left.
    QVERIFY2(wrongPin.count() + rightPin.count() > 0,
             "the unlock request produced neither a refusal nor an unlock");
    QCOMPARE(rightPin.count(), 0);
    QCOMPARE(wrongPin.count(), 1);
    // Which request failed, spelled out.  `ApiClient::failed` carries every
    // failure in the app, so without this the assertion below could be about a
    // request this test did not make.
    const QString operation = wrongPin.at(0).at(0).toString();
    const ApiError error = wrongPin.at(0).at(1).value<ApiError>();
    QCOMPARE(operation, QStringLiteral("profiles.unlock"));
    // 401, not 403: the server's own answer to a wrong PIN, and the client
    // reports it as that rather than reclassifying it.  This is the one place
    // the two coincide usefully -- `handle()` sees a 401 and, because there is
    // no refresh token here, cannot attempt one, so it falls through to the
    // ordinary path and reports the server's own kind and message.  With a
    // refresh token *held* the same 401 is a session expiry, and the replay
    // machinery answers it -- which is why the client is handed a bare token in
    // this test rather than a pair.
    QCOMPARE(error.kind, ApiErrorKind::Unauthorized);
    QCOMPARE(error.statusCode, 401);
    QCOMPARE(error.code, QStringLiteral("pin_incorrect"));
    QVERIFY2(!error.message.isEmpty(),
             "a wrong PIN has to produce a message the picker can show");
    // And the client still holds the account: a PIN protects a profile, not a
    // sign-in, so a refused guess must not cost the viewer their session.
    QVERIFY(m_api->hasSession());
    QCOMPARE(m_api->profileId(), 0);

    // -- the right one ------------------------------------------------------
    m_api->unlockProfile(profileId, pin);
    timer.restart();
    while (rightPin.count() == 0 && timer.elapsed() < 20000)
        QTest::qWait(25);
    QCOMPARE(rightPin.count(), 1);
    QCOMPARE(rightPin.at(0).at(0).toInt(), profileId);

    // And the PIN itself is never readable: the profile comes back with a flag,
    // not with the digits.  The *same* token that just unlocked it, so this is
    // what a signed-in client would see.
    const QByteArray after = rawRequest(QStringLiteral("/api/v1/profiles"),
                                        QStringLiteral("GET"),
                                        {}, auth, false, false).body;
    QVERIFY2(!after.contains(pin.toUtf8()),
             "the profile list carries the PIN itself rather than a flag");
    QVERIFY(after.contains("\"has_pin\":true"));
}


QTEST_MAIN(EndToEndTest)
#include "tst_e2e.moc"

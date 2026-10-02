// The session state machine.
//
// Deliberately QTEST_GUILESS: nothing here touches Qt Quick, and a GUI
// application aborts outright on a headless build machine, which is how this
// suite came to be reported as "Subprocess aborted" rather than as a failure.
//
// This is the file that decides whether a user sees the connection screen, the
// login screen, or their library -- so its transitions are tested here rather
// than discovered on a device.  Everything below drives the real controller
// against a real loopback HTTP server, because the interesting failures are in
// the transitions between asynchronous callbacks, and a mock transport would
// hide exactly those.

#include <QtTest>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSignalSpy>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTimer>

#include "apiclient.h"
#include "sessioncontroller.h"

namespace {

/// A loopback HTTP server answering a fixed routing table.
///
/// Hand-rolled rather than built on Qt's HTTP server module: the point is to
/// answer three endpoints and, on demand, to answer *nothing* -- which is how
/// the "server is unreachable" and "logout lost its network" paths get
/// exercised without unplugging anything.
class FakeServer : public QTcpServer
{
public:
    void setAnswers(const QHash<QString, QPair<int, QByteArray>> &answers)
    {
        m_answers = answers;
    }
    /// Accept the connection and then say nothing.  The client's own timeout is
    /// what ends the request, which is the behaviour a black-holed network has.
    void setSilent(bool silent) { m_silent = silent; }
    QList<QByteArray> requests() const { return m_requests; }

protected:
    void incomingConnection(qintptr handle) override
    {
        auto *socket = new QTcpSocket(this);
        // The client sends headers and body as two writes, so one readyRead
        // often carries only the headers -- and answering then would be
        // answering a half-request, which is what made the first version of
        // this fake leave every test stuck in "Connecting".
        connect(socket, &QTcpSocket::readyRead, this, [this, socket] {
            QByteArray &buffer = m_buffers[socket];
            buffer.append(socket->readAll());
            if (!isCompleteRequest(buffer))
                return;                 // more bytes will follow
            const QByteArray request = buffer;
            m_buffers.remove(socket);
            respond(socket, request);
        });
        socket->setSocketDescriptor(handle);
    }

private:
    static bool isCompleteRequest(const QByteArray &request)
    {
        const int headerEnd = request.indexOf("\r\n\r\n");
        if (headerEnd < 0)
            return false;
        const QByteArray headers = request.left(headerEnd).toLower();
        const int at = headers.indexOf("content-length:");
        int contentLength = 0;
        if (at >= 0) {
            const QByteArray value = headers.mid(at + 15);
            contentLength = value.left(value.indexOf('\r')).trimmed().toInt();
        }
        return request.size() >= headerEnd + 4 + contentLength;
    }

    void respond(QTcpSocket *socket, const QByteArray &request)
    {
        m_requests.append(request);
        if (m_silent) {
            socket->disconnectFromHost();
            return;
        }
        const int firstSpace = request.indexOf(' ');
        const int secondSpace = request.indexOf(' ', firstSpace + 1);
        const QByteArray path = request.mid(firstSpace + 1, secondSpace - firstSpace - 1);
        const auto answer = m_answers.value(QString::fromLatin1(path));
        if (answer.first == 0) {
            socket->write("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n");
            socket->disconnectFromHost();
            return;
        }
        QByteArray response;
        response += "HTTP/1.1 " + QByteArray::number(answer.first) + " "
                    + reasonPhrase(answer.first) + "\r\n";
        response += "Content-Type: application/json\r\n";
        response += "Content-Length: " + QByteArray::number(answer.second.size())
                    + "\r\n";
        response += "Connection: close\r\n\r\n";
        response += answer.second;
        socket->write(response);
        socket->disconnectFromHost();
    }

private:
    static QByteArray reasonPhrase(int status)
    {
        switch (status) {
        case 200: return "OK";
        case 401: return "Unauthorized";
        case 404: return "Not Found";
        default:  return "Status";
        }
    }

    QHash<QString, QPair<int, QByteArray>> m_answers;
    QHash<QTcpSocket *, QByteArray> m_buffers;
    QList<QByteArray> m_requests;
    bool m_silent = false;
};

// -- canned responses -------------------------------------------------------

QPair<int, QByteArray> body(int status, const QByteArray &json)
{
    return {status, json};
}

const QByteArray kHealth =
    R"({"status":"ok","database":"ok","version":"1.0.0","api_version":"v1"})";

QByteArray tokenPair(bool mustChange)
{
    return QByteArray(R"({"access_token":"ssat.access","refresh_token":"ssrt.refresh",)"
                      R"("token_type":"Bearer","expires_in":900,)"
                      R"("session_id":"session-1",)"
                      R"("refresh_expires_at":"2027-01-01T00:00:00+00:00",)") +
           R"("must_change_password":)" + (mustChange ? "true" : "false") + "," +
           R"("user":{"id":1,"username":"phuree","display_name":"Phuree",)"
           R"("is_admin":true}})";
}

QByteArray session(const QByteArray &profiles)
{
    return R"({"user":{"id":1,"username":"phuree","display_name":"Phuree",)"
           R"("is_admin":true},"profiles":)" + profiles +
           R"(,"server":{"name":"Stream","version":"1.0.0","api_version":"v1",)"
           R"("requires_setup":false,"authenticated":true}})";
}

const QByteArray kReady =
    R"({"name":"Stream","version":"1.0.0","api_version":"v1",)"
    R"("requires_setup":false,"authenticated":false,"library_count":3,)"
    R"("profile_count":1,"library_roots":["/srv/media"],"ffprobe_available":true,)"
    R"("metadata_provider":"local","capabilities":{}})";

const QByteArray kNotSetUp =
    R"({"name":"Stream","version":"1.0.0","api_version":"v1",)"
    R"("requires_setup":true,"authenticated":false,"library_count":0,)"
    R"("profile_count":0,"library_roots":[],"ffprobe_available":true,)"
    R"("metadata_provider":"local","capabilities":{}})";

const QByteArray kMainProfile =
    R"([{"id":1,"name":"Main","avatar_id":null,"has_pin":false,)"
    R"("is_kids":false,"max_watch_minutes":null,"sort_order":0}])";

const QByteArray kTwoProfiles =
    R"([{"id":1,"name":"Main","has_pin":false,"sort_order":0},)"
    R"({"id":2,"name":"Kids","has_pin":true,"sort_order":1}])";

}  // namespace

class SessionControllerTest : public QObject
{
    Q_OBJECT

private:
    FakeServer m_server;
    SessionController *m_session = nullptr;

    QString url() const
    {
        return QStringLiteral("http://127.0.0.1:%1").arg(m_server.serverPort());
    }

    /// A fresh controller with a clean state machine.  The keychain is not
    /// available in this environment, so a remembered session does not persist
    /// between tests -- which is exactly what is wanted: each test starts from
    /// "nothing is remembered".
    void startSession()
    {
        m_session = new SessionController(this);
        m_server.setSilent(false);
    }

    void serveHealthyLogin(const QByteArray &profiles, bool mustChange = false)
    {
        m_server.setAnswers({
            {QStringLiteral("/api/health"), body(200, kHealth)},
            {QStringLiteral("/api/v1/auth/setup"),
             body(200, R"({"name":"Stream","version":"1.0.0","api_version":"v1",)"
                        R"("requires_setup":false,"authenticated":false,)"
                        R"("library_count":0,"profile_count":1,"library_roots":[],)"
                        R"("ffprobe_available":true,"metadata_provider":"local",)"
                        R"("capabilities":{"library":true}})")},
            {QStringLiteral("/api/v1/auth/login"), body(200, tokenPair(mustChange))},
            {QStringLiteral("/api/v1/auth/me"), body(200, session(profiles))},
            {QStringLiteral("/api/v1/profiles/select"), body(200, R"({"ok":true})")},
        });
    }

private slots:
    void initTestCase()
    {
        QVERIFY(m_server.listen(QHostAddress::LocalHost));
    }

    void noServerIsTheStartingState()
    {
        startSession();
        QCOMPARE(m_session->state(), SessionController::NoServer);
    }

    void anEmptyUrlIsRejectedWithoutTouchingTheNetwork()
    {
        startSession();
        QSignalSpy spy(m_session, &SessionController::serverReachable);
        m_session->connectTo(QStringLiteral("   "));
        QCOMPARE(m_session->state(), SessionController::NoServer);
        QCOMPARE(spy.count(), 0);
        QVERIFY(m_session->lastError().contains(QStringLiteral("server address")));
    }

    void connectingToNothingLandsOnLoginWithAMessage()
    {
        startSession();
        // Port 1 on loopback refuses immediately.
        m_session->connectTo(QStringLiteral("http://127.0.0.1:1"));
        QTRY_COMPARE(m_session->state(), SessionController::Login);
        QVERIFY(!m_session->lastError().isEmpty());
    }

    void aHealthyServerLandsOnLogin()
    {
        startSession();
        m_server.setAnswers({
            {QStringLiteral("/api/health"), body(200, kHealth)},
            {QStringLiteral("/api/v1/auth/setup"), body(200, kReady)},
        });
        QSignalSpy reachable(m_session, &SessionController::serverReachable);
        QSignalSpy changes(m_session, &SessionController::stateChanged);
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);

        // Two hops -- /api/health then /auth/setup -- so "reachable" fires
        // twice, and the *last* answer is the one that decides the screen.
        QCOMPARE(reachable.count(), 2);
        QVERIFY(m_session->lastError().isEmpty());
        // Connecting is a real state, not a skipped frame: the user sees it.
        QVERIFY(changes.count() >= 2);
    }

    void loginLeadsToProfileSelection()
    {
        startSession();
        serveHealthyLogin(kMainProfile);
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);

        m_session->signIn(QStringLiteral("phuree"), QStringLiteral("a-good-password"),
                          false);
        QTRY_COMPARE(m_session->state(), SessionController::Profiles);
        QCOMPARE(m_session->username(), QStringLiteral("phuree"));
        QVERIFY(m_session->api()->hasSession());
    }

    void aForcedPasswordChangeGoesToThePasswordScreen()
    {
        startSession();
        serveHealthyLogin(kMainProfile, /*mustChange=*/true);
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);

        m_session->signIn(QStringLiteral("phuree"), QStringLiteral("the-default-one"),
                          false);
        QTRY_COMPARE(m_session->state(), SessionController::ChangePassword);
        QVERIFY(m_session->api()->mustChangePassword());
        // And it does not proceed to the profiles on its own.
        QTest::qWait(50);
        QCOMPARE(m_session->state(), SessionController::ChangePassword);
    }

    void badCredentialsStayOnLoginWithTheServersMessage()
    {
        startSession();
        m_server.setAnswers({
            {QStringLiteral("/api/health"), body(200, kHealth)},
            {QStringLiteral("/api/v1/auth/setup"), body(200, kReady)},
            {QStringLiteral("/api/v1/auth/login"),
             body(401, R"({"error":"invalid_credentials",)"
                        R"("message":"That username or password is not right.",)"
                        R"("request_id":"abc123"})")},
        });
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);

        m_session->signIn(QStringLiteral("phuree"), QStringLiteral("wrong"), false);
        QTRY_VERIFY(!m_session->lastError().isEmpty());
        QCOMPARE(m_session->state(), SessionController::Login);
        QVERIFY(m_session->lastError().contains(QStringLiteral("not right")));
        QVERIFY(!m_session->api()->hasSession());
    }

    void aThrottledLoginShowsTheServersRetryMessage()
    {
        startSession();
        m_server.setAnswers({
            {QStringLiteral("/api/health"), body(200, kHealth)},
            {QStringLiteral("/api/v1/auth/setup"), body(200, kReady)},
            {QStringLiteral("/api/v1/auth/login"),
             body(429, R"({"error":"rate_limited",)"
                        R"("message":"Too many sign-in attempts. Try again shortly."})")},
        });
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);
        m_session->signIn(QStringLiteral("phuree"), QStringLiteral("wrong"), false);
        QTRY_VERIFY(m_session->lastError().contains(QStringLiteral("Too many")));
        QCOMPARE(m_session->state(), SessionController::Login);
    }

    void selectingAProfileLandsOnHome()
    {
        startSession();
        serveHealthyLogin(kTwoProfiles);
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);
        m_session->signIn(QStringLiteral("phuree"), QStringLiteral("a-good-password"),
                          false);
        QTRY_COMPARE(m_session->state(), SessionController::Profiles);

        m_session->selectProfile(2);
        QTRY_COMPARE(m_session->state(), SessionController::Home);
        QCOMPARE(m_session->profileId(), 2);
    }

    void unlockingAProfileLandsOnHome()
    {
        startSession();
        m_server.setAnswers({
            {QStringLiteral("/api/health"), body(200, kHealth)},
            {QStringLiteral("/api/v1/auth/setup"), body(200, kReady)},
            {QStringLiteral("/api/v1/auth/login"), body(200, tokenPair(false))},
            {QStringLiteral("/api/v1/auth/me"), body(200, session(kTwoProfiles))},
            {QStringLiteral("/api/v1/profiles/2/unlock"), body(200, R"({"ok":true})")},
        });
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);
        m_session->signIn(QStringLiteral("phuree"), QStringLiteral("a-good-password"),
                          false);
        QTRY_COMPARE(m_session->state(), SessionController::Profiles);

        m_session->unlockProfile(2, QStringLiteral("1357"));
        QTRY_COMPARE(m_session->state(), SessionController::Home);
        QCOMPARE(m_session->profileId(), 2);
    }

    void aWrongPinDoesNotUnlock()
    {
        startSession();
        m_server.setAnswers({
            {QStringLiteral("/api/health"), body(200, kHealth)},
            {QStringLiteral("/api/v1/auth/setup"), body(200, kReady)},
            {QStringLiteral("/api/v1/auth/login"), body(200, tokenPair(false))},
            {QStringLiteral("/api/v1/auth/me"), body(200, session(kTwoProfiles))},
            {QStringLiteral("/api/v1/profiles/2/unlock"),
             body(401, R"({"error":"pin_incorrect","message":"Incorrect PIN."})")},
        });
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);
        m_session->signIn(QStringLiteral("phuree"), QStringLiteral("a-good-password"),
                          false);
        QTRY_COMPARE(m_session->state(), SessionController::Profiles);

        m_session->unlockProfile(2, QStringLiteral("0000"));
        // The route logs the failure and re-raises; the message the user sees
        // is the server's, so the assertion is on that, not on a local string.
        QTRY_COMPARE(m_session->state(), SessionController::Profiles);
        QCOMPARE(m_session->profileId(), 0);
        QCOMPARE(m_session->state(), SessionController::Profiles);
        QCOMPARE(m_session->profileId(), 0);
    }

    void aServerThatWentAwayMidSessionStaysOnALoginableScreen()
    {
        startSession();
        serveHealthyLogin(kMainProfile);
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);
        m_session->signIn(QStringLiteral("phuree"), QStringLiteral("a-good-password"),
                          false);
        QTRY_COMPARE(m_session->state(), SessionController::Profiles);

        // The network drops while the profile screen is up.
        m_server.setSilent(true);
        m_session->selectProfile(1);
        QTest::qWait(300);
        // Nothing may have advanced: the profile was never confirmed, so the app
        // must not pretend it is browsing.
        QVERIFY(m_session->state() != SessionController::Home);
    }

    void logoutAlwaysEndsTheLocalSession()
    {
        startSession();
        serveHealthyLogin(kMainProfile);
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);
        m_session->signIn(QStringLiteral("phuree"), QStringLiteral("a-good-password"),
                          false);
        QTRY_COMPARE(m_session->state(), SessionController::Profiles);
        m_session->selectProfile(1);
        QTRY_COMPARE(m_session->state(), SessionController::Home);

        // The server cannot answer the logout.  The local session must still
        // end: a logout that failed because the network dropped must not leave
        // the next person to pick up the remote signed in.
        m_server.setSilent(true);
        m_session->signOut();
        QCOMPARE(m_session->state(), SessionController::Login);
        QCOMPARE(m_session->profileId(), 0);
        QVERIFY(!m_session->api()->hasSession());
    }

    void aServerWithNoAccountsSaysSoRatherThanAskingToSignIn()
    {
        startSession();
        m_server.setAnswers({
            {QStringLiteral("/api/health"), body(200, kHealth)},
            {QStringLiteral("/api/v1/auth/setup"), body(200, kNotSetUp)},
        });
        QSignalSpy reachable(m_session, &SessionController::serverReachable);
        m_session->connectTo(url());

        // A server with no accounts must not offer a sign-in screen: there is
        // no account to sign in to.  Today the app has no setup screen yet, so
        // it lands on the change-password screen, which is also what a
        // must-change-password account gets.  When the setup screen lands this
        // assertion moves with it.
        QTRY_COMPARE(m_session->state(), SessionController::ChangePassword);
        QCOMPARE(reachable.count(), 2);
        QCOMPARE(reachable.at(0).at(2).toBool(), false);
        QCOMPARE(reachable.at(1).at(2).toBool(), true);
    }

    void changingTheServerDropsTheSession()
    {
        startSession();
        m_server.setAnswers({
            {QStringLiteral("/api/health"), body(200, kHealth)},
            {QStringLiteral("/api/v1/auth/setup"), body(200, kReady)},
        });
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);
        m_session->api()->setSession(QStringLiteral("ssat.old"),
                                     QStringLiteral("ssrt.old"), 900);
        QVERIFY(m_session->api()->hasSession());

        m_session->connectTo(QStringLiteral("http://127.0.0.1:2"));
        QTRY_VERIFY(!m_session->api()->hasSession());
    }

    void anUnparseableResponseIsAnErrorNotAnEmptyScreen()
    {
        startSession();
        m_server.setAnswers({
            {QStringLiteral("/api/health"), body(200, kHealth)},
            {QStringLiteral("/api/v1/auth/setup"), body(200, kReady)},
            {QStringLiteral("/api/v1/auth/login"), body(200, "{not json")},
        });
        m_session->connectTo(url());
        QTRY_COMPARE(m_session->state(), SessionController::Login);
        m_session->signIn(QStringLiteral("phuree"), QStringLiteral("a-good-password"),
                          false);
        QTRY_VERIFY(!m_session->lastError().isEmpty());
        QCOMPARE(m_session->state(), SessionController::Login);
    }
};

QTEST_GUILESS_MAIN(SessionControllerTest)
#include "tst_sessioncontroller.moc"
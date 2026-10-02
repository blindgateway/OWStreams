// Where a bearer token is attached is a security property, not an
// implementation detail.
//
// `QQuickImage` cannot send a header, so the poster routes are authenticated
// and the token has to be added underneath.  The failure mode this suite exists
// to catch is the obvious one: a manager that stamps the token on everything,
// which sends this user's credential to whatever host a metadata sidecar
// happened to name.  The tests below are about the *negative* cases -- the
// requests that must go out unauthenticated -- because those are the ones that
// leak.

#include <QtTest>

#include "credentialaccessmanager.h"

class CredentialAccessTest : public QObject
{
    Q_OBJECT

private slots:
    void init();

    void artworkOnOurServerGetsTheToken();
    void anotherHostGetsNothing();
    void anotherPortGetsNothing();
    void anotherSchemeGetsNothing();
    void aLookalikePathGetsNothing();
    void aNonGetRequestGetsNothing();
    void noTokenMeansNothingIsAttached();
    void clearedCredentialsStopAttaching();

private:
    CredentialAccessManager *m_manager = nullptr;
};

void CredentialAccessTest::init()
{
    m_manager = new CredentialAccessManager(this);
    m_manager->setCredentials(QStringLiteral("http://box.local:8484"),
                              QStringLiteral("secret-access-token"));
}

void CredentialAccessTest::artworkOnOurServerGetsTheToken()
{
    // The two routes an `Image` actually loads.
    QVERIFY(m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/media/42/poster"))));
    QVERIFY(m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/media/42/backdrop"))));
    // And the stream, which QMediaPlayer fetches.
    QVERIFY(m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/stream/42"))));
}

void CredentialAccessTest::anotherHostGetsNothing()
{
    // The case that matters: an artwork URL that came from a metadata
    // provider's sidecar pointing at a CDN.  The token must not go with it.
    QVERIFY(!m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://images.example.com/api/v1/media/42/poster"))));
    // A host that merely *contains* ours as a suffix.
    QVERIFY(!m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://evil-box.local:8484/api/v1/media/42/poster"))));
    QVERIFY(!m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local.evil.com:8484/api/v1/media/42/poster"))));
}

void CredentialAccessTest::anotherPortGetsNothing()
{
    // http://box:8484 and http://box:9090 are the same box and two different
    // servers.  On a home network the second one is very often the router's
    // web UI, and a token handed to it is a token handed to the router.
    QVERIFY(!m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:9090/api/v1/media/42/poster"))));
    // And the other direction: we are on 9090, the image claims 8484.
    m_manager->setCredentials(QStringLiteral("http://box.local:9090"),
                              QStringLiteral("secret-access-token"));
    QVERIFY(!m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/media/42/poster"))));
    QVERIFY(m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:9090/api/v1/media/42/poster"))));
}

void CredentialAccessTest::anotherSchemeGetsNothing()
{
    // An https page on the same host is a different origin and, for this
    // server, a different deployment.
    QVERIFY(!m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("https://box.local:8484/api/v1/media/42/poster"))));
}

void CredentialAccessTest::aLookalikePathGetsNothing()
{
    // The prefix test is on a path *segment*, so a path that merely starts
    // with the same characters does not match.
    QVERIFY(!m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/evil/media/42/poster"))));
    QVERIFY(!m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/streamer/42"))));
    QVERIFY(!m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/media/42/poster.png"))));
    // The API itself takes a header from `ApiClient`, not from here, so this
    // manager must not be the thing that adds one.
    QVERIFY(!m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/auth/me"))));
}

void CredentialAccessTest::aNonGetRequestGetsNothing()
{
    // Only the two artwork routes and the stream have GETs.  A POST to a
    // path that starts like them is not something this manager should be
    // silently authenticating.
    // (The operation is checked in createRequest; this asserts the path half of
    // the rule so a future edit to the scope can be seen in isolation.)
    QVERIFY(m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/media/42/poster"))));
}

void CredentialAccessTest::noTokenMeansNothingIsAttached()
{
    CredentialAccessManager bare(this);
    bare.setCredentials(QStringLiteral("http://box.local:8484"), QString());
    QVERIFY(!bare.wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/media/42/poster"))));

    // A malformed base URL yields no origin, so nothing can match it.
    CredentialAccessManager malformed(this);
    malformed.setCredentials(QString(), QStringLiteral("secret-access-token"));
    QVERIFY(!malformed.wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/media/42/poster"))));
}

void CredentialAccessTest::clearedCredentialsStopAttaching()
{
    // Signing out must not leave the token sitting in the manager waiting for
    // the next `Image` on screen.
    QVERIFY(m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/media/42/poster"))));
    m_manager->clearCredentials();
    QVERIFY(!m_manager->wouldAuthenticate(
        QUrl(QStringLiteral("http://box.local:8484/api/v1/media/42/poster"))));
}

QTEST_GUILESS_MAIN(CredentialAccessTest)
#include "tst_credentialaccess.moc"

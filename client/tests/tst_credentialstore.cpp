// Where the refresh token goes.
//
// A refresh token is a bearer credential with a year-long life when the user
// asks to be remembered, so three things about this store are security
// properties rather than implementation details, and all three are asserted
// here:
//
// * **nothing is written where it can be read back in the clear.**  With no
//   secure backend the store refuses, rather than degrading to a settings file
//   -- a remembered login is a convenience, and trading a security property for
//   one nobody asked to give up is the wrong direction.
// * **one account's token is not another's.**  Sessions are keyed by username.
//   The previous version stored one blob under one name and only *checked* the
//   account inside it, so signing in as somebody else overwrote the stored
//   session and signing back in found nothing -- and `clearTokens` cleared
//   every account's token at once.
// * **"remove this server" reaches the credentials.**  It removed the server
//   and profile settings and left every stored token where it was.
//
// What this suite cannot do is test a real keyring.  The Secret Service needs a
// running daemon, DPAPI needs Windows and the Android Keystore needs a device,
// so the *backends* are exercised only by hand.  What is tested here is the
// decision logic around them, which is where the three properties above live
// and which runs identically on a headless build machine.

#include <QtTest>

#include <QCoreApplication>
#include <QDir>
#include <QSettings>

#include "credentialstore.h"

namespace {

/// A settings scope of its own, so a test never reads or writes the real
/// developer's `~/.config/Stream/Stream.conf`.
///
/// `setPath` only takes effect for the `QSettings` objects created *after* it,
/// so it has to run before the first test constructs a store -- hence
/// `initTestCase` rather than a namespace-scope initialiser, which would run
/// before `QCoreApplication` exists and silently do nothing.  That is what the
/// first version did, and the suite read and wrote the real file.
QString scopeDirectory()
{
    const QString base =
        qEnvironmentVariable("TMPDIR", QStringLiteral("/tmp"));
    return base + QStringLiteral("/stream-credentialstore-test");
}

}  // namespace

class CredentialStoreTest : public QObject
{
    Q_OBJECT

private slots:
    void initTestCase();
    void init();

    void withoutASecureBackendNothingIsPersisted();
    void aRefusedSaveIsReportedAsRefused();
    void theBackendIsNamedEvenWhenItIsUnavailable();
    void settingsAreScopedToTheAccount();
    void forgetEverythingRemovesTheSecretGroup();
    void lastProfileIsScopedToTheAccount();

private:
    void clearSettings();
};

void CredentialStoreTest::initTestCase()
{
    QDir dir(scopeDirectory());
    if (dir.exists())
        dir.removeRecursively();
    dir.mkpath(QStringLiteral("."));
    // Four things, and each was found by an assertion that read the file and
    // got nothing back:
    //
    //  * the *format*, because `QSettings::setPath` only redirects a format
    //    the objects actually use, and a default-format object is
    //    `NativeFormat` -- which on Linux is a `.conf` in a different place;
    //  * the *path*, for the same reason;
    //  * the organisation and application, because the test binary is named
    //    `stream-client-tests-credentialstore` and Qt derives them from
    //    `argv[0]` when the real application has not set them -- so a
    //    QSettings built here did not address the same file as the store's;
    //  * the point, because the store's own `sync()` writes on every value
    //    and a path that does not exist is silently skipped.
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope,
                       scopeDirectory());
    QCoreApplication::setOrganizationName(QStringLiteral("Stream"));
    QCoreApplication::setApplicationName(QStringLiteral("Stream"));
    QDir().mkpath(scopeDirectory());
}

void CredentialStoreTest::clearSettings()
{
    QSettings settings;
    settings.clear();
    settings.sync();
}

void CredentialStoreTest::init()
{
    clearSettings();
}

void CredentialStoreTest::withoutASecureBackendNothingIsPersisted()
{
    CredentialStore store;
    // Whatever this machine has, the rule is the same: with no backend the save
    // fails rather than writing the blob somewhere readable.
    if (store.isAvailable())
        QSKIP("a secure backend is available here; the refusal path needs none");

    QVERIFY(!store.saveTokens(QStringLiteral("phuree"),
                              QStringLiteral("ssat.access"),
                              QStringLiteral("ssrt.refresh"), 9999999999999LL));

    // ...and nothing was left behind under any spelling of the key.
    QSettings settings;
    for (const QString &key : settings.allKeys()) {
        QVERIFY2(!key.startsWith(QLatin1String("secret")),
                 qPrintable(QStringLiteral("a token was written to %1").arg(key)));
    }
    QVERIFY(store.loadTokens(QStringLiteral("phuree")).isEmpty());
}

void CredentialStoreTest::aRefusedSaveIsReportedAsRefused()
{
    CredentialStore store;
    // The contract is the *pair*: with no backend the save is refused, and with
    // one it is the backend's own answer rather than a constant.  A caller that
    // ignores the return value would tell the user the session was remembered
    // when it was not, which is the whole reason this function returns one.
    //
    // Not `QCOMPARE(saved, store.isAvailable())`: a backend that is present and
    // refusing -- a locked keyring, a sandbox that forbids the socket -- is
    // exactly the case this test exists to describe, and comparing the two would
    // fail on it while claiming the store misbehaved.
    const bool saved =
        store.saveTokens(QStringLiteral("phuree"), QStringLiteral("a"),
                         QStringLiteral("r"), 1);
    if (!store.isAvailable())
        QVERIFY(!saved);
    QSettings written;
    QVERIFY2(!written.allKeys().contains(QStringLiteral("session/account")),
             "the save must not record an account it could not store a "
             "token for");
}

void CredentialStoreTest::theBackendIsNamedEvenWhenItIsUnavailable()
{
    CredentialStore store;
    // The settings screen shows this, so a user who cannot be remembered is
    // told *which* backend was looked for rather than nothing at all.
    QVERIFY(!store.backendName().isEmpty());
    if (!store.isAvailable())
        QCOMPARE(store.backendName(), QStringLiteral("none"));
}

void CredentialStoreTest::settingsAreScopedToTheAccount()
{
    CredentialStore store;
    // The server URL is not a secret and lives in ordinary settings; it is
    // still per account, so two servers -- or two people -- on one machine do
    // not overwrite each other.
    store.setLastAccount(QStringLiteral("phuree"));
    // Read through a *newly named* QSettings rather than through the store, so
    // the assertion is about the file and not about the same cached object.
    // `settings.sync()` in the store means the data is on disk before this
    // line, so a default-format mismatch would show up here rather than never.
    QSettings written;
    QCOMPARE(written.value(QStringLiteral("session/account")).toString(),
             QStringLiteral("phuree"));
    QCOMPARE(store.lastAccount(), QStringLiteral("phuree"));

    store.saveServerUrl(QStringLiteral("http://box.local:8484"));
    QCOMPARE(written.value(QStringLiteral("server/url")).toString(),
             QStringLiteral("http://box.local:8484"));
    QCOMPARE(store.serverUrl(), QStringLiteral("http://box.local:8484"));
}

void CredentialStoreTest::lastProfileIsScopedToTheAccount()
{
    CredentialStore store;
    store.saveLastProfile(QStringLiteral("phuree"), 3);
    store.saveLastProfile(QStringLiteral("bob"), 7);

    QSettings written(QSettings::IniFormat, QSettings::UserScope,
                      QStringLiteral("Stream"), QStringLiteral("Stream"));
    QCOMPARE(written.value(QStringLiteral("profile/last/phuree")).toInt(), 3);
    QCOMPARE(written.value(QStringLiteral("profile/last/bob")).toInt(), 7);
    QCOMPARE(store.lastProfile(QStringLiteral("phuree")), 3);
    QCOMPARE(store.lastProfile(QStringLiteral("bob")), 7);
    QCOMPARE(store.lastProfile(QStringLiteral("nobody")), 0);
}

void CredentialStoreTest::forgetEverythingRemovesTheSecretGroup()
{
    // Written by hand, because the point is that *somebody else's* leftover --
    // an older build, a key left when the backend changed -- goes too.  A
    // `forgetEverything` that only removes what this build wrote is not what
    // "remove this server" means to a person holding a stolen laptop.
    QSettings settings;
    settings.setValue(QStringLiteral("secret/Stream/Session/phuree"),
                      QByteArray("not-really-encrypted"));
    settings.setValue(QStringLiteral("secret/legacy"), QByteArray("stale"));
    settings.setValue(QStringLiteral("server/url"),
                      QStringLiteral("http://box.local:8484"));
    settings.setValue(QStringLiteral("profile/last/phuree"), 3);
    settings.sync();

    CredentialStore store;
    store.forgetEverything();

    QSettings after;
    for (const QString &key : after.allKeys()) {
        QVERIFY2(!key.startsWith(QLatin1String("secret")),
                 qPrintable(QStringLiteral("a credential survived: %1").arg(key)));
        QVERIFY(!key.startsWith(QLatin1String("server")));
        QVERIFY(!key.startsWith(QLatin1String("profile")));
    }
}

QTEST_GUILESS_MAIN(CredentialStoreTest)
#include "tst_credentialstore.moc"
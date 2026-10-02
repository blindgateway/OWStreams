#include "credentialstore.h"

#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QStandardPaths>
#include <QSettings>

#ifdef Q_OS_WIN
#  include <windows.h>
#  include <dpapi.h>
#endif

namespace {

/// Keychain backends.  Qt 6 ships no credential-storage API of its own (that is
/// QtKeychain, a third-party library), so each platform gets a small adapter
/// here rather than a new dependency.
enum class Backend { None, SecretService, WindowsDpapi, AndroidKeystore };

QString backendLabel(Backend backend)
{
    switch (backend) {
    case Backend::SecretService:   return QStringLiteral("GNOME Keyring / Secret Service");
    case Backend::WindowsDpapi:    return QStringLiteral("Windows DPAPI");
    case Backend::AndroidKeystore: return QStringLiteral("Android Keystore");
    case Backend::None:            break;
    }
    return QStringLiteral("none");
}

Backend detectBackend()
{
#ifdef Q_OS_WIN
    return Backend::WindowsDpapi;
#elif defined(Q_OS_ANDROID)
    return Backend::AndroidKeystore;
#else
    // libsecret has no Qt binding here, but every desktop that *has* a keyring
    // also ships its CLI, and `secret-tool` is the supported way to reach it.
    // Probing costs one process start at launch and buys a remembered login
    // that is actually encrypted at rest.
    if (!QStandardPaths::findExecutable(QStringLiteral("secret-tool")).isEmpty())
        return Backend::SecretService;
    return Backend::None;
#endif
}

/// Whether the keyring will actually answer *right now*.
///
/// Separate from `detectBackend` on purpose.  "The tool is installed" is a
/// property of the machine; "there is a keyring daemon, and this session can
/// reach it" is not.  Keyring the first matters on a locked desktop: nothing
/// can be stored or fetched there, and reporting "unavailable" is the honest
/// answer -- a remembered login is a convenience, and telling the user it was
/// remembered when it cannot be is worse than not remembering it.
///
/// `lookup` is used rather than `store` because it cannot *create* anything:
/// a probe that writes a secret to prove the keyring works is a probe that
/// leaves a secret behind, and on a machine where the answer is "no" it would
/// leave the wrong one.  `secret-tool` distinguishes the three cases in the
/// exit code -- 0 for an item, 1 for no item, and a failure with its own
/// message and code for no daemon -- so one lookup answers the question.
bool secretServiceResponds()
{
    QProcess probe;
    probe.start(QStringLiteral("secret-tool"),
                {QStringLiteral("lookup"), QStringLiteral("application"),
                 QStringLiteral("stream-keyring-probe")});
    if (!probe.waitForFinished(3000))
        return false;
    if (probe.exitStatus() != QProcess::NormalExit)
        return false;               // hung, or could not be run at all
    // 0 is an item (there is one, which is odd but not a failure to store);
    // 1 is "no such item", which is exactly what an empty keyring says.  Both
    // mean the service answered.
    return probe.exitCode() == 0 || probe.exitCode() == 1;
}

}  // namespace

CredentialStore::CredentialStore(QObject *parent)
    : QObject(parent), m_available(false)
{
    const Backend backend = detectBackend();
    m_backend = backendLabel(backend);
    m_available = backend != Backend::None
        && (backend != Backend::SecretService || secretServiceResponds());

#ifdef Q_OS_WIN
    m_secretKey = QStringLiteral("Stream/Session");
#else
    m_secretKey = QStringLiteral("stream-client-session");
#endif
}

// ---------------------------------------------------------------------------
// Non-secret settings
// ---------------------------------------------------------------------------
QVariant CredentialStore::settingsValue(const QString &key,
                                        const QVariant &fallback) const
{
    QSettings settings;
    return settings.value(key, fallback);
}

void CredentialStore::settingsSet(const QString &key, const QVariant &value)
{
    QSettings settings;
    settings.setValue(key, value);
    settings.sync();
}

QString CredentialStore::lastAccount() const
{
    return settingsValue(QStringLiteral("session/account")).toString();
}

void CredentialStore::setLastAccount(const QString &username)
{
    settingsSet(QStringLiteral("session/account"), username);
}

void CredentialStore::saveServerUrl(const QString &url)
{
    settingsSet(QStringLiteral("server/url"), url);
}

QString CredentialStore::serverUrl() const
{
    return settingsValue(QStringLiteral("server/url")).toString();
}

void CredentialStore::saveLastProfile(const QString &accountKey, int profileId)
{
    settingsSet(QStringLiteral("profile/last/") + accountKey, profileId);
}

int CredentialStore::lastProfile(const QString &accountKey) const
{
    return settingsValue(QStringLiteral("profile/last/") + accountKey).toInt();
}

void CredentialStore::forgetEverything()
{
    // "Forget every server setting and every credential", so it has to reach the
    // credentials: the `secret/` group is where the Windows backend puts its
    // DPAPI blob, and the previous version removed `server/`, `profile/` and a
    // single settings key while leaving every stored token where it was.  A
    // user who removes a server expects the credential for it to be gone, and
    // "remove this server" that keeps the refresh token has removed nothing
    // that matters.
    QSettings settings;
    settings.beginGroup(QStringLiteral("server"));
    settings.remove(QString());
    settings.endGroup();
    settings.beginGroup(QStringLiteral("profile"));
    settings.remove(QString());
    settings.endGroup();
    settings.beginGroup(QStringLiteral("secret"));
    settings.remove(QString());
    settings.endGroup();
    settings.remove(QStringLiteral("session/account"));
    settings.sync();
}

// ---------------------------------------------------------------------------
// Secrets
// ---------------------------------------------------------------------------
bool CredentialStore::saveTokens(const QString &accountKey, const QString &accessToken,
                                 const QString &refreshToken, qint64 expiresAtMs)
{
    if (!m_available)
        return false;   // refuse rather than downgrade to a file

    const QJsonObject payload{
        {QStringLiteral("access"), accessToken},
        {QStringLiteral("refresh"), refreshToken},
        {QStringLiteral("expires"), QString::number(expiresAtMs)},
        {QStringLiteral("account"), accountKey},
    };
    const QByteArray blob =
        QJsonDocument(payload).toJson(QJsonDocument::Compact);

#ifdef Q_OS_ANDROID
    // Keystore-backed: the AES key never leaves the TEE, so the blob on disk is
    // useless without this device.  Wired through the platform bridge in
    // android/KeystoreBridge.cpp; unimplemented builds report unavailable
    // rather than falling back.
    extern bool androidStoreEncrypted(const QString &key, const QByteArray &blob);
    extern QByteArray androidLoadEncrypted(const QString &key);
    if (!androidStoreEncrypted(m_secretKey + QLatin1Char('/') + accountKey, blob))
        return false;
    emit tokensChanged(accountKey);
    return true;
#else
    switch (detectBackend()) {
#ifdef Q_OS_WIN
    case Backend::WindowsDpapi: {
        DATA_BLOB in{static_cast<DWORD>(blob.size()),
                     reinterpret_cast<BYTE *>(const_cast<char *>(blob.constData()))};
        DATA_BLOB out{};
        if (!CryptProtectData(&in, L"Stream session", nullptr, nullptr, nullptr,
                              CRYPTPROTECT_UI_FORBIDDEN, &out)) {
            return false;
        }
        const QByteArray encrypted(reinterpret_cast<char *>(out.pbData),
                                   static_cast<int>(out.cbData));
        SecureZeroMemory(out.pbData, out.cbData);
        settingsSet(QStringLiteral("secret/") + m_secretKey + QLatin1Char('/')
                        + accountKey,
                    encrypted);
        emit tokensChanged(accountKey);
        return true;
    }
#endif
    case Backend::SecretService: {
        QProcess tool;
        tool.start(QStringLiteral("secret-tool"),
                   {QStringLiteral("store"), QStringLiteral("--label=Stream session"),
                    QStringLiteral("application"), m_secretKey,
                    QLatin1Char('/') + accountKey});
        tool.waitForStarted(2000);
        tool.write(blob);
        tool.closeWriteChannel();
        if (!tool.waitForFinished(3000) || tool.exitCode() != 0)
            return false;
        emit tokensChanged(accountKey);
        return true;
    }
    default:
        return false;
    }
#endif
}

QVariantMap CredentialStore::loadTokens(const QString &accountKey) const
{
    QVariantMap result;
    if (!m_available)
        return result;

    QByteArray blob;
#ifdef Q_OS_ANDROID
    extern QByteArray androidLoadEncrypted(const QString &key);
    blob = androidLoadEncrypted(m_secretKey + QLatin1Char('/') + accountKey);
#else
    switch (detectBackend()) {
#ifdef Q_OS_WIN
    case Backend::WindowsDpapi: {
        const QByteArray encrypted = settingsValue(
            QStringLiteral("secret/") + m_secretKey + QLatin1Char('/') + accountKey)
                                         .toByteArray();
        if (encrypted.isEmpty())
            return result;
        DATA_BLOB in{static_cast<DWORD>(encrypted.size()),
                     reinterpret_cast<BYTE *>(const_cast<char *>(encrypted.constData()))};
        DATA_BLOB out{};
        if (!CryptUnprotectData(&in, nullptr, nullptr, nullptr, nullptr,
                                CRYPTPROTECT_UI_FORBIDDEN, &out)) {
            return result;
        }
        blob = QByteArray(reinterpret_cast<char *>(out.pbData),
                          static_cast<int>(out.cbData));
        SecureZeroMemory(out.pbData, out.cbData);
        break;
    }
#endif
    case Backend::SecretService: {
        QProcess tool;
        tool.start(QStringLiteral("secret-tool"),
                   {QStringLiteral("lookup"), QStringLiteral("application"),
                    m_secretKey + QLatin1Char('/') + accountKey});
        if (!tool.waitForFinished(3000) || tool.exitCode() != 0)
            return result;
        blob = tool.readAllStandardOutput().trimmed();
        break;
    }
    default:
        return result;
    }
#endif
    if (blob.isEmpty())
        return result;

    const QJsonObject payload = QJsonDocument::fromJson(blob).object();
    // A stored session for a *different* account is not this account's to
    // resume: adopting it would silently sign the user in as somebody else.
    if (payload.value(QStringLiteral("account")).toString() != accountKey)
        return result;

    result.insert(QStringLiteral("access"), payload.value(QStringLiteral("access")));
    result.insert(QStringLiteral("refresh"), payload.value(QStringLiteral("refresh")));
    result.insert(QStringLiteral("expires"),
                  payload.value(QStringLiteral("expires")).toString().toLongLong());
    return result;
}

void CredentialStore::clearTokens(const QString &accountKey)
{
    if (!m_available)
        return;
#ifdef Q_OS_ANDROID
    extern void androidDeleteEncrypted(const QString &key);
    androidDeleteEncrypted(m_secretKey + QLatin1Char('/') + accountKey);
#else
    switch (detectBackend()) {
#ifdef Q_OS_WIN
    case Backend::WindowsDpapi:
        settingsSet(QStringLiteral("secret/") + m_secretKey + QLatin1Char('/')
                        + accountKey,
                    QByteArray());
        break;
#endif
    case Backend::SecretService: {
        QProcess tool;
        tool.start(QStringLiteral("secret-tool"),
                   {QStringLiteral("clear"), QStringLiteral("application"),
                    m_secretKey + QLatin1Char('/') + accountKey});
        tool.waitForFinished(3000);
        break;
    }
    default:
        break;
    }
#endif
    emit tokensChanged(accountKey);
}
#pragma once

#include <QObject>
#include <QString>
#include <QVariantMap>

/// Where the refresh token and the remembered server live.
///
/// The split is the important part.  A **server URL** is not a secret: it goes
/// in ordinary settings so the app can offer the last server on next launch.  A
/// **refresh token** is a bearer credential with a year-long life when the user
/// asks to be remembered, and it goes in the platform keychain -- the Android
/// Keystore-backed EncryptedSharedPreferences, the Windows Credential Manager,
/// or the Secret Service on Linux.
///
/// Platform note, stated rather than pretended around: the Linux Secret Service
/// depends on a running keyring daemon, which on a headless box or a minimal
/// desktop may not exist.  When it is unavailable this falls back to refusing to
/// persist anything rather than writing a credential to a file.  A remembered
/// login is a convenience; silently downgrading it to a plaintext file would
/// trade a security property for one the user did not ask to give up.
class CredentialStore : public QObject
{
    Q_OBJECT
    Q_PROPERTY(bool available READ isAvailable CONSTANT)
    Q_PROPERTY(QString backendName READ backendName CONSTANT)

public:
    explicit CredentialStore(QObject *parent = nullptr);

    /// Whether a secure backend was found.  When false, `save` does nothing and
    /// returns false rather than writing the credential somewhere weaker.
    bool isAvailable() const { return m_available; }
    QString backendName() const { return m_backend; }

    /// The account the stored session belongs to.  Sessions are keyed by
    /// username so signing in as somebody else cannot inherit a remembered
    /// login, and so a stale entry can be found and dropped.
    QString lastAccount() const;
    void setLastAccount(const QString &username);

    /// Remember the server URL.  Not a secret; stored in ordinary settings.
    void saveServerUrl(const QString &url);
    QString serverUrl() const;

    /// Remember the selected profile per account, so the picker opens on the
    /// one last used.  Not a secret.
    void saveLastProfile(const QString &accountKey, int profileId);
    int lastProfile(const QString &accountKey) const;

    /// Persist a token pair.  Returns false when no secure backend exists.
    bool saveTokens(const QString &accountKey, const QString &accessToken,
                    const QString &refreshToken, qint64 expiresAtMs);
    QVariantMap loadTokens(const QString &accountKey) const;
    void clearTokens(const QString &accountKey);

    /// Forget every server setting and every credential.  Used by "remove this
    /// server", not by logout (logout clears only the one account's tokens).
    void forgetEverything();

signals:
    void tokensChanged(const QString &accountKey);

private:
    QVariant settingsValue(const QString &key, const QVariant &fallback = {}) const;
    void settingsSet(const QString &key, const QVariant &value);

    bool m_available;
    QString m_backend;
    QString m_secretKey = QStringLiteral("stream.session");
};
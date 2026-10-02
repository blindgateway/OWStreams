#include "sessioncontroller.h"

#include <QDateTime>
#include <QTimer>

#include "apiclient.h"
#include "credentialstore.h"

SessionController::SessionController(QObject *parent)
    : QObject(parent),
      m_api(new ApiClient(this)),
      m_credentials(new CredentialStore(this))
{
    // Persist on every new token pair, not just on login: a silent refresh
    // would otherwise be lost on exit and a "remember me" session would last
    // fifteen minutes rather than the year the user asked for.
    connect(m_api, &ApiClient::tokensIssued, this,
            [this](const QString &access, const QString &refresh, int expiresIn) {
                persistSession(access, refresh, expiresIn);
            });

    connect(m_api, &ApiClient::authenticated, this, [this] {
        if (m_api->mustChangePassword()) {
            setState(ChangePassword);
            return;
        }
        // /auth/me rather than trusting the login response: it is the one
        // request that returns the profile list and the server's own view of
        // the account, so the rest of the app has a single source of truth.
        m_api->loadMe();
    });

    connect(m_api, &ApiClient::sessionLoaded, this,
            [this](const QString &, const QString &, const QJsonArray &profiles,
                   const QJsonObject &) {
                if (profiles.isEmpty()) {
                    // An account with no profile cannot browse anything; the
                    // profile screen creates one rather than dead-ending.
                    setState(Profiles);
                    return;
                }
                const int remembered = m_credentials->lastProfile(m_api->username());
                if (remembered > 0) {
                    m_api->setProfileId(remembered);
                    setState(Home);
                    emit profilesChanged();
                    return;
                }
                setState(Profiles);
                emit profilesChanged();
            });

    connect(m_api, &ApiClient::profileSelected, this, [this](int profileId) {
        m_credentials->saveLastProfile(m_api->username(), profileId);
        setState(Home);
        emit profilesChanged();
    });

    connect(m_api, &ApiClient::profileUnlocked, this, [this](int profileId) {
        m_api->setProfileId(profileId);
        m_credentials->saveLastProfile(m_api->username(), profileId);
        setState(Home);
    });

    connect(m_api, &ApiClient::loggedOut, this, [this] { setState(Login); });

    connect(m_api, &ApiClient::failed, this,
            [this](const QString &operation, const ApiError &error) {
                if (operation == QLatin1String("auth.me")
                    && error.kind == ApiErrorKind::Unauthorized) {
                    // The remembered token is no longer good.  Drop it rather
                    // than retrying: this is the restore path, and a refresh
                    // has already been attempted by the client.
                    m_credentials->clearTokens(m_api->username());
                    setState(Login);
                    return;
                }
                if (operation == QLatin1String("server.check")) {
                    m_lastError = error.message;
                    setState(m_api->baseUrl().isEmpty() ? NoServer : Login);
                    emit serverReachable(false, QString(), false);
                    emit stateChanged();
                    return;
                }
                m_lastError = error.message;
                emit stateChanged();
            });

    connect(m_api, &ApiClient::serverChecked, this,
            [this](bool reachable, const QString &version, const QString &,
                   bool requiresSetup) {
                emit serverReachable(reachable, version, requiresSetup);
                if (!reachable) {
                    m_lastError = QStringLiteral("Could not reach that server.");
                    setState(Login);
                    return;
                }
                m_lastError.clear();
                if (requiresSetup) {
                    // A server with no accounts yet.  The setup screen is part
                    // of the connection flow, not a separate mode.
                    setState(ChangePassword);
                    return;
                }
                setState(Login);
            });
}

QString SessionController::serverUrl() const { return m_api->baseUrl(); }
QString SessionController::username() const { return m_api->username(); }
int SessionController::profileId() const { return m_api->profileId(); }
bool SessionController::hasSecureStorage() const { return m_credentials->isAvailable(); }

void SessionController::setState(State state)
{
    if (m_state == state)
        return;
    m_state = state;
    emit stateChanged();
}

void SessionController::connectTo(const QString &url)
{
    m_lastError.clear();
    m_api->setBaseUrl(url);
    if (m_api->baseUrl().isEmpty()) {
        m_lastError = QStringLiteral("Enter a server address.");
        emit stateChanged();
        return;
    }
    setState(Connecting);
    m_api->checkServer();
}

void SessionController::signIn(const QString &username, const QString &password,
                               bool remember)
{
    m_lastError.clear();
    m_remembered = remember;
    m_credentials->setLastAccount(username.trimmed());
    m_api->login(username, password, QStringLiteral("Stream client"), remember);
}

void SessionController::signOut()
{
    // Local state is cleared regardless of what the server says: a logout that
    // failed because the network dropped must still end the session on this
    // device, or the next person to pick up the remote sees a signed-in app.
    m_api->logout();
    m_credentials->clearTokens(m_api->username());
    m_api->clearSession();
    setState(Login);
    emit profilesChanged();
}

void SessionController::submitPasswordChange(const QString &current, const QString &next)
{
    m_lastError.clear();
    m_api->changePassword(current, next);
}

void SessionController::selectProfile(int profileId)
{
    m_api->setProfileId(profileId);
    m_api->selectProfile(profileId);
}

void SessionController::unlockProfile(int profileId, const QString &pin)
{
    m_api->unlockProfile(profileId, pin);
}

void SessionController::showProfiles()
{
    // Forget the profile selection so the picker opens on a decision rather
    // than on the profile that was already in use.
    m_api->setProfileId(0);
    setState(Profiles);
    emit profilesChanged();
}

void SessionController::persistSession(const QString &access, const QString &refresh,
                                       int expiresInSeconds)
{
    if (!m_remembered || !m_credentials->isAvailable())
        return;
    const qint64 expiresAt =
        QDateTime::currentMSecsSinceEpoch() + qint64(expiresInSeconds) * 1000;
    m_credentials->saveTokens(m_api->username(), access, refresh, expiresAt);
    m_credentials->saveServerUrl(m_api->baseUrl());
}

void SessionController::restore()
{
    const QString url = m_credentials->serverUrl();
    if (url.isEmpty()) {
        setState(NoServer);
        return;
    }
    m_api->setBaseUrl(url);

    const QString accountKey = m_credentials->lastAccount();
    const QVariantMap tokens = m_credentials->loadTokens(accountKey);
    if (tokens.isEmpty()) {
        m_api->checkServer();
        return;
    }
    m_remembered = true;
    m_api->setSession(tokens.value(QStringLiteral("access")).toString(),
                      tokens.value(QStringLiteral("refresh")).toString(), 0);
    // /auth/me is the point at which a stale token is discovered.
    m_api->loadMe();
}
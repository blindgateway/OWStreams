#pragma once

#include <QObject>
#include <QJsonArray>
#include <QJsonObject>
#include <QStringList>
#include <QVariantList>
#include <QVariantMap>

// The inline accessors below read ApiClient, so it has to be a complete type
// here -- a forward declaration would make every one of them a compile error
// rather than an inconvenience.  It is a small header; there is no cycle.
#include "apiclient.h"

class CredentialStore;

/// The app's top-level state machine: which of the five states are we in, and
/// what is the app allowed to do in each.
///
/// ```
///  NoServer ──configure──► Checking ──ok──► Login ──authenticated──► Profiles
///     ▲                       │              ▲                        │
///     └────────fail───────────┘              └────must change────────┤
///                                                                    ▼
///  Home ◄── profile selected ──► Profiles
/// ```
///
/// Keeping this in C++ rather than in a stack of QML states is what makes the
/// transitions testable: `SessionController` has no Qt Quick dependency, so a
/// test can drive it with a fake transport and assert the machine never lands
/// somewhere the design does not allow.
class SessionController : public QObject
{
    Q_OBJECT
    Q_PROPERTY(State state READ state NOTIFY stateChanged)
    Q_PROPERTY(QString serverUrl READ serverUrl NOTIFY stateChanged)
    Q_PROPERTY(QString username READ username NOTIFY stateChanged)
    Q_PROPERTY(int profileId READ profileId NOTIFY stateChanged)
    Q_PROPERTY(bool hasSecureStorage READ hasSecureStorage CONSTANT)
    Q_PROPERTY(QString lastError READ lastError NOTIFY stateChanged)
    Q_PROPERTY(QVariantMap user READ user NOTIFY userChanged)
    Q_PROPERTY(QVariantList profiles READ profiles NOTIFY profilesChanged)
    Q_PROPERTY(QVariantMap serverInfo READ serverInfo NOTIFY serverInfoChanged)
    Q_PROPERTY(QVariantList sessions READ sessions NOTIFY sessionsChanged)
    Q_PROPERTY(QString credentialBackend READ credentialBackend CONSTANT)
    Q_PROPERTY(QString serverVersion READ serverVersion NOTIFY serverInfoChanged)

public:
    enum State {
        NoServer,     ///< nothing configured; the connection screen
        Connecting,   ///< probing a URL the user just gave us
        Login,        ///< a server answered; sign in
        ChangePassword,///< signed in, but on a password that must change
        Profiles,     ///< choose or unlock a profile
        Home,         ///< signed in and a profile is selected
    };
    Q_ENUM(State)

    /// Read by QML.  These four accessors are the whole observable state of the
    /// session machine; QML never reaches into ApiClient for them.
    State state() const { return m_state; }
    QString serverUrl() const;
    QString username() const;
    int profileId() const;

    explicit SessionController(QObject *parent = nullptr);

    // The account, its profiles and the server's own summary, cached from the
    // last /auth/me so the settings screen is not a round trip.  The profile
    // picker reads these too, which is why they are exposed here rather than
    // only on ApiClient.
    QVariantMap user() const { return m_api->user(); }
    QVariantList profiles() const { return m_api->profiles(); }
    QVariantMap serverInfo() const { return m_api->serverInfo(); }
    QVariantList sessions() const { return m_api->sessions(); }
    QString credentialBackend() const;
    QString serverVersion() const { return m_serverVersion; }

    ApiClient *api() const { return m_api; }
    CredentialStore *credentials() const { return m_credentials; }
    bool hasSecureStorage() const;
    QString lastError() const { return m_lastError; }

    /// Point the app at a server and find out whether it answers.
    void connectTo(const QString &url);
    /// Sign in.  `remember` persists the token pair in the platform keychain.
    void signIn(const QString &username, const QString &password, bool remember);
    void signOut();
    void submitPasswordChange(const QString &current, const QString &next);

    void selectProfile(int profileId);
    void unlockProfile(int profileId, const QString &pin);

    /// Back to the profile picker, keeping the session.  "Switch profile" is a
    /// navigation, not a sign-out: signing out would throw away a remembered
    /// login because somebody picked a different profile.
    void showProfiles();

    /// Called once at startup: restore the remembered server and session.
    void restore();

    /// Profile management, shared with the settings screen.  These are on the
    /// session controller rather than the app controller because a profile
    /// belongs to the *account*, and it has to be creatable from the profile
    /// picker too -- which is not part of the signed-in shell.
    Q_INVOKABLE void createProfile(const QString &name, const QString &pin = {});
    Q_INVOKABLE void renameProfile(int profileId, const QString &name);
    Q_INVOKABLE void deleteProfile(int profileId);
    Q_INVOKABLE void setProfilePin(int profileId, const QString &pin,
                                   const QString &currentPin = {});
    Q_INVOKABLE void changeProfilePin(int profileId, const QString &currentPin,
                                      const QString &newPin);
    void loadSessions();

    /// The client build, sent with the login so the server's session list can
    /// say *which* device is which.  The user agent alone would say "Stream".
    Q_INVOKABLE void setDeviceName(const QString &name);

signals:
    void stateChanged();
    void profilesChanged();
    void userChanged();
    void sessionsChanged();
    /// The server's own summary changed: version, capabilities, library roots.
    /// One signal for both `serverInfo` and `serverVersion` because they are
    /// the same fact -- the version is a field of the summary.
    void serverInfoChanged();
    /// `requiresSetup` is what /auth/setup says, and it is the only thing
    /// that distinguishes "sign in" from "this server has no accounts".
    void serverReachable(bool reachable, const QString &version,
                         bool requiresSetup);

private:
    void setState(State state);
    void persistSession(const QString &access, const QString &refresh,
                         int expiresInSeconds);

    ApiClient *m_api;
    CredentialStore *m_credentials;
    State m_state = NoServer;
    QString m_lastError;
    bool m_remembered = false;
    QString m_serverVersion;
    QString m_deviceName = QStringLiteral("Stream client");
};
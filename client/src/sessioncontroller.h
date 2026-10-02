#pragma once

#include <QObject>
#include <QJsonArray>
#include <QJsonObject>
#include <QStringList>
#include <QStringList>

class ApiClient;
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

signals:
    void stateChanged();
    void profilesChanged();
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
};
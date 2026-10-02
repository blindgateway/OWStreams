#pragma once

#include "credentialaccessmanager.h"

#include <QByteArray>
#include <QJsonArray>
#include <QJsonObject>
#include <QObject>
#include <QString>
#include <QUrl>
#include <QUrlQuery>
#include <QVariantList>
#include <QVariantMap>

#include "apierror.h"

class QNetworkAccessManager;
class QNetworkReply;
class QTimer;

/// The HTTP client for the v1 API.
///
/// Three things it does that a bare QNetworkAccessManager does not:
///
/// * **Token refresh.**  An access token is valid for fifteen minutes.  When a
///   request comes back 401 and a refresh token is held, it refreshes once,
///   transparently, and replays the original request.  A second 401 is a real
///   logout, not a race.
/// * **One error taxonomy.**  Every failure leaves as an `ApiError` with a
///   kind, never as a raw string for QML to parse.
/// * **Credential scoping.**  Poster images and the stream itself need a
///   bearer token, and `Image`/`VideoOutput` cannot be given one.  A custom
///   access manager attaches the current token to *only* the routes that need
///   it -- which is what lets a poster load without a credential ever becoming
///   a query parameter.
///
/// It is deliberately *not* thread-safe.  It lives on the GUI thread and every
/// call returns through a signal, which is what QML wants anyway.
class ApiClient : public QObject
{
    Q_OBJECT
    Q_PROPERTY(QString baseUrl READ baseUrl NOTIFY baseUrlChanged)
    Q_PROPERTY(bool hasSession READ hasSession NOTIFY sessionChanged)
    Q_PROPERTY(QString username READ username NOTIFY userChanged)
    Q_PROPERTY(bool mustChangePassword READ mustChangePassword NOTIFY mustChangePasswordChanged)
    Q_PROPERTY(int profileId READ profileId NOTIFY profileChanged)

    // The account, its profiles and the server's own summary, kept as plain
    // maps rather than as one opaque blob: the settings screen needs each of
    // them, and re-requesting /auth/me every time it is opened would spend API
    // budget on information that has not changed since sign-in.
    Q_PROPERTY(QVariantMap user READ user NOTIFY userChanged)
    Q_PROPERTY(QVariantList profiles READ profiles NOTIFY profilesChanged)
    Q_PROPERTY(QVariantMap serverInfo READ serverInfo NOTIFY serverInfoChanged)
    Q_PROPERTY(QVariantMap categories READ categories NOTIFY categoriesChanged)
    Q_PROPERTY(QVariantList sessions READ sessions NOTIFY sessionsChanged)

public:
    /// How long a single request may take before it is treated as a timeout.
    /// Long enough for a 4K remux to answer /health on a spinning disk,
    /// short enough that a black-holed connection is not an infinite spinner.
    static constexpr int kDefaultTimeoutMs = 15000;

    explicit ApiClient(QObject *parent = nullptr);
    ~ApiClient() override;

    QString baseUrl() const { return m_baseUrl; }
    void setBaseUrl(const QString &url);

    bool hasSession() const { return !m_accessToken.isEmpty(); }
    QString username() const { return m_username; }
    bool mustChangePassword() const { return m_mustChangePassword; }
    int profileId() const { return m_profileId; }
    void setProfileId(int id);

    QVariantMap user() const { return m_user; }
    QVariantList profiles() const { return m_profiles; }
    QVariantMap serverInfo() const { return m_serverInfo; }
    QVariantMap categories() const { return m_categories; }
    QVariantList sessions() const { return m_sessions; }

    /// Adopt a token pair obtained elsewhere (a restored session, or the
    /// answer to a refresh the caller made itself).
    void setSession(const QString &accessToken, const QString &refreshToken,
                    int expiresInSeconds);
    void clearSession();

    /// Check that the configured URL answers and what it is.
    /// `GET /api/health` -- deliberately outside the version prefix, so a
    /// v1 client can still tell "wrong URL" from "wrong API version".
    void checkServer();

    // -- authentication ------------------------------------------------
    void login(const QString &username, const QString &password,
               const QString &deviceName, bool remember);
    void logout();
    void logoutEverywhere();
    void changePassword(const QString &current, const QString &next);
    void loadSessions();
    void revokeSession(const QString &sessionId);

    // -- library -------------------------------------------------------
    void loadHome(int limit);
    /// `query` carries limit, offset, sort and whatever filters the caller
    /// wants.  Taking a `QUrlQuery` rather than three more positional
    /// arguments means a new filter is one line here instead of a new
    /// parameter in a signature that is already six long.
    void loadMediaPage(const QUrlQuery &query);
    void loadMedia(int mediaId);
    void search(const QString &query, int limit);
    void loadCategories();
    void loadFavorites();
    void loadHistory();
    void clearHistory();
    void removeHistoryEntry(int mediaId);
    void setFavorite(int mediaId, bool favorite);
    void rateMedia(int mediaId, double score);

    // -- playback ------------------------------------------------------
    void startPlayback(int mediaId, double startOffset);
    void reportProgress(int mediaId, double positionSeconds,
                        double durationSeconds);
    void finishPlayback(int mediaId, double positionSeconds,
                        const QString &sessionId = {});

    /// The id of the playback session the server opened for the current title,
    /// so the UI can close exactly the session it started.
    QString playbackSessionId() const { return m_playbackSessionId; }

    // -- profile -------------------------------------------------------
    /// Ask for the current session's identity and profiles: `GET /auth/me`.
    /// This is the call a restored session makes first, because an access token
    /// that is still inside its TTL may still have been revoked server-side --
    /// only /auth/me can tell.
    void loadMe();
    void loadProfiles();
    void selectProfile(int profileId);
    void unlockProfile(int profileId, const QString &pin);
    void createProfile(const QString &name, const QString &pin);
    void renameProfile(int profileId, const QString &name);
    void deleteProfile(int profileId);
    /// An empty `pin` clears the PIN, which a locked profile will not accept
    /// without the current one.
    void setProfilePin(int profileId, const QString &pin,
                       const QString &currentPin = {});
    void changeProfilePin(int profileId, const QString &currentPin,
                          const QString &newPin);

    /// Absolute URL for the stream endpoint of a title, given a playback token.
    /// Empty when there is no playback token for that title.
    QUrl streamUrl(int mediaId) const;

    /// `"Bearer <access token>"`, for a caller that has to send the credential
    /// itself -- a raw HTTP check in the end-to-end suite, or a download.
    ///
    /// Deliberately the whole header value rather than the bare token: every
    /// caller of this wants the same string, and the one place that decides
    /// what a bearer credential looks like should be the client that issues
    /// them.  Empty when there is no session.
    QString authorizationHeader() const;

    /// What `playbackFinished` carries.  Published because it is the only way a
    /// caller -- or a test -- can observe that a close was requested without a
    /// server to answer it, and because a signal that names its own payload type
    /// is one the linter can check.
    struct FinishedPlayback {
        int mediaId = 0;
        double positionSeconds = 0.0;
        QString sessionId;
    };
    /// Absolute URL of one title's poster or backdrop (`kind` is "poster" or
    /// "backdrop"), or an invalid URL when `hasPosterKey` is false.
    ///
    /// The key is passed rather than looked up: this is a `const` function and
    /// the answer it gives does not depend on the media row, only on whether
    /// that row *has* artwork.  A caller that already has the row -- every
    /// delegate, and the player -- already has the key, and a lookup table of
    /// which titles have posters would be a second source of truth the moment
    /// the server's answer changed.
    QUrl artworkUrl(int mediaId, const QString &kind, bool hasPosterKey = true) const;

    /// Whether the access manager would attach a credential to `url`.
    ///
    /// Exposed because "this request would carry the user's bearer token" is a
    /// security property, and reasoning about it from the two halves -- the
    /// caller's intent and the manager's scope -- is how the two drifted apart
    /// without anything noticing.
    bool wouldAuthenticate(const QUrl &url) const
    {
        return m_manager->wouldAuthenticate(url);
    }

signals:
    void baseUrlChanged();
    void sessionChanged();
    void userChanged();
    void mustChangePasswordChanged();
    void profileChanged();
    void profilesChanged();
    void serverInfoChanged();
    void categoriesChanged();
    void sessionsChanged();

    /// Every request this client sends, as `VERB path?query` -- the token
    /// deliberately excluded.
    ///
    /// Exists because the end-to-end suite has to assert on the *wire*, not on
    /// the model: a filter that looks right in the UI and sends nothing is the
    /// failure that survives every unit test, because every unit test asserts
    /// on the model it also drives.  Recording the request here rather than in
    /// the test means the assertion is about what went out, not about what the
    /// test believes it asked for.
    void requestSent(const QString &description);

    void serverChecked(bool reachable, const QString &version,
                       const QString &message, bool requiresSetup);
    void authenticated();
    void loggedOut();
    void loggedOutEverywhere();
    void passwordChanged();
    /// `GET /auth/me` answered: identity, the profile list and server info.
    void sessionLoaded(const QString &username, const QString &displayName,
                       const QJsonArray &profiles, const QJsonObject &server);
    /// Emitted whenever a *new* token pair is issued, so the session controller
    /// can persist it without duplicating the login path.
    void tokensIssued(const QString &accessToken, const QString &refreshToken,
                      int expiresInSeconds);

    void homeLoaded(const QJsonObject &payload);
    void mediaPageLoaded(const QJsonObject &payload);
    void mediaLoaded(const QJsonObject &media);
    void searchLoaded(const QJsonObject &payload);
    void categoriesLoaded(const QJsonObject &payload);
    void favoritesLoaded(const QJsonObject &payload);
    void historyLoaded(const QJsonObject &payload);
    void historyCleared();
    void historyEntryRemoved(int mediaId);
    void favoriteChanged(int mediaId, bool favorite);
    void ratingChanged(int mediaId, double score);

    void playbackStarted(int mediaId, const QString &streamUrl,
                         double startOffset, int expiresInSeconds);
    void progressReported(int mediaId, double percent);
    void playbackFinished(const ApiClient::FinishedPlayback &playback);

    void profilesLoaded(const QJsonArray &profiles);
    void profileSelected(int profileId);
    void profileUnlocked(int profileId);
    void profileCreated(int profileId);
    void profileUpdated(int profileId, const QJsonObject &profile);
    void profileDeleted(int profileId);

    void sessionsLoaded(const QJsonArray &sessions);
    void sessionRevoked(const QString &sessionId);

    /// Every failure funnels through this one signal, tagged with the
    /// operation so a screen can decide whether it still matters.
    void failed(const QString &operation, const ApiError &error);

private:
    enum class Auth { None, Session, Playback };

    /// One in-flight request, kept so its answer can be interpreted.
    ///
    /// `mediaId` is set by the operations whose answer refers to a title, and
    /// `handle` falls back to the last path segment when it is zero.  That
    /// fallback is right for most routes and wrong for three: the playback
    /// routes carry the media id in the path *and* in a segment that is not
    /// last (`/media/7/playback/finish`, where the last segment is the verb).
    /// Reading the trailing segment there made every `playbackFinished`
    /// announce id 0, and `finish` 404s an id no title has -- so closing a
    /// playback session never recorded that it had been watched.  The rule is
    /// therefore stated per-request rather than inferred per-route.
    struct Pending {
        /// The title this request is about.  Zero means "read it off the path".
        int mediaId = 0;

        QByteArray verb;
        QString path;
        QJsonObject body;
        Auth auth = Auth::None;
        QString operation;
        QUrlQuery query;
        /// Declare `Content-Type: application/json` even when the body is
        /// empty.  Two routes take no body at all -- finishing a playback and
        /// starting one -- and Qt 6 warns about the missing type on every one.
        bool emptyBodyIsJson = false;
    };

    void checkSetupStatus();

    QNetworkReply *send(const Pending &request);
    QNetworkReply *send(const Pending &request, const QString &bearerToken);
    void handle(QNetworkReply *reply, const Pending &pending);
    /// Every failure leaves through here, so the last touch -- an empty
    /// message becoming a real one -- is in one place rather than at each of
    /// the three exits from `handle()`.
    void finishFailure(const Pending &pending, ApiError error);
    /// The *transport's* error, and nothing else -- in particular it does
    /// not read the body.  `handle()` reads it once and owns the parse.
    ApiError transportErrorFrom(QNetworkReply *reply) const;
    /// Hand the current token to the access manager, or take it away.  Called
    /// whenever the token or the base URL changes, so the manager cannot hold
    /// a stale credential after a switch to another server.
    void syncCredentials();

    void attemptRefresh();
    void replay();
    void adoptTokens(const QJsonObject &payload);
    void applySessionUser(const QJsonObject &payload);
    void replaceProfiles(const QJsonArray &profiles);
    /// Apply a profile row to the cached list from the answer to a rename or a
    /// PIN change, so the settings screen updates without another round trip.
    void patchCachedProfile(const QJsonObject &profile);
    void removeCachedProfile(int profileId);

    QString m_lastUsername;
    QString m_serverVersion;
    QString m_playbackSessionId;

    /// The access manager, typed.
    ///
    /// `QNetworkAccessManager` is all `createRequest` overrides, so every use
    /// of it here needed a cast back to the class that actually does anything.
    CredentialAccessManager *m_manager = nullptr;
    QString m_baseUrl;
    QString m_accessToken;
    QString m_refreshToken;
    QString m_playbackToken;
    /// Whether a playback session has been minted and not yet closed.
    ///
    /// Separate from `m_playbackToken` because the token is deliberately *not*
    /// used to decide whether streaming is allowed -- see `streamUrl`.
    bool m_hasPlaybackSession = false;
    int m_playbackMediaId = 0;
    QString m_username;
    int m_profileId = 0;
    bool m_mustChangePassword = false;
    bool m_refreshing = false;
    bool m_loggedOut = false;
    QList<Pending> m_replayQueue;

    QVariantMap m_user;
    QVariantList m_profiles;
    QVariantMap m_serverInfo;
    QVariantMap m_categories;
    QVariantList m_sessions;
};

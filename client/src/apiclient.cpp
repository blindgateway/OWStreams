#include "apiclient.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTimer>
#include <QUrlQuery>

#include "credentialaccessmanager.h"

namespace {

const QString kApiRoot = QStringLiteral("/api/v1");
const QByteArray kAppName = QByteArrayLiteral("Stream/1.0");

/// Split an absolute base URL from the path we append to it.
///
/// The user types whatever they have: `192.168.1.5:8484`, a trailing slash,
/// a full `https://host/stream/`.  Normalising here means every other function
/// can just concatenate.
QString normaliseBase(const QString &raw)
{
    QString text = raw.trimmed();
    if (text.isEmpty())
        return {};
    if (!text.contains(QStringLiteral("://")))
        text.prepend(QStringLiteral("http://"));
    while (text.endsWith(QLatin1Char('/')))
        text.chop(1);
    return text;
}

}  // namespace

ApiClient::ApiClient(QObject *parent)
    : QObject(parent), m_manager(new CredentialAccessManager(this))
{
    // A bare QNetworkAccessManager would send every poster request
    // unauthenticated and every poster would 401.  Attaching the token in a
    // header, on the routes that need it, is the only way `Image` can load
    // them -- see CredentialAccessManager for why the scope is narrow.
}

void ApiClient::syncCredentials()
{
    if (m_accessToken.isEmpty())
        m_manager->clearCredentials();
    else
        m_manager->setCredentials(m_baseUrl, m_accessToken);
    // The streaming route is profile-scoped and its request is made by the
    // media backend, so the manager needs the profile as well as the token.
    m_manager->setProfileId(m_profileId);
}

ApiClient::~ApiClient() = default;

void ApiClient::setBaseUrl(const QString &url)
{
    const QString normalised = normaliseBase(url);
    if (normalised == m_baseUrl)
        return;
    m_baseUrl = normalised;
    // A different server is a different trust boundary: every token we hold was
    // issued by the old one and means nothing here.  Keeping them would send
    // this user's bearer token to whatever host they just typed.
    clearSession();
    emit baseUrlChanged();
}

void ApiClient::setProfileId(int id)
{
    if (m_profileId == id)
        return;
    m_profileId = id;
    // The access manager carries it for the streaming route, whose request
    // never goes through `send()`.
    syncCredentials();
    emit profileChanged();
}

void ApiClient::setSession(const QString &accessToken, const QString &refreshToken,
                           int expiresInSeconds)
{
    Q_UNUSED(expiresInSeconds)
    m_accessToken = accessToken;
    m_refreshToken = refreshToken;
    m_loggedOut = false;
    syncCredentials();
    emit sessionChanged();
}

void ApiClient::clearSession()
{
    m_accessToken.clear();
    m_refreshToken.clear();
    m_playbackToken.clear();
    m_hasPlaybackSession = false;
    m_playbackMediaId = 0;
    m_username.clear();
    m_profileId = 0;
    m_mustChangePassword = false;
    m_loggedOut = false;
    m_playbackSessionId.clear();
    m_user.clear();
    m_profiles.clear();
    m_serverInfo.clear();
    m_sessions.clear();
    syncCredentials();
    emit sessionChanged();
    emit userChanged();
    emit profileChanged();
    emit mustChangePasswordChanged();
    emit profilesChanged();
    emit serverInfoChanged();
    emit sessionsChanged();
}

QUrl ApiClient::streamUrl(int mediaId) const
{
    // Deliberately ignores `m_playbackToken`.  The server accepts a playback
    // token in the Authorization header as well as a session one, and this
    // application streams with the session token: a client-side expiry clock
    // would have to agree with the server's to be safe, and a wrong answer
    // either refuses to play a valid token or -- worse -- keeps playing one the
    // server has already expired.  The server is the only authority on that,
    // and a 403 from it ends playback like any other authorisation failure.
    if (!m_hasPlaybackSession || m_playbackMediaId != mediaId)
        return {};
    return QUrl(m_baseUrl + kApiRoot + QStringLiteral("/stream/")
                + QString::number(mediaId));
}

QString ApiClient::authorizationHeader() const
{
    if (m_accessToken.isEmpty())
        return {};
    return QStringLiteral("Bearer ") + m_accessToken;
}

QUrl ApiClient::artworkUrl(int mediaId, const QString &kind, bool hasPosterKey) const
{
    if (mediaId <= 0 || !hasPosterKey)
        return {};
    // Only the two artwork routes exist.  A caller that invents a third gets
    // an empty URL rather than a 404 the UI has to interpret.
    if (kind != QLatin1String("poster") && kind != QLatin1String("backdrop"))
        return {};
    return QUrl(m_baseUrl + kApiRoot + QStringLiteral("/media/")
                + QString::number(mediaId) + QLatin1Char('/') + kind);
}

// ---------------------------------------------------------------------------
// Request plumbing
// ---------------------------------------------------------------------------
QNetworkReply *ApiClient::send(const Pending &request)
{
    return send(request, QString());
}

QNetworkReply *ApiClient::send(const Pending &request, const QString &bearerToken)
{
    QNetworkRequest netRequest{QUrl(m_baseUrl + request.path)};
    netRequest.setRawHeader("Accept", QByteArrayLiteral("application/json"));
    netRequest.setRawHeader("User-Agent", kAppName);
    netRequest.setAttribute(QNetworkRequest::RedirectPolicyAttribute,
                            QNetworkRequest::ManualRedirectPolicy);
    if (!request.query.isEmpty())
        netRequest.setUrl(QUrl(m_baseUrl + request.path
                          + QStringLiteral("?") + request.query.toString(QUrl::FullyEncoded)));

    // A profile-scoped route is meaningless without this, and the server
    // answers 404 for a profile the caller does not own -- so sending the wrong
    // one is a silent no-op rather than an error the UI could show.
    //
    // The check is on the *route path*, not on a prefix list kept elsewhere:
    // the profile-scoped roots are /library, /profiles, /media and /stream, and
    // anything added under them inherits the header automatically.  An earlier
    // version listed only /library, so the playback routes and every profile
    // call went out unscoped and the server had no profile to resolve.
    // `kApiRoot` is stripped before the comparison.  A path is
    // `/api/v1/library/favorites`, so testing it for a prefix of `/library`
    // is false for *every* profile-scoped route -- the header was never
    // written, and the server fell back to the account's first profile for
    // every library read.  The prefix list is written against the API root so
    // the two cannot drift: it says "the route is under /library", not "the
    // string begins with /library".
    const QString route = request.path.startsWith(kApiRoot)
                              ? request.path.mid(kApiRoot.size())
                              : request.path;
    if (request.auth != Auth::None
        && (route.startsWith(QLatin1String("/library"))
            || route.startsWith(QLatin1String("/profiles"))
            || route.startsWith(QLatin1String("/media"))
            || route.startsWith(QLatin1String("/stream")))) {
        // Always set, including to "0".  Leaving the header off when no
        // profile is selected used to look equivalent and was not: the server
        // then resolved the account's *first* profile and answered 200, so a
        // request made before a profile was chosen silently returned another
        // profile's favourites, history and ratings.  Naming "0" is refused
        // (`profile_not_found`), which is a failure a person can see.
        netRequest.setRawHeader("X-Profile-Id", QByteArray::number(m_profileId));
    }

    const QByteArray payload =
        request.body.isEmpty() ? QByteArray()
                               : QJsonDocument(request.body).toJson(QJsonDocument::Compact);
    if (!payload.isEmpty() || request.emptyBodyIsJson)
        netRequest.setHeader(QNetworkRequest::ContentTypeHeader,
                             QByteArrayLiteral("application/json; charset=utf-8"));

    if (!bearerToken.isEmpty())
        netRequest.setRawHeader("Authorization", "Bearer " + bearerToken.toUtf8());

    QNetworkReply *reply =
        (request.verb == "GET") ? m_manager->get(netRequest)
                                : m_manager->sendCustomRequest(netRequest, request.verb,
                                                                payload);
    // Qt's default has no timeout at all: a black-holed connection leaves the
    // spinner up forever, which on a TV is indistinguishable from a crash.
    QTimer::singleShot(kDefaultTimeoutMs, reply, [reply] {
        if (reply->isRunning())
            reply->abort();
    });
    return reply;
}

void ApiClient::handle(QNetworkReply *reply, const Pending &pending)
{
    const QByteArray body = reply->readAll();
    const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();

    if (status == 401 && !m_refreshToken.isEmpty() && !m_loggedOut
        && pending.auth == Auth::Session) {
        m_replayQueue.append(pending);
        attemptRefresh();
        return;
    }

    QJsonParseError parseError{};
    const QJsonDocument document = QJsonDocument::fromJson(body, &parseError);
    const QJsonValue root = document.isArray() ? QJsonValue(document.array())
                                               : QJsonValue(document.object());
    const QJsonObject object = root.isObject() ? root.toObject() : QJsonObject{};

    // Operations are dispatched by name.  That keeps every endpoint's wiring
    // in one readable place, and adding a screen is one case here rather than
    // a new signal-plumbing exercise.
    if (status >= 200 && status < 300) {
        if (pending.operation == QLatin1String("server.check")) {
            // /api/health is outside the version prefix precisely so a client
            // can tell "wrong URL" from "wrong API version".  It says nothing
            // about whether the server has any accounts yet, so the setup flag
            // comes from /auth/setup on the next hop.
            m_serverVersion = object.value(QStringLiteral("version")).toString();
            emit serverChecked(true, m_serverVersion, QString(), false);
            checkSetupStatus();
        } else if (pending.operation == QLatin1String("server.setup")) {
            emit serverChecked(true, m_serverVersion, QString(),
                               object.value(QStringLiteral("requires_setup")).toBool());
        } else if (pending.operation == QLatin1String("auth.login")) {
            adoptTokens(object);
            m_lastUsername = pending.body.value(QStringLiteral("username")).toString();
            emit authenticated();
        } else if (pending.operation == QLatin1String("auth.me")) {
            const QJsonObject user = object.value(QStringLiteral("user")).toObject();
            applySessionUser(user);
            m_user = user.toVariantMap();
            const QJsonObject server = object.value(QStringLiteral("server")).toObject();
            m_serverInfo = server.toVariantMap();
            const QJsonArray profiles = object.value(QStringLiteral("profiles")).toArray();
            replaceProfiles(profiles);
            emit userChanged();
            emit serverInfoChanged();
            emit sessionLoaded(user.value(QStringLiteral("username")).toString(),
                               user.value(QStringLiteral("display_name")).toString(),
                               profiles, server);
        } else if (pending.operation == QLatin1String("auth.logout")) {
            clearSession();
            emit loggedOut();
        } else if (pending.operation == QLatin1String("auth.password")) {
            m_mustChangePassword = false;
            emit mustChangePasswordChanged();
            emit passwordChanged();
        } else if (pending.operation == QLatin1String("library.home")) {
            emit homeLoaded(object);
        } else if (pending.operation == QLatin1String("library.page")) {
            emit mediaPageLoaded(object);
        } else if (pending.operation == QLatin1String("library.media")) {
            emit mediaLoaded(object);
        } else if (pending.operation == QLatin1String("library.search")) {
            emit searchLoaded(object);
        } else if (pending.operation == QLatin1String("library.categories")) {
            m_categories = object.toVariantMap();
            emit categoriesChanged();
            emit categoriesLoaded(object);
        } else if (pending.operation == QLatin1String("library.favorites")) {
            emit favoritesLoaded(object);
        } else if (pending.operation == QLatin1String("library.history")) {
            emit historyLoaded(object);
        } else if (pending.operation == QLatin1String("library.history.clear")) {
            emit historyCleared();
        } else if (pending.operation == QLatin1String("library.history.remove")) {
            emit historyEntryRemoved(pending.mediaId);
        } else if (pending.operation == QLatin1String("library.favorite.put")) {
            // The intent comes out of the verb, because the request carries no
            // body: `/favorites/42` with PUT is "make 42 a favourite", DELETE
            // is "not any more".
            emit favoriteChanged(pending.mediaId,
                                 pending.verb == QByteArrayLiteral("PUT"));
        } else if (pending.operation == QLatin1String("library.rating.put")) {
            emit ratingChanged(pending.mediaId,
                              pending.body.value(QStringLiteral("score")).toDouble());
        } else if (pending.operation == QLatin1String("playback.start")) {
            m_playbackToken = object.value(QStringLiteral("playback_token")).toString();
            m_hasPlaybackSession = !m_playbackToken.isEmpty();
            m_playbackSessionId = object.value(QStringLiteral("session_id")).toString();
            m_playbackMediaId = pending.mediaId;
            // The media id is stamped *before* the URL is built, not after.
            //
            // Order of operations that reads like a style choice and is not:
            // `streamUrl` hands back an empty QUrl for any id that is not the
            // current one, and the id was being assigned after the call, so
            // every successful playback announced an empty URL.  The player
            // reads that as "the server did not return a playable source" and
            // reports a playback failure for a title the server had just
            // happily authorised.  Nothing failed visibly, which is why it
            // survived as long as it did.
            // The URL is deliberately bare.  The server accepts an access
            // token *or* a playback token in the header, and the playback
            // token is the narrower one -- it is bound to this title and to
            // this profile, and the stream route enforces that binding.  The
            // media backend cannot send a header, so the access token rides
            // along in one instead; see CredentialAccessManager.
            emit playbackStarted(m_playbackMediaId,
                                 streamUrl(m_playbackMediaId).toString(),
                                 object.value(QStringLiteral("start_offset")).toDouble(),
                                 object.value(QStringLiteral("expires_in")).toInt());
        } else if (pending.operation == QLatin1String("playback.progress")) {
            emit progressReported(pending.mediaId,
                                  pending.body.value(QStringLiteral("position_seconds")).toDouble());
        } else if (pending.operation == QLatin1String("playback.finish")) {
            const int mediaId = pending.mediaId;
            m_hasPlaybackSession = false;
            m_playbackToken.clear();
            m_playbackSessionId.clear();
            m_playbackMediaId = 0;
            // The acknowledgement, not the request: nothing else observes a
            // close, so a caller that did not see it cannot tell one apart from
            // a player that was simply torn down.
            emit playbackFinished(FinishedPlayback{
                mediaId,
                pending.query.queryItemValue(QStringLiteral("position_seconds"),
                                             QUrl::FullyDecoded).toDouble(),
                pending.query.queryItemValue(QStringLiteral("session_id"))});
        } else if (pending.operation == QLatin1String("profiles.list")) {
            // The route answers a bare array.  An earlier version read
            // object["profiles"] and therefore always reported an empty list,
            // so the profile picker rendered as "no profiles".
            const QJsonArray list = root.isArray() ? root.toArray()
                                                  : object.value(QStringLiteral("profiles")).toArray();
            replaceProfiles(list);
            emit profilesLoaded(list);
        } else if (pending.operation == QLatin1String("profiles.update")) {
            const QJsonObject profile = object;
            patchCachedProfile(profile);
            emit profileUpdated(profile.value(QStringLiteral("id")).toInt(), profile);
        } else if (pending.operation == QLatin1String("profiles.delete")) {
            removeCachedProfile(pending.mediaId);
            emit profileDeleted(pending.mediaId);
        } else if (pending.operation == QLatin1String("profiles.pin.set")) {
            const QJsonObject profile = object;
            patchCachedProfile(profile);
            emit profileUpdated(profile.value(QStringLiteral("id")).toInt(), profile);
        } else if (pending.operation == QLatin1String("profiles.pin.change")) {
            const QJsonObject profile = object;
            patchCachedProfile(profile);
            emit profileUpdated(profile.value(QStringLiteral("id")).toInt(), profile);
        } else if (pending.operation == QLatin1String("sessions.list")) {
            const QJsonArray list = root.isArray() ? root.toArray()
                                                  : object.value(QStringLiteral("sessions")).toArray();
            m_sessions = list.toVariantList();
            emit sessionsChanged();
            emit sessionsLoaded(list);
        } else if (pending.operation == QLatin1String("sessions.revoke")) {
            const QString id = pending.path.section(QLatin1Char('/'), -1);
            for (int i = 0; i < m_sessions.size(); ++i) {
                if (m_sessions.at(i).toMap().value(QStringLiteral("id")).toString() == id)
                    m_sessions.removeAt(i);
            }
            emit sessionsChanged();
            emit sessionRevoked(id);
        } else if (pending.operation == QLatin1String("auth.logout_all")) {
            clearSession();
            emit loggedOut();
            emit loggedOutEverywhere();
        } else if (pending.operation == QLatin1String("profiles.select")) {
            // `/profiles/select` names no id in its path -- it is a payload,
            // not a path parameter -- so this one genuinely is the body's.
            emit profileSelected(pending.body.value(QStringLiteral("profile_id")).toInt());
        } else if (pending.operation == QLatin1String("profiles.unlock")) {
            // The id is the pending request's own, not a re-read of the path:
            // an unlock is for *this* profile, and `/profiles/{id}/unlock` ends
            // in the verb.  Taking the trailing segment announced profile 0 --
            // and `selectProfile` below then adopted it, so unlocking a PIN
            // profile signed you in as no profile at all and every later
            // request went out without the header the server needs.
            emit profileUnlocked(pending.mediaId);
        } else if (pending.operation == QLatin1String("profiles.create")) {
            // The body carries only the name; the id is the answer's.
            emit profileCreated(object.value(QStringLiteral("id")).toInt());
        }
        return;
    }

    ApiError error = errorFrom(reply);
    error.kind = kindForStatus(status);
    error.statusCode = status;
    if (object.contains(QStringLiteral("error"))) {
        error.code = object.value(QStringLiteral("error")).toString();
        error.message = object.value(QStringLiteral("message")).toString();
        error.requestId = object.value(QStringLiteral("request_id")).toString();
        if (status == 429)
            error.retryAfterSeconds =
                reply->rawHeader(QByteArrayLiteral("Retry-After")).toInt();
    } else if (parseError.error != QJsonParseError::NoError) {
        error.message =
            QStringLiteral("The server sent a response this app could not read.");
    }
    if (error.message.isEmpty())
        error.message = QStringLiteral("Request failed (%1).").arg(status);
    emit failed(pending.operation, error);
}

ApiError ApiClient::errorFrom(QNetworkReply *reply) const
{
    ApiError error;
    error.statusCode =
        reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();

    if (error.statusCode != 0)
        return error;   // the server answered; handle() has the body

    switch (reply->error()) {
    case QNetworkReply::OperationCanceledError:
        error.kind = ApiErrorKind::Timeout;
        error.message = QStringLiteral("The server did not answer in time.");
        break;
    case QNetworkReply::HostNotFoundError:
    case QNetworkReply::ConnectionRefusedError:
    case QNetworkReply::RemoteHostClosedError:
        error.kind = ApiErrorKind::NetworkUnreachable;
        error.message = QStringLiteral("Could not reach the server.");
        break;
    case QNetworkReply::TimeoutError:
        error.kind = ApiErrorKind::Timeout;
        error.message = QStringLiteral("The server did not answer in time.");
        break;
    case QNetworkReply::SslHandshakeFailedError:
        error.kind = ApiErrorKind::NetworkUnreachable;
        error.message = QStringLiteral("The secure connection failed.");
        break;
    default:
        error.kind = ApiErrorKind::Unknown;
        error.message = reply->errorString();
        break;
    }
    return error;
}

void ApiClient::adoptTokens(const QJsonObject &payload)
{
    m_accessToken = payload.value(QStringLiteral("access_token")).toString();
    m_refreshToken = payload.value(QStringLiteral("refresh_token")).toString();
    m_loggedOut = false;
    syncCredentials();
    m_mustChangePassword = payload.value(QStringLiteral("must_change_password")).toBool();
    emit sessionChanged();
    emit mustChangePasswordChanged();
    applySessionUser(payload.value(QStringLiteral("user")).toObject());
    emit tokensIssued(m_accessToken, m_refreshToken,
                      payload.value(QStringLiteral("expires_in")).toInt());
}

void ApiClient::loadMe()
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/auth/me");
    request.auth = Auth::Session;
    request.operation = QStringLiteral("auth.me");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::applySessionUser(const QJsonObject &user)
{
    if (user.isEmpty())
        return;
    m_username = user.value(QStringLiteral("username")).toString();
    emit userChanged();
}

void ApiClient::attemptRefresh()
{
    if (m_refreshing || m_refreshToken.isEmpty())
        return;
    m_refreshing = true;

    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/auth/refresh");
    request.body = {{QStringLiteral("refresh_token"), m_refreshToken}};
    request.operation = QStringLiteral("auth.refresh");

    QNetworkReply *reply = send(request);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        const QByteArray body = reply->readAll();
        const int status =
            reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        m_refreshing = false;

        QJsonParseError parseError{};
        const QJsonObject object =
            QJsonDocument::fromJson(body, &parseError).object();

        if (status < 200 || status >= 300) {
            // The refresh token itself is dead: this is a real logout, and
            // retrying with it would just fail again.
            m_loggedOut = true;
            clearSession();
            ApiError error;
            error.statusCode = status;
            error.kind = kindForStatus(status);
            error.message = QStringLiteral("Your session has expired. Sign in again.");
            if (object.contains(QStringLiteral("message")))
                error.message = object.value(QStringLiteral("message")).toString();
            emit failed(request.operation, error);
            emit loggedOut();
            return;
        }
        adoptTokens(object);
        replay();
        emit authenticated();
    });
}

void ApiClient::replay()
{
    // Drain the queue after a successful refresh.  Each entry carries the
    // *original* Pending, so it goes out again with the new access token and
    // the body it was built with.
    const QList<Pending> queued = m_replayQueue;
    m_replayQueue.clear();
    for (const Pending &request : queued) {
        QNetworkReply *reply = send(request, m_accessToken);
        connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
            reply->deleteLater();
            handle(reply, request);
        });
    }
}

// ---------------------------------------------------------------------------
// Operations
// ---------------------------------------------------------------------------
void ApiClient::checkServer()
{
    Pending request;
    request.verb = "GET";
    request.path = QStringLiteral("/api/health");
    request.operation = QStringLiteral("server.check");
    QNetworkReply *reply = send(request);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::checkSetupStatus()
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/auth/setup");
    request.operation = QStringLiteral("server.setup");
    QNetworkReply *reply = send(request);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::login(const QString &username, const QString &password,
                      const QString &deviceName, bool remember)
{
    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/auth/login");
    request.body = {{QStringLiteral("username"), username},
                    {QStringLiteral("password"), password},
                    {QStringLiteral("device_name"), deviceName},
                    {QStringLiteral("remember"), remember}};
    request.operation = QStringLiteral("auth.login");
    QNetworkReply *reply = send(request);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::logout()
{
    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/auth/logout");
    request.auth = Auth::Session;
    request.operation = QStringLiteral("auth.logout");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::changePassword(const QString &current, const QString &next)
{
    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/auth/password");
    request.body = {{QStringLiteral("current_password"), current},
                    {QStringLiteral("new_password"), next},
                    {QStringLiteral("logout_other_sessions"), true}};
    request.auth = Auth::Session;
    request.operation = QStringLiteral("auth.password");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::loadHome(int limit)
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/library/home");
    request.query = QUrlQuery{{QStringLiteral("limit"), QString::number(limit)}};
    request.auth = Auth::Session;
    request.operation = QStringLiteral("library.home");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::loadMediaPage(const QUrlQuery &query)
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/library/media");
    request.query = query;
    request.auth = Auth::Session;
    request.operation = QStringLiteral("library.page");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::loadMedia(int mediaId)
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/library/media/") +
                   QString::number(mediaId);
    request.auth = Auth::Session;
    request.operation = QStringLiteral("library.media");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::search(const QString &query, int limit)
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/library/search");
    request.query = QUrlQuery{{QStringLiteral("q"), query},
                              {QStringLiteral("limit"), QString::number(limit)}};
    request.auth = Auth::Session;
    request.operation = QStringLiteral("library.search");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::loadCategories()
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/library/categories");
    request.auth = Auth::Session;
    request.operation = QStringLiteral("library.categories");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::loadFavorites()
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/library/favorites");
    request.auth = Auth::Session;
    request.operation = QStringLiteral("library.favorites");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::loadHistory()
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/library/history");
    request.auth = Auth::Session;
    request.operation = QStringLiteral("library.history");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::setFavorite(int mediaId, bool favorite)
{
    Pending request;
    request.verb = favorite ? "PUT" : "DELETE";
    request.path = kApiRoot + QStringLiteral("/library/favorites/") +
                   QString::number(mediaId);
    request.auth = Auth::Session;
    // No body: the media id is in the path and the verb already says whether
    // this is adding or removing.  `OkResponse` is a `StrictModel`, so the two
    // extra fields made every favourite toggle a 422 -- and the favourite rail
    // updated anyway, because `handle()` reports the *intent* rather than the
    // server's answer.
    request.mediaId = mediaId;
    request.operation = QStringLiteral("library.favorite.put");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::rateMedia(int mediaId, double score)
{
    // `/library/media/{id}/rating`, not `/media/{id}/rating`: ratings are
    // served by the *library* router, and only it.  The distinction is not
    // cosmetic -- `/media/{id}/...` is the playback and artwork root, so a
    // rating sent there is a 404, and `AppController::setRating` has no error
    // path for one, which is how a rating can silently do nothing.
    Pending request;
    request.verb = "PUT";
    request.path = kApiRoot + QStringLiteral("/library/media/") +
                   QString::number(mediaId) + QStringLiteral("/rating");
    request.body = {{QStringLiteral("score"), score}};
    request.auth = Auth::Session;
    request.mediaId = mediaId;
    request.operation = QStringLiteral("library.rating.put");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::startPlayback(int mediaId, double startOffset)
{
    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/media/") +
                   QString::number(mediaId) + QStringLiteral("/playback");
    request.query = QUrlQuery{{QStringLiteral("start_offset"),
                               QString::number(startOffset, 'f', 3)}};
    // No body: the route takes its offset as a query parameter.  The content
    // type is still declared, because Qt 6 warns about it on every POST whose
    // body is empty and a warning per playback start is noise nobody reads.
    request.emptyBodyIsJson = true;
    request.auth = Auth::Session;
    request.mediaId = mediaId;
    request.operation = QStringLiteral("playback.start");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::reportProgress(int mediaId, double positionSeconds,
                               double durationSeconds)
{
    Pending request;
    request.verb = "PUT";
    request.path = kApiRoot + QStringLiteral("/media/") +
                   QString::number(mediaId) + QStringLiteral("/progress");
    // No `media_id` in the body: it is already in the path, and
    // `ProgressUpdate` is a `StrictModel` with `extra="forbid"`, so the server
    // answered 422 to every progress report.  The `media_id` that came back was
    // always the path's, which is how this stayed invisible -- the report was
    // rejected and `PlaybackController` deliberately ignores progress
    // failures, so nothing ever surfaced.
    request.body = {{QStringLiteral("position_seconds"), positionSeconds},
                    {QStringLiteral("duration_seconds"), durationSeconds}};
    // Qt 6 logs `content-type missing in HTTP POST` for a POST whose body is
    // an empty QVariantMap, and the server does not care -- but a warning on
    // every progress report is the kind of noise that teaches people to ignore
    // warnings.  Declared explicitly, it is not a lie: `send` replaces the type
    // with `application/json; charset=utf-8` as soon as there is a payload.
    request.emptyBodyIsJson = true;
    request.auth = Auth::Session;
    request.mediaId = mediaId;
    request.operation = QStringLiteral("playback.progress");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::finishPlayback(int mediaId, double positionSeconds,
                              const QString &sessionId)
{
    // The server scopes the close to the caller and 404s a session id that is
    // not this user's, so sending our own is safe -- and sending it is what
    // stops a playback row lingering as "never finished" in the audit trail.
    const QString id = sessionId.isEmpty() ? m_playbackSessionId : sessionId;

    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/media/") +
                   QString::number(mediaId) + QStringLiteral("/playback/finish");
    // The route takes *no* body at all -- both values are query parameters --
    // so an empty body with the parameters in the query is the whole contract.
    // Sending `media_id` in the body was ignored, which is why a body here at
    // all is worth removing: it is one more thing that can drift from the
    // route without anything noticing.
    request.query = QUrlQuery{{QStringLiteral("position_seconds"),
                               QString::number(positionSeconds, 'f', 3)}};
    if (!id.isEmpty())
        request.query.addQueryItem(QStringLiteral("session_id"), id);
    request.body = QJsonObject();
    request.emptyBodyIsJson = true;
    request.auth = Auth::Session;
    request.mediaId = mediaId;
    request.operation = QStringLiteral("playback.finish");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::loadProfiles()
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/profiles");
    request.auth = Auth::Session;
    request.operation = QStringLiteral("profiles.list");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::selectProfile(int profileId)
{
    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/profiles/select");
    // The body, because the route names no id in its path.  This is the one
    // request where the id *is* a payload: `ProfileSelection` is a `StrictModel`
    // with `profile_id: int = Field(ge=1)`, so an empty body is a 422 and the
    // profile switch silently never happens -- the session machine waits for
    // `profileSelected`, which then never comes.
    request.body = {{QStringLiteral("profile_id"), profileId}};
    request.auth = Auth::Session;
    request.operation = QStringLiteral("profiles.select");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::unlockProfile(int profileId, const QString &pin)
{
    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/profiles/") +
                   QString::number(profileId) + QStringLiteral("/unlock");
    request.body = {{QStringLiteral("pin"), pin}};
    request.auth = Auth::Session;
    request.mediaId = profileId;
    request.operation = QStringLiteral("profiles.unlock");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::createProfile(const QString &name, const QString &pin)
{
    QJsonObject body{{QStringLiteral("name"), name}};
    if (!pin.isEmpty())
        body.insert(QStringLiteral("pin"), pin);

    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/profiles");
    request.body = body;
    request.auth = Auth::Session;
    request.operation = QStringLiteral("profiles.create");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::renameProfile(int profileId, const QString &name)
{
    Pending request;
    request.verb = "PATCH";
    request.path = kApiRoot + QStringLiteral("/profiles/") + QString::number(profileId);
    request.body = {{QStringLiteral("name"), name}};
    request.auth = Auth::Session;
    request.operation = QStringLiteral("profiles.update");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::deleteProfile(int profileId)
{
    Pending request;
    request.verb = "DELETE";
    request.path = kApiRoot + QStringLiteral("/profiles/") + QString::number(profileId);
    request.auth = Auth::Session;
        request.mediaId = profileId;
request.operation = QStringLiteral("profiles.delete");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::setProfilePin(int profileId, const QString &pin,
                              const QString &currentPin)
{
    // An absent `pin` means "clear it"; an absent `current_pin` is only
    // allowed by the server when the profile is not locked, which is the
    // check that stops a client clearing somebody else's PIN by omission.
    QJsonObject body{{QStringLiteral("pin"), pin.isEmpty() ? QJsonValue(QJsonValue::Null)
                                                          : QJsonValue(pin)}};
    if (!currentPin.isEmpty())
        body.insert(QStringLiteral("current_pin"), currentPin);

    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/profiles/") + QString::number(profileId)
                   + QStringLiteral("/pin");
    request.body = body;
    request.auth = Auth::Session;
        request.mediaId = profileId;
request.operation = QStringLiteral("profiles.pin.set");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::changeProfilePin(int profileId, const QString &currentPin,
                                 const QString &newPin)
{
    Pending request;
    request.verb = "PUT";
    request.path = kApiRoot + QStringLiteral("/profiles/") + QString::number(profileId)
                   + QStringLiteral("/pin");
    request.body = {{QStringLiteral("current_pin"), currentPin},
                    {QStringLiteral("new_pin"), newPin}};
    request.auth = Auth::Session;
        request.mediaId = profileId;
request.operation = QStringLiteral("profiles.pin.change");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::loadSessions()
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/auth/sessions");
    request.auth = Auth::Session;
    request.operation = QStringLiteral("sessions.list");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::revokeSession(const QString &sessionId)
{
    Pending request;
    request.verb = "DELETE";
    request.path = kApiRoot + QStringLiteral("/auth/sessions/") + sessionId;
    request.auth = Auth::Session;
    request.operation = QStringLiteral("sessions.revoke");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::logoutEverywhere()
{
    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/auth/logout-all");
    request.auth = Auth::Session;
    request.operation = QStringLiteral("auth.logout_all");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::clearHistory()
{
    Pending request;
    request.verb = "DELETE";
    request.path = kApiRoot + QStringLiteral("/library/history");
    request.auth = Auth::Session;
    request.operation = QStringLiteral("library.history.clear");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::removeHistoryEntry(int mediaId)
{
    Pending request;
    request.verb = "DELETE";
    request.path = kApiRoot + QStringLiteral("/library/history/") + QString::number(mediaId);
    request.auth = Auth::Session;
    request.mediaId = mediaId;
    request.operation = QStringLiteral("library.history.remove");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::replaceProfiles(const QJsonArray &profiles)
{
    m_profiles = profiles.toVariantList();
    emit profilesChanged();
}

void ApiClient::patchCachedProfile(const QJsonObject &profile)
{
    const int id = profile.value(QStringLiteral("id")).toInt();
    if (id <= 0)
        return;
    const QVariantMap incoming = profile.toVariantMap();
    for (int i = 0; i < m_profiles.size(); ++i) {
        if (m_profiles.at(i).toMap().value(QStringLiteral("id")).toInt() != id)
            continue;
        QVariantMap merged = m_profiles.at(i).toMap();
        // Only overwrite keys the server actually sent.  A partial answer
        // (a 204, say) must not blank out a field we already know.
        for (auto it = incoming.constBegin(); it != incoming.constEnd(); ++it)
            merged.insert(it.key(), it.value());
        m_profiles[i] = merged;
        emit profilesChanged();
        return;
    }
    m_profiles.append(incoming);
    emit profilesChanged();
}

void ApiClient::removeCachedProfile(int profileId)
{
    for (int i = 0; i < m_profiles.size(); ++i) {
        if (m_profiles.at(i).toMap().value(QStringLiteral("id")).toInt() != profileId)
            continue;
        m_profiles.removeAt(i);
        // The profile that was deleted must not stay selected: every
        // profile-scoped request would now 404 on a header naming a profile
        // that no longer exists.
        if (m_profileId == profileId) {
            m_profileId = 0;
            emit profileChanged();
        }
        emit profilesChanged();
        return;
    }
}

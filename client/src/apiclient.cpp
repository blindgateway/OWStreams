#include "apiclient.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QTimer>
#include <QUrlQuery>

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
    : QObject(parent), m_manager(new QNetworkAccessManager(this))
{
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
    emit profileChanged();
}

void ApiClient::setSession(const QString &accessToken, const QString &refreshToken,
                           int expiresInSeconds)
{
    Q_UNUSED(expiresInSeconds)
    m_accessToken = accessToken;
    m_refreshToken = refreshToken;
    m_loggedOut = false;
    emit sessionChanged();
}

void ApiClient::clearSession()
{
    m_accessToken.clear();
    m_refreshToken.clear();
    m_playbackToken.clear();
    m_playbackMediaId = 0;
    m_username.clear();
    m_profileId = 0;
    m_mustChangePassword = false;
    m_loggedOut = false;
    emit sessionChanged();
    emit userChanged();
    emit profileChanged();
    emit mustChangePasswordChanged();
}

QUrl ApiClient::streamUrl(int mediaId) const
{
    if (m_playbackToken.isEmpty() || m_playbackMediaId != mediaId)
        return {};
    QUrl url(m_baseUrl + kApiRoot + QStringLiteral("/stream/") +
             QString::number(mediaId));
    return url;
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
    if (m_profileId > 0
        && request.auth != Auth::None
        && request.path.startsWith(QStringLiteral("/library"))
    ) {
        netRequest.setRawHeader("X-Profile-Id", QByteArray::number(m_profileId));
    }

    const QByteArray payload =
        request.body.isEmpty() ? QByteArray()
                               : QJsonDocument(request.body).toJson(QJsonDocument::Compact);
    if (!payload.isEmpty())
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
    const QJsonObject object = document.isObject() ? document.object() : QJsonObject{};

    // Operations are dispatched by name.  That keeps every endpoint's wiring
    // in one readable place, and adding a screen is one case here rather than
    // a new signal-plumbing exercise.
    if (status >= 200 && status < 300) {
        if (pending.operation == QLatin1String("auth.login")) {
            adoptTokens(object);
            m_lastUsername = pending.body.value(QStringLiteral("username")).toString();
            emit authenticated();
        } else if (pending.operation == QLatin1String("auth.me")) {
            const QJsonObject user = object.value(QStringLiteral("user")).toObject();
            applySessionUser(user);
            emit sessionLoaded(user.value(QStringLiteral("username")).toString(),
                               user.value(QStringLiteral("display_name")).toString(),
                               object.value(QStringLiteral("profiles")).toArray(),
                               object.value(QStringLiteral("server")).toObject());
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
            emit categoriesLoaded(object);
        } else if (pending.operation == QLatin1String("library.favorites")) {
            emit favoritesLoaded(object);
        } else if (pending.operation == QLatin1String("library.history")) {
            emit historyLoaded(object);
        } else if (pending.operation == QLatin1String("library.favorite.put")) {
            emit favoriteChanged(pending.body.value(QStringLiteral("media_id")).toInt(),
                                 pending.body.value(QStringLiteral("favorite")).toBool());
        } else if (pending.operation == QLatin1String("library.rating.put")) {
            emit ratingChanged(pending.body.value(QStringLiteral("media_id")).toInt(),
                              pending.body.value(QStringLiteral("score")).toDouble());
        } else if (pending.operation == QLatin1String("playback.start")) {
            m_playbackToken = object.value(QStringLiteral("playback_token")).toString();
            m_playbackMediaId = pending.body.value(QStringLiteral("media_id")).toInt();
            emit playbackStarted(m_playbackMediaId,
                                 streamUrl(m_playbackMediaId).toString(),
                                 object.value(QStringLiteral("start_offset")).toDouble(),
                                 object.value(QStringLiteral("expires_in")).toInt());
        } else if (pending.operation == QLatin1String("playback.progress")) {
            emit progressReported(pending.body.value(QStringLiteral("media_id")).toInt(),
                                  pending.body.value(QStringLiteral("position_seconds")).toDouble());
        } else if (pending.operation == QLatin1String("playback.finish")) {
            m_playbackToken.clear();
            m_playbackMediaId = 0;
            emit playbackFinished(pending.body.value(QStringLiteral("media_id")).toInt());
        } else if (pending.operation == QLatin1String("profiles.list")) {
            emit profilesLoaded(object.value(QStringLiteral("profiles")).toArray());
        } else if (pending.operation == QLatin1String("profiles.select")) {
            emit profileSelected(pending.body.value(QStringLiteral("profile_id")).toInt());
        } else if (pending.operation == QLatin1String("profiles.unlock")) {
            emit profileUnlocked(pending.body.value(QStringLiteral("profile_id")).toInt());
        } else if (pending.operation == QLatin1String("profiles.create")) {
            emit profileCreated(pending.body.value(QStringLiteral("profile_id")).toInt());
        }
        return;
    }

    ApiError error = errorFrom(reply, pending.operation);
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

void ApiClient::handleError(QNetworkReply *reply, const Pending &pending)
{
    ApiError error = errorFrom(reply, pending.operation);
    // A 401 with a session token is the refresh path, not a failure to show.
    if (error.statusCode == 401 && !m_refreshToken.isEmpty() && !m_loggedOut
        && pending.auth == Auth::Session) {
        m_replayQueue.append(pending);
        attemptRefresh();
        return;
    }
    error.kind = kindForStatus(error.statusCode);
    if (error.kind == ApiErrorKind::ServerError && error.message.isEmpty())
        error.message = QStringLiteral("The server had a problem. Try again shortly.");
    emit failed(pending.operation, error);
}

ApiError ApiClient::errorFrom(QNetworkReply *reply, const QString &) const
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
        replay(request.operation);
        emit authenticated();
    });
}

void ApiClient::replay(const QString &operation)
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
    Q_UNUSED(operation)
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

void ApiClient::loadMediaPage(int limit, int offset)
{
    Pending request;
    request.verb = "GET";
    request.path = kApiRoot + QStringLiteral("/library/media");
    request.query = QUrlQuery{{QStringLiteral("limit"), QString::number(limit)},
                              {QStringLiteral("offset"), QString::number(offset)},
                              {QStringLiteral("sort"), QStringLiteral("added")}};
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
    request.operation = QStringLiteral("library.favorite.put");
    request.body = {{QStringLiteral("media_id"), mediaId}, {QStringLiteral("favorite"), favorite}};
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::rateMedia(int mediaId, double score)
{
    Pending request;
    request.verb = "PUT";
    request.path = kApiRoot + QStringLiteral("/library/media/") +
                   QString::number(mediaId) + QStringLiteral("/rating");
    request.body = {{QStringLiteral("media_id"), mediaId}, {QStringLiteral("score"), score}};
    request.auth = Auth::Session;
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
    request.body = {{QStringLiteral("media_id"), mediaId}};
    request.auth = Auth::Session;
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
    request.body = {{QStringLiteral("media_id"), mediaId},
                    {QStringLiteral("position_seconds"), positionSeconds},
                    {QStringLiteral("duration_seconds"), durationSeconds}};
    request.auth = Auth::Session;
    request.operation = QStringLiteral("playback.progress");
    QNetworkReply *reply = send(request, m_accessToken);
    connect(reply, &QNetworkReply::finished, this, [this, reply, request] {
        reply->deleteLater();
        handle(reply, request);
    });
}

void ApiClient::finishPlayback(int mediaId, double positionSeconds)
{
    Pending request;
    request.verb = "POST";
    request.path = kApiRoot + QStringLiteral("/media/") +
                   QString::number(mediaId) + QStringLiteral("/playback/finish");
    request.query = QUrlQuery{{QStringLiteral("position_seconds"),
                               QString::number(positionSeconds, 'f', 3)}};
    request.body = {{QStringLiteral("media_id"), mediaId}};
    request.auth = Auth::Session;
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
    request.body = {{QStringLiteral("profile_id"), profileId},
                    {QStringLiteral("pin"), pin}};
    request.auth = Auth::Session;
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
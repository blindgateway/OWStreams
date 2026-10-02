#include "credentialaccessmanager.h"

#include <QNetworkReply>
#include <QNetworkRequest>
#include <QUrl>

namespace {

/// True when `path` is one of the routes that need a credential.
///
/// Written as explicit prefixes rather than "not /api/v1", because the safe
/// default for a request this code does not recognise is *no token*: a
/// credential should never be a side effect of adding a new screen.
bool pathNeedsCredential(const QString &path)
{
    return (path.startsWith(QLatin1String("/api/v1/media/"))
            && (path.endsWith(QLatin1String("/poster"))
                || path.endsWith(QLatin1String("/backdrop"))))
        || path.startsWith(QLatin1String("/api/v1/stream/"));
}

/// The profile this client is acting as, for the routes that are profile-scoped.
///
/// The streaming route is *also* profile-scoped -- `require_stream_profile`
/// resolves it from `X-Profile-Id` -- and the request is made by ffmpeg inside
/// the media backend rather than by `ApiClient`, so there is no `send()` call
/// for this manager to add the header to.  Without it the server has no profile
/// to resolve, and answers 404 for a file it is holding.
bool pathNeedsProfile(const QString &path)
{
    return path.startsWith(QLatin1String("/api/v1/stream/"));
}

/// scheme://host:port, or an empty string for anything without one.
///
/// The port is part of it on purpose.  Origin comparison without the port
/// would treat `http://box:8484` and `http://box:9090` -- which on a home
/// network is very often the web UI of the router -- as the same place, and a
/// token would then be handed to whatever was listening on the other port.
QString originOf(const QUrl &url)
{
    if (!url.isValid() || url.host().isEmpty())
        return {};
    return url.scheme() + QLatin1String("://") + url.host() + QLatin1Char(':')
           + QString::number(url.port(-1));
}

}  // namespace

CredentialAccessManager::CredentialAccessManager(QObject *parent)
    : QNetworkAccessManager(parent)
{
}

void CredentialAccessManager::setCredentials(const QString &baseUrl,
                                             const QString &accessToken)
{
    m_origin = originOf(QUrl(baseUrl));
    m_accessToken = accessToken;
}

void CredentialAccessManager::clearCredentials()
{
    m_accessToken.clear();
    m_origin.clear();
}

void CredentialAccessManager::setProfileId(int profileId)
{
    if (m_profileId == profileId)
        return;
    m_profileId = profileId;
}

bool CredentialAccessManager::wouldAuthenticate(const QUrl &url) const
{
    if (m_accessToken.isEmpty() || m_origin.isEmpty())
        return false;
    return originOf(url) == m_origin
        && (pathNeedsCredential(url.path()) || pathNeedsProfile(url.path()));
}

QNetworkReply *CredentialAccessManager::createRequest(Operation operation,
                                                      const QNetworkRequest &request,
                                                      QIODevice *outgoingData)
{
    QNetworkRequest scoped = request;
    const QUrl url = scoped.url();

    if (operation == GetOperation && wouldAuthenticate(url)) {
        // A test asserting on a request is still a request, so the same
        // credential goes on it.  Skipping non-QNetworkAccessManager callers
        // left `X-Profile-Id` off the test's stream request and the server
        // answered 401 -- while telling nobody, because a test that only
        // asserts on a 206 never says who was supposed to be calling.
        if (!scoped.hasRawHeader("Authorization")) {
            scoped.setRawHeader("Authorization",
                                QByteArrayLiteral("Bearer ")
                                    + m_accessToken.toUtf8());
        }
        if (pathNeedsProfile(url.path()) && m_profileId > 0
            && !scoped.hasRawHeader("X-Profile-Id")) {
            scoped.setRawHeader("X-Profile-Id", QByteArray::number(m_profileId));
        }
    }
    return QNetworkAccessManager::createRequest(operation, scoped, outgoingData);
}

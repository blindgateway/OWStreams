#pragma once

#include <QNetworkAccessManager>
#include <QString>

/// A network manager that attaches a bearer token to the routes that need one.
///
/// `QQuickImage` and `QMediaPlayer` fetch over HTTP by URL and offer no way to
/// set a request header.  Their two options are a credential in the query string
/// or a credential-free endpoint -- and this server has neither: the poster
/// route is authenticated, and a token in a URL leaks into proxy logs, browser
/// history and any error message that prints the URL.
///
/// So the token goes in a header, attached here, and only here.
///
/// The scope is the point.  A manager that stamped the token on *every* request
/// would send this user's bearer credential to any host an `Image` was pointed
/// at -- including a third-party artwork URL from a metadata sidecar.  Instead
/// three questions must all be true before a token is attached:
///
/// * is the host the one we are signed in to (compared on host *and* port,
///   because http://box:8484 and http://box:9090 are different machines as far
///   as a credential is concerned);
/// * is the path one of the artwork or stream routes;
/// * is the request a GET, which is the only method those routes have.
///
/// Anything else goes out unauthenticated, which is correct for everything the
/// UI loads from elsewhere.
class CredentialAccessManager : public QNetworkAccessManager
{
    Q_OBJECT

public:
    explicit CredentialAccessManager(QObject *parent = nullptr);

    /// The token to attach, and the server it belongs to.  An empty token
    /// means "attach nothing", which is what makes this safe to leave attached:
    /// signing out does not have to reach in and disconnect it.
    void setCredentials(const QString &baseUrl, const QString &accessToken);
    void clearCredentials();

    QString accessToken() const { return m_accessToken; }

    /// The profile the streaming requests act as.  `QMediaPlayer` fetches
    /// through ffmpeg rather than through `ApiClient::send`, so there is no
    /// other place this header could be added -- and the stream route is
    /// profile-scoped, so without it the server answers 404.
    void setProfileId(int profileId);
    int profileId() const { return m_profileId; }


    /// Whether this manager would attach a token to `url`.  Exposed for the
    /// tests, because "would this request leak a credential" is a security
    /// property and should be asserted rather than reasoned about.
    Q_INVOKABLE bool wouldAuthenticate(const QUrl &url) const;

protected:
    QNetworkReply *createRequest(Operation operation,
                                 const QNetworkRequest &request,
                                 QIODevice *outgoingData) override;

private:
    /// The scheme://host:port we are signed in to, or an empty string.
    QString credentialOrigin() const { return m_origin; }

    QString m_origin;
    QString m_accessToken;
    int m_profileId = 0;
};

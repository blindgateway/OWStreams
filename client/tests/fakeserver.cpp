#include "fakeserver.h"

#include <QHostAddress>
#include <QTimer>

namespace {

/// How long to keep reading a connection before deciding there is nothing more.
///
/// Qt writes the header block and the body as separate socket writes, and a
/// `Content-Length: 0` body means the *second* write never comes.  Answering
/// only on the first `readyRead` therefore deadlocks every bodyless POST --
/// playback start and finish -- until the client times out and the test fails
/// with no signal at all.  One further turn of the event loop resolves it.
const int kSettleMs = 30;

}  // namespace

void FakeServer::incomingConnection(qintptr handle)
{
    auto *socket = new QTcpSocket(this);
    connect(socket, &QTcpSocket::readyRead, this, [this, socket] {
        QByteArray &buffer = m_buffers[socket];
        buffer.append(socket->readAll());
        if (!isCompleteRequest(buffer))
            return;                 // more bytes will follow
        const QByteArray request = buffer;
        m_buffers.remove(socket);
        respond(socket, request);
    });
    connect(socket, &QTcpSocket::disconnected, this, [socket] {
        socket->deleteLater();
    });
    socket->setSocketDescriptor(handle);
    // And this for a header block that arrives without a body: a timer is the
    // only way a socket that has said all it is going to say gets noticed.
    QTimer::singleShot(kSettleMs, socket, [this, socket] {
        if (m_buffers.contains(socket)) {
            const QByteArray request = m_buffers.take(socket);
            respond(socket, request);
        }
    });
}

bool FakeServer::isCompleteRequest(const QByteArray &request)
{
    const int headerEnd = request.indexOf("\r\n\r\n");
    if (headerEnd < 0)
        return false;
    const QByteArray headers = request.left(headerEnd).toLower();
    const int at = headers.indexOf("content-length:");
    int contentLength = 0;
    if (at >= 0) {
        const QByteArray value = headers.mid(at + 15);
        contentLength = value.left(value.indexOf('\r')).trimmed().toInt();
    }
    return request.size() >= headerEnd + 4 + contentLength;
}

int FakeServer::indexOfRequest(const QByteArray &expect, int fromIndex) const
{
    for (int i = qMax(0, fromIndex); i < m_requests.size(); ++i) {
        if (m_requests.at(i).startsWith(expect))
            return i;
    }
    return -1;
}

QByteArray FakeServer::request(int index) const
{
    return index < 0 || index >= m_requests.size() ? QByteArray{}
                                                   : m_requests.at(index);
}

QByteArray FakeServer::requestLine(int index) const
{
    const QByteArray recorded = request(index);
    return recorded.left(recorded.indexOf("\r\n"));
}

QByteArray FakeServer::lastResponse() const { return m_lastResponse; }

QByteArray FakeServer::headers(int index) const
{
    const QByteArray recorded = request(index);
    // Headers only: the request line says nothing a reader needs, and a failure
    // message that opens with it sends the reader to the wrong line.
    const int firstLine = recorded.indexOf("\r\n");
    const int headerEnd = recorded.indexOf("\r\n\r\n");
    if (firstLine < 0)
        return {};
    return headerEnd < 0 ? recorded.mid(firstLine + 2)
                         : recorded.mid(firstLine + 2, headerEnd - firstLine - 2);
}

QByteArray FakeServer::lastHeaders() const
{
    return headers(m_requests.size() - 1);
}

QByteArray FakeServer::requestHeader(int index, const QByteArray &name) const
{
    if (index < 0 || index >= m_requests.size())
        return {};
    return headerOf(m_requests.at(index), name);
}

QByteArray FakeServer::lastHeader(const QByteArray &name) const
{
    if (m_requests.isEmpty())
        return {};
    return headerOf(m_requests.last(), name);
}

QByteArray FakeServer::headerOf(const QByteArray &request, const QByteArray &name)
{
    // Normalised first, then split.  Lower-casing the whole request and *then*
    // searching the block for `name + ":"` was wrong in a way that cost a week:
    // `x-profile-id:` appears in more than one request (the one being asserted
    // and the next one), and which was found depended on the order they
    // happened to arrive in -- so an assertion read as missing for a header
    // that was plainly on the request being printed beside it.  Taking the
    // headers of *this* request first, and asking this request's lines, cannot
    // reach outside it.
    const int firstLine = request.indexOf("\r\n");
    if (firstLine < 0)
        return {};
    const int headerEnd = request.indexOf("\r\n\r\n");
    const QByteArray block =
        request.mid(firstLine + 2,
                    headerEnd < 0 ? request.size() - firstLine - 2
                                  : headerEnd - firstLine - 2);

    const QByteArray wanted = name.toLower() + ":";
    const auto lines = block.split('\n');
    for (const QByteArray &raw : lines) {
        QByteArray line = raw.trimmed().toLower();
        if (!line.startsWith(wanted))
            continue;
        return line.mid(wanted.size()).trimmed();
    }
    return {};
}

QByteArray FakeServer::lastBody() const
{
    if (m_requests.isEmpty())
        return {};
    const QByteArray request = m_requests.last();
    const int headerEnd = request.indexOf("\r\n\r\n");
    return headerEnd < 0 ? QByteArray{} : request.mid(headerEnd + 4);
}

void FakeServer::respond(QTcpSocket *socket, const QByteArray &request)
{
    m_requests.append(request);
    emit requestArrived(m_requests.size());
    if (m_silent) {
        socket->disconnectFromHost();
        return;
    }
    const int firstSpace = request.indexOf(' ');
    const int secondSpace = request.indexOf(' ', firstSpace + 1);
    QByteArray path = request.mid(firstSpace + 1, secondSpace - firstSpace - 1);
    // Routing ignores the query string.  Several routes take no path parameter
    // at all -- playback start and finish name their title by path and their
    // offset by query -- and a table keyed on the whole request target then
    // answers them 404, which reads as "the client built the wrong URL" when
    // what actually happened is that the table was keyed too precisely.
    const int query = path.indexOf('?');
    if (query >= 0)
        path.truncate(query);
    const auto answer = m_answers.value(QString::fromLatin1(path));
    m_lastResponse = "HTTP/1.1 " + QByteArray::number(
        answer.first == 0 ? 404 : answer.first) + " "
        + reasonPhrase(answer.first == 0 ? 404 : answer.first);
    if (answer.first == 0) {
        socket->write(m_lastResponse + "\r\nContent-Length: 0\r\n\r\n");
        socket->disconnectFromHost();
        return;
    }
    QByteArray response;
    response += m_lastResponse + "\r\n";
    response += "Content-Type: application/json\r\n";
    response += "Content-Length: " + QByteArray::number(answer.second.size())
                + "\r\n";
    response += "Connection: close\r\n\r\n";
    response += answer.second;
    socket->write(response);
    socket->disconnectFromHost();
}

QByteArray FakeServer::reasonPhrase(int status)
{
    switch (status) {
    case 200: return "OK";
    case 401: return "Unauthorized";
    case 403: return "Forbidden";
    case 404: return "Not Found";
    case 410: return "Gone";
    case 416: return "Range Not Satisfiable";
    case 422: return "Unprocessable Content";
    case 429: return "Too Many Requests";
    default:  return "Status";
    }
}
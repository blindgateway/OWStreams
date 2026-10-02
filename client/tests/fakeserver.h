#pragma once

#include <QByteArray>
#include <QHash>
#include <QList>
#include <QPair>
#include <QTcpServer>
#include <QTcpSocket>

/// A loopback HTTP server answering a fixed routing table.
///
/// Hand-rolled rather than built on Qt's HTTP server module: the point is to
/// answer a handful of endpoints and, on demand, to answer *nothing* -- which
/// is how the "server is unreachable" and "logout lost its network" paths get
/// exercised without unplugging anything.
///
/// It records every request it answered, headers and body both, because
/// "the client sent the wrong thing" is invisible from the response alone: the
/// server's reply is the same for a body that is missing a field as for one
/// that has an extra.
class FakeServer : public QTcpServer
{
    Q_OBJECT

public:
    using Answer = QPair<int, QByteArray>;

    void setAnswers(const QHash<QString, Answer> &answers) { m_answers = answers; }
    void addAnswer(const QString &path, const Answer &answer)
    {
        m_answers.insert(path, answer);
    }

    /// Accept the connection and then say nothing.  The client's own timeout is
    /// what ends the request, which is the behaviour a black-holed network has.
    void setSilent(bool silent) { m_silent = silent; }

    /// Every request received, in order, headers and body concatenated.
    QList<QByteArray> requests() const { return m_requests; }
    void clearRequests() { m_requests.clear(); }

    /// The most recent request line, e.g. ``PUT /api/v1/media/7/progress``.
    /// Empty when nothing has arrived.
    QByteArray lastRequestLine() const
    {
        return m_requests.isEmpty() ? QByteArray{} : m_requests.last().left(
            m_requests.last().indexOf("\r\n"));
    }

    /// One header of the most recent request, lower-cased name.
    QByteArray lastHeader(const QByteArray &name) const;
    /// Every header of the most recent request, `name: value`, for a failure
    /// message.  Debugging "the header was absent" needs the whole request
    /// shown -- and it was shown, but by a printf that printed its own
    /// placeholders literally.
    QByteArray lastHeaders() const;
    /// The body of the most recent request.
    QByteArray lastBody() const;

    /// The index of the first recorded request at or after `fromIndex` whose
    /// line starts with `expect`, or -1.
    ///
    /// Indexed rather than returning the request, because "the header of the
    /// request I asked for" and "the header of whatever arrived most recently"
    /// are different questions -- and the second one is answered by the *next*
    /// request the client happens to make, which is how a profile header reads
    /// as absent when it was sent.
    int indexOfRequest(const QByteArray &expect, int fromIndex = 0) const;
    /// One header of the request at `index`, lower-cased name.
    QByteArray requestHeader(int index, const QByteArray &name) const;
    /// The whole recorded request at `index`.
    QByteArray request(int index) const;
    /// The request line of the request at `index`.
    QByteArray requestLine(int index) const;
    /// The header block of the request at `index`, for a failure message.
    QByteArray headers(int index) const;
    /// The status line of the most recent response, e.g. ``HTTP/1.1 403
    /// Forbidden``.  Empty when nothing has been answered.
    ///
    /// Recorded rather than read off the connection, so a test can say what the
    /// server *decided*: a 403 is the opposite of a silent socket, and the two
    /// look identical from the client's side.
    QByteArray lastResponse() const;

protected:
    void incomingConnection(qintptr handle) override;

signals:
    /// Emitted once per request *after* it is recorded, with the new count.
    ///
    /// The alternative -- polling `requests().size()` from the test -- races
    /// the record against the reply.  A test that sees the count and then reads
    /// the list can be overtaken between the two, and does this loop: it never
    /// sees its own request, so it times out and reports the header as absent
    /// on a request that arrived.  Driving the wait off this signal removes the
    /// race outright.
    void requestArrived(int count);

private:
    /// True once the buffer holds a whole request: headers, blank line, and as
    /// many body bytes as `Content-Length` promised.
    ///
    /// Reading on the first `readyRead` instead is the bug this exists to
    /// avoid -- Qt sends headers and body as two writes, so one readyRead
    /// often carries only the headers, and answering then is answering half a
    /// request.  The first version of this fake left every test stuck in
    /// "Connecting".
    static bool isCompleteRequest(const QByteArray &request);

    void respond(QTcpSocket *socket, const QByteArray &request);
    static QByteArray reasonPhrase(int status);
    /// One header of `request`, lower-cased name.  Empty when absent.
    ///
    /// Scans the header lines one at a time rather than searching the block as
    /// a string: see the note on the definition, which records the two
    /// plausible-looking implementations that were both wrong.
    static QByteArray headerOf(const QByteArray &request, const QByteArray &name);

    QHash<QString, Answer> m_answers;
    QHash<QTcpSocket *, QByteArray> m_buffers;
    QList<QByteArray> m_requests;
    QByteArray m_lastResponse;
    bool m_silent = false;
};

/// A status and body, for `setAnswers`.
inline FakeServer::Answer answer(int status, const QByteArray &json)
{
    return {status, json};
}
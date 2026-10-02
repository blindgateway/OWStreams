#include "serverfixture.h"

#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QProcessEnvironment>
#include <QStandardPaths>

namespace {

/// The environment every child of the fixture needs.
///
/// `no_proxy` is the important one.  A lot of build machines have `http_proxy`
/// set, and a request to `127.0.0.1` that goes to the proxy fails in a way
/// that looks nothing like a proxy problem: the server starts, answers its own
/// health check, and is then unreachable from everything else including the
/// tests.  Setting it once here rather than at each call site means a new child
/// process cannot forget.
QProcessEnvironment childEnvironment()
{
    QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    environment.insert(QStringLiteral("no_proxy"),
                       QStringLiteral("127.0.0.1,localhost,::1"));
    environment.insert(QStringLiteral("NO_PROXY"),
                       QStringLiteral("127.0.0.1,localhost,::1"));
    return environment;
}

QString pythonExecutable()
{
    return QStandardPaths::findExecutable(QStringLiteral("python3"));
}

}  // namespace

ServerFixture::ServerFixture(const QString &scratchDir, const QString &harnessScript)
    : m_scratch(scratchDir), m_harnessScript(harnessScript)
{
}

bool ServerFixture::runHarness(const QStringList &arguments, QString *output,
                               QString *error) const
{
    const QString python = pythonExecutable();
    if (python.isEmpty()) {
        if (error)
            *error = QStringLiteral("python3 is not on PATH");
        return false;
    }

    QProcess process;
    process.setProcessEnvironment(childEnvironment());
    process.start(python, arguments);
    if (!process.waitForStarted(15000)) {
        if (error)
            *error = QStringLiteral("could not start %1: %2")
                         .arg(python, process.errorString());
        return false;
    }
    if (!process.waitForFinished(240000)) {
        process.kill();
        process.waitForFinished(5000);
        if (error)
            *error = QStringLiteral("harness.py did not finish within four minutes");
        return false;
    }
    if (output)
        *output = QString::fromUtf8(process.readAllStandardOutput());
    if (error)
        *error = QString::fromUtf8(process.readAllStandardError());
    return process.exitStatus() == QProcess::NormalExit && process.exitCode() == 0;
}

bool ServerFixture::start(QString *skipReason)
{
    if (QStandardPaths::findExecutable(QStringLiteral("ffmpeg")).isEmpty()) {
        // The fixture needs a real, decodable clip.  Random bytes would only
        // exercise the error paths, and a suite of twenty failures that all
        // mean "no ffmpeg" is worse than an honest skip.
        if (skipReason)
            *skipReason = QStringLiteral("ffmpeg is not on PATH");
        return false;
    }
    if (!QFile::exists(m_harnessScript)) {
        if (skipReason)
            *skipReason =
                QStringLiteral("harness.py was not found at %1").arg(m_harnessScript);
        return false;
    }

    // Removed first rather than reused: a stale `server.pid` from a previous
    // run would make `down` at the end signal a process that is long gone, and
    // the database inside would be the previous run's.
    stop();

    QString output;
    QString error;
    if (!runHarness({m_harnessScript, QStringLiteral("up"),
                     QStringLiteral("--dir"), m_scratch.absolutePath()},
                    &output, &error)) {
        if (skipReason) {
            *skipReason = QStringLiteral("the fixture did not start.\n%1\n%2")
                              .arg(error.trimmed(), output.trimmed())
                              .trimmed();
        }
        return false;
    }

    // `up` prints one JSON line.  Parsing the whole stdout is deliberate: a
    // partial line would produce an empty URL, and the connection error that
    // follows would look like a client bug rather than a fixture one.
    const QJsonObject fixture =
        QJsonDocument::fromJson(output.trimmed().toUtf8()).object();
    m_url = fixture.value(QStringLiteral("url")).toString();
    m_username = fixture.value(QStringLiteral("username")).toString();
    m_password = fixture.value(QStringLiteral("password")).toString();
    m_root = fixture.value(QStringLiteral("root")).toString();
    if (m_url.isEmpty() || m_username.isEmpty()) {
        if (skipReason)
            *skipReason = QStringLiteral("the fixture printed no usable JSON: %1")
                              .arg(output.trimmed());
        return false;
    }
    return true;
}

void ServerFixture::stop()
{
    if (!QFile::exists(m_harnessScript))
        return;
    // `down` stops the server and removes the directory.  Its absence is not an
    // error: `start` calls this to clear a previous run, and there may not be
    // one.
    runHarness({m_harnessScript, QStringLiteral("down"),
                QStringLiteral("--dir"), m_scratch.absolutePath()},
               nullptr, nullptr);
}

QString ServerFixture::serverLog() const
{
    QFile log(m_scratch.absoluteFilePath(QStringLiteral("server.log")));
    if (!log.open(QIODevice::ReadOnly | QIODevice::Text))
        return {};
    return QString::fromUtf8(log.readAll());
}

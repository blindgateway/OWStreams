#pragma once

#include <QDir>
#include <QString>

/// A live Stream server, stood up by `harness.py`, for the end-to-end tests.
///
/// The point is that it is a *server* and not a stand-in for one: a real
/// configuration file, a real SQLite database with its own signing key, a real
/// account, and a library root holding a real decodable clip. Everything the
/// client does afterwards goes over HTTP against it, because the failures that
/// survive both unit suites live at the seam -- a resume offset that never
/// leaves the client, a Range header the server ignores, a profile header that
/// arrives on the wrong paths. A canned response table cannot catch any of
/// those.
///
/// One server for the whole executable, not one per test. It costs a process
/// and a database either way, and no test here changes the library in a way
/// that would leak into the next; standing one up per test would make the suite
/// slower than the code it tests.
///
/// Skipped rather than failed when it cannot be built: the tests need ffmpeg to
/// make a real clip, and a machine without it should say so rather than report
/// twenty failures that all mean "no server".
class ServerFixture
{
public:
    ServerFixture(const QString &scratchDir, const QString &harnessScript);

    /// Start the server and wait for it to answer.  `skipReason` is filled in
    /// when it could not be started, so the test can skip rather than fail.
    bool start(QString *skipReason);

    /// Stop it and delete the scratch directory.  Safe to call twice.
    void stop();

    QString url() const { return m_url; }
    QString username() const { return m_username; }
    QString password() const { return m_password; }
    /// The library root the fixture imported into.
    QString root() const { return m_root; }
    /// Everything the server wrote to its log.  Used in a failure message, so
    /// that a red test says what the server thought rather than only what the
    /// client saw.
    QString serverLog() const;

private:
    bool runHarness(const QStringList &arguments, QString *output, QString *error) const;

    QDir m_scratch;
    QString m_harnessScript;   ///< where harness.py lives in the source tree
    QString m_url;
    QString m_username;
    QString m_password;
    QString m_root;
};

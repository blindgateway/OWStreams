// Every screen the shell names, and the work it has to start.
//
// This is the one suite that can see a *screen* without rendering it. A QML
// file is a tree of property bindings and handlers; the handlers are what call
// the C++ this project wrote, and a screen whose calls are misspelled, aimed at
// the wrong object, or missing entirely is a screen that opens and shows
// nothing. Nothing else here looks for those. The unit suites test the
// controllers in isolation, the end-to-end suite drives the controllers and not
// the QML, and `qmllint` resolves types and properties but says nothing about
// whether a handler calls anything real.
//
// So: read each screen's source and check that every method a handler calls is
// one this project declares -- in C++, in a sibling QML file's `function`, or in
// the QML language itself. Read rather than instantiate, because a component
// that needs a `QGuiApplication` cannot be created from a guiless test, and the
// names in the source are what is being checked anyway.
//
// What it has found, each of them silent at run time:
//
// * `HomeScreenModel::rails()` called as a method where it is a `QList`
//   property -- two TypeErrors per frame and an empty home screen.
// * `Shell`'s PlayerScreen wired every property except `fullscreenRequested`,
//   which the player emits for F and Escape, so both keys did nothing.
// * The detail screen read a `rating` field the server does not send; the score
//   is `rating_value` and its origin is `rating_source`.

#include <QtTest>

#include <QDir>
#include <QFile>
#include <QRegularExpression>
#include <QSet>

namespace {

/// Every screen the shell names.  Checked against `Shell.qml` by a test of its
/// own below, because a list of screens that drifts from the shell is a list of
/// screens nobody runs.
const QStringList kScreens = {
    QStringLiteral("screens/ServerScreen.qml"),
    QStringLiteral("screens/LoginScreen.qml"),
    QStringLiteral("screens/ProfileScreen.qml"),
    QStringLiteral("screens/HomeScreen.qml"),
    QStringLiteral("screens/MediaGridScreen.qml"),
    QStringLiteral("screens/SearchScreen.qml"),
    QStringLiteral("screens/DetailScreen.qml"),
    QStringLiteral("screens/PlayerScreen.qml"),
    QStringLiteral("screens/SettingsScreen.qml"),
    QStringLiteral("ui/FilterBar.qml"),
    QStringLiteral("ui/PosterRail.qml"),
    QStringLiteral("ui/PosterCard.qml"),
    QStringLiteral("ui/ChangePasswordScreen.qml"),
    QStringLiteral("ui/StatusPane.qml"),
    QStringLiteral("ui/NavItem.qml"),
};

/// The C++ whose methods a handler may call.  Each header is read for
/// `Q_PROPERTY(... READ name ...)`, `Q_INVOKABLE ... name(`, `signals:`, and
/// public slots -- so the set is what the class offers, not what this test
/// remembered.
const QStringList kCppHeaders = {
    QStringLiteral("../src/apiclient.h"),
    QStringLiteral("../src/appcontroller.h"),
    QStringLiteral("../src/sessioncontroller.h"),
    QStringLiteral("../src/playbackcontroller.h"),
    QStringLiteral("../src/librarymodel.h"),
    QStringLiteral("../src/homescreenmodel.h"),
    QStringLiteral("../src/credentialstore.h"),
    QStringLiteral("../src/captureguard.h"),
};

/// The QML language's own calls, plus the ones a screen legitimately makes on a
/// model object.  Listed rather than discovered: discovery is the thing this
/// suite exists to catch.
const QStringList kQmlBuiltins = {
    QStringLiteral("requestPaint"),   QStringLiteral("clear"),
    QStringLiteral("append"),         QStringLiteral("count"),
    QStringLiteral("remove"),         QStringLiteral("push"),
    QStringLiteral("callLater"),      QStringLiteral("createObject"),
    QStringLiteral("createComponent"), QStringLiteral("destroy"),
    QStringLiteral("release"),        QStringLiteral("trim"),
    QStringLiteral("split"),          QStringLiteral("join"),
    QStringLiteral("filter"),         QStringLiteral("map"),
    QStringLiteral("forEach"),        QStringLiteral("indexOf"),
    QStringLiteral("includes"),       QStringLiteral("replace"),
    QStringLiteral("toUpperCase"),    QStringLiteral("toLowerCase"),
    QStringLiteral("substring"),      QStringLiteral("charAt"),
    QStringLiteral("startsWith"),     QStringLiteral("endsWith"),
    QStringLiteral("toFixed"),        QStringLiteral("toString"),
    QStringLiteral("toNumber"),       QStringLiteral("parseInt"),
    QStringLiteral("parseFloat"),     QStringLiteral("keys"),
    QStringLiteral("values"),         QStringLiteral("warn"),
    QStringLiteral("error"),          QStringLiteral("log"),
    QStringLiteral("arg"),            QStringLiteral("arg"),
    QStringLiteral("forceActiveFocus"), QStringLiteral("setProperty"),
    QStringLiteral("property"),       QStringLiteral("createObject"),
};

QString qmlRoot()
{
    // The suite's own source directory, not the build directory: the module
    // compiles the QML into a resource, and reading the *source* is what makes
    // this a check on the files a person edits.
    return QDir(QStringLiteral(STREAM_TESTCASE_SOURCEDIR))
        .absoluteFilePath(QStringLiteral(".."));
}

QString sourcePath(const QString &relative)
{
    return QDir(qmlRoot()).absoluteFilePath(QStringLiteral("qml/") + relative);
}

QStringList readLines(const QString &path, bool *ok = nullptr)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        if (ok)
            *ok = false;
        return {};
    }
    if (ok)
        *ok = true;
    return QString::fromUtf8(file.readAll()).split(QLatin1Char('\n'));
}

/// Every method name the C++ offers: `Q_PROPERTY` readers, invokables, slots
/// and signals.
QSet<QString> cppCallables()
{
    QSet<QString> names;
    const QRegularExpression property(
        QStringLiteral("Q_PROPERTY\\s*\\([^)]*\\bREAD\\s+([a-zA-Z_][A-Za-z0-9_]*)"));
    const QRegularExpression method(
        QStringLiteral("^\\s*(?:Q_INVOKABLE\\s+)?(?:static\\s+)?"
                       "[A-Za-z_][A-Za-z0-9_:<>,\\* ]*?\\s+"
                       "([a-z][A-Za-z0-9_]*)\\s*\\("));

    for (const QString &header : kCppHeaders) {
        for (const QString &line : readLines(sourcePath(header))) {
            const QRegularExpressionMatch propertyMatch = property.match(line);
            if (propertyMatch.hasMatch()) {
                names.insert(propertyMatch.captured(1));
                continue;
            }
            const QRegularExpressionMatch methodMatch = method.match(line);
            if (methodMatch.hasMatch())
                names.insert(methodMatch.captured(1));
        }
    }
    return names;
}

/// Every `function name(` and every `signal name(` the QML module declares.
///
/// Signals too, because a handler emits one with exactly the syntax it calls a
/// method with -- `onActivated: screen.titleActivated(id)`.  Leaving them out
/// reported all seventeen of the screens' own signals as undeclared, which is
/// how a first run of this suite produced a list longer than the defect it was
/// looking for.
QSet<QString> qmlFunctions()
{
    QSet<QString> names;
    const QRegularExpression own(
        QStringLiteral("^\\s*function\\s+([a-z][A-Za-z0-9_]*)\\s*\\("));
    const QRegularExpression signal(
        QStringLiteral("^\\s*signal\\s+([a-z][A-Za-z0-9_]*)"));
    const QStringList qmlDir = {
        QStringLiteral("."), QStringLiteral("ui"), QStringLiteral("screens"),
    };
    for (const QString &dir : qmlDir) {
        const QDir folder(qmlRoot() + QStringLiteral("/qml/") + dir);
        const QStringList files =
            folder.entryList({QStringLiteral("*.qml")}, QDir::Files, QDir::Name);
        for (const QString &name : files) {
            for (const QString &line : readLines(folder.absoluteFilePath(name))) {
                const QRegularExpressionMatch match = own.match(line);
                if (match.hasMatch()) {
                    names.insert(match.captured(1));
                    continue;
                }
                const QRegularExpressionMatch emitted = signal.match(line);
                if (emitted.hasMatch())
                    names.insert(emitted.captured(1));
            }
        }
    }
    return names;
}

/// Every `object.method(` a handler calls, keyed by method, with the file and
/// line that asked for it.
QList<QPair<QString, QString>> handlerCalls()
{
    QList<QPair<QString, QString>> calls;
    const QRegularExpression handler(
        QStringLiteral("^\\s*on[A-Z][A-Za-z0-9]*\\s*:"));
    const QRegularExpression call(
        QStringLiteral("\\b([a-z][A-Za-z0-9_]*)\\.([A-Za-z_][A-Za-z0-9_]*)\\s*\\("));

    for (const QString &screen : kScreens) {
        const QStringList lines = readLines(sourcePath(screen));
        for (int number = 0; number < lines.size(); ++number) {
            if (!handler.match(lines.at(number)).hasMatch())
                continue;
            auto it = call.globalMatch(lines.at(number));
            while (it.hasNext()) {
                const QRegularExpressionMatch one = it.next();
                calls.append({one.captured(2),
                              QStringLiteral("%1:%2").arg(screen).arg(number + 1)});
            }
        }
    }
    return calls;
}

}  // namespace

class ScreenRoutesTest : public QObject
{
    Q_OBJECT

private slots:
    void everyScreenInTheShellIsChecked();
    void everyHandlerCallNamesSomethingThisProjectDeclares();
    void thePlayerEmitsFullscreenAndSomethingHandlesIt();
    void detailScreenReadsTheFieldsTheServerSends();
};

void ScreenRoutesTest::everyScreenInTheShellIsChecked()
{
    bool ok = false;
    const QStringList shell =
        readLines(QDir(qmlRoot()).absoluteFilePath(QStringLiteral("qml/Shell.qml")), &ok);
    QVERIFY2(ok, "Shell.qml could not be read");
    const QString signedIn = shell.join(QLatin1Char('\n'));

    // The gate -- server, login, forced password change, profile picker --
    // belongs to `Main.qml`, and the shell only holds what is inside it.
    const QStringList gate =
        readLines(QDir(qmlRoot()).absoluteFilePath(QStringLiteral("qml/Main.qml")), &ok);
    QVERIFY2(ok, "Main.qml could not be read");
    const QString unsignedIn = gate.join(QLatin1Char('\n'));

    for (const QString &screen : kScreens) {
        QVERIFY2(QFileInfo::exists(sourcePath(screen)),
                 qPrintable(QStringLiteral("%1 is listed here but does not "
                                           "exist").arg(screen)));
    }
    for (const QString &type : {QStringLiteral("ServerScreen"),
                                QStringLiteral("LoginScreen"),
                                QStringLiteral("ChangePasswordScreen"),
                                QStringLiteral("ProfileScreen")}) {
        QVERIFY2(unsignedIn.contains(type),
                 qPrintable(QStringLiteral(
                     "Main.qml never mentions %1, so this suite does not check "
                     "it").arg(type)));
    }
    for (const QString &type : {QStringLiteral("HomeScreen"),
                                QStringLiteral("MediaGridScreen"),
                                QStringLiteral("SearchScreen"),
                                QStringLiteral("DetailScreen"),
                                QStringLiteral("PlayerScreen"),
                                QStringLiteral("SettingsScreen")}) {
        QVERIFY2(signedIn.contains(type),
                 qPrintable(QStringLiteral(
                     "Shell.qml never mentions %1, so this suite does not check "
                     "it").arg(type)));
    }
}

void ScreenRoutesTest::everyHandlerCallNamesSomethingThisProjectDeclares()
{
    QSet<QString> known = cppCallables() | qmlFunctions();
    for (const QString &builtin : kQmlBuiltins)
        known.insert(builtin);

    const QList<QPair<QString, QString>> calls = handlerCalls();
    QVERIFY2(!calls.isEmpty(),
             "no handler calls were found, so this suite is not looking at "
             "anything");

    QStringList unknown;
    for (const auto &call : calls) {
        if (!known.contains(call.first)) {
            unknown.append(QStringLiteral("%1 calls %2()")
                               .arg(call.second, call.first));
        }
    }
    QVERIFY2(unknown.isEmpty(),
             qPrintable(QStringLiteral("a screen calls something this project "
                                       "does not declare:\n  %1")
                            .arg(unknown.join(QStringLiteral("\n  ")))));
}

void ScreenRoutesTest::thePlayerEmitsFullscreenAndSomethingHandlesIt()
{
    // A signal nobody handles is a button that does nothing, and Qt is silent
    // about it -- no warning, no error.  The player emits
    // `fullscreenRequested` for the F key and Escape; if nothing connects it,
    // both keys are dead and nothing says so.
    const QString player =
        readLines(sourcePath(QStringLiteral("screens/PlayerScreen.qml"))).join(QLatin1Char('\n'));
    QVERIFY2(player.contains(QStringLiteral("signal fullscreenRequested")),
             "the player no longer offers fullscreen");

    const QString shell = readLines(
        QDir(qmlRoot()).absoluteFilePath(QStringLiteral("qml/Shell.qml")))
                              .join(QLatin1Char('\n'));
    QVERIFY2(shell.contains(QStringLiteral("onFullscreenRequested")),
             qPrintable(QStringLiteral(
                 "the player emits fullscreenRequested and nothing handles it, "
                 "so F and Escape do nothing")));

    // The handler must reach a real toggle, not a `// TODO`.  Called on a
    // window, the correct implementation is the only one that makes sense:
    // `visibility` is Window/WindowState, an `ApplicationWindow` exposes
    // nothing else that means full-screen, and the same key is the Escape key.
    // Anything else -- a boolean nobody reads, a method that does not exist --
    // would pass the check above and do nothing on a television, which is where
    // F and Escape are pressed.
    // Both directions, because the two halves fail differently: a handler that
    // only ever goes *in* leaves a television viewer with no way out, and one
    // that only ever comes *out* does nothing at all.
    QVERIFY2(shell.contains(QStringLiteral("Window.FullScreen")),
             qPrintable(QStringLiteral(
                 "the fullscreen handler never names Window.FullScreen; see "
                 "this test for why anything else is not a fullscreen")));
    QVERIFY2(shell.contains(QStringLiteral("Window.Windowed")),
             qPrintable(QStringLiteral(
                 "the fullscreen handler cannot leave full screen: on a "
                 "television, where a remote has no other way out of a film, "
                 "that is the difference between finishing it and restarting "
                 "the app")));
}

void ScreenRoutesTest::detailScreenReadsTheFieldsTheServerSends()
{
    // The detail object is the server's media row verbatim, and `MEDIA_PUBLIC_COLUMNS`
    // in `backend/streamserver/db/repositories.py` is where those names live.
    const QString detail =
        readLines(sourcePath(QStringLiteral("screens/DetailScreen.qml")))
            .join(QLatin1Char('\n'));

    const QStringList fields = {
        QStringLiteral("title"),           QStringLiteral("original_title"),
        QStringLiteral("overview"),        QStringLiteral("year"),
        QStringLiteral("runtime_minutes"), QStringLiteral("genres"),
        QStringLiteral("poster_key"),
        QStringLiteral("rating_value"),    QStringLiteral("rating_source"),
        QStringLiteral("release_date"),    QStringLiteral("file_size_bytes"),
        QStringLiteral("added_at"),        QStringLiteral("updated_at"),
        QStringLiteral("parental_rating"), QStringLiteral("container"),
        QStringLiteral("video_codec"),     QStringLiteral("audio_codec"),
        QStringLiteral("audio_languages"), QStringLiteral("subtitle_languages"),
        QStringLiteral("hdr_format"),      QStringLiteral("frame_rate"),
        QStringLiteral("width"),           QStringLiteral("height"),
        QStringLiteral("director"),        QStringLiteral("cast"),
    };
    for (const QString &field : fields) {
        QVERIFY2(detail.contains(field),
                 qPrintable(QStringLiteral(
                     "DetailScreen.qml never reads %1, so the detail screen "
                     "cannot show it").arg(field)));
    }

    // And the one that is *not* a column.  `rating_value` is the score and
    // `rating_source` its origin; there is no `rating`, and a binding that reads
    // one is a binding that renders `undefined` -- silently, because an
    // undefined read is not an error.
    QVERIFY2(!detail.contains(QStringLiteral("media.rating\n"))
                 && !detail.contains(QStringLiteral("media.rating ")),
             "the detail screen reads a `rating` field the server does not "
             "send; the score is `rating_value` and the origin is "
             "`rating_source`");
}

QTEST_GUILESS_MAIN(ScreenRoutesTest)
#include "tst_screenroutes.moc"
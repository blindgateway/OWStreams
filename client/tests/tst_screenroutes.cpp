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
// * The player's next/previous reached three handlers and performed nothing.
// * And five features the brief names that the client could already do and no
//   screen offered: ratings, library refresh, library paging, clearing a
//   search, and reloading the home screen.  The library paging one is the
//   reason this suite earns its keep -- a `GridView` sized to its own content
//   never fires `onReached`, so `loadMoreLibrary` had no caller and a library
//   of a thousand titles stopped at sixty, with no error and no empty state.
//
// Read the sources, do not render them, and check both directions: that every
// call a handler makes resolves (`everyHandlerCallNamesSomethingThisProject
// Declares`), and that every feature the brief names is offered somewhere
// (`everyControllerCallIsReachableFromAScreen`).  The second direction is the
// one that finds the features above; the first cannot, because a feature that
// nobody calls has nothing misspelled about it.

#include <QtTest>

#include <QDir>
#include <QFile>
#include <QRegularExpression>
#include <QSet>

#include <algorithm>

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
    //
    // The name says "root" rather than "sourcedir" because it is also used as
    // the working directory when ctest runs a suite; that one is
    // `QT_TESTCASE_BUILDDIR`, not this.  `theScreensAreWhereTheMacroSays` below
    // asserts that what this returns really is the directory holding `qml/`,
    // because a wrong root makes every read empty and an empty read is not an
    // error: the last three defects in this file all presented as "this
    // feature is missing" when the file had never been opened.
    return QStringLiteral(STREAM_TESTCASE_QMLROOT);
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
    void theScreensAreWhereTheMacroSays();
    void everyScreenInTheShellIsChecked();
    void everyHandlerCallNamesSomethingThisProjectDeclares();
    void thePlayerEmitsFullscreenAndSomethingHandlesIt();
    void thePlayerEmitsNextAndPreviousAndSomethingHandlesThoseToo();
    void detailScreenReadsTheFieldsTheServerSends();
    void everyControllerCallIsReachableFromAScreen();
};

void ScreenRoutesTest::theScreensAreWhereTheMacroSays()
{
    // The guard on every other test in this file.
    //
    // Five separate defects in this suite presented as "the brief's features
    // are all missing", and the common cause was that no QML file was being
    // read at all: the compile definition had no quotes in it, so the macro
    // expanded inside a `QStringLiteral` to something that was not a string
    // literal; `readLines` returns an empty list for a path that is not there,
    // and an empty list is not an error.  Every test then reported the honest
    // consequence -- it found nothing -- as if the application were missing
    // fifteen features.
    //
    // So: the root is named, it exists, and `qml/` is inside it.  Fail here and
    // the next failures are worth reading; pass here and the next failures are
    // about the QML.
    const QString root = qmlRoot();
    QVERIFY2(!root.isEmpty(),
             "the compile definition that names the QML directory is empty, so "
             "every read below returns nothing and every result is a lie");
    QVERIFY2(QFileInfo::exists(root),
             qPrintable(QStringLiteral("the QML root does not exist: %1").arg(root)));
    QVERIFY2(QFileInfo(root).isDir(),
             qPrintable(QStringLiteral("the QML root is not a directory: %1").arg(root)));
    QVERIFY2(QFileInfo::exists(sourcePath(QStringLiteral("Shell.qml"))),
             qPrintable(QStringLiteral(
                 "Shell.qml is not under %1, so nothing in this suite is reading "
                 "the screens").arg(root)));
}

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

void ScreenRoutesTest::thePlayerEmitsNextAndPreviousAndSomethingHandlesThoseToo()
{
    // Next and previous were the brief's list, and they were *reached* three
    // times and *performed* zero: the player forwarded the controller's requests
    // to its own signals, the shell connected none of them, and the transport
    // row's two buttons were ten-second seeks.  Nobody pressing anything could
    // have found that out.
    const QString player =
        readLines(sourcePath(QStringLiteral("screens/PlayerScreen.qml"))).join(QLatin1Char('\n'));
    const QString shell = readLines(
        QDir(qmlRoot()).absoluteFilePath(QStringLiteral("qml/Shell.qml")))
                              .join(QLatin1Char('\n'));

    QVERIFY2(player.contains(QStringLiteral("signal nextRequested()")),
             "the player no longer offers a next title");
    QVERIFY2(player.contains(QStringLiteral("signal previousRequested()")),
             "the player no longer offers a previous title");
    QVERIFY2(shell.contains(QStringLiteral("onNextRequested")),
             "the player emits nextRequested and nothing handles it");
    QVERIFY2(shell.contains(QStringLiteral("onPreviousRequested")),
             "the player emits previousRequested and nothing handles it");

    // And the buttons are bound to them, not left as ten-second seeks wearing
    // the same icons.  `seekRelative(10000)` may not appear beside the forward
    // button -- it is still reachable by the wheel and the right arrow, which is
    // where a ten-second seek belongs.
    QVERIFY2(!player.contains(QStringLiteral("onClicked: if (screen.controller) "
                                             "screen.controller.seekRelative(10000)")),
             "the forward button is still a ten-second seek; next/previous "
             "have to be reachable from the transport, not only from the keys");
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

void ScreenRoutesTest::everyControllerCallIsReachableFromAScreen()
{
    // The other direction.  The test above asks whether a screen's calls
    // resolve; this asks whether a *feature* the client exposes is offered
    // anywhere -- because a method nobody calls is a feature that does not
    // exist, and the brief lists features rather than methods.
    //
    // This is a named list, and it is named for the reason it exists: the first
    // version derived it from `AppController`'s public slots and reported all
    // thirty of them, because the navigation is `shell.navigate()` switching on
    // a key rather than a call per method.  A test that lists everything is a
    // test that reports everything.  So each entry below is a feature the brief
    // names, and each says where it is reachable from.
    //
    // The features below are the brief's, and each was found missing when this
    // test first ran against the screens.  They are kept as a list rather than
    // derived, because a derived list is a list of every public slot, and
    // every public slot includes the ones the shell calls from `navigate()`.
    //
    // * `setRating` -- the server stored ratings and `AppController::setRating`
    //   sent them, and no screen anywhere called it.  Ratings were writeable by
    //   the API and unreachable by the application.
    // * `refreshLibrary`, `loadMoreLibrary` -- the library's only two ways of
    //   changing.  `loadMoreLibrary` had no caller at all: the `GridView` is
    //   sized to its content so the *outer* `Flickable` scrolls, and a
    //   `GridView` that nothing scrolls never fires `onReached`.  A thousand
    //   titles were sixty.
    // * `clearSearch` -- the only way out of a search was deleting the query
    //   one character at a time.
    // * `refresh` -- reachable from the home screen *model*, not the
    //   controller: the screen is handed no controller.
    // * The three `setLibrary*` -- the FilterBar assigned to read-only
    //   properties, which Qt discarded with a warning nobody reads.  The
    //   setters existed, were tested, and nothing called them; the properties
    //   are now writeable and a write *is* the request.
    struct Feature { const char *name; const char *where; };
    const QList<Feature> features = {
        {"setRating", "DetailScreen"},
        {"toggleFavorite", "DetailScreen"},
        {"openDetail", "Shell"},
        {"openPlayer", "Shell"},
        {"back", "Shell"},
        {"refresh", "HomeScreen"},
        {"refreshLibrary", "MediaGridScreen"},
        {"loadMoreLibrary", "MediaGridScreen"},
        {"clearSearch", "SearchScreen"},
        {"playNextInList", "Shell"},
        {"playPreviousInList", "Shell"},
        {"clearLibraryFilters", "MediaGridScreen"},
    };

    // A filter, which is *not* a method call.
    //
    // The three library filters were read-only `Q_PROPERTY`s beside a
    // `setLibrarySort`-style slot, and `MediaGridScreen` assigned to them:
    // `onSortRequested: function (sort) { screen.controller.librarySort = sort }`.
    // Qt discarded that assignment with a warning nobody reads, so narrowing
    // the library to 1994 filtered nothing while the setter and its test both
    // passed.  The properties are writeable now and the assignment is the
    // request -- so the screen has nothing left to call, and a test that
    // insists it call something would send someone looking for a method to
    // re-introduce the one that was not being used.
    struct Assignment { const char *where; const char *property; };
    const QList<Assignment> assignments = {
        {"MediaGridScreen", "librarySort"},
        {"MediaGridScreen", "libraryGenre"},
        {"MediaGridScreen", "libraryYear"},
    };

    // Every `receiver.name(` anywhere in the QML, as `name@receiver` tagged with
    // the file it was in.
    //
    // The receiver is kept as `name@receiver` but is not *checked*: a feature
    // is "called from somewhere", not "called on a particular object", and a
    // screen says `screen.controller.setRating(...)` where the shell says
    // `host.controller.openDetail(id)`.  Requiring a literal receiver would be
    // another way for this test to be wrong rather than strict.
    //
    // What this test got wrong before it got anything right is worth writing
    // down, because all five of these presented identically -- fourteen
    // features "missing" while the QML plainly called them:
    //
    // * the compile definition had no quotes, so inside a `QStringLiteral` the
    //   macro did not expand to a string literal at all and every read came
    //   back empty;
    // * the key stored the receiver where the lookup read the method;
    // * the lookup used `QString::section` on a key whose middle field is
    //   empty, which is *valid* and returns "" -- so even with the key fixed it
    //   silently found nothing;
    // * `kScreens` does not contain `Shell.qml`, which is where half of them
    //   are called from;
    // * and the failure message said nothing about what *was* called, so each
    //   run produced fourteen identical sentences and no evidence.
    //
    // `Shell.qml` is here and not in `kScreens`: `kScreens` is the set of things
    // the shell *instantiates*, and the shell is not one of its own screens.
    // A list built from `kScreens` alone cannot see a single navigation call the
    // shell makes.
    QSet<QString> calledWith;
    QStringList sources = kScreens;
    sources.append(QStringLiteral("Shell.qml"));
    const QRegularExpression call(
        QStringLiteral("\\b([a-z][A-Za-z0-9_]*)\\.([a-z][A-Za-z0-9_]*)\\s*\\("));
    for (const QString &screen : sources) {
        const QString type = QFileInfo(screen).completeBaseName();
        for (const QString &line : readLines(sourcePath(screen))) {
            auto it = call.globalMatch(line);
            while (it.hasNext()) {
                const QRegularExpressionMatch one = it.next();
                calledWith.insert(type + QLatin1Char('@') + one.captured(2)
                                  + QLatin1Char('@') + one.captured(1));
            }
        }
    }
    QVERIFY2(!calledWith.isEmpty(),
             "no `receiver.method(` calls were collected, so every feature below "
             "would be reported missing no matter what the screens do");

    // The file it has to be called from.  `where` is the type name, so a
    // feature that moved to another screen is a failure to look at rather than a
    // feature that silently went missing: the brief says what the application
    // offers, not which file offers it.
    QStringList unreachable;
    for (const Feature &feature : features) {
        const QString where = QString::fromLatin1(feature.where);
        const QString wanted = QString::fromLatin1(feature.name);
        bool found = false;
        QStringList here;
        for (const QString &entry : calledWith) {
            if (!entry.startsWith(where + QLatin1Char('@')))
                continue;
            const QString method = entry.split(QLatin1Char('@')).value(1);
            if (method == wanted)
                found = true;
            else if (!here.contains(method))
                here.append(method);
        }
        if (!found) {
            // What *is* called from that file, so a failure says whether the
            // feature is missing or the collector is.  The collector was
            // broken three times, and each of those runs printed fourteen
            // identical sentences with nothing attached; this line is what
            // turned the third one into a two-minute fix.
            std::sort(here.begin(), here.end());
            unreachable.append(QStringLiteral("%1 -- the brief's feature, reachable "
                                              "from %2, and nothing calls it (%2 "
                                              "calls: %3)")
                                   .arg(wanted, where,
                                        here.isEmpty()
                                            ? QStringLiteral("nothing at all")
                                            : here.join(QStringLiteral(", "))));
        }
    }
    // The same check for the three writeable filter properties: a screen that
    // reads `librarySort` but never writes it is offering a filter that does
    // not filter, which is exactly what the pair-of-a-setter-and-a-read-only-
    // property shape produced.
    for (const Assignment &assignment : assignments) {
        const QString where = QString::fromLatin1(assignment.where);
        const QString property = QString::fromLatin1(assignment.property);
        bool written = false;
        for (const QString &line : readLines(sourcePath(QStringLiteral("screens/")
                                                        + where + QStringLiteral(".qml")))) {
            const QRegularExpression write(QStringLiteral("%1\\s*=").arg(property));
            if (write.match(line).hasMatch()
                && !line.contains(QStringLiteral("controller.%1 READ").arg(property))) {
                written = true;
                break;
            }
        }
        if (!written) {
            unreachable.append(QStringLiteral("%1.%2 -- a filter the brief asks for, "
                                              "shown in the UI and never written")
                                   .arg(where, property));
        }
    }

    QVERIFY2(unreachable.isEmpty(),
             qPrintable(QStringLiteral(
                 "the client can do these and no screen asks for it, which is a "
                 "feature that exists in the API and not in the application:\n  %1")
                            .arg(unreachable.join(QStringLiteral("\n  ")))));
}

QTEST_GUILESS_MAIN(ScreenRoutesTest)
#include "tst_screenroutes.moc"
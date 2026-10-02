#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QtQml>
#include <QtQml>
#include <QQmlContext>

#include "apiclient.h"
#include "appcontroller.h"
#include "credentialstore.h"
#include "homescreenmodel.h"
#include "librarymodel.h"
#include "sessioncontroller.h"

/// Entry point.
///
/// The C++ side is constructed first and handed to QML as context properties,
/// so the QML layer never reaches for a global and the whole object graph can
/// be built by a test without a QGuiApplication.
int main(int argc, char *argv[])
{
    // The controller types are registered *uncreatable*: QML may name their
    // enums (`AppController.Detail`, `SessionController.Home`) but must not
    // construct one.  A navigation model that a screen could instantiate a
    // second copy of is a model with two sources of truth.
    qmlRegisterUncreatableType<AppController>(
        "Stream", 1, 0, "AppController",
        QStringLiteral("Provided by main(); not constructible from QML."));
    qmlRegisterUncreatableType<SessionController>(
        "Stream", 1, 0, "SessionController",
        QStringLiteral("Provided by main(); not constructible from QML."));
    qmlRegisterUncreatableType<MediaListModel>(
        "Stream", 1, 0, "MediaListModel",
        QStringLiteral("Provided by AppController; not constructible from QML."));
    qmlRegisterUncreatableType<HomeScreenModel>(
        "Stream", 1, 0, "HomeScreenModel",
        QStringLiteral("Provided by AppController; not constructible from QML."));

    QGuiApplication app(argc, argv);
    app.setOrganizationName(QStringLiteral("Stream"));
    app.setApplicationName(QStringLiteral("Stream"));
    app.setApplicationDisplayName(QStringLiteral("Stream"));
    app.setApplicationVersion(QStringLiteral("1.0.0"));

    SessionController session;
    // The signed-in app exists before anyone has signed in; it is only shown
    // once a profile is selected, and every screen inside it reloads when the
    // profile changes, so switching profiles is a controller call rather than a
    // rebuild of the window.
    AppController controller(session.api());
    session.restore();

    QQmlApplicationEngine engine;
    // Context properties are invisible to qmllint, so they are bound to the
    // AppContext singleton's named properties and everything else in the UI
    // reads `App.session` / `App.api` / `App.credentials`.  One place for the
    // linter to check, and a renamed property fails the build rather than
    // becoming a runtime TypeError.
    engine.rootContext()->setContextProperty(QStringLiteral("session"), &session);
    engine.rootContext()->setContextProperty(QStringLiteral("api"), session.api());
    engine.rootContext()->setContextProperty(QStringLiteral("credentials"),
                                             session.credentials());
    engine.rootContext()->setContextProperty(QStringLiteral("controller"), &controller);

    QObject::connect(
        &engine, &QQmlApplicationEngine::objectCreationFailed, &app,
        []() { QCoreApplication::exit(-1); }, Qt::QueuedConnection);

    engine.loadFromModule("Stream", "Main");
    return app.exec();
}

#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>

#include "apiclient.h"
#include "credentialstore.h"
#include "sessioncontroller.h"

/// Entry point.
///
/// The C++ side is constructed first and handed to QML as context properties,
/// so the QML layer never reaches for a global and the whole object graph can
/// be built by a test without a QGuiApplication.
int main(int argc, char *argv[])
{
    QGuiApplication app(argc, argv);
    app.setOrganizationName(QStringLiteral("Stream"));
    app.setApplicationName(QStringLiteral("Stream"));
    app.setApplicationDisplayName(QStringLiteral("Stream"));
    app.setApplicationVersion(QStringLiteral("1.0.0"));

    SessionController session;
    session.restore();

    QQmlApplicationEngine engine;
    // Context properties are invisible to qmllint, so they are bound to the
    // AppContext singleton's named properties and everything else in the UI
    // reads `App.session` / `App.api` / `App.credentials`.  One place for the
    // linter to check, and a renamed property fails the build rather than
    // becoming a runtime TypeError.
    engine.rootContext()->setContextProperty(QStringLiteral("App"), nullptr);
    engine.rootContext()->setContextProperty(QStringLiteral("session"), &session);
    engine.rootContext()->setContextProperty(QStringLiteral("api"), session.api());
    engine.rootContext()->setContextProperty(QStringLiteral("credentials"),
                                             session.credentials());

    QObject::connect(
        &engine, &QQmlApplicationEngine::objectCreationFailed, &app,
        []() { QCoreApplication::exit(-1); }, Qt::QueuedConnection);

    engine.loadFromModule("Stream", "Main");
    return app.exec();
}

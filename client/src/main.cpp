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

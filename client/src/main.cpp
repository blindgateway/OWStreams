#include <QGuiApplication>
#include <QLoggingCategory>
#include <QQmlApplicationEngine>
#include <QtQml>
#include <QQmlContext>

#include "apiclient.h"
#include "appcontroller.h"
#include "credentialstore.h"
#include "homescreenmodel.h"
#include "librarymodel.h"
#include "playbackcontroller.h"
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

    // A root object that failed to instantiate is a *runtime* failure: the
    // engine logs it, the process carries on, and the operator is left looking
    // at a blank window with no exit code and no message.  Failing here, with
    // what the engine said, is the difference between a crash report and a
    // mystery.
    if (engine.rootObjects().isEmpty()) {
        qCritical("The main window could not be created. The QML engine's "
                  "messages above say why.");
        return 1;
    }

    // Started -- and say so.
    //
    // Without this the only evidence that the application works is that it does
    // not exit, which is a thing you learn by watching a terminal rather than
    // by reading one.  On a build machine `QT_QPA_PLATFORM=offscreen` the whole
    // run is silence otherwise, and silence is indistinguishable from a window
    // that never appeared: an aborted run and a working one look the same.
    qInfo() << "Stream" << app.applicationVersion() << "started;"
            << "Qt" << qVersion() << "on" << QGuiApplication::platformName()
            << "-- server:" << (session.serverUrl().isEmpty()
                                     ? QStringLiteral("(not configured)")
                                     : session.serverUrl());

    return app.exec();
}

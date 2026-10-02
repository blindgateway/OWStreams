pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import Stream

/// Root window.
///
/// Two things live here and nothing else:
///
/// * the **session gate** -- the screen for each state of `SessionController`,
///   which decides whether the signed-in app exists at all;
/// * the **shell** -- navigation plus the current screen, which exists only
///   once a profile is selected.
///
/// Every decision about *which* screen and *what* state it is in is made in
/// C++.  QML draws what it is told.  That split is what lets the session
/// machine be tested without a window, and the navigation be tested without a
/// server.
ApplicationWindow {
    id: root
    width: 1100
    height: 720
    minimumWidth: 360
    minimumHeight: 480
    visible: true
    title: qsTr("Stream")
    color: Theme.background

    readonly property var session: App.session
    readonly property var controller: App.controller

    Component.onCompleted: {
        // Hand the C++ objects to the linter-visible singletons.  Done here
        // rather than as context properties so every reference in the UI goes
        // through a declared property that qmllint can check.
        App.session = session
        App.api = api
        App.credentials = credentials
        App.controller = controller
        Responsive.window = root
        Theme.adoptWindowFont(root)
    }

    // -- the session gate -------------------------------------------------
    Loader {
        id: gate
        anchors.fill: parent
        active: root.session.state !== SessionController.Home
        visible: active

        sourceComponent: Component {
            Loader {
                anchors.fill: parent
                readonly property var session: root.session

                ServerScreen {
                    id: server
                    anchors.fill: parent
                    visible: session.state === SessionController.NoServer
                          || session.state === SessionController.Connecting
                    initialUrl: session.serverUrl
                    busy: session.state === SessionController.Connecting
                    errorMessage: session.lastError
                    onSubmitted: function (url) { session.connectTo(url) }
                }

                LoginScreen {
                    id: login
                    anchors.fill: parent
                    visible: session.state === SessionController.Login
                    serverUrl: session.serverUrl
                    username: App.credentials.lastAccount
                    busy: session.state === SessionController.Connecting
                    errorMessage: session.lastError
                    onSubmitted: function (username, password, remember) {
                        session.signIn(username, password, remember)
                    }
                    onChangeServerRequested: {
                        App.credentials.forgetEverything()
                        session.connectTo("")
                    }
                }

                ProfileScreen {
                    id: profiles
                    anchors.fill: parent
                    visible: session.state === SessionController.Profiles
                    username: session.username
                    busy: false
                    errorMessage: session.lastError
                    onProfileSelected: function (id) { session.selectProfile(id) }
                    onUnlockRequested: function (id, pin) { session.unlockProfile(id, pin) }
                    onCreateRequested: function (name, pin) {
                        App.api.createProfile(name, pin)
                    }
                    onSignOutRequested: session.signOut()
                }

                // A server with no accounts has no sign-in to offer, so it
                // lands here too: the screen asks for the first administrator.
                ChangePasswordScreen {
                    id: changePassword
                    anchors.fill: parent
                    visible: session.state === SessionController.ChangePassword
                    username: session.username
                    busy: false
                    errorMessage: session.lastError
                    onSubmitted: function (current, next) {
                        session.submitPasswordChange(current, next)
                    }
                    onSignOutRequested: session.signOut()
                }
            }
        }
    }

    // -- the signed-in shell ----------------------------------------------
    Shell {
        id: shell
        anchors.fill: parent
        visible: root.session.state === SessionController.Home
        controller: root.controller
        session: root.session

        onProfileSwitchRequested: root.session.showProfiles()
    }
}
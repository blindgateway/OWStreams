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

    Component.onCompleted: {
        // The four C++ objects arrive as context properties, which qmllint
        // cannot resolve -- every unqualified use of one is a false positive.
        // The `App` singleton exists so the rest of the UI never has to spell
        // them; this is the one place that does, exactly once, before any
        // Loader below has instantiated a screen.
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
        active: App.session.state !== SessionController.Home
        visible: active

        sourceComponent: Component {
            // Five screens behind one `visible` binding each, not a StackView:
            // they are cheap to construct, and a StackView would make "back" a
            // question about navigation depth when the state machine already
            // knows the answer.
            Item {
                id: gateRoot
                anchors.fill: parent

                ServerScreen {
                    id: server
                    anchors.fill: parent
                    visible: App.session.state === SessionController.NoServer
                          || App.session.state === SessionController.Connecting
                    initialUrl: App.session.serverUrl
                    busy: App.session.state === SessionController.Connecting
                    errorMessage: App.session.lastError
                    onSubmitted: function (url) { App.session.connectTo(url) }
                }

                LoginScreen {
                    id: login
                    anchors.fill: parent
                    visible: App.session.state === SessionController.Login
                    serverUrl: App.session.serverUrl
                    username: App.credentials.lastAccount
                    busy: App.session.state === SessionController.Connecting
                    errorMessage: App.session.lastError
                    onSubmitted: function (username, password, remember) {
                        App.session.signIn(username, password, remember)
                    }
                    onChangeServerRequested: {
                        App.credentials.forgetEverything()
                        App.session.connectTo("")
                    }
                }

                ProfileScreen {
                    id: profiles
                    anchors.fill: parent
                    visible: App.session.state === SessionController.Profiles
                    username: App.session.username
                    // The screen owns its own busy state: a PIN attempt is
                    // short enough that a global spinner would flicker through
                    // the whole picker.
                    busy: false
                    errorMessage: App.session.lastError
                    onProfileSelected: function (id) { App.session.selectProfile(id) }
                    onUnlockRequested: function (id, pin) {
                        App.session.unlockProfile(id, pin)
                    }
                    onCreateRequested: function (name, pin) {
                        App.session.createProfile(name, pin)
                    }
                    onSignOutRequested: App.session.signOut()
                }

                // A server with no accounts has no sign-in to offer, so it
                // lands here too: the screen asks for the first administrator.
                ChangePasswordScreen {
                    id: changePassword
                    anchors.fill: parent
                    visible: App.session.state === SessionController.ChangePassword
                    username: App.session.username
                    busy: false
                    errorMessage: App.session.lastError
                    onSubmitted: function (current, next) {
                        App.session.submitPasswordChange(current, next)
                    }
                    onSignOutRequested: App.session.signOut()
                }
            }
        }
    }

    // -- the signed-in shell ----------------------------------------------
    Shell {
        id: shell
        anchors.fill: parent
        visible: App.session.state === SessionController.Home
        controller: App.controller
        session: App.session

        onProfileSwitchRequested: App.session.showProfiles()
    }
}

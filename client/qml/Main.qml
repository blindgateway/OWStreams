pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import Stream
import Stream as StreamModule

/// Root window.
///
/// The state switch is the whole navigation model for now: one state machine
/// in C++ (`SessionController`), one place that decides what is on screen.
/// Adding a screen is a case here, not a new stack of navigation state in QML.
ApplicationWindow {
    id: root
    width: 1100
    height: 720
    minimumWidth: 360
    minimumHeight: 480
    visible: true
    title: qsTr("Stream")
    color: Theme.background

    readonly property string stateLabel: {
        switch (session.state) {
        case SessionController.NoServer:    return qsTr("no server configured")
        case SessionController.Connecting:  return qsTr("connecting to %1").arg(session.serverUrl)
        case SessionController.Login:       return qsTr("sign in to %1").arg(session.serverUrl)
        case SessionController.ChangePassword: return qsTr("change password")
        case SessionController.Profiles:    return qsTr("choose a profile")
        case SessionController.Home:        return qsTr("home")
        default:                            return qsTr("unknown")
        }
    }

    Component.onCompleted: {
        // Hand the C++ objects to the linter-visible singletons.  Done here
        // rather than as context properties so every reference in the UI goes
        // through a declared property that qmllint can check.
        App.session = session
        App.api = api
        App.credentials = credentials
        Responsive.window = root
    }

    Text {
        anchors.centerIn: parent
        color: Theme.textMuted
        font.pixelSize: Theme.typeBody
        text: root.stateLabel
    }
}

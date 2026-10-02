pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Stream

/// First run, and "add a server" later.
///
/// The field is the whole screen, and everything else exists to say whether
/// what is in it worked.  That is the shape a connection screen should have:
/// one input, one action, one status.
Item {
    id: screen

    /// Prefilled from the remembered server, so the common case is "press
    /// Connect" rather than "type the address again".
    property string initialUrl: ""
    property bool busy: false
    property string errorMessage: ""
    property string version: ""

    signal submitted(string url)

    ColumnLayout {
        anchors.centerIn: parent
        width: Math.min(parent.width - Theme.spaceXxl * 2, 420)
        spacing: Theme.spaceLg

        Text {
            Layout.fillWidth: true
            text: qsTr("Connect to your server")
            color: Theme.text
            font.pixelSize: Theme.typeDisplay
            font.family: Theme.family
            font.weight: Font.DemiBold
            wrapMode: Text.WordWrap
        }

        Text {
            Layout.fillWidth: true
            text: qsTr("The address of the machine running Stream. A local server is usually http://localhost:8484.")
            color: Theme.textMuted
            font.pixelSize: Theme.typeBody
            font.family: Theme.family
            wrapMode: Text.WordWrap
        }

        AppTextField {
            id: field
            Layout.fillWidth: true
            label: qsTr("Server address")
            text: screen.initialUrl
            placeholderText: qsTr("http://192.168.1.10:8484")
            enabled: !screen.busy
            onAccepted: screen.submit()
        }

        AppButton {
            Layout.fillWidth: true
            weight: "primary"
            text: qsTr("Connect")
            busy: screen.busy
            enabled: field.text.trim().length > 0
            onClicked: screen.submit()
        }

        // The status line is where the *server's* answer goes.  It is not a
        // toast: it stays until the next attempt, because "could not reach"
        // with no explanation is the failure that makes people give up.
        ColumnLayout {
            Layout.fillWidth: true
            Layout.topMargin: Theme.spaceSm
            spacing: Theme.spaceXs
            visible: screen.errorMessage !== "" || screen.version !== ""

            Text {
                Layout.fillWidth: true
                visible: screen.errorMessage !== ""
                text: screen.errorMessage
                color: Theme.danger
                font.pixelSize: Theme.typeCaption
                font.family: Theme.family
                wrapMode: Text.WordWrap
            }

            Text {
                Layout.fillWidth: true
                visible: screen.version !== ""
                text: qsTr("Connected to Stream %1").arg(screen.version)
                color: Theme.textMuted
                font.pixelSize: Theme.typeCaption
                font.family: Theme.family
            }
        }
    }

    function submit() {
        screen.submitted(field.text.trim())
    }

    Component.onCompleted: if (!field.activeFocus) field.forceActiveFocus()
}
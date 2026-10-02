pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Stream

/// Sign in.
///
/// "Remember me" is on by default and the reason is stated underneath it,
/// because a ticked box that stores a year-long credential in the platform
/// keychain deserves a sentence rather than a tooltip nobody reads.
Item {
    id: screen

    property string serverUrl: ""
    property string username: ""
    property bool busy: false
    property string errorMessage: ""
    /// The account last signed in here, used only to prefill the field.
    property string lastAccount: ""

    /// False when no keychain was found: the app refuses to persist rather than
    /// downgrade to a file, so offering "remember me" would be a lie.
    readonly property bool canRemember: App.hasSecureStorage

    signal submitted(string username, string password, bool remember)
    signal changeServerRequested()

    ColumnLayout {
        anchors.centerIn: parent
        width: Math.min(parent.width - Theme.spaceXxl * 2, 400)
        spacing: Theme.spaceMd

        Text {
            Layout.fillWidth: true
            text: qsTr("Sign in")
            color: Theme.text
            font.pixelSize: Theme.typeDisplay
            font.family: Theme.family
            font.weight: Font.DemiBold
        }

        Text {
            Layout.fillWidth: true
            text: screen.serverUrl
            color: Theme.textMuted
            font.pixelSize: Theme.typeCaption
            font.family: Theme.family
            elide: Text.ElideMiddle
        }

        AppTextField {
            id: userField
            Layout.fillWidth: true
            label: qsTr("Username")
            text: screen.username
            enabled: !screen.busy
            onAccepted: passwordField.forceActiveFocus()
        }

        AppTextField {
            id: passwordField
            Layout.fillWidth: true
            label: qsTr("Password")
            echoMode: TextInput.Password
            enabled: !screen.busy
            onAccepted: screen.submit()
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.spaceSm
            visible: screen.canRemember

            CheckBox {
                id: rememberBox
                checked: true
                enabled: !screen.busy
                implicitWidth: 20
                implicitHeight: 20
                indicator: Rectangle {
                    implicitWidth: 18
                    implicitHeight: 18
                    radius: Theme.radiusSm
                    color: rememberBox.checked ? Theme.accent : "transparent"
                    border.width: 1
                    border.color: Theme.hairline

                    Text {
                        anchors.centerIn: parent
                        visible: rememberBox.checked
                        text: "✓"
                        color: Theme.accentText
                        font.pixelSize: 12
                        font.family: Theme.family
                    }
                }
            }

            Text {
                text: qsTr("Stay signed in on this device")
                color: Theme.text
                font.pixelSize: Theme.typeCaption
                font.family: Theme.family
            }
        }

        Text {
            Layout.fillWidth: true
            Layout.topMargin: Theme.spaceXs
            visible: !screen.canRemember
            // Names the backend, so "remember me is off" is a reason rather
            // than a mystery.  On a locked keyring the tool is present and the
            // store reports unavailable, and the two need different answers.
            text: qsTr("This device could not open a secure key store (%1), so this session will end when the app closes.")
                    .arg(App.credentialBackend)
            color: Theme.textMuted
            font.pixelSize: Theme.typeMicro
            font.family: Theme.family
            wrapMode: Text.WordWrap
        }

        AppButton {
            Layout.fillWidth: true
            Layout.topMargin: Theme.spaceSm
            weight: "primary"
            text: qsTr("Sign in")
            busy: screen.busy
            enabled: userField.text.trim().length > 0
                      && passwordField.text.length > 0
            onClicked: screen.submit()
        }

        // The server's message, verbatim.  "That username or password is not
        // right" is more useful than anything the client could invent, and it
        // is already written to distinguish the cases the server can distinguish
        // without leaking which.
        Text {
            Layout.fillWidth: true
            Layout.topMargin: Theme.spaceXs
            visible: screen.errorMessage !== ""
            text: screen.errorMessage
            color: Theme.danger
            font.pixelSize: Theme.typeCaption
            font.family: Theme.family
            wrapMode: Text.WordWrap
        }

        AppButton {
            Layout.fillWidth: true
            Layout.topMargin: Theme.spaceLg
            weight: "quiet"
            text: qsTr("Use a different server")
            enabled: !screen.busy
            onClicked: screen.changeServerRequested()
        }
    }

    function submit() {
        screen.submitted(userField.text.trim(), passwordField.text,
                         screen.canRemember && rememberBox.checked)
    }

    Component.onCompleted: {
        if (!screen.username && screen.lastAccount !== "")
            userField.text = screen.lastAccount
        if (!userField.activeFocus)
            userField.forceActiveFocus()
    }
}
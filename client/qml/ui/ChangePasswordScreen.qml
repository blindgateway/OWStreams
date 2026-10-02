pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Stream

/// Change a password.
///
/// Shown when the account must change it -- after an administrative reset, or
/// on a deployment that still uses its initial password -- and also reachable
/// from settings.  The new-password field is not "required" beyond the server's
/// policy, so the screen states the policy rather than discovering it on
/// submit.
Item {
    id: screen

    property string username: ""
    property bool busy: false
    property string errorMessage: ""

    signal submitted(string currentPassword, string newPassword)
    signal signOutRequested()

    ColumnLayout {
        anchors.centerIn: parent
        width: Math.min(parent.width - Theme.spaceXxl * 2, 400)
        spacing: Theme.spaceMd

        Text {
            Layout.fillWidth: true
            text: qsTr("Change your password")
            color: Theme.text
            font.pixelSize: Theme.typeDisplay
            font.family: Theme.family
            font.weight: Font.DemiBold
        }

        Text {
            Layout.fillWidth: true
            text: screen.username !== ""
                  ? qsTr("Signed in as %1. This account must use a new password before it can browse.").arg(screen.username)
                  : qsTr("This account must use a new password before it can browse.")
            color: Theme.textMuted
            font.pixelSize: Theme.typeBody
            font.family: Theme.family
            wrapMode: Text.WordWrap
        }

        AppTextField {
            id: currentField
            Layout.fillWidth: true
            label: qsTr("Current password")
            echoMode: TextInput.Password
            enabled: !screen.busy
            onAccepted: nextField.forceActiveFocus()
        }

        AppTextField {
            id: nextField
            Layout.fillWidth: true
            label: qsTr("New password")
            echoMode: TextInput.Password
            enabled: !screen.busy
            onAccepted: confirmField.forceActiveFocus()
        }

        AppTextField {
            id: confirmField
            Layout.fillWidth: true
            label: qsTr("Confirm new password")
            echoMode: TextInput.Password
            enabled: !screen.busy
            onAccepted: screen.submit()
        }

        // The two things that can be wrong, stated before submitting rather
        // than returned as a rejection afterwards.
        Text {
            Layout.fillWidth: true
            visible: nextField.text.length > 0 && nextField.text.length < 10
            text: qsTr("At least 10 characters.")
            color: Theme.textMuted
            font.pixelSize: Theme.typeMicro
            font.family: Theme.family
        }

        Text {
            Layout.fillWidth: true
            visible: confirmField.text.length > 0 && confirmField.text !== nextField.text
            text: qsTr("The two passwords do not match.")
            color: Theme.textMuted
            font.pixelSize: Theme.typeMicro
            font.family: Theme.family
        }

        AppButton {
            Layout.fillWidth: true
            Layout.topMargin: Theme.spaceSm
            weight: "primary"
            text: qsTr("Change password")
            busy: screen.busy
            enabled: currentField.text.length > 0
                      && nextField.text.length >= 10
                      && nextField.text === confirmField.text
                      && nextField.text !== currentField.text
            onClicked: screen.submit()
        }

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
            text: qsTr("Sign out")
            onClicked: screen.signOutRequested()
        }
    }

    function submit() {
        screen.submitted(currentField.text, nextField.text)
        currentField.text = ""
        nextField.text = ""
        confirmField.text = ""
    }

    Component.onCompleted: if (!currentField.activeFocus) currentField.forceActiveFocus()
}

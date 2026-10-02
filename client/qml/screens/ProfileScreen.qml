pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Stream

/// Choose a profile, unlock one, or create one.
///
/// One screen for all three because they are three states of one decision,
/// and a separate "create profile" route would mean a different look, a
/// different back button and a different empty state for what is the same
/// question.
Item {
    id: screen

    property var profiles: []          // [{id, name, has_pin, is_kids}]
    property string username: ""
    property int selectedId: 0
    property bool busy: false
    property string errorMessage: ""

    property int unlockProfileId: 0
    property string unlockError: ""
    property int attemptsLeft: -1

    signal profileSelected(int id)
    signal unlockRequested(int id, string pin)
    signal createRequested(string name, string pin)
    signal signOutRequested()

    readonly property bool unlocking: unlockProfileId > 0

    // -- header ---------------------------------------------------------
    Text {
        id: heading
        anchors.top: parent.top
        anchors.topMargin: Responsive.pageMargin
        anchors.left: parent.left
        anchors.leftMargin: Responsive.pageMargin
        anchors.right: parent.right
        anchors.rightMargin: Responsive.pageMargin
        text: screen.unlocking ? qsTr("Enter the PIN") : qsTr("Who's watching?")
        color: Theme.text
        font.pixelSize: Responsive.isPhone ? Theme.typeTitle : Theme.typeDisplay
        font.family: Theme.family
        font.weight: Font.DemiBold
    }

    Text {
        anchors.top: heading.bottom
        anchors.topMargin: Theme.spaceXs
        anchors.left: heading.left
        anchors.right: heading.right
        visible: !screen.unlocking
        text: screen.username !== "" ? qsTr("Signed in as %1").arg(screen.username) : ""
        color: Theme.textMuted
        font.pixelSize: Theme.typeCaption
        font.family: Theme.family
    }

    // -- the grid -------------------------------------------------------
    GridView {
        id: grid
        anchors.top: heading.bottom
        anchors.topMargin: Theme.spaceXxl
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: footer.top
        anchors.margins: Responsive.pageMargin
        visible: !screen.unlocking
        clip: true
        model: screen.profiles
        boundsBehavior: Flickable.StopAtBounds
        cellWidth: Responsive.isPhone ? grid.width / 3
                                      : Math.min(180, grid.width / Math.max(2, Math.floor(grid.width / 180)))
        cellHeight: cellWidth * 0.78 + Theme.spaceLg * 2

        ScrollBar.vertical: ScrollBar {}

        delegate: Item {
            width: grid.cellWidth
            height: grid.cellHeight

            required property var modelData
            required property int index

            readonly property bool isSelected:
                modelData.id === screen.selectedId

            Rectangle {
                id: tile
                anchors.centerIn: parent
                width: Math.min(grid.cellWidth - Theme.spaceLg, 132)
                height: width * 0.78
                radius: Theme.radiusMd
                color: Theme.surfaceHigh
                border.width: isSelected ? 2 : 1
                border.color: isSelected ? Theme.accent : Theme.hairline

                Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }

                // A profile has no poster, so its "artwork" is its initial on
                // the surface colour.  Anything more elaborate would be a
                // gradient and a glow.
                Text {
                    anchors.centerIn: parent
                    text: (modelData.name || "?").charAt(0).toUpperCase()
                    color: Theme.accent
                    font.pixelSize: 40
                    font.family: Theme.family
                    font.weight: Font.Light
                }

                // A locked profile says so before it is pressed, not after a
                // failed attempt.
                Rectangle {
                    anchors.bottom: parent.bottom
                    anchors.left: parent.left
                    anchors.right: parent.right
                    height: 26
                    radius: Theme.radiusMd
                    color: Theme.overlay
                    visible: modelData.has_pin

                    Text {
                        anchors.centerIn: parent
                        text: qsTr("PIN")
                        color: Theme.textMuted
                        font.pixelSize: Theme.typeMicro
                        font.family: Theme.family
                        font.weight: Font.DemiBold
                    }
                }

                MouseArea {
                    anchors.fill: parent
                    enabled: !screen.busy
                    cursorShape: Qt.PointingHandCursor
                    onClicked: screen.choose(modelData)
                }
            }

            Text {
                anchors.top: tile.bottom
                anchors.topMargin: Theme.spaceSm
                anchors.left: parent.left
                anchors.right: parent.right
                horizontalAlignment: Text.AlignHCenter
                text: modelData.name
                color: Theme.text
                font.pixelSize: Theme.typeCaption
                font.family: Theme.family
                elide: Text.ElideRight
                maximumLineCount: 1
            }
        }
    }

    // -- the PIN pad ----------------------------------------------------
    Item {
        anchors.fill: grid
        visible: screen.unlocking

        ColumnLayout {
            anchors.centerIn: parent
            width: Math.min(parent.width - Theme.spaceXxl * 2, 320)
            spacing: Theme.spaceLg

            Text {
                Layout.fillWidth: true
                text: qsTr("This profile is locked.")
                color: Theme.textMuted
                font.pixelSize: Theme.typeBody
                font.family: Theme.family
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
            }

            AppTextField {
                id: pinField
                Layout.fillWidth: true
                label: qsTr("PIN")
                echoMode: TextInput.Password
                maximumLength: 8
                inputMethodHints: Qt.ImhDigitsOnly
                enabled: !screen.busy
                horizontalAlignment: TextInput.AlignHCenter
                onAccepted: screen.confirmPin()
            }

            Text {
                Layout.fillWidth: true
                visible: screen.unlockError !== ""
                text: screen.unlockError
                color: Theme.danger
                font.pixelSize: Theme.typeCaption
                font.family: Theme.family
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
            }

            Text {
                Layout.fillWidth: true
                visible: screen.attemptsLeft >= 0 && screen.unlockError === ""
                text: qsTr("%n attempt(s) left", "", screen.attemptsLeft)
                color: Theme.textFaint
                font.pixelSize: Theme.typeMicro
                font.family: Theme.family
                horizontalAlignment: Text.AlignHCenter
            }

            AppButton {
                Layout.fillWidth: true
                weight: "primary"
                text: qsTr("Unlock")
                busy: screen.busy
                enabled: pinField.text.length >= 4
                onClicked: screen.confirmPin()
            }

            AppButton {
                Layout.fillWidth: true
                weight: "quiet"
                text: qsTr("Back")
                enabled: !screen.busy
                onClicked: screen.cancelUnlock()
            }
        }
    }

    // -- create a profile ----------------------------------------------
    Item {
        id: creator
        anchors.fill: grid
        visible: !screen.unlocking && screen.creating

        ColumnLayout {
            anchors.centerIn: parent
            width: Math.min(parent.width - Theme.spaceXxl * 2, 320)
            spacing: Theme.spaceMd

            Text {
                Layout.fillWidth: true
                text: qsTr("Add a profile")
                color: Theme.text
                font.pixelSize: Theme.typeTitle
                font.family: Theme.family
                font.weight: Font.DemiBold
            }

            AppTextField {
                id: nameField
                Layout.fillWidth: true
                label: qsTr("Name")
                maximumLength: 24
                enabled: !screen.busy
                onAccepted: pinField.forceActiveFocus()
            }

            AppTextField {
                id: pinField
                Layout.fillWidth: true
                label: qsTr("PIN (optional)")
                echoMode: TextInput.Password
                maximumLength: 8
                inputMethodHints: Qt.ImhDigitsOnly
                enabled: !screen.busy
                onAccepted: screen.confirmCreate()
            }

            Text {
                Layout.fillWidth: true
                visible: pinField.text.length > 0 && pinField.text.length < 4
                text: qsTr("A PIN needs at least four digits.")
                color: Theme.textMuted
                font.pixelSize: Theme.typeMicro
                font.family: Theme.family
            }

            AppButton {
                Layout.fillWidth: true
                weight: "primary"
                text: qsTr("Create")
                busy: screen.busy
                enabled: nameField.text.trim().length > 0
                onClicked: screen.confirmCreate()
            }

            AppButton {
                Layout.fillWidth: true
                weight: "quiet"
                text: qsTr("Cancel")
                enabled: !screen.busy
                onClicked: screen.creating = false
            }
        }
    }

    // -- footer ---------------------------------------------------------
    Item {
        id: footer
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: Theme.spaceHuge * 2

        AppButton {
            anchors.centerIn: parent
            weight: "quiet"
            text: qsTr("Sign out")
            visible: !screen.unlocking
            onClicked: screen.signOutRequested()
        }

        AppButton {
            anchors.centerIn: parent
            weight: "quiet"
            text: qsTr("Add a profile")
            visible: !screen.unlocking && !screen.creating
                    && screen.profiles.length < 8
            onClicked: screen.creating = true
        }
    }

    property bool creating: false

    function choose(profile) {
        if (profile.has_pin) {
            screen.unlockProfileId = profile.id
            screen.unlockError = ""
            screen.attemptsLeft = -1
            pinField.text = ""
            pinField.forceActiveFocus()
            return
        }
        screen.profileSelected(profile.id)
    }

    function confirmPin() {
        if (pinField.text.length < 4)
            return
        screen.unlockRequested(screen.unlockProfileId, pinField.text)
    }

    function cancelUnlock() {
        screen.unlockProfileId = 0
        screen.unlockError = ""
        screen.attemptsLeft = -1
        pinField.text = ""
    }

    function confirmCreate() {
        if (nameField.text.trim().length === 0)
            return
        if (pinField.text.length > 0 && pinField.text.length < 4)
            return
        screen.createRequested(nameField.text.trim(), pinField.text)
    }

    Component.onCompleted: {
        if (screen.profiles.length === 1 && !screen.profiles[0].has_pin)
            screen.choose(screen.profiles[0])
    }
}
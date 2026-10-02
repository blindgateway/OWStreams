pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import Stream

/// Loading, empty and error states, as one component.
///
/// Three screens that each draw their own spinner end up with three
/// different spinners.  One component with three modes means the *content* of
/// each state is written once, and a rail that is loading looks like a rail
/// that is erroring if it gets the mode wrong -- which is the bug worth
/// designing out.
Item {
    id: root

    /// One of "loading", "empty", "error".
    property string mode
    property string message
    property string actionText
    property bool retryable

    signal actionTriggered()

    implicitHeight: column.implicitHeight
    visible: root.mode !== undefined && root.mode !== ""

    Column {
        id: column
        anchors.centerIn: parent
        width: parent.width
        spacing: Theme.spaceLg
        visible: root.visible

        BusyIndicator {
            anchors.horizontalCenter: parent.horizontalCenter
            running: root.mode === "loading"
            visible: root.mode === "loading"
            implicitWidth: 32
            implicitHeight: 32
        }

        // A failed state gets a small filled circle with an exclamation; it is
        // the one thing on the screen that is unmistakably not content.
        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            visible: root.mode === "error"
            width: 40
            height: 40
            radius: 20
            color: Theme.danger
            opacity: 0.16

            Text {
                anchors.centerIn: parent
                text: "!"
                color: Theme.danger
                font.pixelSize: 22
                font.family: Theme.family
                font.weight: Font.Bold
            }
        }

        Text {
            width: Math.min(parent.width, 420)
            horizontalAlignment: Text.AlignHCenter
            visible: root.mode !== "loading"
            text: root.message || ""
            color: root.mode === "error" ? Theme.text : Theme.textMuted
            font.pixelSize: Theme.typeBody
            font.family: Theme.family
            wrapMode: Text.WordWrap
        }

        // A retry button only where retrying can help: a 404 or a 403 will be
        // exactly as true in thirty seconds as it is now.
        Button {
            id: retry
            anchors.horizontalCenter: parent.horizontalCenter
            visible: root.mode === "error" && root.retryable
                     && root.actionText !== undefined && root.actionText !== ""
            text: root.actionText
            onClicked: root.actionTriggered()

            contentItem: Text {
                text: retry.text
                color: Theme.accentText
                font.pixelSize: Theme.typeBody
                font.family: Theme.family
                horizontalAlignment: Text.AlignHCenter
            }
            background: Rectangle {
                implicitWidth: 112
                implicitHeight: 36
                radius: Theme.radiusSm
                color: retry.down ? Qt.darker(Theme.accent, 1.15) : Theme.accent
            }
        }
    }
}

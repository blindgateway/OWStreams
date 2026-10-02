pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import Stream

/// The one button.
///
/// Three different-looking buttons on one screen is the first thing that makes
/// a UI read as assembled rather than designed, so there is a single one with
/// three weights instead: primary (filled), secondary (outlined) and quiet
/// (text only, for things that are not the point of the screen).
Button {
    id: control

    /// "primary" | "secondary" | "quiet"
    property string weight: "secondary"
    property bool busy: false

    readonly property color _foreground:
        weight === "primary" ? Theme.accentText : Theme.text

    implicitHeight: 40
    implicitWidth: Math.max(96, label.implicitWidth + Theme.spaceXl * 2)
    hoverEnabled: true
    enabled: !busy

    contentItem: Item {
        Row {
            anchors.centerIn: parent
            spacing: Theme.spaceSm
            visible: control.text !== undefined && control.text !== ""

            BusyIndicator {
                anchors.verticalCenter: parent.verticalCenter
                running: control.busy
                visible: control.busy
                implicitWidth: 16
                implicitHeight: 16
            }

            Text {
                id: label
                anchors.verticalCenter: parent.verticalCenter
                text: control.text
                color: control.enabled ? control._foreground : Theme.textFaint
                font.pixelSize: Theme.typeBody
                font.family: Theme.family
                font.weight: control.weight === "primary" ? Font.DemiBold : Font.Normal
            }
        }
    }

    background: Rectangle {
        radius: Theme.radiusSm
        color: {
            if (!control.enabled)
                return control.weight === "primary" ? Theme.surfaceHigh : "transparent"
            if (control.weight === "primary")
                return control.down ? Qt.darker(Theme.accent, 1.2)
                                    : (control.hovered ? Qt.lighter(Theme.accent, 1.1)
                                                      : Theme.accent)
            return control.down ? Theme.surfaceHigh
                                : (control.hovered ? Theme.surface : "transparent")
        }
        border.width: control.weight === "secondary" ? 1 : 0
        border.color: Theme.hairline

        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
    }
}

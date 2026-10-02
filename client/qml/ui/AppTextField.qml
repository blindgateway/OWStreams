pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import Stream

/// The one text input.
///
/// The focus ring is the accent and nothing else changes on focus: a second
/// shadow, a second border and a label that floats are three more things to
/// keep consistent across every form in the app.
TextField {
    id: control

    property string label
    property string hint
    property bool revealable

    color: Theme.text
    placeholderTextColor: Theme.textFaint
    font.pixelSize: Theme.typeBody
    font.family: Theme.family
    selectionColor: Theme.accent
    selectedTextColor: Theme.accentText
    implicitHeight: 44
    leftPadding: Theme.spaceLg
    rightPadding: revealable ? 48 : Theme.spaceLg

    background: Rectangle {
        radius: Theme.radiusSm
        color: Theme.surface
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus ? Theme.accent : Theme.hairline

        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
    }

    Text {
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.leftMargin: Theme.spaceLg
        anchors.bottomMargin: -Theme.typeMicro - 6
        visible: control.activeFocus && control.label !== undefined
        text: control.label
        color: Theme.accent
        font.pixelSize: Theme.typeMicro
        font.family: Theme.family
    }
}

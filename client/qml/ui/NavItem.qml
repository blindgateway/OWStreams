pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import Stream

/// One entry in the navigation.
///
/// Icons are drawn rather than imported: a navigation bar is five glyphs, and
/// shipping five SVG files to get them means five files that can disagree
/// about stroke width.  These are 24-unit paths on a 1.75 px stroke, which is
/// the one place a hand-drawn set earns its keep.
Item {
    id: item

    /// One of: "home", "library", "search", "favorites", "history", "settings".
    property string icon: ""
    property string label: ""
    property bool selected: false
    /// Tablet rails are icons only; desktop rails have labels.
    property bool showLabel: true

    signal activated()

    implicitHeight: showLabel ? 52 : 48
    implicitWidth: showLabel ? 200 : 56

    Rectangle {
        id: highlight
        anchors.fill: parent
        anchors.margins: Theme.spaceXs
        radius: Theme.radiusSm
        color: item.selected ? Theme.surfaceHigh : "transparent"
        visible: item.selected
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
    }

    // The selected marker is a bar on the *leading* edge, not a filled pill:
    // one accent line reads as "you are here" without tinting the icon.
    Rectangle {
        anchors.left: parent.left
        anchors.leftMargin: Theme.spaceXs
        anchors.verticalCenter: parent.verticalCenter
        width: 3
        height: item.showLabel ? 22 : 20
        radius: 2
        color: Theme.accent
        visible: item.selected
    }

    Row {
        anchors.left: parent.left
        anchors.leftMargin: item.showLabel ? Theme.spaceLg : 0
        anchors.right: item.showLabel ? parent.right : parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: item.showLabel ? Theme.spaceMd : 0

        Icon {
            anchors.verticalCenter: parent.verticalCenter
            anchors.horizontalCenter: item.showLabel ? undefined : parent.horizontalCenter
            width: 22
            height: 22
            name: item.icon
            color: item.selected ? Theme.text : Theme.textMuted
        }

        Text {
            anchors.verticalCenter: parent.verticalCenter
            visible: item.showLabel
            text: item.label
            color: item.selected ? Theme.text : Theme.textMuted
            font.pixelSize: Theme.typeBody
            font.family: Theme.family
            font.weight: item.selected ? Font.DemiBold : Font.Normal
        }
    }

    MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: item.activated()
    }
}
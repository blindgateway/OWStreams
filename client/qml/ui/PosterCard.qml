pragma ComponentBehavior: Bound

import QtQuick
import Stream

/// One title in a rail.
///
/// Deliberately not a rounded card with a shadow and a gradient: a poster grid
/// is a grid of rectangles.  The affordance is the poster itself plus a title
/// underneath, and the only motion is a border that appears on hover/focus.
Item {
    id: root

    property string title
    property string subtitle
    property int mediaId
    property real progress        // 0..1; > 0 draws a resume bar
    property bool isFavorite
    property string posterUrl

    signal activated()
    signal secondaryActivated()

    implicitHeight: poster.height + 44

    Rectangle {
        id: poster
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        // 2:3 is the poster ratio; constraining the height off the width is the
        // only way to get it, since Rectangle has no aspectRatio property.
        height: Math.round(width * 3 / 2)
        radius: Theme.radiusSm
        color: Theme.surfaceHigh
        border.width: 2
        border.color: hoverArea.containsMouse || focusRing.visible ? Theme.accent : "transparent"

        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }

        // Artwork is fetched through the authenticated poster route, so a
        // failed image is normal (a title with no poster) and is drawn as the
        // surface colour rather than as a broken-image glyph.
        Image {
            id: art
            anchors.fill: parent
            anchors.margins: 2
            asynchronous: true
            cache: true
            source: root.posterUrl
            fillMode: Image.PreserveAspectCrop
            visible: status === Image.Ready
            // The artwork is fetched through an authenticated route, so a
            // failed load is normal and must not print a URL or an error.
            onStatusChanged: if (status === Image.Error) source = ""
        }

        // Fallback: the title, set the way a record sleeve would set it.
        Text {
            anchors.centerIn: parent
            width: parent.width - Theme.spaceLg * 2
            visible: art.status !== Image.Ready
            text: root.title
            color: Theme.textFaint
            font.pixelSize: Theme.typeBody
            font.family: Theme.family
            wrapMode: Text.WordWrap
            maximumLineCount: 3
            elide: Text.ElideRight
            horizontalAlignment: Text.AlignHCenter
        }

        // Resume bar.  A thin line at the bottom edge, the way a progress bar
        // belongs at the edge of a thing rather than across it.
        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            height: 3
            color: Theme.accent
            visible: root.progress > 0
            width: parent.width * Math.max(0, Math.min(1, root.progress))
        }

        // Keyboard focus has to be visible for a TV remote or a desktop
        // keyboard; hover is not a thing on either of those.
        Rectangle {
            id: focusRing
            anchors.fill: parent
            radius: Theme.radiusSm
            color: "transparent"
            border.width: 2
            border.color: Theme.accent
            opacity: 0.85
            visible: root.activeFocus
        }

        MouseArea {
            id: hoverArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.activated()
        }
    }

    Text {
        id: titleText
        anchors.top: poster.bottom
        anchors.topMargin: Theme.spaceSm
        anchors.left: parent.left
        anchors.right: parent.right
        text: root.title
        color: Theme.text
        font.pixelSize: Theme.typeCaption
        font.family: Theme.family
        elide: Text.ElideRight
        maximumLineCount: 1
    }

    Text {
        anchors.top: titleText.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        visible: root.subtitle !== undefined && root.subtitle !== ""
        text: root.subtitle
        color: Theme.textMuted
        font.pixelSize: Theme.typeMicro
        font.family: Theme.family
        elide: Text.ElideRight
        maximumLineCount: 1
    }
}

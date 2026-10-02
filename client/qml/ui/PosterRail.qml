pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import Stream

/// A horizontal row of posters with a heading.
///
/// Horizontal, not a wrapping grid: a streaming service's rails are rows, and
/// on a phone they scroll sideways rather than pushing the next section off
/// the bottom of the screen.  The scrollbar is hidden until the row is
/// actually wider than the viewport, because a permanent scrollbar on a
/// phone-sized row is visual noise on every screen.
Item {
    id: root

    property string title
    /// A MediaListModel, or a JS array of maps for previews.  A model rather
    /// than an array because a list of 500 maps cannot be indexed cheaply from
    /// QML, and a favourite toggle has to update one row rather than reset the
    /// whole rail.
    property var model
    property int limit
    property bool loading
    property string emptyText

    /// True when the model holds nothing -- an empty rail shows its message
    /// and a populated one does not, and only the model can say which.
    readonly property bool isEmpty: {
        if (!root.model)
            return true
        if (root.model.count !== undefined)     // MediaListModel
            return root.model.count === 0
        return root.model.length === 0          // a plain array
    }

    signal activated(int mediaId)

    implicitHeight: heading.height + Theme.spaceLg + row.height + Theme.spaceSm

    Text {
        id: heading
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        visible: root.title !== undefined && root.title !== ""
        text: root.title
        color: Theme.text
        font.pixelSize: Theme.typeTitle
        font.family: Theme.family
        font.weight: Font.DemiBold
    }

    BusyIndicator {
        anchors.centerIn: row
        running: root.loading
        visible: root.loading
        implicitWidth: 28
        implicitHeight: 28
    }

    Flickable {
        id: row
        anchors.top: heading.bottom
        anchors.topMargin: root.title !== undefined && root.title !== "" ? Theme.spaceLg : 0
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        contentWidth: repeater.count * (cardWidth + Theme.spaceLg)
        contentHeight: height
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.HorizontalFlick
        ScrollBar.horizontal: ScrollBar {
            policy: row.contentWidth > row.width ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
            // A rail scrolled with a mouse wheel would be a horizontal row you
            // can only reach by dragging: the wheel is the common case on
            // desktop, so it drives the flick.
            active: row.contentWidth > row.width
        }

        // Width tracks the form factor rather than a column count, so a phone
        // poster is thumb-sized and a desktop one is not a postage stamp.
        readonly property real cardWidth: Responsive.isPhone ? 132 : 150

        MouseArea {
            id: wheelArea
            anchors.fill: parent
            acceptedButtons: Qt.NoButton
            // Wheel -> vertical movement on a horizontal flickable.
            onWheel: function (event) {
                row.contentX -= event.angleDelta.y
                event.accepted = true
            }
        }

        Row {
            spacing: Theme.spaceLg
            Repeater {
                id: repeater
                model: root.model
                delegate: PosterCard {
                    id: card
                    required property var modelData
                    width: row.cardWidth
                    // `required property modelData` is the supported way to
                    // name a delegate's model data: an implicit `modelData`
                    // is ambiguous the moment the delegate has an id.
                    title: card.modelData.title || ""
                    subtitle: card.modelData.subtitle || ""
                    mediaId: card.modelData.id || 0
                    progress: card.modelData.progress || 0
                    isFavorite: card.modelData.favorite || false
                    posterUrl: card.modelData.posterUrl || ""
                    onActivated: root.activated(card.mediaId)
                }
            }
        }
    }

    Text {
        anchors.centerIn: parent
        visible: !root.loading && root.isEmpty
                 && (root.emptyText !== undefined && root.emptyText !== "")
        text: root.emptyText
        color: Theme.textMuted
        font.pixelSize: Theme.typeBody
        font.family: Theme.family
    }
}

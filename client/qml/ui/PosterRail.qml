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

    // Measured from the heading and the card, never from `row.height`.
    //
    // `row` is anchored `top: heading.bottom` *and* `bottom: parent.bottom`,
    // so its height is "whatever is left" -- and this property is part of what
    // is left, because `HomeScreen` sizes its host row from
    // `rail.implicitHeight`.  Each read raised the floor, each raise made the
    // row taller, and Qt dropped the binding with
    //
    //     Binding loop detected for property "implicitHeight"
    //
    // and left the rail sized by the layout's fallback.  It looked right, and
    // stopped responding to a change of window size.
    implicitHeight: heading.height + Theme.spaceLg + row.cardHeight + Theme.spaceSm

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

        /// The height of one card, taken from `PosterCard`'s own
        /// `implicitHeight` rather than re-derived here.
        ///
        /// The two have to agree -- the rail's content height and the card's --
        /// and they are the same formula written twice otherwise.  One card is
        /// enough to measure because a rail is a single horizontal line: its
        /// height does not depend on how many posters are in it.
        readonly property real cardHeight: cardSize.implicitHeight

        // Invisible, and never parented to the flickable: a real card would be
        // laid out inside it and counted in `contentWidth`.  It exists only to
        // be measured, and it costs one item.
        PosterCard {
            id: cardSize
            visible: false
            width: row.cardWidth
        }

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

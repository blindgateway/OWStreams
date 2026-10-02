pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Stream

/// A grid of titles: the library, favourites, history.
///
/// A grid rather than rails, because these are lists you *browse* rather than
/// scan.  The column count comes from the available width and the card width,
/// so a phone gets three across and a desktop gets as many as fit -- one rule,
/// no breakpoints to keep in sync.
Flickable {
    id: screen

    property var model: null
    property string heading: ""

    signal titleActivated(int mediaId)

    readonly property int cardWidth: 150
    readonly property int columns: Responsive.columnsFor(
        width - Responsive.pageMargin * 2, cardWidth, 2, 8)

    implicitHeight: column.implicitHeight
    contentHeight: column.implicitHeight + Theme.spaceXxl * 2
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}

    ColumnLayout {
        id: column
        width: screen.width
        spacing: Theme.spaceLg

        Item {
            Layout.fillWidth: true
            Layout.topMargin: Responsive.pageMargin
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            Layout.preferredHeight: 44

            Text {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: screen.heading
                color: Theme.text
                font.pixelSize: Responsive.isPhone ? Theme.typeTitle : Theme.typeDisplay
                font.family: Theme.family
                font.weight: Font.DemiBold
            }

            Text {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                visible: screen.model && screen.model.count > 0
                text: qsTr("%n title(s)", "", screen.model ? screen.model.count : 0)
                color: Theme.textMuted
                font.pixelSize: Theme.typeCaption
                font.family: Theme.family
            }
        }

        StatusPane {
            Layout.fillWidth: true
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            Layout.preferredHeight: 180
            mode: screen.model && screen.model.failed ? "error"
                  : (screen.model && screen.model.loading ? "loading" : "")
            message: screen.model ? screen.model.errorMessage : ""
            retryable: screen.model ? screen.model.retryable : false
        }

        GridView {
            id: grid
            Layout.fillWidth: true
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            Layout.preferredHeight: Math.min(contentHeight,
                                             Math.max(0, (screen.height - 200)))
            model: screen.model
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            cellWidth: Math.floor((screen.width - Responsive.pageMargin * 2)
                                  / Math.max(1, screen.columns))
            cellHeight: cellWidth * 1.5 + 44

            ScrollBar.vertical: ScrollBar {}

            // The roles are declared on the *delegate* rather than read as
            // `model.title`: a named role is a property lookup on the item,
            // and `model.x` is a string lookup resolved again on every repaint
            // of every cell.
            delegate: Item {
                id: cell
                width: grid.cellWidth
                height: grid.cellHeight

                required property int index
                required property string title
                required property string subtitle
                required property int mediaId
                required property real progress
                required property bool favorite
                required property url posterUrl

                PosterCard {
                    anchors.fill: parent
                    anchors.margins: Theme.spaceSm
                    title: cell.title
                    subtitle: cell.subtitle
                    mediaId: cell.mediaId
                    progress: cell.progress
                    isFavorite: cell.favorite
                    posterUrl: cell.posterUrl
                    onActivated: screen.titleActivated(cell.mediaId)
                }
            }
        }
    }
}

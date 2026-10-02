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

    implicitHeight: content.height
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

            delegate: Item {
                required property int index
                width: grid.cellWidth
                height: grid.cellHeight

                PosterCard {
                    anchors.fill: parent
                    anchors.margins: Theme.spaceSm
                    title: model.title
                    subtitle: model.subtitle
                    mediaId: model.mediaId
                    progress: model.progress
                    isFavorite: model.favorite
                    posterUrl: model.posterUrl
                    onActivated: screen.titleActivated(model.mediaId)
                }
            }
        }
    }
}

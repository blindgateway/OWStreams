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

    /// The library's filter row.  Only the library has one -- favourites and
    /// history are already narrow, and a filter over "the nine titles you
    /// marked as favourites" is noise.
    property bool filterable: false
    /// `AppController`, bound only when `filterable`.  A screen that shows a
    /// grid does not have to know about the controller; this one does, and
    /// only the library instance of it.
    property var controller: null

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

        FilterBar {
            Layout.fillWidth: true
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            visible: screen.filterable
            // Read *through* the controller rather than being handed copies of
            // each filter: a copy has to be updated when the controller changes
            // it, and then the two can disagree about what is selected.
            options: screen.controller ? screen.controller.filterOptions : null
            sort: screen.controller ? screen.controller.librarySort : "added"
            genre: screen.controller ? screen.controller.libraryGenre : ""
            year: screen.controller ? screen.controller.libraryYear : ""
            onSortRequested: function (sort) { screen.controller.librarySort = sort }
            onGenreRequested: function (slug) { screen.controller.libraryGenre = slug }
            onYearRequested: function (year) { screen.controller.libraryYear = year }
            onCleared: screen.controller.clearLibraryFilters()
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
            // The *whole page* scrolls -- this is the outer `Flickable`.  An
            // earlier version gave the grid its own height and its own
            // `ScrollBar`, which put a scroll view inside a scroll view: the
            // inner one swallowed the wheel, so the outer one could not be
            // scrolled past it and rows below the fold were unreachable.
            //
            // It measured `screen.height - 200`, a literal standing in for the
            // heading, the filter bar and the status pane.  On a phone that is
            // most of the screen, so a 1000-title library showed the first three
            // rows and no way to reach row four.
            Layout.preferredHeight: Math.max(0, contentHeight)
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

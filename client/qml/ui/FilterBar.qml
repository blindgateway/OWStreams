pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Stream

/// The library's filter row: sort, genre, year, and a way back out.
///
/// Built from what the *server* says exists rather than from a list written
/// here.  A genre nobody thought to put in the UI would then be unfindable,
/// which is the whole reason `GET /library/categories` exists: a library of
/// 1000+ titles is browsed by narrowing it, and narrowing by a hard-coded list
/// is narrowing by what the programmer knew.
///
/// Not a `ComboBox`.  Qt's is a popup with its own style, and the brief asks
/// for controls that do not look like defaults; this is a row of small
/// buttons that scrolls sideways, which is also the right shape for a phone
/// where a dropdown would cover the grid.
///
/// **Nothing is hidden behind a cap.**  `AppController.loadFilterOptions`
/// fetches `/library/categories` once per session and `FilterBar` shows
/// everything in it.  A library with 200 genres produces 200 buttons, and the
/// `ScrollView` scrolls them -- not a "Genres ▾" menu holding sixty of them.
/// The reason is that the filter is the *only* way to narrow a 1000-title
/// grid, and a genre that cannot be clicked is a genre that does not exist as
/// far as the person watching is concerned.  A menu would have to paginate,
/// search or truncate to stay inside a phone's width, and all three lose
/// titles.
Item {
    id: bar

    /// `AppController.filterOptions` -- genres, years, decades, tags.
    property var options: null
    /// The active selections, so a button can say which one is on.
    property string sort: "added"
    property string genre: ""
    property string year: ""

    signal sortRequested(string sort)
    signal genreRequested(string slug)
    signal yearRequested(string year)
    signal cleared()

    implicitHeight: row.implicitHeight

    // Three groups of buttons and the "clear" one, in that order, because the
    // sort is the thing you change most often and the filters are the thing you
    // change once.
    //
    // Every `key` here is one of the server's `SORT_COLUMNS`.  An unrecognised
    // key is a 422 -- `LibraryService._filter` raises on anything not in that
    // set rather than quietly sorting by something else -- so a button whose
    // key drifts away from it does not degrade, it empties the grid.
    readonly property var sortOptions: [
        { key: "added",     label: qsTr("Recently added") },
        { key: "title",     label: qsTr("A–Z") },
        { key: "year",      label: qsTr("Newest first") },
        { key: "rating",    label: qsTr("Best rated") },
        { key: "popularity", label: qsTr("Most played") },
        { key: "last_played", label: qsTr("Last played") },
        { key: "runtime",   label: qsTr("Shortest first") },
        { key: "release_date", label: qsTr("Release date") },
    ]

    readonly property var genreList: bar.options && bar.options.genres
                               ? bar.options.genres : []
    readonly property var yearList: bar.options && bar.options.years
                              ? bar.options.years : []

    readonly property bool filtering: bar.genre !== "" || bar.year !== ""

    ScrollView {
        id: scroll
        anchors.fill: parent
        clip: true
        contentWidth: row.implicitWidth
        ScrollBar.horizontal: ScrollBar {}

        RowLayout {
            id: row
            spacing: Theme.spaceSm

            // -- sort ------------------------------------------------------
            Repeater {
                model: bar.sortOptions
                delegate: AppButton {
                    required property var modelData
                    required property int index
                    // The active sort is "primary", which is filled: it is the
                    // current *mode*, not a command.  The rest are quiet, so
                    // nine buttons do not compete with the grid for attention.
                    weight: bar.sort === modelData.key ? "primary" : "quiet"
                    text: modelData.label
                    onClicked: bar.sortRequested(modelData.key)
                }
            }

            Rectangle {
                Layout.preferredWidth: 1
                Layout.preferredHeight: 20
                Layout.leftMargin: Theme.spaceSm
                Layout.rightMargin: Theme.spaceXs
                color: Theme.hairline
            }

            // -- genre -----------------------------------------------------
            Repeater {
                model: bar.genreList
                delegate: AppButton {
                    required property var modelData
                    // The genre's *slug* is what the API takes; the name is
                    // what a person reads, and "Sci-Fi" and "Science Fiction"
                    // are the same row with different spellings.
                    weight: bar.genre === modelData.slug ? "primary" : "quiet"
                    text: modelData.name + "  " + modelData.count
                    onClicked: bar.genreRequested(modelData.slug)
                }
            }

            Rectangle {
                Layout.preferredWidth: 1
                Layout.preferredHeight: 20
                Layout.leftMargin: Theme.spaceSm
                Layout.rightMargin: Theme.spaceXs
                color: Theme.hairline
                visible: bar.yearList.length > 0
            }

            // -- year ------------------------------------------------------
            Repeater {
                model: bar.yearList
                delegate: AppButton {
                    required property var modelData
                    // Newest first, because "1997" next to "2021" in a row of
                    // twenty buttons is a list nobody reads; the decade is one
                    // click instead of five.
                    weight: bar.year === String(modelData.year) ? "primary" : "quiet"
                    // The count is what tells a person which of two buttons is
                    // worth clicking before they click it.
                    text: modelData.year + "  " + modelData.count
                    onClicked: bar.yearRequested(String(modelData.year))
                }
            }

            AppButton {
                visible: bar.filtering
                weight: "secondary"
                text: qsTr("Clear")
                onClicked: bar.cleared()
            }
        }
    }
}

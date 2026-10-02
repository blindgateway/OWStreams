pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Stream

/// Search.
///
/// The field is the screen.  Results appear below as they come back, and the
/// query is submitted on a short delay rather than on every keystroke: the
/// search bucket is 120 requests a minute, and a fast typist can exceed that
/// without noticing.
Item {
    id: screen

    property var controller: null
    property var results: null

    signal titleActivated(int mediaId)

    readonly property bool hasQuery:
        controller !== null && controller.searchQuery.trim().length > 0

    function scheduleSearch() {
        searchTimer.restart()
    }

    Timer {
        id: searchTimer
        // Long enough to coalesce a burst of typing, short enough that the
        // results feel attached to the keyboard.
        interval: 350
        onTriggered: if (screen.controller) screen.controller.submitSearch()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 96
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            Layout.topMargin: Responsive.pageMargin

            AppTextField {
                id: field
                anchors.left: parent.left
                anchors.right: clearButton.visible ? clearButton.left : parent.right
                anchors.rightMargin: clearButton.visible ? Theme.spaceSm : 0
                anchors.topMargin: Theme.spaceSm
                label: qsTr("Search")
                placeholderText: qsTr("Title, cast, genre, year…")
                text: screen.controller ? screen.controller.searchQuery : ""
                // Assigning back would be a binding loop; the controller owns
                // the query and this field is told what it is.
                onTextChanged: if (screen.controller) screen.controller.setSearchQuery(text)
                onAccepted: if (screen.controller) screen.controller.submitSearch()
            }

            // Empty the search.
            //
            // The only way back to "not searching" was to select the field's
            // text and delete it, one character at a time.  A search that
            // matched nothing could not be dismissed at all: the results pane
            // stayed, the field stayed full of the query, and the grid below
            // was the only evidence that anything had happened.
            AppButton {
                id: clearButton
                anchors.right: parent.right
                anchors.verticalCenter: field.verticalCenter
                visible: screen.hasQuery
                weight: "quiet"
                text: qsTr("Clear")
                onClicked: if (screen.controller) screen.controller.clearSearch()
            }
        }

        MediaGridScreen {
            id: grid
            Layout.fillWidth: true
            Layout.fillHeight: true
            model: screen.results
            heading: screen.hasQuery ? qsTr("Results") : ""
            visible: screen.hasQuery
            onTitleActivated: function (id) { screen.titleActivated(id) }
        }

        StatusPane {
            Layout.fillWidth: true
            Layout.preferredHeight: 200
            visible: !screen.hasQuery
            mode: "empty"
            message: qsTr("Search your library by title, cast, genre, tag or year.")
        }
    }

    Component.onCompleted: field.forceActiveFocus()
}

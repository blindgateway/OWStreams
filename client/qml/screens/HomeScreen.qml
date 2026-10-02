pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Stream

/// The home screen.
///
/// A vertical list of rails, built from the C++ model's rail objects rather
/// than from a hard-coded set of eight `if`s.  Adding a rail to the server is
/// then a one-line change on the C++ side instead of a new block here, and the
/// order lives in one place -- the order *is* the screen.
Flickable {
    id: screen

    property var home: null          // HomeScreenModel *
    property string searchQuery: ""

    signal titleActivated(int mediaId)
    signal searchRequested()
    signal profileSwitchRequested()

    // `contentHeight` is already set; naming `content` would be an ambiguous
    // read of an attached property the linter is right to flag.
    implicitHeight: column.implicitHeight + Theme.spaceXxl * 2
    contentHeight: column.implicitHeight + Theme.spaceXxl * 2
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}

    // The screen only scrolls when the content is taller than the window.
    // A rails screen that is exactly one screen tall should not bounce.
    readonly property bool overflows: contentHeight > height

    ColumnLayout {
        id: column
        width: screen.width
        spacing: Responsive.sectionGap

        // -- header ------------------------------------------------------
        Item {
            Layout.fillWidth: true
            Layout.topMargin: Responsive.pageMargin
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            Layout.preferredHeight: 56

            Text {
                anchors.left: parent.left
                anchors.right: headerActions.left
                anchors.rightMargin: Theme.spaceSm
                anchors.verticalCenter: parent.verticalCenter
                elide: Text.ElideRight
                text: qsTr("Stream")
                color: Theme.text
                font.pixelSize: Responsive.isPhone ? Theme.typeTitle : Theme.typeDisplay
                font.family: Theme.family
                font.weight: Font.DemiBold
            }

            Row {
                id: headerActions
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: Theme.spaceXs

                // Reload the rails.
                //
                // `AppController::refresh` is the only way to re-fetch the home
                // screen and no screen called it, so the rails showed whatever
                // the first load returned until the application was restarted:
                // a title added on the server, or a rating given in another
                // session, never appeared.  It asks the model, and the model
                // asks the controller -- the screen holds no controller.
                AppButton {
                    anchors.verticalCenter: parent.verticalCenter
                    weight: "quiet"
                    text: qsTr("Refresh")
                    visible: screen.home !== null
                    onClicked: screen.home.refresh()
                }

                AppButton {
                    anchors.verticalCenter: parent.verticalCenter
                    weight: "quiet"
                    text: qsTr("Switch profile")
                    onClicked: screen.profileSwitchRequested()
                }
            }
        }

        StatusPane {
            Layout.fillWidth: true
            Layout.preferredHeight: 160
            mode: screen.home && screen.home.failed ? "error" : "loading"
            visible: mode !== ""
            message: screen.home ? screen.home.errorMessage : ""
            actionText: qsTr("Try again")
            retryable: screen.home ? screen.home.retryable : false
            onActionTriggered: if (screen.home) screen.home.retryRequested()
        }

        // -- rails -------------------------------------------------------
        Repeater {
            id: railRepeater
            model: screen.home ? screen.home.rails : []

            delegate: Item {
                id: railHost
                required property var modelData
                Layout.fillWidth: true
                Layout.leftMargin: Responsive.pageMargin
                Layout.rightMargin: Responsive.pageMargin
                Layout.preferredHeight: rail.visible ? rail.implicitHeight : 0
                visible: railHost.modelData.visible

                PosterRail {
                    id: rail
                    anchors.fill: parent
                    title: railHost.modelData.title
                    model: railHost.modelData.model
                    loading: railHost.modelData.model.loading
                    emptyText: railHost.modelData.model.emptyMessage
                    onActivated: function (mediaId) { screen.titleActivated(mediaId) }
                }
            }
        }

        // A home screen with nothing in it at all needs to say what to do, not
        // just be blank.  The most common cause by far is "the server has no
        // library yet", and the fix is on the server.
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 200
            visible: Boolean(screen.home) && !screen.home.loading && !screen.home.failed
                     && screen.home.rails.every(function (r) { return r.model.count === 0 })

            StatusPane {
                anchors.fill: parent
                mode: "empty"
                message: qsTr("Your library is empty.\nRun a scan on the server, or add a media root in its settings.")
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: Theme.spaceXxl
        }
    }
}
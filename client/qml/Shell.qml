pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import Stream

/// The signed-in shell: navigation plus whatever screen the controller says.
///
/// Three arrangements of one tree, not three UIs:
///
/// * **desktop**  a fixed sidebar with labels, content beside it;
/// * **tablet**   a compact icon rail, content beside it;
/// * **phone**    no sidebar at all -- a bottom bar, content above it.
///
/// The screen itself never knows which it is in; it fills whatever space it is
/// given.  That is what stops the desktop layout being *stretched* onto a
/// phone, which is the failure this arrangement exists to prevent.
Item {
    id: shell

    property var controller: null
    property var session: null

    signal profileSwitchRequested()

    readonly property bool isPhone: Responsive.isPhone

    readonly property var model: [
        { key: "home",      icon: "home",      label: qsTr("Home") },
        { key: "library",   icon: "library",   label: qsTr("Library") },
        { key: "search",    icon: "search",    label: qsTr("Search") },
        { key: "favorites", icon: "favorites", label: qsTr("Favourites") },
        { key: "history",   icon: "history",   label: qsTr("History") },
        { key: "settings",  icon: "settings",  label: qsTr("Settings") },
    ]

    function navigate(key) {
        if (!shell.controller)
            return
        switch (key) {
        case "home":      shell.controller.goHome(); break
        case "library":   shell.controller.goLibrary(); break
        case "search":    shell.controller.goSearch(); break
        case "favorites": shell.controller.goFavorites(); break
        case "history":   shell.controller.goHistory(); break
        case "settings":  shell.controller.goSettings(); break
        default: break
        }
    }

    function isCurrent(key) {
        if (!shell.controller)
            return false
        switch (key) {
        case "home":      return shell.controller.screen === 0
        case "library":   return shell.controller.screen === 1
        case "search":    return shell.controller.screen === 2
        case "favorites": return shell.controller.screen === 3
        case "history":   return shell.controller.screen === 4
        case "settings":  return shell.controller.screen === 5
        default:          return false
        }
    }

    /// Full screen, and back out of it.
    ///
    /// A toggle rather than "go full screen", because the same key leaves: a
    /// viewer who has just gone full screen on a television has no other way
    /// back if the only implementation is one-way.  `Window.FullScreen` is the
    /// only visibility that means it; `Window.Maximized` is not full screen and
    /// a remote's "home" button will not leave it either.
    function toggleFullscreen() {
        const window = Responsive.window
        if (!window || typeof window.visibility === "undefined")
            return
        window.visibility = (window.visibility === Window.FullScreen)
                               ? Window.Windowed
                               : Window.FullScreen
    }

    // -- sidebar (desktop and tablet) ------------------------------------
    Rectangle {
        id: sidebar
        visible: !shell.isPhone
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        width: Responsive.railWidth
        color: Theme.surface

        // A hairline rather than a shadow: a one-pixel edge between two
        // surfaces is what a desktop app actually uses, and a drop shadow on a
        // full-height panel is the thing that makes a UI read as a template.
        Rectangle {
            anchors.right: parent.right
            width: 1
            height: parent.height
            color: Theme.hairline
        }

        Text {
            id: wordmark
            anchors.top: parent.top
            anchors.topMargin: Theme.spaceXl
            anchors.left: parent.left
            anchors.leftMargin: Theme.spaceLg
            visible: Responsive.isDesktop
            text: qsTr("Stream")
            color: Theme.text
            font.pixelSize: Theme.typeTitle
            font.family: Theme.family
            font.weight: Font.DemiBold
        }

        Column {
            id: navColumn
            anchors.top: Responsive.isDesktop ? wordmark.bottom : parent.top
            anchors.topMargin: Responsive.isDesktop ? Theme.spaceXl : Theme.spaceLg
            anchors.left: parent.left
            anchors.right: parent.right
            spacing: Theme.spaceXs

            Repeater {
                model: shell.model

                delegate: NavItem {
                    required property var modelData
                    width: navColumn.width
                    icon: modelData.icon
                    label: modelData.label
                    showLabel: Responsive.isDesktop
                    selected: shell.isCurrent(modelData.key)
                    onActivated: shell.navigate(modelData.key)
                }
            }
        }
    }

    // -- content ---------------------------------------------------------
    Item {
        id: content
        anchors.top: parent.top
        anchors.left: shell.isPhone ? parent.left : sidebar.right
        anchors.right: parent.right
        anchors.bottom: shell.isPhone ? bottomBar.top : parent.bottom

        Loader {
            id: screenLoader
            anchors.fill: parent
            active: shell.controller !== null
            visible: active

            sourceComponent: Component {
                Item {
                    id: host
                    anchors.fill: parent
                    readonly property var controller: shell.controller

                    /// One line the settings screen prints about the server it
                    /// is talking to, built here because it is three fields
                    /// from two objects and the screen should not have to know
                    /// that.
                    function describeServer(session) {
                        if (!session)
                            return ""
                        const info = session.serverInfo || ({})
                        const parts = []
                        if (info.name || info.version)
                            parts.push((info.name || qsTr("Stream")) + " "
                                       + (info.version || ""))
                        if (info.api_version)
                            parts.push(qsTr("API %1").arg(info.api_version))
                        parts.push(session.serverUrl || "")
                        return parts.filter(function (p) { return p.length > 0 }).join(" · ")
                    }

                    HomeScreen {
                        id: home
                        anchors.fill: parent
                        visible: host.controller.screen === 0
                        home: host.controller.home
                        onTitleActivated: function (id) { host.controller.openDetail(id) }
                        onProfileSwitchRequested: shell.profileSwitchRequested()
                    }

                    MediaGridScreen {
                        id: library
                        anchors.fill: parent
                        visible: host.controller.screen === 1
                        model: host.controller.library
                        heading: qsTr("Library")
                        filterable: true
                        controller: host.controller
                        onTitleActivated: function (id) { host.controller.openDetail(id) }
                    }

                    SearchScreen {
                        id: search
                        anchors.fill: parent
                        visible: host.controller.screen === 2
                        controller: controller
                        results: host.controller.searchResults
                        onTitleActivated: function (id) { host.controller.openDetail(id) }
                    }

                    MediaGridScreen {
                        id: favorites
                        anchors.fill: parent
                        visible: host.controller.screen === 3
                        model: host.controller.favorites
                        heading: qsTr("Favourites")
                        onTitleActivated: function (id) { host.controller.openDetail(id) }
                    }

                    MediaGridScreen {
                        id: history
                        anchors.fill: parent
                        visible: host.controller.screen === 4
                        model: host.controller.history
                        heading: qsTr("History")
                        onTitleActivated: function (id) { host.controller.openDetail(id) }
                    }

                    SettingsScreen {
                        id: settings
                        anchors.fill: parent
                        visible: host.controller.screen === AppController.Settings
                        session: shell.session
                        controller: controller
                        account: shell.session ? shell.session.user : ({})
                        profiles: shell.session ? shell.session.profiles : []
                        sessions: shell.session ? shell.session.sessions : []
                        serverInfo: shell.session
                                     ? host.describeServer(shell.session) : ""
                        credentialBackend: shell.session
                            ? shell.session.credentialBackend : "none"
                        captureReport: host.controller.captureGuard
                            ? host.controller.captureGuard.platformReport : ""
                        captureApplied: host.controller.captureGuard
                            ? host.controller.captureGuard.applied : []
                        currentProfileId: shell.session ? shell.session.profileId : 0

                        onRenameProfileRequested: function (id, name) {
                            shell.session.renameProfile(id, name)
                        }
                        onDeleteProfileRequested: function (id) {
                            shell.session.deleteProfile(id)
                        }
                        onSetPinRequested: function (id, pin) {
                            shell.session.setProfilePin(id, pin)
                        }
                        onClearPinRequested: function (id, currentPin) {
                            shell.session.setProfilePin(id, "", currentPin)
                        }
                        onChangePinRequested: function (id, currentPin, newPin) {
                            shell.session.changeProfilePin(id, currentPin, newPin)
                        }
                        onChangePasswordRequested: function (current, next) {
                            shell.session.submitPasswordChange(current, next)
                        }
                        onRevokeSessionRequested: function (id) {
                            App.api.revokeSession(id)
                        }
                        onLogoutEverywhereRequested: App.api.logoutEverywhere()
                        onSwitchProfileRequested: shell.profileSwitchRequested()
                        onForgetServerRequested: {
                            App.credentials.forgetEverything()
                            shell.session.connectTo("")
                        }
                        onSignOutRequested: shell.session.signOut()
                    }

                    DetailScreen {
                        id: detail
                        anchors.fill: parent
                        visible: host.controller.screen === AppController.Detail
                        mediaId: host.controller.detailMediaId
                        media: host.controller.detail
                        controller: controller
                        onBackRequested: host.controller.back()
                        onPlayRequested: function (id) { host.controller.openPlayer(id) }
                    }

                    // The player is a *layer*, not a destination: it covers the
                    // shell -- sidebar and all -- and closing it reveals the
                    // screen it was opened from, untouched.
                    PlayerScreen {
                        id: player
                        anchors.fill: parent
                        visible: host.controller.screen === AppController.Player
                        controller: host.controller.player
                        api: App.api
                        guard: host.controller.captureGuard
                        media: host.controller.detail
                        onCloseRequested: host.controller.back()
                        // F and Escape, from the player's own key handler.
                        // Without this the player offers fullscreen and nothing
                        // performs it, so both keys are dead -- and on a
                        // television, where a remote has no other way out of a
                        // film, that is the difference between finishing it and
                        // restarting the app.
                        onFullscreenRequested: shell.toggleFullscreen()
                    }
                }
            }
        }
    }

    // -- bottom bar (phone) -----------------------------------------------
    Rectangle {
        id: bottomBar
        visible: shell.isPhone
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        // 58, full stop.  The expression this replaces was
        // `58 + (Screen.desktopAvailableHeight > 0 ? 0 : 0)`, which is 58 on
        // every platform -- a term that multiplies by zero is either a
        // forgotten conditional or a layout that was meant to grow and never
        // did.  If the bar ever does need to be taller, `Responsive` is where
        // the decision belongs; `Screen.desktopAvailableHeight` also pulls a
        // second screen's metrics into a phone's layout, which changes with
        // where the window is.
        height: 58
        color: Theme.surface

        Rectangle {
            anchors.top: parent.top
            width: parent.width
            height: 1
            color: Theme.hairline
        }

        Row {
            anchors.fill: parent
            anchors.leftMargin: Theme.spaceXs
            anchors.rightMargin: Theme.spaceXs

            Repeater {
                model: shell.model

                delegate: NavItem {
                    required property var modelData
                    width: bottomBar.width / 6
                    height: bottomBar.height
                    icon: modelData.icon
                    label: modelData.label
                    showLabel: false
                    selected: shell.isCurrent(modelData.key)
                    onActivated: shell.navigate(modelData.key)
                }
            }
        }
    }
}

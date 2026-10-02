pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Stream

/// Settings and account management.
///
/// A scrolling page of *sections*, not a tab bar.  A tab bar here would hide
/// four of the five things somebody came to do -- rename a profile, end a
/// stolen session, forget a server -- behind a switch, and on a phone it would
/// hide them behind a row of five tabs taking the width of the screen.
///
/// Sections rather than one long form because the five things are unrelated
/// and each should be doable without scrolling past the other four.  Which
/// means the page is tall, which is what a Flickable is for.
///
/// Every action leaves as a signal.  This screen decides what somebody is
/// allowed to *start*; the controller decides whether it works.  A client-side
/// check that says "this succeeded" would be a client-side check that says
/// nothing at all.
Flickable {
    id: screen

    // -- data ------------------------------------------------------------
    // Bound by Main from the controller's properties of the same names.  Held
    // as plain values rather than read from the controller here so that the
    // screen renders identically with a controller that has answered nothing
    // yet, which is what it looks like for the first frame.
    property var controller: null    // AppController *
    property var session: null       // SessionController *

    /// `{ username, display_name, is_admin, last_login_at }`
    property var account: ({})
    /// `[{ id, name, has_pin, is_kids, avatar_id }]`
    property var profiles: []
    /// `[{ id, device_name, client_version, ip_address, created_at,
    ///     last_used_at, expires_at, revoked_at }]`
    property var sessions: []
    /// e.g. "Stream 1.0.0 · API v1 · http://host:8484"
    property string serverInfo: ""
    /// e.g. "GNOME Keyring / Secret Service", or "none".
    property string credentialBackend: ""
    property int currentProfileId: 0

    readonly property bool busy: controller ? controller.busy : false

    /// "none" is the store's own word for "nothing was found", so it is
    /// treated as absent rather than as a backend called "none".
    readonly property bool hasCredentialStorage:
        credentialBackend !== "" && credentialBackend.toLowerCase() !== "none"

    readonly property int profileCount:
        (profiles && profiles.length !== undefined) ? profiles.length : 0

    readonly property int sessionCount:
        (sessions && sessions.length !== undefined) ? sessions.length : 0

    /// The bounds a form field is held to: 288 px and 416 px, taken from the
    /// spacing scale so they are not two more magic numbers.  Below the first
    /// the field's own label no longer sits beside its contents; above the
    /// second the eye loses the start of the line on a wide window.
    readonly property int minimumFieldWidth: Theme.spaceXxl * 9
    readonly property int maximumFieldWidth: Theme.spaceXxl * 13

    // -- actions ---------------------------------------------------------
    // Main wires these to the controller.  Nothing here calls it directly, so
    // the screen has no opinion about how a request is made or retried.
    signal renameProfileRequested(int profileId, string name)
    signal deleteProfileRequested(int profileId)
    signal setPinRequested(int profileId, string pin)
    signal clearPinRequested(int profileId, string currentPin)
    signal changePinRequested(int profileId, string currentPin, string newPin)
    signal changePasswordRequested(string currentPassword, string newPassword)
    signal revokeSessionRequested(string sessionId)
    signal logoutEverywhereRequested()
    signal switchProfileRequested()
    signal forgetServerRequested()
    signal signOutRequested()

    implicitHeight: contentHeight
    contentHeight: column.implicitHeight + Responsive.pageMargin * 2
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}

    ColumnLayout {
        id: column
        width: screen.width
        spacing: Responsive.sectionGap

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: Responsive.pageMargin
        }

        // -- account + password + server, side by side -------------------
        // Only where there is room for it.  The two columns are fixed
        // proportions rather than equal halves because the password form is
        // the widest thing in the page and the account column is two short
        // sections; splitting it evenly would leave the form in a column half
        // the width it was designed for.
        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            visible: Responsive.isDesktop
            spacing: Theme.spaceXl

            Loader {
                Layout.fillWidth: true
                Layout.preferredWidth: 2
                Layout.alignment: Qt.AlignTop
                sourceComponent: Responsive.isDesktop ? identityColumn : null
            }

            Loader {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                Layout.alignment: Qt.AlignTop
                sourceComponent: Responsive.isDesktop ? passwordSection : null
            }
        }

        // Below a desktop the same three sections, in order, full width.
        Loader {
            Layout.fillWidth: true
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            // A hidden child is skipped by the layout entirely, so this one
            // costs nothing on a desktop rather than reserving a blank gap.
            visible: !Responsive.isDesktop
            sourceComponent: Responsive.isDesktop ? null : stackedColumn
        }

        // -- profiles ----------------------------------------------------
        // Stays outside the columns: a list of eight profiles is full width,
        // and a half-width list of eight profiles is eight two-word rows.
        ColumnLayout {
            id: profilesSection
            Layout.fillWidth: true
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            spacing: Theme.spaceMd

            Text {
                Layout.fillWidth: true
                text: qsTr("Profiles")
                color: Theme.textMuted
                font.pixelSize: Theme.typeMicro
                font.family: Theme.family
                font.capitalization: Font.AllUppercase
            }

            // Plain muted text rather than a StatusPane: an account with no
            // profiles is an odd state, not a failure, and StatusPane is built
            // to be the loudest thing on a screen.
            Text {
                Layout.fillWidth: true
                visible: screen.profileCount === 0
                text: qsTr("No profiles on this account.")
                color: Theme.textMuted
                font.pixelSize: Theme.typeBody
                font.family: Theme.family
                wrapMode: Text.WordWrap
            }

            Repeater {
                model: screen.profiles

                delegate: ColumnLayout {
                    id: profileRow
                    // `required` is how a delegate names its model data.  A
                    // plain `property var modelData` declares a *new* property
                    // that shadows nothing and is injected with nothing, so
                    // the row reads an empty object and every field on it is
                    // undefined -- silently, and only at run time.
                    required property var modelData
                    required property int index

                    // "", "rename", "pin-set", "pin-new" or "pin-clear".  The
                    // editor lives in the row rather than in a dialog because
                    // a dialog for four digits is a window to close as well as
                    // a field to fill in.
                    property string editing: ""

                    Layout.fillWidth: true
                    spacing: Theme.spaceSm

                    readonly property bool isCurrent:
                        profileRow.modelData.id === screen.currentProfileId

                    // -- the row itself -----------------------------------
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spaceSm

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spaceSm

                            Text {
                                Layout.fillWidth: true
                                text: profileRow.modelData.name ? String(profileRow.modelData.name) : ""
                                color: Theme.text
                                font.pixelSize: Theme.typeBody
                                font.family: Theme.family
                                wrapMode: Text.WordWrap
                            }

                            Text {
                                text: qsTr("Kids")
                                color: Theme.textFaint
                                font.pixelSize: Theme.typeMicro
                                font.family: Theme.family
                                visible: profileRow.modelData.is_kids
                            }

                            // Accent, not a lock glyph: the accent is the one
                            // thing in this app that means "this has
                            // something set that you will be asked for".
                            Text {
                                text: qsTr("PIN")
                                color: Theme.accent
                                font.pixelSize: Theme.typeMicro
                                font.family: Theme.family
                                visible: profileRow.modelData.has_pin
                            }

                            Text {
                                text: qsTr("Current")
                                color: Theme.textFaint
                                font.pixelSize: Theme.typeMicro
                                font.family: Theme.family
                                visible: profileRow.isCurrent
                            }
                        }

                        // Below the name rather than beside it: on a phone
                        // there is not 330 px for a name, two markers and three
                        // buttons, and a row that clips is worse than a row
                        // that wraps.
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spaceSm

                            AppButton {
                                weight: "quiet"
                                text: qsTr("Rename")
                                enabled: !screen.busy
                                onClicked: profileRow.openEditor("rename")
                            }

                            AppButton {
                                weight: "quiet"
                                // A profile that already has one opens the
                                // editor that changes it; clearing is a second
                                // step inside that editor, because "remove the
                                // PIN" is not the same request as "set a new
                                // one" and the server asks for the current PIN
                                // for both.
                                text: profileRow.modelData.has_pin ? qsTr("Change PIN")
                                                        : qsTr("Set PIN")
                                enabled: !screen.busy
                                onClicked: profileRow.openEditor(
                                    profileRow.modelData.has_pin ? "pin-new" : "pin-set")
                            }

                            AppButton {
                                weight: "quiet"
                                text: qsTr("Remove")
                                enabled: !screen.busy
                                // The server refuses to delete the last profile
                                // and answers with its own message; the client
                                // does not pre-empt that with a rule of its own.
                                // Any confirmation belongs to whoever wired
                                // this signal, not to the screen that raised it.
                                onClicked: screen.deleteProfileRequested(profileRow.modelData.id)
                            }
                        }
                    }

                    // -- rename ------------------------------------------
                    ColumnLayout {
                        Layout.fillWidth: true
                        visible: profileRow.editing === "rename"
                        spacing: Theme.spaceSm

                        AppTextField {
                            id: renameField
                            Layout.fillWidth: true
                            Layout.minimumWidth: screen.minimumFieldWidth
                            Layout.maximumWidth: screen.maximumFieldWidth
                            label: qsTr("Name")
                            enabled: !screen.busy
                            onAccepted: profileRow.saveRename()
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spaceSm

                            AppButton {
                                weight: "primary"
                                text: qsTr("Save")
                                enabled: !screen.busy
                                         && renameField.text.trim().length > 0
                                onClicked: profileRow.saveRename()
                            }

                            AppButton {
                                weight: "quiet"
                                text: qsTr("Cancel")
                                // Staying available while busy is deliberate:
                                // an editor that cannot be dismissed until the
                                // request finishes is a page with no way out.
                                onClicked: profileRow.closeEditor()
                            }
                        }
                    }

                    // -- PIN ----------------------------------------------
                    ColumnLayout {
                        Layout.fillWidth: true
                        visible: profileRow.editing !== ""
                                && profileRow.editing !== "rename"
                        spacing: Theme.spaceSm

                        Text {
                            Layout.fillWidth: true
                            text: profileRow.editing === "pin-clear"
                                  ? qsTr("Confirm the current PIN to remove it.")
                                  : qsTr("Four to eight digits.")
                            color: Theme.textMuted
                            font.pixelSize: Theme.typeMicro
                            font.family: Theme.family
                            wrapMode: Text.WordWrap
                        }

                        // Asked for in both the change and the clear case: the
                        // server refuses either one without it, so asking for
                        // it up front is the difference between one request
                        // and two.
                        AppTextField {
                            id: currentPinField
                            Layout.fillWidth: true
                            Layout.minimumWidth: screen.minimumFieldWidth
                            Layout.maximumWidth: screen.maximumFieldWidth
                            label: qsTr("Current PIN")
                            echoMode: TextInput.Password
                            maximumLength: 8
                            inputMethodHints: Qt.ImhDigitsOnly
                            enabled: !screen.busy
                            visible: profileRow.editing === "pin-new"
                                     || profileRow.editing === "pin-clear"
                            onAccepted: profileRow.editing === "pin-clear"
                                        ? profileRow.savePin()
                                        : newPinField.forceActiveFocus()
                        }

                        AppTextField {
                            id: newPinField
                            Layout.fillWidth: true
                            Layout.minimumWidth: screen.minimumFieldWidth
                            Layout.maximumWidth: screen.maximumFieldWidth
                            label: qsTr("New PIN")
                            echoMode: TextInput.Password
                            maximumLength: 8
                            inputMethodHints: Qt.ImhDigitsOnly
                            enabled: !screen.busy
                            visible: profileRow.editing === "pin-set"
                                     || profileRow.editing === "pin-new"
                            onAccepted: profileRow.savePin()
                        }

                        Text {
                            Layout.fillWidth: true
                            visible: newPinField.visible
                                     && newPinField.text.length > 0
                                     && newPinField.text.length < 4
                            text: qsTr("A PIN needs at least four digits.")
                            color: Theme.textMuted
                            font.pixelSize: Theme.typeMicro
                            font.family: Theme.family
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: Theme.spaceSm

                            AppButton {
                                weight: "primary"
                                text: qsTr("Save")
                                enabled: !screen.busy && profileRow.pinAcceptable()
                                onClicked: profileRow.savePin()
                            }

                            AppButton {
                                weight: "quiet"
                                text: qsTr("Cancel")
                                // Staying available while busy is deliberate:
                                // an editor that cannot be dismissed until the
                                // request finishes is a page with no way out.
                                onClicked: profileRow.closeEditor()
                            }

                            AppButton {
                                weight: "quiet"
                                text: qsTr("Remove PIN")
                                visible: profileRow.editing === "pin-new"
                                         && profileRow.modelData.has_pin
                                enabled: !screen.busy
                                onClicked: profileRow.openEditor("pin-clear")
                            }
                        }
                    }

                    // -- the row's own behaviour ----------------------------
                    // Declared here rather than on the screen because a
                    // delegate's fields belong to the delegate; the screen has
                    // no way to reach them and no business emptying them.
                    function openEditor(mode) {
                        closeEditor()
                        editing = mode
                        if (mode === "rename")
                            renameField.forceActiveFocus()
                        else if (profileRow.modelData.has_pin || mode === "pin-clear")
                            currentPinField.forceActiveFocus()
                        else
                            newPinField.forceActiveFocus()
                    }

                    function closeEditor() {
                        editing = ""
                        // Cleared, never rendered.  A PIN that stays in a field
                        // after the request has gone is a PIN left on a screen
                        // somebody else may walk up to.
                        renameField.text = ""
                        currentPinField.text = ""
                        newPinField.text = ""
                    }

                    function saveRename() {
                        const name = renameField.text.trim()
                        if (name.length === 0)
                            return
                        screen.renameProfileRequested(profileRow.modelData.id, name)
                        closeEditor()
                    }

                    /// The server's rule, stated rather than discovered: a PIN
                    /// is 4 to 8 digits (the field caps the top), and once one
                    /// exists it cannot be changed or removed without the
                    /// current one.
                    function pinAcceptable() {
                        if (editing === "pin-set")
                            return newPinField.text.length >= 4
                        if (editing === "pin-clear")
                            return currentPinField.text.length >= 4
                        if (editing === "pin-new")
                            return currentPinField.text.length >= 4
                                   && newPinField.text.length >= 4
                                   && newPinField.text !== currentPinField.text
                        return false
                    }

                    function savePin() {
                        if (!pinAcceptable())
                            return
                        if (editing === "pin-set")
                            screen.setPinRequested(profileRow.modelData.id, newPinField.text)
                        else if (editing === "pin-clear")
                            screen.clearPinRequested(profileRow.modelData.id,
                                                     currentPinField.text)
                        else
                            screen.changePinRequested(profileRow.modelData.id,
                                                     currentPinField.text,
                                                     newPinField.text)
                        closeEditor()
                    }
                }
            }
        }

        // -- sessions ---------------------------------------------------
        ColumnLayout {
            id: sessionsSection
            Layout.fillWidth: true
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            spacing: Theme.spaceMd

            Text {
                Layout.fillWidth: true
                text: qsTr("Sessions")
                color: Theme.textMuted
                font.pixelSize: Theme.typeMicro
                font.family: Theme.family
                font.capitalization: Font.AllUppercase
            }

            AppButton {
                weight: "quiet"
                text: qsTr("Sign out everywhere")
                enabled: !screen.busy
                onClicked: screen.logoutEverywhereRequested()
            }

            Text {
                Layout.fillWidth: true
                visible: screen.sessionCount === 0
                text: qsTr("No other signed-in devices.")
                color: Theme.textMuted
                font.pixelSize: Theme.typeBody
                font.family: Theme.family
                wrapMode: Text.WordWrap
            }

            Repeater {
                model: screen.sessions

                delegate: RowLayout {
                    id: sessionRow
                    // Required for the same reason as the profile row's.
                    required property var modelData
                    required property int index

                    Layout.fillWidth: true
                    spacing: Theme.spaceSm

                    // Dimmed rather than hidden: an ended session is still a
                    // fact somebody may want to read, and the reason it ended
                    // is usually the reason they opened this screen.
                    readonly property bool revoked:
                        sessionRow.modelData.revoked_at !== undefined
                        && sessionRow.modelData.revoked_at !== null
                        && String(sessionRow.modelData.revoked_at).length > 0

                    opacity: sessionRow.revoked ? 0.5 : 1.0

                    /// The device name, or "This device" when the server has
                    /// none -- a session with a blank name is still a session.
                    readonly property string deviceLabel:
                        (sessionRow.modelData.device_name
                         && String(sessionRow.modelData.device_name).length > 0)
                        ? String(sessionRow.modelData.device_name)
                        : qsTr("This device")

                    /// The two facts below the name, joined only when both are
                    /// there.  A server that sends no address still dates the
                    /// session, and a separator with nothing after it reads as
                    /// a rendering fault.
                    readonly property string whenAndWhere: {
                        const parts = []
                        const used = sessionRow.modelData.last_used_at
                        if (used && String(used).length > 0)
                            parts.push(String(used))
                        const address = sessionRow.modelData.ip_address
                        if (address && String(address).length > 0)
                            parts.push(String(address))
                        return parts.join(" · ")
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Theme.spaceXs

                        Text {
                            Layout.fillWidth: true
                            text: sessionRow.deviceLabel
                            color: Theme.text
                            font.pixelSize: Theme.typeBody
                            font.family: Theme.family
                            wrapMode: Text.WordWrap
                        }

                        Text {
                            Layout.fillWidth: true
                            text: sessionRow.whenAndWhere
                            color: Theme.textFaint
                            font.pixelSize: Theme.typeMicro
                            font.family: Theme.family
                            wrapMode: Text.WordWrap
                        }
                    }

                    Text {
                        text: qsTr("Ended")
                        color: Theme.textFaint
                        font.pixelSize: Theme.typeMicro
                        font.family: Theme.family
                        visible: sessionRow.revoked
                    }

                    AppButton {
                        weight: "quiet"
                        text: qsTr("End")
                        visible: !sessionRow.revoked
                        enabled: !screen.busy
                        onClicked: screen.revokeSessionRequested(
                            String(sessionRow.modelData.id))
                    }
                }
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: Responsive.pageMargin
        }
    }

    // -- the sections, written once --------------------------------------
    // A Loader parents the item it loads to *itself*, so a section cannot use
    // Layout attached properties: it sizes itself from the loader's width, and
    // the page gets its height from the section's implicit height.  That is
    // the price of moving the same three sections between a two-column row and
    // a single column when the window changes size, and it is cheaper than
    // writing them twice.
    Component {
        id: accountSection

        ColumnLayout {
            width: parent ? parent.width : 0
            spacing: Theme.spaceMd

            Text {
                Layout.fillWidth: true
                text: qsTr("Account")
                color: Theme.textMuted
                font.pixelSize: Theme.typeMicro
                font.family: Theme.family
                font.capitalization: Font.AllUppercase
            }

            Text {
                Layout.fillWidth: true
                text: (screen.account && screen.account.username)
                      ? String(screen.account.username) : ""
                color: Theme.text
                font.pixelSize: Theme.typeTitle
                font.family: Theme.family
                font.weight: Font.DemiBold
                wrapMode: Text.WordWrap
            }

            Text {
                Layout.fillWidth: true
                visible: screen.account && screen.account.last_login_at
                         && String(screen.account.last_login_at).length > 0
                text: qsTr("Last signed in %1")
                      .arg(String(screen.account.last_login_at))
                color: Theme.textMuted
                font.pixelSize: Theme.typeCaption
                font.family: Theme.family
                wrapMode: Text.WordWrap
            }

            // Said once, quietly.  It is not an action and it does not need to
            // be a badge.
            Text {
                Layout.fillWidth: true
                visible: !!(screen.account && screen.account.is_admin)
                text: qsTr("This account is an administrator on this server.")
                color: Theme.textFaint
                font.pixelSize: Theme.typeMicro
                font.family: Theme.family
                wrapMode: Text.WordWrap
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: Theme.spaceXs
                spacing: Theme.spaceSm

                AppButton {
                    weight: "quiet"
                    text: qsTr("Switch profile")
                    enabled: !screen.busy
                    onClicked: screen.switchProfileRequested()
                }

                AppButton {
                    weight: "quiet"
                    text: qsTr("Sign out")
                    enabled: !screen.busy
                    onClicked: screen.signOutRequested()
                }
            }
        }
    }

    Component {
        id: passwordSection

        /// A template with an id, so that the section and the function that
        /// acts on it are the same object.  An `Item` rather than a
        /// `ColumnLayout`: the loaded item is parented to its Loader, and a
        /// Loader has no layout, so the section needs a layout of its own for
        /// the attached properties to apply to.
        Item {
            id: form
            width: parent ? parent.width : 0
            implicitHeight: formColumn.implicitHeight

            ColumnLayout {
                id: formColumn
                width: parent.width
                spacing: Theme.spaceMd

                Text {
                    Layout.fillWidth: true
                    text: qsTr("Password")
                    color: Theme.textMuted
                    font.pixelSize: Theme.typeMicro
                    font.family: Theme.family
                    font.capitalization: Font.AllUppercase
                }

                AppTextField {
                    id: currentField
                    Layout.fillWidth: true
                    Layout.minimumWidth: screen.minimumFieldWidth
                    Layout.maximumWidth: screen.maximumFieldWidth
                    label: qsTr("Current password")
                    echoMode: TextInput.Password
                    enabled: !screen.busy
                    onAccepted: nextField.forceActiveFocus()
                }

                AppTextField {
                    id: nextField
                    Layout.fillWidth: true
                    Layout.minimumWidth: screen.minimumFieldWidth
                    Layout.maximumWidth: screen.maximumFieldWidth
                    label: qsTr("New password")
                    echoMode: TextInput.Password
                    enabled: !screen.busy
                    onAccepted: confirmField.forceActiveFocus()
                }

                AppTextField {
                    id: confirmField
                    Layout.fillWidth: true
                    Layout.minimumWidth: screen.minimumFieldWidth
                    Layout.maximumWidth: screen.maximumFieldWidth
                    label: qsTr("Confirm new password")
                    echoMode: TextInput.Password
                    enabled: !screen.busy
                    onAccepted: form.submitPassword()
                }

                // The two things that can be wrong, said before submitting
                // rather than returned as a rejection afterwards.  The wording
                // is the forced-password screen's, because it is the same
                // policy and a message that changes with the screen that shows
                // it is a message nobody can search for.
                Text {
                    Layout.fillWidth: true
                    visible: nextField.text.length > 0
                             && nextField.text.length < 10
                    text: qsTr("At least 10 characters.")
                    color: Theme.textMuted
                    font.pixelSize: Theme.typeMicro
                    font.family: Theme.family
                }

                Text {
                    Layout.fillWidth: true
                    visible: confirmField.text.length > 0
                             && confirmField.text !== nextField.text
                    text: qsTr("The two passwords do not match.")
                    color: Theme.textMuted
                    font.pixelSize: Theme.typeMicro
                    font.family: Theme.family
                }

                AppButton {
                    Layout.fillWidth: true
                    Layout.maximumWidth: screen.maximumFieldWidth
                    Layout.topMargin: Theme.spaceXs
                    weight: "primary"
                    text: qsTr("Change password")
                    busy: screen.busy
                    enabled: currentField.text.length > 0
                              && nextField.text.length >= 10
                              && nextField.text === confirmField.text
                              && nextField.text !== currentField.text
                    onClicked: form.submitPassword()
                }
            }

            // Not on the screen: a Loaded item's fields are not reachable from
            // outside the component, so the function that empties them lives
            // beside them.
            function submitPassword() {
                screen.changePasswordRequested(currentField.text, nextField.text)
                currentField.text = ""
                nextField.text = ""
                confirmField.text = ""
            }
        }
    }

    Component {
        id: serverSection

        ColumnLayout {
            width: parent ? parent.width : 0
            spacing: Theme.spaceMd

            Text {
                Layout.fillWidth: true
                text: qsTr("Server")
                color: Theme.textMuted
                font.pixelSize: Theme.typeMicro
                font.family: Theme.family
                font.capitalization: Font.AllUppercase
            }

            Text {
                Layout.fillWidth: true
                visible: screen.serverInfo !== ""
                text: screen.serverInfo
                color: Theme.textMuted
                font.pixelSize: Theme.typeBody
                font.family: Theme.family
                wrapMode: Text.WordWrap
            }

            Text {
                Layout.fillWidth: true
                visible: screen.hasCredentialStorage
                text: qsTr("Credentials are kept in %1.")
                      .arg(screen.credentialBackend)
                color: Theme.textMuted
                font.pixelSize: Theme.typeMicro
                font.family: Theme.family
                wrapMode: Text.WordWrap
            }

            // A missing keychain is not a detail about this installation, it
            // is the reason a remembered login will not be there next time --
            // so it is stated in the same place the buttons are, before
            // somebody taps "Stay signed in" somewhere else and expects it.
            Text {
                Layout.fillWidth: true
                visible: !screen.hasCredentialStorage
                text: qsTr("This device has no secure key storage, so remembered logins are not stored on it.")
                color: Theme.textMuted
                font.pixelSize: Theme.typeMicro
                font.family: Theme.family
                wrapMode: Text.WordWrap
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: Theme.spaceXs
                spacing: Theme.spaceSm

                AppButton {
                    weight: "quiet"
                    text: qsTr("Forget this server")
                    enabled: !screen.busy
                    onClicked: screen.forgetServerRequested()
                }

                AppButton {
                    weight: "quiet"
                    text: qsTr("Sign out")
                    enabled: !screen.busy
                    onClicked: screen.signOutRequested()
                }
            }
        }
    }

    // The desktop's left column: who you are, and what you are connected to.
    Component {
        id: identityColumn

        ColumnLayout {
            width: parent ? parent.width : 0
            spacing: Responsive.sectionGap

            Loader {
                Layout.fillWidth: true
                sourceComponent: accountSection
            }

            Loader {
                Layout.fillWidth: true
                sourceComponent: serverSection
            }
        }
    }

    // Everything else: one column, in the order the sections are read.
    Component {
        id: stackedColumn

        ColumnLayout {
            width: parent ? parent.width : 0
            spacing: Responsive.sectionGap

            Loader {
                Layout.fillWidth: true
                sourceComponent: accountSection
            }

            Loader {
                Layout.fillWidth: true
                sourceComponent: passwordSection
            }

            Loader {
                Layout.fillWidth: true
                sourceComponent: serverSection
            }
        }
    }
}
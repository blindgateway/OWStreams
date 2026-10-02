pragma Singleton

import QtQuick

/// The app's singletons, in one place.
///
/// QML context properties (`session`, `api`, `credentials`) are injected from
/// C++ and are therefore invisible to `qmllint`: every unqualified reference
/// to them is a warning, and a typo in one is a runtime failure rather than a
/// lint error.
///
/// So the context properties are bound to *named* properties here, once, and
/// everything else in the UI reads `App.session`, `App.credentials`.  The
/// linter can then check every use, and a renamed property fails the build.
QtObject {
    // QML cannot assign to a `readonly` property, and C++ context properties
    // cannot write into a singleton.  These are therefore `property var`,
    // bound once from Main.qml on the `Component.onCompleted` of the window --
    // which runs before any Loader instantiates a screen.
    property var session: null
    property var api: null
    property var credentials: null

    /// Whether the credential store found a platform keychain.  When false the
    /// app refuses to persist a session rather than writing a token to a file.
    readonly property bool hasSecureStorage:
        credentials !== null && credentials.available === true
}
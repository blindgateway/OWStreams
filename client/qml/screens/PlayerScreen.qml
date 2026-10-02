pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtMultimedia
import Stream

/// The player.
///
/// Video fills the item and everything else is a thin bar over it: a title bar
/// at the top, a scrubber and transport at the bottom, and a pane in the middle
/// for the two states that are not video -- buffering, and failed.  None of it
/// is a card, a panel or a floating rounded rectangle; over a moving picture,
/// chrome that reads as chrome is what makes a player feel like a video player.
///
/// Fullscreen is the shell's job.  This screen fills whatever it is given and
/// asks for nothing, so the same file is the player in a phone, in a window
/// and in a full-screen window.
Item {
    id: screen

    /// PlaybackController *
    property var controller: null

    /// ApiClient *, used for the poster fallback: a title with no poster key
    /// still has artwork most of the time, and the URL for it is a route, not
    /// a field on the media object.
    property var api: null

    /// `AppController.captureGuard` -- set while the player is on screen, and
    /// only then.  The guard is given a window rather than being handed a QML
    /// object because what it protects is a native window handle, and no QML
    /// property can name one.
    property var guard: null

    /// The server's media object, remembered so the retry button can re-open
    /// the title properly.  By the time the screen is showing an error the
    /// controller no longer has it -- the error is what stopped the open.
    property var media: ({})

    /// Everything the screen *asks* for goes out as a signal: this screen does
    /// not know what is next in a queue, and does not get to decide.
    signal closeRequested()
    signal nextRequested()
    signal previousRequested()
    signal fullscreenRequested()

    // Named enum values, not numbers.  `PlaybackController` carries QML_ELEMENT
    // now, so the linter resolves these -- and a number here would be a silent
    // break the moment somebody inserted a state above Buffering.
    readonly property bool preparing:
        controller !== null
        && (controller.state === PlaybackController.Idle
            || controller.state === PlaybackController.Preparing)
    readonly property bool buffering:
        controller !== null && controller.state === PlaybackController.Buffering
    readonly property bool failed:
        controller !== null && controller.state === PlaybackController.Error
    readonly property bool controlsUp: controller === null || controller.controlsVisible

    readonly property string posterUrl: controller && controller.posterUrl
                                       ? controller.posterUrl
                                       : (api && media && media.poster_key
                                          ? api.baseUrl + "/api/v1/media/" + media.id + "/poster"
                                          : "")

    implicitWidth: 640
    implicitHeight: 360
    focus: true

    // -- the picture -------------------------------------------------------
    VideoOutput {
        id: picture
        anchors.fill: parent
        fillMode: VideoOutput.PreserveAspectFit
        // An error is not a frozen frame with a message on top of it; the
        // message is the whole screen.
        visible: !screen.failed
    }

    // A poster while the source is being negotiated.  A cold start on a
    // spinning disk is several seconds of black, and a still of the film is
    // the difference between "slow" and "broken".
    Image {
        id: poster
        anchors.centerIn: parent
        width: Math.min(parent.width * 0.4, parent.height * 0.6)
        height: width * 1.5
        visible: screen.preparing && status === Image.Ready
        asynchronous: true
        cache: true
        fillMode: Image.PreserveAspectFit
        source: screen.posterUrl
        // A title with no artwork is normal, not a failure: clearing the
        // source takes the broken-image path out of the picture entirely.
        onStatusChanged: if (status === Image.Error) source = ""
    }

    // The click surface sits *under* the chrome, so a tap on the video brings
    // the controls back and a tap on a button hits the button.
    MouseArea {
        id: surface
        anchors.fill: parent
        onClicked: if (screen.controller)
                       screen.controller.setControlsVisible(true)
                   else
                       screen.retry()
        onWheel: function (wheel) {
            if (!screen.controller)
                return
            screen.controller.setControlsVisible(true)
            screen.controller.seekRelative(wheel.angleDelta.y > 0 ? 10000 : -10000)
        }
    }

    // -- buffering and failure ----------------------------------------------
    StatusPane {
        anchors.fill: parent
        visible: screen.buffering || screen.failed
        mode: screen.failed ? "error" : (screen.buffering ? "loading" : "")
        message: screen.controller ? screen.controller.errorMessage : ""
        actionText: qsTr("Try again")
        retryable: screen.failed
        onActionTriggered: screen.retry()
    }

    // -- title bar ----------------------------------------------------------
    Item {
        id: titleBar
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 64

        // A flat scrim, not a gradient: one overlay is enough to keep text
        // legible over a bright frame, and a gradient laid over moving video is
        // the single thing that most makes a cheap player look cheap.
        Rectangle {
            anchors.fill: parent
            color: Theme.overlay
            opacity: titleBar.opacity
        }

        opacity: screen.controlsUp ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }

        AppButton {
            anchors.left: parent.left
            anchors.leftMargin: Theme.spaceLg
            anchors.verticalCenter: parent.verticalCenter
            weight: "quiet"
            text: qsTr("Back")
            onClicked: screen.closeRequested()
        }

        Text {
            anchors.left: parent.left
            anchors.leftMargin: Theme.spaceXl * 4
            anchors.right: timecode.left
            anchors.rightMargin: Theme.spaceLg
            anchors.verticalCenter: parent.verticalCenter
            text: screen.controller ? screen.controller.title : ""
            color: Theme.text
            font.pixelSize: Theme.typeTitle
            font.family: Theme.family
            font.weight: Font.DemiBold
            elide: Text.ElideRight
        }

        Text {
            id: timecode
            anchors.right: parent.right
            anchors.rightMargin: Theme.spaceLg
            anchors.top: parent.top
            anchors.topMargin: Theme.spaceSm
            text: screen.formatTime(screen.controller ? screen.controller.position : 0)
                  + " / "
                  + screen.formatTime(screen.controller ? screen.controller.duration : 0)
            color: Theme.textMuted
            font.pixelSize: Theme.typeCaption
            font.family: Theme.family
        }
    }

    // -- the transport ------------------------------------------------------
    Item {
        id: chrome
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 88

        Rectangle {
            anchors.fill: parent
            color: Theme.overlay
            opacity: chrome.opacity
        }

        opacity: screen.controlsUp ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }

        Column {
            anchors.fill: parent
            anchors.leftMargin: Theme.spaceLg
            anchors.rightMargin: Theme.spaceLg
            anchors.bottomMargin: Theme.spaceMd
            spacing: Theme.spaceXs

            // -- the scrubber -----------------------------------------------
            Slider {
                id: scrubber
                width: parent.width
                enabled: screen.controller ? screen.controller.seekable : false
                from: 0
                to: Math.max(1, screen.controller ? screen.controller.duration : 0)
                // `live` so the value follows the thumb while it is being
                // dragged: the seek has to happen during the drag, not on the
                // release, or a two-hour film is unusable with a mouse.
                live: true
                onMoved: if (screen.controller) screen.controller.seek(value)

                Behavior on value {
                    NumberAnimation { duration: Theme.motionBase; easing.type: Easing.OutCubic }
                }

                // A separate `Binding` rather than `value: pressed ? value : x`:
                // that form reads `value` from inside its own binding, which Qt
                // detects as a loop, drops, and leaves a scrubber that stops
                // following the film at all.  `when` is the same guard.
                Binding {
                    target: scrubber
                    property: "value"
                    value: screen.controller ? screen.controller.position : 0
                    when: !scrubber.pressed
                    restoreMode: Binding.RestoreBindingOrValue
                }

                // The filled part is the accent, the rest a hairline.  That is
                // the entire design of a progress bar, and the default style's
                // groove-and-shadow has neither of them.
                background: Item {
                    x: scrubber.leftPadding
                    y: scrubber.topPadding + scrubber.availableHeight / 2 - height / 2
                    width: scrubber.availableWidth
                    height: 4

                    Rectangle {
                        anchors.fill: parent
                        radius: 2
                        color: Theme.hairline
                    }

                    Rectangle {
                        anchors.left: parent.left
                        anchors.top: parent.top
                        anchors.bottom: parent.bottom
                        width: scrubber.visualPosition * parent.width
                        radius: 2
                        color: scrubber.enabled ? Theme.accent : Theme.textFaint
                        Behavior on width { NumberAnimation { duration: Theme.motionFast } }
                    }
                }

                handle: Rectangle {
                    x: scrubber.leftPadding + scrubber.visualPosition
                       * (scrubber.availableWidth - width) - width / 2
                    y: scrubber.topPadding + scrubber.availableHeight / 2 - height / 2
                    implicitWidth: 12
                    implicitHeight: 12
                    radius: 6
                    color: scrubber.pressed ? Theme.accent : Theme.text
                    opacity: scrubber.enabled ? 1 : 0
                    Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                }
            }

            // -- the transport row ------------------------------------------
            Item {
                width: parent.width
                height: 44

                Row {
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Theme.spaceSm

                    AppButton {
                        weight: "primary"
                        text: ""
                        implicitWidth: 44
                        onClicked: if (screen.controller) screen.controller.togglePlay()

                        Icon {
                            anchors.centerIn: parent
                            width: 24
                            height: 24
                            name: screen.controller && screen.controller.isPlaying
                                   ? "pause" : "play"
                            color: Theme.accentText
                        }
                    }

                    AppButton {
                        weight: "quiet"
                        text: ""
                        implicitWidth: 44
                        onClicked: if (screen.controller) screen.controller.seekRelative(-10000)

                        Icon {
                            anchors.centerIn: parent
                            width: 22
                            height: 22
                            name: "back"
                            color: Theme.text
                        }
                    }

                    AppButton {
                        weight: "quiet"
                        text: ""
                        implicitWidth: 44
                        onClicked: if (screen.controller) screen.controller.seekRelative(10000)

                        Icon {
                            anchors.centerIn: parent
                            width: 22
                            height: 22
                            name: "forward"
                            color: Theme.text
                        }
                    }

                    // -- volume ------------------------------------------------
                    AppButton {
                        weight: "quiet"
                        text: ""
                        implicitWidth: 44
                        onClicked: if (screen.controller) screen.controller.toggleMuted()

                        Icon {
                            anchors.centerIn: parent
                            width: 22
                            height: 22
                            name: "volume"
                            // The icon set has one speaker glyph, so the state
                            // is carried by colour and by the slider beside it
                            // rather than by a second drawing of the same icon.
                            color: (screen.controller
                                    && (screen.controller.muted || screen.controller.volume === 0))
                                   ? Theme.textFaint : Theme.text
                        }
                    }

                    Slider {
                        id: volume
                        width: 90
                        from: 0
                        to: 1
                        // The same 0.05 the arrow keys move, so the keyboard
                        // and the mouse agree on what one step is.
                        stepSize: 0.05
                        live: true
                        onMoved: if (screen.controller) screen.controller.setVolume(value)

                        Binding {
                            target: volume
                            property: "value"
                            value: screen.controller ? screen.controller.volume : 1
                            when: !volume.pressed
                            restoreMode: Binding.RestoreBindingOrValue
                        }

                        background: Item {
                            x: volume.leftPadding
                            y: volume.topPadding + volume.availableHeight / 2 - height / 2
                            width: volume.availableWidth
                            height: 4

                            Rectangle {
                                anchors.fill: parent
                                radius: 2
                                color: Theme.hairline
                            }

                            Rectangle {
                                anchors.left: parent.left
                                anchors.top: parent.top
                                anchors.bottom: parent.bottom
                                width: volume.visualPosition * parent.width
                                radius: 2
                                color: Theme.accent
                            }
                        }

                        handle: Rectangle {
                            x: volume.leftPadding + volume.visualPosition
                               * (volume.availableWidth - width) - width / 2
                            y: volume.topPadding + volume.availableHeight / 2 - height / 2
                            implicitWidth: 10
                            implicitHeight: 10
                            radius: 5
                            color: Theme.text
                        }
                    }
                }

                // -- the track pickers ----------------------------------------
                // Two rows rather than one, because they are two independent
                // lists and lumping them together is what makes a picker look
                // like a menu of unrelated options.  Each names the *current*
                // selection: cycling with nothing named leaves a viewer with
                // no idea what they are hearing or reading right now.
                Row {
                    id: pickers
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Theme.spaceSm

                    // One row, two buttons, and they swap ends when the audio
                    // track has a longer name than the subtitle one -- laid
                    // out as two independent rows the shorter one would leave a
                    // gap, and a gap between two related pickers reads as a
                    // third, empty, control.
                    Item {
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.max(audio.width, subtitles.width)
                        height: Math.max(audio.height, subtitles.height)

                        AppButton {
                            id: subtitles
                            anchors.right: parent.right
                            weight: "quiet"
                            visible: screen.controller
                                     ? screen.controller.subtitleTracksAvailable : false
                            text: (screen.controller && screen.controller.subtitleEnabled)
                                  ? screen.controller.subtitleTrackNames[
                                        screen.controller.subtitleTrackIndex]
                                  : qsTr("Subtitles off")
                            onClicked: if (screen.controller)
                                           screen.controller.toggleSubtitles()
                        }

                        AppButton {
                            id: audio
                            anchors.right: subtitles.visible ? subtitles.left : parent.right
                            anchors.rightMargin: subtitles.visible ? Theme.spaceSm : 0
                            weight: "quiet"
                            visible: screen.controller
                                     ? screen.controller.audioTracksAvailable : false
                            text: (screen.controller && screen.controller.audioTrackIndex >= 0)
                                  ? screen.controller.audioTrackNames[
                                        screen.controller.audioTrackIndex]
                                  : qsTr("Audio")
                            onClicked: if (screen.controller)
                                           screen.controller.cycleAudioTrack()
                        }
                    }
                }
            }
        }
    }

    // -- input ---------------------------------------------------------------
    // The controller decides when the controls go away; anything that reaches
    // the screen is interaction, and tells it so.
    Keys.onPressed: function (event) {
        if (!screen.controller) {
            if (event.key === Qt.Key_Space || event.key === Qt.Key_Return) {
                screen.retry()
                event.accepted = true
            }
            return
        }

        switch (event.key) {
        case Qt.Key_Space:
        case Qt.Key_K:
            screen.controller.togglePlay()
            break
        case Qt.Key_Left:
            screen.controller.seekRelative(-10000)
            break
        case Qt.Key_Right:
            screen.controller.seekRelative(10000)
            break
        case Qt.Key_Up:
            screen.controller.setVolume(screen.controller.volume + 0.05)
            break
        case Qt.Key_Down:
            screen.controller.setVolume(screen.controller.volume - 0.05)
            break
        case Qt.Key_M:
            screen.controller.toggleMuted()
            break
        case Qt.Key_S:
            screen.controller.toggleSubtitles()
            break
        case Qt.Key_F:
            screen.fullscreenRequested()
            break
        case Qt.Key_Escape:
            // Escape is the shell's business: it owns fullscreen, so it has to
            // be able to bring a full-screen player back to a window.
            screen.closeRequested()
            break
        case Qt.Key_N:
            screen.controller.requestNext()
            break
        case Qt.Key_P:
            screen.controller.requestPrevious()
            break
        default:
            return
        }

        screen.controller.setControlsVisible(true)
        event.accepted = true
    }

    // The controller emits the queue requests as signals; forwarding them
    // keeps one vocabulary at the boundary -- the shell only ever sees
    // PlayerScreen's signals, never the controller's.
    Connections {
        target: screen.controller
        function onNextRequested() { screen.nextRequested() }
        function onPreviousRequested() { screen.previousRequested() }
    }

    Component.onCompleted: {
        // The frames go where this screen draws them.  `videoSink` rather than
        // the item itself: `VideoOutput` is not a `QVideoSink`, it owns one.
        if (screen.controller && screen.controller.setVideoOutput)
            screen.controller.setVideoOutput(picture.videoSink)
        // Protect the window *before* the first frame rather than when somebody
        // presses play: a window that is unprotected between opening the title
        // and pressing play is a window the taskbar has already thumbnailed,
        // and on Android the thumbnail is the capture this is meant to stop.
        if (screen.guard)
            screen.guard.protect(screen.window)
        // Deferred by a turn of the event loop: a Loader-created item is not
        // focusable until the loader has finished its own activation, and a
        // `forceActiveFocus()` that runs too early returns false silently.
        Qt.callLater(function () { screen.forceActiveFocus() })
    }

    // A re-map drops whatever was set on the native window -- a resolution
    // change or a compositor restart destroys the surface and makes a new one
    // from the same QML item -- so this is not a one-shot call.  It runs on
    // `visible` becoming true rather than on every frame for that reason.
    onVisibleChanged: {
        if (screen.visible && screen.guard)
            screen.guard.protect(screen.window)
    }

    Component.onDestruction: {
        // Detach before the `VideoOutput` goes away: a destroyed sink still
        // attached to the backend is a dangling frame target.
        if (screen.controller && screen.controller.setVideoOutput)
            screen.controller.setVideoOutput(null)
        if (screen.guard)
            screen.guard.release()
    }

    /// Leave, recording the position first.
    ///
    /// `close()` before the signal, not after and not never: it is the only
    /// thing that tells the server where the viewer stopped, and navigating
    /// first means the screen may already be gone when that happens.
    function leave() {
        if (screen.controller)
            screen.controller.close()
        screen.closeRequested()
    }

    /// Re-open the title.  A retry has to mint a new playback token rather
    /// than call `play()` again: what this button exists for is a source that
    /// has become valid again -- a token that expired, a file the scan found --
    /// and none of those are fixed by pressing play on the old one.
    function retry() {
        if (screen.controller)
            screen.controller.open(screen.controller.mediaId, screen.media)
    }

    /// `h:mm:ss`, or `m:ss` under an hour.  Written out rather than taken from
    /// a locale-aware formatter because a timecode is not a date: it reads the
    /// same in every language, and `0:05` is unambiguous everywhere.
    function formatTime(ms) {
        if (!ms || ms < 0)
            return "0:00"
        const total = Math.floor(ms / 1000)
        const seconds = total % 60
        const minutes = Math.floor(total / 60) % 60
        const hours = Math.floor(total / 3600)
        const pad = function (n) { return n < 10 ? "0" + n : String(n) }
        if (hours > 0)
            return hours + ":" + pad(minutes) + ":" + pad(seconds)
        return minutes + ":" + pad(seconds)
    }

    onCloseRequested: leave()
}
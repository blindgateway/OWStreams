pragma Singleton
import QtQuick

/// The design tokens.
///
/// One file, referred to by name everywhere else.  This is the thing that
/// makes the app look deliberate rather than assembled: spacing is a scale,
/// not a series of numbers someone chose per screen; type is four sizes with
/// defined weights; there is exactly one accent colour and one set of surfaces.
///
/// The palette is warm-neutral rather than blue-black, and the accent is a
/// desaturated amber rather than a saturated brand hue.  Both are choices
/// about looking like a service rather than a template; neither is arbitrary.
QtObject {
    // -- spacing: a 4 px base, used as 4/8/12/16/24/32/48 ------------------------
    readonly property int spaceXs: 4
    readonly property int spaceSm: 8
    readonly property int spaceMd: 12
    readonly property int spaceLg: 16
    readonly property int spaceXl: 24
    readonly property int spaceXxl: 32
    readonly property int spaceHuge: 48

    // -- type ---------------------------------------------------------------
    readonly property string family: Qt.application.font.family
    readonly property int typeDisplay: 34
    readonly property int typeTitle: 22
    readonly property int typeBody: 15
    readonly property int typeCaption: 13
    readonly property int typeMicro: 11

    // -- shape --------------------------------------------------------------
    // Cards are 8 px.  Larger radii are what make a UI read as "soft template",
    // and a poster grid is rectangles, not pills.
    readonly property int radiusSm: 4
    readonly property int radiusMd: 8
    readonly property int radiusPill: 999

    // -- colour -------------------------------------------------------------
    readonly property color background:  "#101014"
    readonly property color surface:     "#17171C"
    readonly property color surfaceHigh: "#1F1F26"
    readonly property color overlay:     "#00000099"

    readonly property color text:         "#F2F2F4"
    readonly property color textMuted:    "#9A9AA6"
    readonly property color textFaint:    "#6C6C78"

    readonly property color accent:       "#E0A458"
    readonly property color accentText:   "#101014"
    readonly property color danger:       "#D9634F"

    readonly property color hairline:     "#FFFFFF14"

    // -- motion -------------------------------------------------------------
    // Short and flat.  A streaming UI should feel instant; nothing here is
    // long enough to become something to wait for.
    readonly property int motionFast: 120
    readonly property int motionBase: 200
    readonly property int motionSlow: 320
    readonly property int motionCurve: 3   // OutCubic
}

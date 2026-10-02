pragma Singleton
import QtQuick

/// One place that answers "is this a phone, a tablet, or a desktop?".
///
/// Every component asks this rather than testing `width` against a number of
/// its own.  The break points are the design's, not Qt's defaults, and they
/// live here so that a change to the layout rules is a change to one file
/// instead of a search for magic numbers.
QtObject {
    /// The window this singleton measures.  Set once by Main; a bare `width`
    /// on a non-Item is ambiguous, and the break points are the one thing in
    /// the UI that must never be ambiguous.
    /// `var`, not `Item`.
    ///
    /// It is assigned an `ApplicationWindow`, which is a `QQuickWindowQmlImpl`
    /// -- a QObject, not a QQuickItem -- so an `Item`-typed property refuses
    /// the assignment at run time with "Cannot assign QObject* to QQuickItem*".
    /// Nothing reads `window` as an item; it is read for `width` and `height`.
    /// An earlier version that did read it as an item, `screen.width`, would
    /// have been `undefined` for the same reason.
    property var window

    readonly property real width: window ? window.width : 0
    readonly property real height: window ? window.height : 0

    /// Below this, the navigation is a bottom bar and rails scroll vertically.
    readonly property int phoneMax: 599
    /// Between phone and desktop, navigation is a compact icon rail.
    readonly property int tabletMax: 1023

    readonly property bool isPhone: width <= phoneMax
    readonly property bool isTablet: width > phoneMax && width <= tabletMax
    readonly property bool isDesktop: width > tabletMax

    /// True for anything that navigates by touch rather than pointer.
    readonly property bool isTouch: isPhone || isTablet

    /// Navigation bar width: full labels on desktop, icons only on a tablet.
    readonly property int railWidth: isDesktop ? 240 : 72

    /// Grid columns for a poster rail.  One phone-width poster per column,
    /// with the count raised until the rail fills.
    function columnsFor(availableWidth, posterWidth, minimum, maximum) {
        const spacing = 16
        const perRow = Math.max(minimum,
                                Math.min(maximum,
                                         Math.floor((availableWidth + spacing)
                                                    / (posterWidth + spacing))))
        return perRow
    }

    /// Horizontal page margin: the phone needs less air than a desktop window.
    readonly property int pageMargin: isPhone ? 16 : (isTablet ? 24 : 40)

    /// Vertical gap between content sections.
    readonly property int sectionGap: isPhone ? 24 : 32
}

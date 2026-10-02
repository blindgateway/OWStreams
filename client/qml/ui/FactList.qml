pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Stream

/// A list of labelled facts: "Container · MKV", "Video · HEVC 3840×2160".
///
/// The technical record under a title, and nothing else.  It is deliberately
/// *not* a card.  A definition list is a list of pairs, and a pair earns its
/// place through the gap between a faint micro label and a body-sized value;
/// putting a border around it says "this is a special object" when it is in
/// fact just the facts, set in a narrower measure than the page.
///
/// Two columns on a desktop because the pairs are short and the column is
/// long; one everywhere else, because two columns on a phone is a table
/// somebody has to read sideways.
ColumnLayout {
    id: root

    /// The heading above the pairs.  Optional -- a list with no title is still
    /// a list, and DetailScreen's cast list reads better without one.
    property string title

    /// `[{ label: "...", value: "..." }, ...]`
    property var entries: []

    spacing: Theme.spaceSm

    /// The pairs worth drawing.  A fact is its value; a label with nothing
    /// beside it is a field the server did not fill in, and printing it leaves
    /// a word hanging over a gap.  The callers already skip those when they
    /// build the list, so dropping them here makes a missing field invisible
    /// rather than visible and empty.
    readonly property var shown: {
        if (!root.entries || root.entries.length === undefined)
            return []
        const out = []
        for (const e of root.entries) {
            if (!e)
                continue
            const value = (e.value !== undefined && e.value !== null)
                          ? String(e.value) : ""
            if (value.length === 0)
                continue
            const label = (e.label !== undefined && e.label !== null)
                          ? String(e.label) : ""
            out.push({ label: label, value: value })
        }
        return out
    }

    readonly property int entryCount: root.shown.length

    // A list with nothing in it collapses rather than leaving a heading
    // floating above empty space.  The parent can still bind `visible` (the
    // detail screen does, for "is there a title"), so the children are gated
    // too -- an empty list draws nothing however the caller sets it.
    visible: root.entryCount > 0

    // The one heading treatment in the app: uppercase micro, muted.  Micro
    // rather than caption because at this size and weight it reads as a label
    // on a list rather than as a title competing with the one on the page.
    Text {
        Layout.fillWidth: true
        Layout.bottomMargin: Theme.spaceXs
        visible: root.title !== undefined && root.title !== ""
                 && root.entryCount > 0
        text: root.title
        color: Theme.textMuted
        font.pixelSize: Theme.typeMicro
        font.family: Theme.family
        font.capitalization: Font.AllUppercase
        elide: Text.ElideRight
    }

    GridLayout {
        id: grid
        Layout.fillWidth: true
        columns: Responsive.isDesktop ? 2 : 1
        columnSpacing: Theme.spaceXl
        rowSpacing: Theme.spaceSm
        visible: root.entryCount > 0

        Repeater {
            model: root.shown

            delegate: RowLayout {
                id: cell
                required property var modelData

                Layout.fillWidth: true
                spacing: Theme.spaceSm

                // A fixed label measure, so every value in the list starts in
                // the same place and a column of pairs can be scanned down its
                // left edge.  Five times the extra-large space is 120 px,
                // which fits the longest label the app ships ("Audio
                // languages") at type micro with room to spare; anything past
                // that elides rather than pushing its value out of line.
                Text {
                    Layout.fillHeight: true
                    Layout.alignment: Qt.AlignTop
                    Layout.minimumWidth: Theme.spaceXl * 5
                    Layout.maximumWidth: Theme.spaceXl * 5
                    text: cell.modelData.label
                    color: Theme.textFaint
                    font.pixelSize: Theme.typeMicro
                    font.family: Theme.family
                    elide: Text.ElideRight
                    maximumLineCount: 1
                }

                // No rule between pairs.  A hairline under every cell turns a
                // short list of facts into a spreadsheet; the contrast between
                // a faint micro label and a body-sized value already separates
                // them, and rowSpacing keeps the pairs apart.
                Text {
                    Layout.fillWidth: true
                    text: cell.modelData.value
                    color: Theme.text
                    font.pixelSize: Theme.typeBody
                    font.family: Theme.family
                    wrapMode: Text.WordWrap
                    lineHeight: 1.3
                }
            }
        }
    }
}
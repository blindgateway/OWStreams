pragma ComponentBehavior: Bound

import QtQuick
import Stream

/// The icon set.
///
/// Five navigation glyphs and a handful of player controls, drawn on one
/// 24-unit grid with one stroke weight.  Shipping SVG files for this would mean
/// files that can disagree about stroke width and optical size; a Canvas path
/// set is one file, one weight, one grid, and the stroke is the same on every
/// platform because nothing scales it.
///
/// Deliberately *not* a filled/rounded icon font: a house drawn as a house and a
/// magnifier drawn as a circle-with-a-stick read better at 22 px than any
/// generic symbol set.
Item {
    id: icon

    /// One of the names below.  An unknown name draws nothing rather than a
    /// box: a missing icon should look absent, not broken.
    property string name: ""
    property color color: Theme.text
    property real strokeWidth: 1.75
    /// So an unknown name warns once rather than on every repaint.
    property bool missingReported: false

    implicitWidth: 24
    implicitHeight: 24
    antialiasing: true

    Canvas {
        id: canvas
        anchors.fill: parent
        renderStrategy: Canvas.Cooperative
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            ctx.scale(width / 24, height / 24)
            ctx.strokeStyle = icon.color
            ctx.fillStyle = icon.color
            ctx.lineWidth = icon.strokeWidth
            ctx.lineCap = "round"
            ctx.lineJoin = "round"

            switch (icon.name) {
            case "home":
                ctx.beginPath()
                ctx.moveTo(3, 11)
                ctx.lineTo(12, 3.5)
                ctx.lineTo(21, 11)
                ctx.stroke()
                ctx.beginPath()
                ctx.moveTo(5.5, 10)
                ctx.lineTo(5.5, 20)
                ctx.lineTo(18.5, 20)
                ctx.lineTo(18.5, 10)
                ctx.stroke()
                break
            case "library":
                // A shelf, not a stack: four uprights on one horizontal line.
                ctx.beginPath()
                ctx.moveTo(4, 20)
                ctx.lineTo(20, 20)
                ctx.stroke()
                ctx.beginPath()
                ctx.moveTo(6, 20)
                ctx.lineTo(6, 9)
                ctx.moveTo(9.5, 20)
                ctx.lineTo(9.5, 12)
                ctx.moveTo(13, 20)
                ctx.lineTo(13, 8)
                ctx.moveTo(16.5, 20)
                ctx.lineTo(16.5, 12)
                ctx.stroke()
                break
            case "search":
                ctx.beginPath()
                ctx.arc(10.5, 10.5, 6, 0, Math.PI * 2)
                ctx.stroke()
                ctx.beginPath()
                ctx.moveTo(15, 15)
                ctx.lineTo(20, 20)
                ctx.stroke()
                break
            case "favorites":
                ctx.beginPath()
                ctx.moveTo(12, 20)
                ctx.lineTo(4.5, 12.8)
                ctx.quadraticCurveTo(2.5, 9.5, 5.5, 7)
                ctx.quadraticCurveTo(9, 5.2, 12, 8.4)
                ctx.quadraticCurveTo(15, 5.2, 18.5, 7)
                ctx.quadraticCurveTo(21.5, 9.5, 19.5, 12.8)
                ctx.closePath()
                if (icon.filled) {
                    ctx.fill()
                } else {
                    ctx.stroke()
                }
                break
            case "history":
                ctx.beginPath()
                ctx.arc(12, 12, 8, 0, Math.PI * 2)
                ctx.stroke()
                ctx.beginPath()
                ctx.moveTo(12, 7)
                ctx.lineTo(12, 12)
                ctx.lineTo(16, 14.5)
                ctx.stroke()
                break
            case "settings":
                // A gear is the one glyph that has to be a gear; a spoked
                // circle with a hole is what it is at 22 px.
                ctx.beginPath()
                for (let i = 0; i < 8; ++i) {
                    const a = (Math.PI * 2 * i) / 8
                    const x = 12 + Math.cos(a) * 8
                    const y = 12 + Math.sin(a) * 8
                    if (i === 0) { ctx.moveTo(x, y) } else { ctx.lineTo(x, y) }
                }
                ctx.closePath()
                ctx.stroke()
                ctx.beginPath()
                ctx.arc(12, 12, 3, 0, Math.PI * 2)
                ctx.stroke()
                break
            case "play":
                ctx.beginPath()
                ctx.moveTo(8, 5)
                ctx.lineTo(19, 12)
                ctx.lineTo(8, 19)
                ctx.closePath()
                ctx.fill()
                break
            case "pause":
                ctx.fillRect(8, 5, 3, 14)
                ctx.fillRect(13, 5, 3, 14)
                break
            case "back":
                ctx.beginPath()
                ctx.moveTo(14, 5)
                ctx.lineTo(7, 12)
                ctx.lineTo(14, 19)
                ctx.stroke()
                break
            case "forward":
                ctx.beginPath()
                ctx.moveTo(10, 5)
                ctx.lineTo(17, 12)
                ctx.lineTo(10, 19)
                ctx.stroke()
                break
            case "close":
                ctx.beginPath()
                ctx.moveTo(6, 6)
                ctx.lineTo(18, 18)
                ctx.moveTo(18, 6)
                ctx.lineTo(6, 18)
                ctx.stroke()
                break
            case "volume":
                ctx.beginPath()
                ctx.moveTo(4, 10)
                ctx.lineTo(8, 10)
                ctx.lineTo(13, 6)
                ctx.lineTo(13, 18)
                ctx.lineTo(8, 14)
                ctx.lineTo(4, 14)
                ctx.closePath()
                ctx.fill()
                break
            default:
                // Nothing is drawn, and the component says so once rather than
                // drawing a placeholder that looks like a design decision.  A
                // missing icon is a bug in the caller; a silent one is a bug
                // nobody finds.
                if (!icon.missingReported) {
                    icon.missingReported = true
                    console.warn("Icon: no drawing for \"" + icon.name + "\"")
                }
                break
            }
        }
        // Repaint when anything the drawing depends on changes.
        Connections {
            function onNameChanged() { canvas.requestPaint() }
            function onColorChanged() { canvas.requestPaint() }
            function onStrokeWidthChanged() { canvas.requestPaint() }
        }
        Component.onCompleted: canvas.requestPaint()
        Connections {
            target: icon
            function onWidthChanged() { canvas.requestPaint() }
            function onHeightChanged() { canvas.requestPaint() }
        }
    }

    /// Filled icons are the ones where an outline reads as "not selected".
    property bool filled: name === "play"
                         || (name === "favorites" && filledOverride)
    /// The detail screen sets this so a heart can be filled when favourited.
    property bool filledOverride: false
}

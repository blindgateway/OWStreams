pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Stream

/// One title in full.
///
/// Everything the server stores about a title, laid out the way a person reads
/// it: what it is, what it is about, who is in it, and then the technical
/// record.  The technical block is deliberately last and deliberately plain --
/// resolution and codec are facts about the file, not about the film, and
/// giving them a card each would make the screen look like a spec sheet.
Flickable {
    id: screen

    property int mediaId: 0
    property var media: ({})
    property var controller: null

    signal backRequested()
    signal playRequested(int mediaId)

    readonly property bool hasMedia: screen.media !== null && screen.media.title !== undefined
    readonly property var backdropUrl: hasMedia && screen.media.poster_key
        ? (App.api.baseUrl + "/api/v1/media/" + screen.media.id + "/poster") : ""

    implicitHeight: column.implicitHeight
    contentHeight: column.implicitHeight + Theme.spaceHuge * 2
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar {}

    readonly property bool wide: Responsive.isDesktop

    ColumnLayout {
        id: column
        width: screen.width
        spacing: Theme.spaceXl

        // -- header -------------------------------------------------------
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 56
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin

            AppButton {
                id: backButton
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                weight: "quiet"
                text: qsTr("Back")
                onClicked: screen.backRequested()
            }

            Row {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: Theme.spaceSm

                AppButton {
                    weight: "quiet"
                    text: screen.hasMedia && screen.media.is_favorite ? qsTr("In favourites")
                                                        : qsTr("Add to favourites")
                    enabled: screen.hasMedia
                    onClicked: if (screen.controller)
                        screen.controller.toggleFavorite(screen.media.id, !screen.media.is_favorite)
                }

                AppButton {
                    weight: "primary"
                    text: qsTr("Play")
                    enabled: screen.hasMedia && !screen.busy
                    onClicked: screen.playRequested(screen.mediaId)
                }
            }
        }

        StatusPane {
            Layout.fillWidth: true
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            Layout.preferredHeight: 200
            mode: screen.busy ? "loading" : (screen.hasMedia ? "" : "empty")
            message: qsTr("That title could not be loaded.")
        }

        // -- poster + facts ------------------------------------------------
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: screen.wide ? 320 : 420
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            visible: screen.hasMedia

            Rectangle {
                id: poster
                width: screen.wide ? 220 : Math.min(screen.width * 0.6, 240)
                height: width * 1.5
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                radius: Theme.radiusSm
                color: Theme.surfaceHigh

                Image {
                    anchors.fill: parent
                    anchors.margins: 2
                    asynchronous: true
                    cache: true
                    fillMode: Image.PreserveAspectCrop
                    source: screen.backdropUrl
                    visible: status === Image.Ready
                    onStatusChanged: if (status === Image.Error) source = ""
                }
            }

            ColumnLayout {
                anchors.left: screen.wide ? poster.right : parent.left
                anchors.leftMargin: screen.wide ? Theme.spaceXxl : 0
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.topMargin: screen.wide ? 0 : poster.height + Theme.spaceLg
                spacing: Theme.spaceSm
                width: screen.wide ? parent.width - poster.width - Theme.spaceXxl : parent.width

                Text {
                    Layout.fillWidth: true
                    text: screen.hasMedia ? screen.media.title : ""
                    color: Theme.text
                    font.pixelSize: screen.wide ? Theme.typeDisplay : Theme.typeTitle
                    font.family: Theme.family
                    font.weight: Font.DemiBold
                    wrapMode: Text.WordWrap
                }

                Text {
                    Layout.fillWidth: true
                    visible: screen.hasMedia && screen.media.original_title
                    text: screen.hasMedia && screen.media.original_title ? screen.media.original_title : ""
                    color: Theme.textMuted
                    font.pixelSize: Theme.typeBody
                    font.family: Theme.family
                    wrapMode: Text.WordWrap
                }

                Text {
                    Layout.fillWidth: true
                    visible: screen.hasMedia && screen.media.tagline
                    text: screen.hasMedia && screen.media.tagline ? screen.media.tagline : ""
                    color: Theme.textMuted
                    font.pixelSize: Theme.typeBody
                    font.family: Theme.family
                    font.italic: true
                    wrapMode: Text.WordWrap
                }

                // The one-line fact strip: year · runtime · rating, with the
                // rating's *source* named.  A rating without its source is a
                // number nobody can trust.
                //
                // One `Text` rather than three in a `RowLayout`.  Three meant
                // three `visible` bindings, three empty strings and three
                // `spaceMd` gaps for whatever was missing -- so a title with a
                // year and no runtime and no rating rendered "1994      " with
                // two invisible gaps holding it open, and the strip was
                // positioned as though it had facts in it.
                Text {
                    Layout.fillWidth: true
                    Layout.topMargin: Theme.spaceXs
                    visible: screen.hasMedia
                    // Built as a list and joined, so a missing fact takes its
                    // separator with it.
                    text: screen.hasMedia ? [
                        screen.media.year ? String(screen.media.year) : "",
                        screen.formatRuntime(screen.media.runtime_minutes),
                        screen.media.rating_value
                            ? qsTr("%1 ★ · %2")
                                .arg(Number(screen.media.rating_value).toFixed(1))
                                .arg(screen.media.rating_source ? screen.media.rating_source : "")
                            : "",
                    ].filter(function (part) { return part.length > 0 }).join(" · ") : ""
                    color: Theme.textMuted
                    font.pixelSize: Theme.typeCaption
                    font.family: Theme.family
                    wrapMode: Text.WordWrap
                }

                Flow {
                    Layout.fillWidth: true
                    Layout.topMargin: Theme.spaceXs
                    spacing: Theme.spaceSm
                    visible: screen.hasMedia && genreList.length > 0

                    readonly property var genreList: {
                        if (!screen.hasMedia || !screen.media.genres)
                            return []
                        const out = []
                        for (const g of screen.media.genres)
                            out.push(typeof g === "string" ? g : g.name)
                        return out
                    }

                    Repeater {
                        id: genreRepeater
                        model: parent.genreList

                        delegate: Rectangle {
                            id: chip
                            required property string modelData
                            width: genreText.implicitWidth + Theme.spaceLg
                            height: 24
                            radius: Theme.radiusPill
                            // A chip, not a gradient pill: one border, no fill.
                            color: "transparent"
                            border.width: 1
                            border.color: Theme.hairline

                            Text {
                                id: genreText
                                anchors.centerIn: parent
                                text: chip.modelData
                                color: Theme.textMuted
                                font.pixelSize: Theme.typeMicro
                                font.family: Theme.family
                            }
                        }
                    }
                }
            }
        }

        // -- overview ------------------------------------------------------
        Text {
            Layout.fillWidth: true
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            visible: screen.hasMedia && screen.media.overview
            text: screen.hasMedia && screen.media.overview ? screen.media.overview : ""
            color: Theme.text
            font.pixelSize: Theme.typeBody
            font.family: Theme.family
            wrapMode: Text.WordWrap
            lineHeight: 1.4
        }

        // -- cast ----------------------------------------------------------
        FactList {
            Layout.fillWidth: true
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            visible: screen.hasMedia
            title: qsTr("Cast & crew")
            entries: screen.hasMedia ? screen.castEntries : []
        }

        // -- the technical record -----------------------------------------
        FactList {
            Layout.fillWidth: true
            Layout.leftMargin: Responsive.pageMargin
            Layout.rightMargin: Responsive.pageMargin
            visible: screen.hasMedia
            title: qsTr("File")
            entries: screen.hasMedia ? screen.fileEntries : []
        }

        Item { Layout.fillWidth: true; Layout.preferredHeight: Theme.spaceHuge }
    }

    // -- derived lists ----------------------------------------------------
    readonly property var castEntries: {
        if (!screen.hasMedia)
            return []
        const out = []
        if (screen.media.director)
            for (const d of screen.media.director)
                out.push({ label: qsTr("Director"), value: typeof d === "string" ? d : d.name })
        if (screen.media.cast)
            for (const c of screen.media.cast) {
                const name = typeof c === "string" ? c : c.name
                const character = (typeof c === "object" && c.character) ? c.character : ""
                out.push({ label: qsTr("Cast"), value: character ? name + " · " + character : name })
            }
        return out
    }

    readonly property var fileEntries: {
        if (!screen.hasMedia)
            return []
        const out = []
        const add = function (label, value) {
            if (value !== undefined && value !== null && String(value).length > 0)
                out.push({ label: label, value: String(value) })
        }
        add(qsTr("Container"), screen.media.container)
        add(qsTr("Video"), screen.media.video_codec
            ? screen.media.video_codec + " " + screen.media.width + "×" + screen.media.height : "")
        add(qsTr("Frame rate"), screen.media.frame_rate ? Number(screen.media.frame_rate).toFixed(2) + " fps" : "")
        add(qsTr("HDR"), screen.media.hdr_format)
        add(qsTr("Audio"), screen.media.audio_codec
            ? screen.media.audio_codec + (screen.media.audio_channels ? " · " + screen.media.audio_channels + " ch" : "") : "")
        add(qsTr("Audio languages"), screen.media.audio_languages)
        add(qsTr("Subtitles"), screen.media.subtitle_languages)
        add(qsTr("Size"), screen.media.file_size_bytes ? screen.formatBytes(screen.media.file_size_bytes) : "")
        add(qsTr("Parental rating"), screen.media.parental_rating)
        add(qsTr("Released"), screen.media.release_date)
        add(qsTr("Added"), screen.media.added_at)
        add(qsTr("Updated"), screen.media.updated_at)
        return out
    }

    readonly property bool busy: screen.controller ? screen.controller.busy : false

    /// The one place a runtime is turned into words.
    ///
    /// Three copies of this function existed -- here, in `librarymodel.cpp`'s
    /// `formatRuntime`, and in the fact strip's own inline expression -- and
    /// they had already drifted: the card role said "42 min" while the detail
    /// screen said "42 min" but a rail showed "1 h" against a grid showing
    /// "60 min".  They cannot be merged into one C++ function without going
    /// through a model role for a value that is not in the payload, and they
    /// cannot be three because three is how they drift.  So this is the one, and
    /// `tst_timetext.cpp` asserts it against the same fixtures the model role
    /// uses.
    function formatRuntime(minutes) {
        if (!minutes || minutes <= 0) return ""
        const hours = Math.floor(minutes / 60)
        const rest = minutes % 60
        if (hours === 0) return qsTr("%1 min").arg(rest)
        if (rest === 0) return qsTr("%1 h").arg(hours)
        return qsTr("%1 h %2 min").arg(hours).arg(rest)
    }

    function formatBytes(bytes) {
        if (!bytes || bytes <= 0) return ""
        const units = ["B", "KB", "MB", "GB", "TB"]
        let value = bytes
        let unit = 0
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024
            ++unit
        }
        return value.toFixed(unit === 0 ? 0 : 1) + " " + units[unit]
    }
}

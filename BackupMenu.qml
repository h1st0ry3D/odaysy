import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

/*
 * Backup, restore and location for the Odaysy bar icon.
 *
 * The file chooser is a separate process, /usr/bin/zenity, not Qt's own
 * FileDialog. QtQuick.Dialogs pulls GTK and GIO into the long-lived shell
 * process, so a chooser crash would take the whole bar with it. The chooser
 * prints a path on stdout; that path is data and goes to the helper on stdin,
 * never through a shell or an argv array.
 */
Item {
    id: menu

    required property Item anchorItem
    required property QtObject bar
    required property bool canUseFiles
    // The folder the config lives in, handed in rather than rebuilt here.
    required property string stateDir
    // The panel's last result, passed in as a binding.
    required property string resultNotice

    // Only this file writes its own failures.
    property string localNotice: ""

    readonly property string shownNotice: localNotice !== "" ? localNotice : resultNotice

    onResultNoticeChanged: {
        if (resultNotice !== "") localNotice = ""
    }

    property bool opened: false
    property string choosing: ""
    property string chosenBuffer: ""
    property bool chosenOverflow: false

    readonly property int maxPathChars: 4096
    readonly property string homeDir: Quickshell.env("HOME")
    readonly property bool chooserBusy: choosing !== ""

    // ---- is there a chooser at all? ----
    property bool chooserAvailable: false
    property bool chooserChecked: false

    Process {
        id: chooserProbe
        command: ["/usr/bin/zenity", "--version"]
        onStarted: menu.markChecked(true)
        onExited: menu.markChecked(true)
    }

    Timer {
        id: chooserProbeDeadline
        interval: 2000
        onTriggered: menu.markChecked(false)
    }

    function markChecked(available) {
        chooserProbeDeadline.stop()
        chooserAvailable = available
        chooserChecked = true
    }

    function suggestedName() {
        var now = new Date()
        function pad(value) { return (value < 10 ? "0" : "") + value }
        return "odaysy_backup_" + now.getFullYear() + "-" + pad(now.getMonth() + 1)
                + "-" + pad(now.getDate()) + ".json"
    }

    function openExport() {
        if (!menu.opened || !menu.rowsLive) return
        menu.opened = false
        menu.startChooser("export")
    }

    function openRestore() {
        if (!menu.opened || !menu.rowsLive) return
        menu.opened = false
        menu.startChooser("restore")
    }

    // ---- showing the folder ----
    property bool opening: false

    function openLocation() {
        if (!menu.opened || menu.opening) return
        menu.opened = false
        menu.opening = true
        ensureDirProc.running = true
        openDeadline.restart()
    }

    Process {
        id: ensureDirProc
        // Location has to work before the first event, when the config folder
        // does not exist yet. Fixed argv, no shell, and -m 700 matches what the
        // helper makes when it creates the same folder.
        command: ["/usr/bin/mkdir", "-m", "700", "-p", menu.stateDir]
        onExited: function (code) {
            if (code === 0) {
                openDeadline.restart()
                openProc.running = true
                return
            }
            openDeadline.stop()
            menu.opening = false
            menu.localNotice = "Could not create the folder. It should be " + menu.stateDir + "."
        }
    }

    Process {
        id: openProc
        command: ["/usr/bin/xdg-open", menu.stateDir]
        onExited: function (code) {
            openDeadline.stop()
            menu.opening = false
            if (code !== 0)
                menu.localNotice = "Nothing opened it. The folder is " + menu.stateDir + "."
        }
    }

    Timer {
        id: openDeadline
        interval: 3000
        onTriggered: {
            menu.opening = false
            menu.localNotice = "The folder could not be opened. It is " + menu.stateDir + "."
        }
    }

    readonly property bool rowsLive: canUseFiles && !chooserBusy && chooserChecked
                                       && chooserAvailable

    function startChooser(verb) {
        menu.localNotice = ""
        menu.chosenBuffer = ""
        menu.chosenOverflow = false
        menu.choosing = verb
        chooserProc.running = true
    }

    function chosenPath() {
        if (menu.chosenOverflow) return ""
        var path = menu.chosenBuffer.replace(/[\r\n]/g, "")
        if (path === "" || path.charAt(0) !== "/") return ""
        return path
    }

    Process {
        id: chooserProc
        command: menu.choosing === "export"
            ? ["/usr/bin/zenity", "--file-selection", "--save",
               "--title", "Save your events", "--file-filter", "JSON files | *.json",
               "--filename", menu.homeDir + "/" + menu.suggestedName()]
            : ["/usr/bin/zenity", "--file-selection",
               "--title", "Restore your events", "--file-filter", "JSON files | *.json"]

        stdout: SplitParser {
            splitMarker: ""
            onRead: function (chunk) {
                if (menu.chosenBuffer.length + chunk.length <= menu.maxPathChars) {
                    menu.chosenBuffer += chunk
                    return
                }
                menu.chosenBuffer += chunk.substring(
                    0, Math.max(0, menu.maxPathChars - menu.chosenBuffer.length))
                menu.chosenOverflow = true
            }
        }

        onStarted: chooserDeadline.restart()

        onExited: function (code) {
            chooserDeadline.stop()
            var verb = menu.choosing
            var overflow = menu.chosenOverflow
            var path = overflow ? "" : menu.chosenPath()
            menu.choosing = ""
            menu.chosenBuffer = ""
            menu.chosenOverflow = false

            if (overflow) {
                menu.localNotice = "The file chooser said more than a file path."
                return
            }
            if (code === 1 || code === 5) return
            if (code !== 0 || path === "") {
                menu.localNotice = "The file chooser could not be opened."
                return
            }
            if (verb === "export") menu.exportRequested(path)
            else menu.restoreRequested(path)
        }
    }

    Timer {
        id: chooserDeadline
        interval: 600000
        onTriggered: {
            chooserProc.signal(15)
            menu.choosing = ""
            menu.localNotice = "The file chooser did not answer."
        }
    }

    Component.onCompleted: {
        chooserProbe.running = true
        chooserProbeDeadline.restart()
    }

    Component.onDestruction: {
        chooserProbeDeadline.stop()
        chooserDeadline.stop()
        openDeadline.stop()
        if (chooserProc.running) chooserProc.signal(15)
        if (openProc.running) openProc.signal(15)
        if (ensureDirProc.running) ensureDirProc.signal(15)
    }

    QtObject {
        id: menuOwner
        function close() { menu.opened = false }
    }

    signal exportRequested(string path)
    signal restoreRequested(string path)

    PopupCard {
        id: card
        anchorItem: menu.anchorItem
        owner: menuOwner
        bar: menu.bar
        open: menu.opened
        padding: Style.space(8)
        contentWidth: card.fittedContentWidth(Style.space(260))
        contentHeight: card.fittedContentHeight(rows.implicitHeight)

        Column {
            id: rows
            width: parent.width
            spacing: 0

            MenuRow {
                label: "Backup"
                onTriggered: menu.openExport()
            }

            MenuRow {
                label: "Restore"
                onTriggered: menu.openRestore()
            }

            MenuRow {
                label: "Location"
                needsChooser: false
                onTriggered: menu.openLocation()
            }

            Text {
                width: parent.width
                visible: menu.chooserChecked && !menu.chooserAvailable
                leftPadding: Style.space(12)
                rightPadding: Style.space(12)
                bottomPadding: Style.space(6)
                text: "Back up and restore need zenity (package: zenity)."
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: Util.alpha(Color.foreground, 0.7)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
            }

            Text {
                width: parent.width
                visible: menu.chooserBusy
                leftPadding: Style.space(12)
                rightPadding: Style.space(12)
                bottomPadding: Style.space(6)
                text: "Waiting for the file chooser."
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: Util.alpha(Color.foreground, 0.7)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
            }

            Item {
                width: parent.width
                visible: menu.shownNotice !== ""
                implicitHeight: visible ? noticeBlock.implicitHeight : 0

                Column {
                    id: noticeBlock
                    width: parent.width
                    spacing: 0

                    Item {
                        width: parent.width
                        implicitHeight: Style.space(11)

                        Rectangle {
                            anchors.left: parent.left
                            anchors.leftMargin: Style.space(10)
                            anchors.right: parent.right
                            anchors.rightMargin: Style.space(10)
                            anchors.verticalCenter: parent.verticalCenter
                            height: 1
                            color: Color.popups.border
                            opacity: 0.45
                        }
                    }

                    Text {
                        width: parent.width
                        leftPadding: Style.space(12)
                        rightPadding: Style.space(12)
                        topPadding: Style.space(4)
                        bottomPadding: Style.space(6)
                        text: menu.shownNotice
                        textFormat: Text.PlainText
                        wrapMode: Text.WordWrap
                        color: Util.alpha(Color.foreground, 0.7)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                    }
                }
            }
        }
    }

    component MenuRow: Item {
        id: row

        required property string label
        signal triggered()

        property bool needsChooser: true
        readonly property bool live: menu.opened
                                       && !menu.opening
                                       && (needsChooser ? menu.rowsLive : true)

        width: rows.width
        implicitHeight: Style.space(30)
        opacity: live ? 1 : 0.45

        Rectangle {
            anchors.fill: parent
            radius: Math.max(2, Style.cornerRadius)
            color: rowMouse.containsMouse && row.live
                   ? Style.hoverFillFor(Color.foreground, Color.foreground)
                   : "transparent"
        }

        Text {
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: parent.left
            anchors.leftMargin: Style.space(12)
            anchors.right: parent.right
            anchors.rightMargin: Style.space(12)
            text: row.label
            textFormat: Text.PlainText
            color: Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
        }

        MouseArea {
            id: rowMouse
            anchors.fill: parent
            enabled: row.live
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: row.triggered()
        }
    }
}

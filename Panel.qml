import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

/*
 * Odaysy: a list of things to count days since, and one of those counts in the
 * bar.
 *
 * The panel never reads or writes the config itself. It runs the helper next
 * to this file as an argv array, and the helper is the only reader and writer.
 * That keeps the checked-descriptor and atomic-replace rules in one place, and
 * means the panel has no opinion about JSON, symlinks or file modes.
 *
 * Helper output is untrusted input. Titles can be anything the user typed. A
 * Text without textFormat sits on Text.AutoText, which renders a string that
 * looks like markup as rich text, and rich text can load an image from a URL
 * the string chooses. Text this file draws is Text.PlainText; values handed to
 * host-drawn components go through Model.plain() first.
 */
Panel {
    id: root

    moduleName: "h1st0ry3d.odaysy"
    ipcTarget: "h1st0ry3d.odaysy"
    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    // ---- the helper ---------------------------------------------------------
    readonly property string homeDir: Quickshell.env("HOME") || ""
    readonly property string python: "/usr/bin/python3"
    readonly property string helperPath: decodeURIComponent(
        Qt.resolvedUrl("odaysy_helper.py").toString().replace(/^file:\/\//, ""))
    // The helper needs HOME to find the config, and XDG_CONFIG_HOME if the
    // user set one. Everything else is withheld: no PATH search, no inherited
    // environment that can change what Python imports or how it encodes text.
    readonly property var helperEnvironment: {
        var environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "HOME": root.homeDir
        }
        var configHome = Quickshell.env("XDG_CONFIG_HOME") || ""
        if (configHome !== "") environment["XDG_CONFIG_HOME"] = configHome
        return environment
    }
    readonly property int outputLimit: Model.MAX_EVENTS * 2048 + 4096
    readonly property int errorLimit: 4096
    readonly property string configDir: {
        var base = Quickshell.env("XDG_CONFIG_HOME") || ""
        if (base === "") base = root.homeDir + "/.config"
        if (base.length > 1 && base.charAt(base.length - 1) === "/")
            base = base.substring(0, base.length - 1)
        return base + "/odaysy"
    }

    // ---- the document -------------------------------------------------------
    readonly property var emptyStore: ({ version: 1, pinnedId: "", events: [] })
    property var store: root.emptyStore
    property bool storeReady: false
    property bool busy: false
    property string errorText: ""

    readonly property var events: root.store && Array.isArray(root.store.events)
        ? root.store.events : []
    readonly property string pinnedId: root.store && typeof root.store.pinnedId === "string"
        ? root.store.pinnedId : ""
    readonly property var barEntry: Model.barEntry(root.events, root.pinnedId)
    readonly property string barText: Model.barText(root.events, root.pinnedId, root.nowMs)
    readonly property string barTooltip: {
        if (!root.storeReady) return "Odaysy"
        if (root.events.length === 0) return "Odaysy: nothing tracked yet"
        if (!root.barEntry) return "Odaysy: pin an event to see its count"
        var days = Model.daysSince(root.barEntry.since, root.nowMs)
        return Model.plain(root.barEntry.title, 60) + ", " + Model.dayLabel(days)
    }

    // ---- interaction state --------------------------------------------------
    property string editingId: ""
    property string editDraft: ""
    property Item focusedField: null
    property string pendingAction: ""
    property string confirmKind: ""
    property string confirmId: ""
    property string confirmMessage: ""
    property string confirmActionText: "Confirm"
    property string restorePath: ""
    property string queuedFileVerb: ""
    property string queuedFilePath: ""
    property string fileNotice: ""
    property double nowMs: Date.now()

    // ---- helper calls -------------------------------------------------------
    function callHelper(action, argumentsList, stdinLine) {
        if (helper.running) return
        root.busy = true
        root.errorText = ""
        root.pendingAction = action
        helper.timedOut = false
        helper.stdoutBuffer = ""
        helper.stderrBuffer = ""
        helper.stdinPayload = stdinLine || ""
        helper.stdinEnabled = helper.stdinPayload !== ""
        helper.command = [root.python, "-I", "-B", root.helperPath].concat(argumentsList)
        helper.running = true
        helperTimeout.interval = (action === "export" || action === "restore") ? 30000 : 5000
        helperTimeout.restart()
    }

    function fileName(path) {
        var text = String(path === undefined || path === null ? "" : path)
        var slash = text.lastIndexOf("/")
        return slash >= 0 ? text.substring(slash + 1) : text
    }

    function reportFileOp(message) {
        root.fileNotice = Model.plain(message, 300)
        fileNoticeClear.restart()
    }

    function clearFileNotice() {
        fileNoticeClear.stop()
        root.fileNotice = ""
    }

    function runFileOp(verb, path) {
        if (helper.running) {
            root.queuedFileVerb = verb
            root.queuedFilePath = path
            return
        }
        root.callHelper(verb, ["backup", verb], JSON.stringify({ path: path }))
    }

    function startQueuedFileOp() {
        if (helper.running || root.queuedFileVerb === "") return
        var verb = root.queuedFileVerb
        var path = root.queuedFilePath
        root.queuedFileVerb = ""
        root.queuedFilePath = ""
        root.callHelper(verb, ["backup", verb], JSON.stringify({ path: path }))
    }

    function requestRestore(path) {
        if (!path) return
        backupMenu.opened = false
        root.clearFileNotice()
        root.restorePath = String(path)
        root.confirmKind = "restore"
        root.confirmId = ""
        root.confirmMessage = "Replace your events with \"" + Model.plain(root.fileName(path), 80)
            + "\"?\n\nThe current list is copied into backups/ first."
        root.confirmActionText = "Restore"
        confirm.selectedIndex = 0
        root.focusedField = null
        confirm.opened = true
        if (!root.opened) root.open()
        keyCatcher.forceActiveFocus()
    }

    function reload() {
        root.callHelper("list", ["list"])
    }

    function addEvent() {
        var title = String(addField.text).trim()
        if (title === "" || root.busy) return
        root.callHelper("add", ["add", "--", title])
    }

    function beginEdit(entry) {
        if (root.busy || !entry) return
        root.focusedField = null
        root.editingId = String(entry.id)
        root.editDraft = String(entry.title)
    }

    function cancelEdit() {
        root.editingId = ""
        root.editDraft = ""
        root.focusedField = null
    }

    function saveRename(identifier) {
        var title = String(root.editDraft).trim()
        if (title === "" || root.busy) return
        root.callHelper("rename", ["rename", "--", String(identifier), title])
    }

    function togglePin(identifier) {
        if (root.busy) return
        if (root.pinnedId === String(identifier))
            root.callHelper("unpin", ["unpin"])
        else
            root.callHelper("pin", ["pin", "--", String(identifier)])
    }

    function askReset(entry) {
        if (!entry || root.busy) return
        root.confirmKind = "reset"
        root.confirmId = String(entry.id)
        root.confirmMessage = "Reset \"" + Model.plain(entry.title, 80)
            + "\"?\n\nThe counter starts again at 0 today."
        root.confirmActionText = "Reset"
        confirm.selectedIndex = 0
        root.focusedField = null
        confirm.opened = true
        keyCatcher.forceActiveFocus()
    }

    function askRemove(entry) {
        if (!entry || root.busy) return
        root.confirmKind = "remove"
        root.confirmId = String(entry.id)
        root.confirmMessage = "Remove \"" + Model.plain(entry.title, 80)
            + "\"?\n\nThe event and its counter are deleted. This cannot be undone."
        root.confirmActionText = "Remove"
        confirm.selectedIndex = 0
        root.focusedField = null
        confirm.opened = true
        keyCatcher.forceActiveFocus()
    }

    function closeConfirm() {
        confirm.opened = false
        root.confirmKind = ""
        root.confirmId = ""
        root.restorePath = ""
    }

    function acceptConfirm() {
        var kind = root.confirmKind
        var identifier = root.confirmId
        var path = root.restorePath
        root.closeConfirm()
        if (kind === "reset") root.callHelper("reset", ["reset", "--", identifier])
        else if (kind === "remove") root.callHelper("remove", ["remove", "--", identifier])
        else if (kind === "restore" && path !== "") root.runFileOp("restore", path)
    }

    Process {
        id: helper
        running: false
        property bool timedOut: false
        property string stdoutBuffer: ""
        property string stderrBuffer: ""
        property string stdinPayload: ""

        environment: root.helperEnvironment
        clearEnvironment: true
        command: [root.python, "-I", "-B", root.helperPath, "list"]
        stdinEnabled: false

        onStarted: {
            if (helper.stdinPayload !== "") {
                helper.write(helper.stdinPayload + "\n")
                helper.stdinPayload = ""
            }
            helper.stdinEnabled = false
        }

        stdout: SplitParser {
            splitMarker: ""
            onRead: function(chunk) {
                if (helper.stdoutBuffer.length <= root.outputLimit)
                    helper.stdoutBuffer += chunk
            }
        }
        stderr: SplitParser {
            splitMarker: ""
            onRead: function(chunk) {
                if (helper.stderrBuffer.length <= root.errorLimit)
                    helper.stderrBuffer += chunk
            }
        }

        onExited: function(code) {
            helperTimeout.stop()
            helperKill.stop()
            var action = root.pendingAction
            var output = helper.stdoutBuffer
            var failure = helper.stderrBuffer
            helper.stdoutBuffer = ""
            helper.stderrBuffer = ""
            helper.stdinPayload = ""
            helper.stdinEnabled = false
            root.busy = false
            root.pendingAction = ""
            if (root.queuedFileVerb !== "") Qt.callLater(function() { root.startQueuedFileOp() })

            if (helper.timedOut) {
                helper.timedOut = false
                if (action === "export" || action === "restore") {
                    root.reportFileOp("The file operation took too long.")
                    if (action === "export") backupMenu.opened = true
                } else {
                    root.errorText = "Odaysy did not answer in time. Try again."
                }
                return
            }
            if (code !== 0) {
                var message = Model.plain(
                    failure.trim() || "The event store could not be read.", 300)
                if (action === "export" || action === "restore") {
                    root.reportFileOp(message)
                    if (action === "export") backupMenu.opened = true
                } else {
                    root.errorText = message
                }
                return
            }

            var parsed = null
            try { parsed = JSON.parse(output.trim()) } catch (error) { parsed = null }
            if (action === "export" || action === "restore") {
                if (!parsed || parsed.ok !== true || !Array.isArray(parsed.events)) {
                    root.reportFileOp("The file operation returned something this panel does not understand.")
                    if (action === "export") backupMenu.opened = true
                    return
                }
                root.store = parsed
                root.storeReady = true
                root.errorText = ""
                root.reportFileOp(parsed.message || (action === "export"
                    ? "Backed up." : "Restored."))
                if (action === "export") backupMenu.opened = true
                return
            }
            if (!parsed || !Array.isArray(parsed.events)) {
                root.errorText = "The event store returned something this panel does not understand."
                return
            }

            root.store = parsed
            root.storeReady = true
            root.errorText = ""

            if (action === "add") {
                addField.text = ""
                Qt.callLater(function() {
                    if (root.opened && !root.busy) addField.forceActiveFocus()
                })
            } else if (action === "rename" || action === "remove") {
                root.cancelEdit()
            }
        }
    }

    Timer {
        id: helperTimeout
        interval: 5000
        repeat: false
        onTriggered: {
            if (!helper.running) return
            helper.timedOut = true
            helper.signal(15)
            helperKill.restart()
        }
    }

    Timer {
        id: helperKill
        interval: 1000
        repeat: false
        onTriggered: if (helper.running) helper.signal(9)
    }

    Timer {
        id: fileNoticeClear
        interval: 5000
        repeat: false
        onTriggered: root.fileNotice = ""
    }

    // The calendar, not elapsed runtime: a suspend or a clock correction has to
    // move a counter the same way midnight does.
    SystemClock {
        id: systemClock
        precision: SystemClock.Minutes
        onDateChanged: root.nowMs = Date.now()
    }

    onOpenedChanged: {
        if (root.opened) {
            backupMenu.opened = false
            root.clearFileNotice()
            root.cancelEdit()
            root.reload()
            return
        }
        root.cancelEdit()
        root.closeConfirm()
        root.focusedField = null
    }

    Component.onCompleted: {
        root.nowMs = systemClock.date.getTime()
        root.reload()
    }

    // ---- the bar ------------------------------------------------------------
    WidgetButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        text: root.barText
        tooltipText: root.barTooltip
        fontSize: Style.font.body
        horizontalMargin: 8.75
        hasVisualContent: true
        onPressed: function(mouseButton) {
            if (mouseButton === Qt.RightButton) {
                if (root.opened) root.close()
                backupMenu.opened = !backupMenu.opened
                return
            }
            backupMenu.opened = false
            root.toggle()
        }
    }

    // ---- backup, restore and location --------------------------------------
    BackupMenu {
        id: backupMenu
        anchorItem: button
        bar: root.bar
        canUseFiles: !root.busy
        stateDir: root.configDir
        resultNotice: root.fileNotice
        onExportRequested: function(path) {
            root.clearFileNotice()
            root.runFileOp("export", path)
        }
        onRestoreRequested: function(path) {
            root.requestRestore(path)
        }
    }

    // ---- the panel ----------------------------------------------------------
    KeyboardPanel {
        id: dropdown
        anchorItem: button
        owner: root
        bar: root.bar
        open: root.opened
        focusTarget: keyCatcher
        contentWidth: dropdown.fittedContentWidth(Style.space(400))
        contentHeight: dropdown.fittedContentHeight(col.implicitHeight, Style.space(600))

        PanelKeyCatcher {
            id: keyCatcher
            anchors.fill: parent
            blocked: root.focusedField !== null && !confirm.opened

            onCloseRequested: {
                if (confirm.opened) confirm.canceled()
                else if (root.editingId !== "") root.cancelEdit()
                else root.close()
            }
            onMoveRequested: function(dx, dy) {
                if (confirm.opened && (dx !== 0 || dy !== 0))
                    confirm.selectedIndex = confirm.selectedIndex === 0 ? 1 : 0
            }
            onActivateRequested: {
                if (!confirm.opened) return
                if (confirm.selectedIndex === 0) confirm.canceled()
                else confirm.confirmed()
            }
            onTabRequested: function(direction) {
                if (!confirm.opened) root.switchPanel(direction)
            }

            Flickable {
                anchors.fill: parent
                contentWidth: width
                contentHeight: col.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                Column {
                    id: col
                    width: parent.width
                    spacing: Style.space(8)
                    topPadding: Style.space(12)
                    bottomPadding: Style.space(12)

                    PanelHero {
                        width: parent.width
                        title: "Odaysy"
                        meta: !root.storeReady
                            ? (root.errorText !== "" ? "No data" : "Loading")
                            : root.busy
                                ? "Saving"
                                : Model.countLabel(root.events.length)
                        detail: root.events.length > 1 && root.barEntry ? "Pinned" : ""
                        foreground: Color.foreground
                        fontFamily: Style.font.family
                        iconComponent: Component {
                            Text {
                                textFormat: Text.PlainText
                                text: "∞"
                                color: Color.accent
                                font.family: Style.font.family
                                font.pixelSize: Style.font.displayLarge
                            }
                        }
                    }

                    Text {
                        width: parent.width
                        visible: root.errorText !== ""
                        text: root.errorText
                        textFormat: Text.PlainText
                        color: Color.urgent
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                        wrapMode: Text.WordWrap
                    }

                    Text {
                        width: parent.width
                        visible: root.fileNotice !== ""
                        text: root.fileNotice
                        textFormat: Text.PlainText
                        color: Util.alpha(Color.foreground, 0.8)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                        wrapMode: Text.WordWrap
                    }

                    // -- add
                    Row {
                        width: parent.width
                        spacing: Style.space(8)

                        TextField {
                            id: addField
                            width: parent.width - addButton.width - parent.spacing
                            maximumLength: Model.MAX_TITLE
                            placeholderText: "Name a new event…"
                            enabled: !root.busy
                            onAccepted: root.addEvent()
                            Keys.onEscapePressed: root.close()
                            onActiveFocusChanged: {
                                if (activeFocus) root.focusedField = addField
                                else if (root.focusedField === addField) root.focusedField = null
                            }
                        }

                        Button {
                            id: addButton
                            width: Style.space(66)
                            height: Style.space(30)
                            text: "Add"
                            bordered: true
                            enabled: !root.busy && String(addField.text).trim() !== ""
                            onClicked: root.addEvent()
                        }
                    }

                    PanelSeparator { foreground: Color.foreground }

                    // -- empty state
                    Item {
                        width: parent.width
                        visible: root.storeReady && root.events.length === 0
                        height: emptyHint.y + emptyHint.implicitHeight

                        Text {
                            id: emptyMark
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.top: parent.top
                            anchors.topMargin: Style.space(8)
                            textFormat: Text.PlainText
                            text: "∞"
                            color: Color.accent
                            font.family: Style.font.family
                            font.pixelSize: Style.font.displayLarge
                        }
                        Text {
                            id: emptyTitle
                            anchors.top: emptyMark.bottom
                            anchors.topMargin: Style.space(4)
                            width: parent.width
                            textFormat: Text.PlainText
                            text: "Nothing tracked yet."
                            horizontalAlignment: Text.AlignHCenter
                            color: Color.foreground
                            font.family: Style.font.family
                            font.pixelSize: Style.font.title
                            font.bold: true
                        }
                        Text {
                            id: emptyHint
                            anchors.top: emptyTitle.bottom
                            anchors.topMargin: Style.space(4)
                            width: parent.width
                            textFormat: Text.PlainText
                            text: "Add an event above. Its counter starts at 0 today and grows at midnight."
                            horizontalAlignment: Text.AlignHCenter
                            color: Util.alpha(Color.foreground, 0.7)
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption
                            wrapMode: Text.WordWrap
                        }
                    }

                    // -- the list
                    Repeater {
                        model: root.events

                        delegate: BorderSurface {
                            id: entryRow
                            required property var modelData

                            readonly property bool pinned: root.pinnedId === modelData.id
                            readonly property bool editing: root.editingId === modelData.id
                            readonly property int days: Model.daysSince(modelData.since, root.nowMs)

                            width: parent.width
                            height: Style.space(56)
                            color: entryRow.pinned
                                ? Style.selectedFillFor(Color.foreground, Color.accent)
                                : "transparent"
                            borderSpec: Border.controlSpec(
                                entryRow.pinned ? "selected" : "normal",
                                Color.foreground, Color.accent)
                            radius: Style.cornerRadius

                            Item {
                                anchors.fill: parent
                                anchors.leftMargin: Style.space(10)
                                anchors.rightMargin: Style.space(8)

                                // One radio per row. Clicking the selected one
                                // clears the pin; the bar then shows ∞ while
                                // more than one event is being tracked.
                                Item {
                                    id: radio
                                    width: Style.space(20)
                                    height: Style.space(20)
                                    anchors.verticalCenter: parent.verticalCenter

                                    Rectangle {
                                        anchors.fill: parent
                                        radius: width / 2
                                        color: "transparent"
                                        border.width: Style.normalBorderWidth
                                        border.color: entryRow.pinned
                                            ? Color.accent
                                            : Util.alpha(Color.foreground, 0.5)
                                    }
                                    Rectangle {
                                        visible: entryRow.pinned
                                        width: parent.width / 2
                                        height: width
                                        radius: width / 2
                                        anchors.centerIn: parent
                                        color: Color.accent
                                    }
                                    MouseArea {
                                        id: radioMouse
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        enabled: !root.busy
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: root.togglePin(entryRow.modelData.id)
                                    }
                                    PanelToolTip {
                                        visible: radioMouse.containsMouse
                                        text: entryRow.pinned
                                            ? "Pinned to the bar (click to unpin)"
                                            : "Show this count in the bar"
                                    }
                                }

                                Item {
                                    id: daysBox
                                    visible: !entryRow.editing
                                    width: visible ? Style.space(46) : 0
                                    height: daysNumber.implicitHeight + daysWord.implicitHeight
                                    anchors.right: actions.left
                                    anchors.rightMargin: Style.space(10)
                                    anchors.verticalCenter: parent.verticalCenter

                                    Text {
                                        id: daysNumber
                                        width: parent.width
                                        textFormat: Text.PlainText
                                        text: entryRow.days
                                        horizontalAlignment: Text.AlignRight
                                        color: entryRow.pinned ? Color.accent : Color.foreground
                                        font.family: Style.font.family
                                        font.pixelSize: Style.font.display
                                    }
                                    Text {
                                        id: daysWord
                                        width: parent.width
                                        anchors.top: daysNumber.bottom
                                        textFormat: Text.PlainText
                                        text: entryRow.days === 1 ? "day" : "days"
                                        horizontalAlignment: Text.AlignRight
                                        color: Util.alpha(Color.foreground, 0.6)
                                        font.family: Style.font.family
                                        font.pixelSize: Style.font.caption
                                    }
                                }

                                Item {
                                    anchors.left: radio.right
                                    anchors.leftMargin: Style.space(10)
                                    anchors.right: daysBox.left
                                    anchors.rightMargin: Style.space(8)
                                    anchors.verticalCenter: parent.verticalCenter
                                    height: parent.height

                                    Item {
                                        visible: !entryRow.editing
                                        anchors.fill: parent

                                        Text {
                                            id: entryTitle
                                            width: parent.width
                                            y: Math.max(0, (parent.height - entryTitle.implicitHeight
                                                - Style.space(2) - entryMeta.implicitHeight) / 2)
                                            textFormat: Text.PlainText
                                            text: entryRow.modelData.title
                                            color: Color.foreground
                                            font.family: Style.font.family
                                            font.pixelSize: Style.font.subtitle
                                            font.bold: true
                                            elide: Text.ElideRight
                                        }
                                        Text {
                                            id: entryMeta
                                            width: parent.width
                                            anchors.top: entryTitle.bottom
                                            anchors.topMargin: Style.space(2)
                                            textFormat: Text.PlainText
                                            text: "Since " + Model.sinceLabel(entryRow.modelData.since)
                                                + (entryRow.pinned ? " · in the bar" : "")
                                            color: Util.alpha(Color.foreground, 0.6)
                                            font.family: Style.font.family
                                            font.pixelSize: Style.font.caption
                                            elide: Text.ElideRight
                                        }
                                    }

                                    Loader {
                                        anchors.fill: parent
                                        active: entryRow.editing
                                        visible: entryRow.editing
                                        sourceComponent: Component {
                                            TextField {
                                                id: renameField
                                                anchors.left: parent.left
                                                anchors.right: parent.right
                                                anchors.verticalCenter: parent.verticalCenter
                                                height: Style.space(30)
                                                text: root.editDraft
                                                maximumLength: Model.MAX_TITLE
                                                onTextEdited: root.editDraft = text
                                                onAccepted: root.saveRename(root.editingId)
                                                Keys.onEscapePressed: root.cancelEdit()
                                                onActiveFocusChanged: {
                                                    if (activeFocus) root.focusedField = renameField
                                                    else if (root.focusedField === renameField) root.focusedField = null
                                                }
                                            }
                                        }
                                        onLoaded: {
                                            if (item) {
                                                item.forceActiveFocus()
                                                item.selectAll()
                                            }
                                        }
                                    }
                                }

                                Row {
                                    id: actions
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    spacing: Style.space(4)

                                    Button {
                                        visible: !entryRow.editing
                                        width: Style.space(26)
                                        height: Style.space(26)
                                        iconText: "\uf044"
                                        bordered: true
                                        enabled: !root.busy
                                        tooltipText: "Rename"
                                        onClicked: root.beginEdit(entryRow.modelData)
                                    }
                                    Button {
                                        visible: !entryRow.editing
                                        width: Style.space(26)
                                        height: Style.space(26)
                                        iconText: "\uf0e2"
                                        bordered: true
                                        enabled: !root.busy
                                        tooltipText: "Reset to 0"
                                        onClicked: root.askReset(entryRow.modelData)
                                    }
                                    Button {
                                        visible: entryRow.editing
                                        width: Style.space(26)
                                        height: Style.space(26)
                                        iconText: "\uf00c"
                                        bordered: true
                                        enabled: !root.busy
                                        tooltipText: "Save name"
                                        onClicked: root.saveRename(entryRow.modelData.id)
                                    }
                                    Button {
                                        visible: entryRow.editing
                                        width: Style.space(26)
                                        height: Style.space(26)
                                        iconText: "\uf00d"
                                        bordered: true
                                        enabled: !root.busy
                                        tooltipText: "Cancel"
                                        onClicked: root.cancelEdit()
                                    }
                                    Button {
                                        visible: entryRow.editing
                                        width: Style.space(26)
                                        height: Style.space(26)
                                        iconText: "\uf1f8"
                                        bordered: true
                                        enabled: !root.busy
                                        tooltipText: "Remove"
                                        onClicked: root.askRemove(entryRow.modelData)
                                    }
                                }
                            }
                        }
                    }

                    Text {
                        width: parent.width
                        visible: root.events.length > 1 && !root.barEntry
                        text: "Pin one event with its circle to show that number in the bar."
                        textFormat: Text.PlainText
                        color: Util.alpha(Color.foreground, 0.7)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                        wrapMode: Text.WordWrap
                    }

                    PanelSeparator {
                        visible: root.storeReady && root.events.length > 0
                        foreground: Color.foreground
                    }

                    Text {
                        width: parent.width
                        visible: root.storeReady && root.events.length > 0
                        text: "Counters change at midnight. Use the pencil to rename or remove an event."
                        textFormat: Text.PlainText
                        color: Util.alpha(Color.foreground, 0.55)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                        wrapMode: Text.WordWrap
                    }
                }
            }

            ConfirmDialog {
                id: confirm
                anchors.fill: parent
                opened: false
                message: root.confirmMessage
                cancelText: "Cancel"
                confirmText: root.confirmActionText
                background: Color.popups.background
                foreground: Color.foreground
                onCanceled: root.closeConfirm()
                onConfirmed: root.acceptConfirm()
            }
        }
    }
}

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// The Dolly panel under the bar button.
//
// Idle:       capture mode, audio (None / Mic / System / Mic + System, device
//             pickers, live level meters while the panel is open), camera
//             (Off / Bubble in edit / Live bubble), Start, the six most recent
//             recordings (poster frame, name, date, length, badges; click opens the
//             editor; per-row actions), "All recordings" and a prominent Settings.
// Recording:  timer, pause/resume, mute mic, stop, discard (right-click on the bar).
//
// Everything is themed from Commons.Color / Style (live on `omarchy theme set`),
// strings come from i18n/<lang>.json through Commons.Translations. The recent list
// shows the cached index at once (FileView); one `dolly panel --json` per open then
// fills everything else (Settings, devices, cameras, Set up state, missing packages),
// so opening the panel never waits on disk or ffmpeg and starts one command, not six.
Panel {
  id: root
  moduleName: "io.github.jagritvats.dolly"
  ipcTarget: "io.github.jagritvats.dolly"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root
  readonly property string dolly: hostWidget ? hostWidget.dolly : ""
  property var cfg: ({})              // Settings (config.json), loaded each time the panel opens

  Commons.Translations { id: tr; directory: Qt.resolvedUrl("i18n") }

  // config.json is the source of truth; the widget's shell.json values are the fallback
  property string mode: (cfg.capture && cfg.capture.mode) || (hostWidget ? hostWidget.setting("mode", "window") : "window")
  property string audioMode: (cfg.audio && cfg.audio.mode) || (hostWidget ? hostWidget.setting("audioMode", "none") : "none")
  property string cameraMode: (cfg.camera && cfg.camera.mode) || (hostWidget ? hostWidget.setting("camera", "off") : "off")
  property bool keys: cfg.capture ? cfg.capture.keys !== false : (hostWidget ? hostWidget.setting("keys", true) : true)
  // Settings > Auto-zoom > Zoom in automatically (setConfig keeps the value as text until reload)
  readonly property bool autoZoom: !(cfg.zoom && (cfg.zoom.enabled === false || cfg.zoom.enabled === "false"))
  readonly property string micDevice: (cfg.audio && cfg.audio.mic_device) || "default"
  readonly property string systemDevice: (cfg.audio && cfg.audio.system_device) || "default"
  readonly property string cameraDevice: (cfg.camera && cfg.camera.device) || ""

  property var audioDevices: ({ sources: [], sinks: [], default_source: "", default_sink: "", available: false })
  property var cameras: []
  property var recent: []
  property string expanded: ""        // dir of the row whose actions are open
  property string renaming: ""
  property string undoText: ""
  property real micLevel: 0
  property real sysLevel: 0

  readonly property color fg: bar ? bar.foreground : Commons.Color.foreground
  readonly property color accent: Commons.Color.accent
  readonly property color urgent: Commons.Color.urgent
  readonly property color dim: Qt.rgba(fg.r, fg.g, fg.b, 0.6)
  readonly property string ff: bar ? bar.fontFamily : Style.font.family
  readonly property bool recording: hostWidget ? hostWidget.recording : false

  // ---------------------------------------------------------------- actions
  function persist(key, value) {
    if (!hostWidget || !bar || !bar.shell || typeof bar.shell.updateEntryInline !== "function") return
    var entry = { id: root.moduleName }
    for (var k in hostWidget.settings) if (k !== "id") entry[k] = hostWidget.settings[k]
    entry[key] = value
    hostWidget.settings = entry
    bar.shell.updateEntryInline(root.moduleName, entry)
  }
  // a choice in the panel is remembered in Settings (config.json) and the widget entry
  function remember(key, value) {
    persist(key, value)
    Quickshell.execDetached([root.dolly, "config", "set", "--from-bar", key + "=" + value])
  }
  function setConfig(pairs) {
    var c = JSON.parse(JSON.stringify(root.cfg))
    for (var i = 0; i < pairs.length; i++) {
      var kv = pairs[i].split("="), p = kv[0].split(".")
      if (c[p[0]]) c[p[0]][p[1]] = kv.slice(1).join("=")
    }
    root.cfg = c
    Quickshell.execDetached([root.dolly, "config", "set"].concat(pairs))
  }
  function run(args) { Quickshell.execDetached([root.dolly].concat(args)) }
  function runThenRefresh(args) {
    actionProc.command = [root.dolly].concat(args)
    actionProc.running = true
  }

  function start() {
    if (hostWidget) hostWidget.startRecording({ mode: root.mode, keys: root.keys, audio: root.audioMode,
                                                camera: root.cameraMode })
  }

  function sourceFor(kind) {
    if (kind === "mic") return root.micDevice === "default" ? (audioDevices.default_source || "@DEFAULT_SOURCE@") : root.micDevice
    var s = root.systemDevice === "default" ? (audioDevices.default_sink || "@DEFAULT_SINK@") : root.systemDevice
    return s.indexOf("@") === 0 ? "@DEFAULT_MONITOR@" : s + ".monitor"
  }
  function deviceOptions(list, current) {
    var out = [{ value: "default", label: tr.t("panel.audio.default") }]
    for (var i = 0; i < list.length; i++) out.push({ value: list[i].name, label: list[i].description || list[i].name })
    return out
  }
  function fmtDuration(s) {
    var t = Math.max(0, Math.round(Number(s || 0)))
    var m = Math.floor(t / 60), ss = t % 60
    return m + ":" + (ss < 10 ? "0" : "") + ss
  }
  function fmtDate(ts) {
    var d = new Date(Number(ts || 0) * 1000)
    return d.toLocaleString(Qt.locale(Commons.I18n.formatLocale), "d MMM · HH:mm")
  }

  function open() { root.controller.show(); refreshAll() }
  function close() { root.controller.hide() }
  function toggle() { root.opened ? close() : open() }
  function refreshAll() { if (!panelProc.running) panelProc.running = true }

  // ---------------------------------------------------------------- data
  FileView {
    id: cacheFile
    path: Quickshell.env("XDG_CACHE_HOME") ? Quickshell.env("XDG_CACHE_HOME") + "/dolly/library.json"
                                           : Quickshell.env("HOME") + "/.cache/dolly/library.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try { root.recent = (JSON.parse(text()).items || []).slice(0, 5) } catch (e) {}
    }
  }
  // hotkeys + menu entry (bin/dolly-setup): a marketplace install runs no setup
  property bool integrationKnown: false
  property bool setUp: true
  property var hotkeyConflicts: []
  // packages Dolly still needs (fresh Omarchy: the Python packages; VMs: wf-recorder)
  property var missingPackages: []
  Process {
    id: panelProc
    command: [root.dolly, "panel", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var d = null
        try { d = JSON.parse(text) } catch (e) { root.integrationKnown = false; return }
        if (!d) return
        if (d.config) root.cfg = d.config
        if (d.recent) root.recent = d.recent.slice(0, 5)
        if (d.audio) root.audioDevices = d.audio
        root.cameras = d.cameras || []
        if (d.integration) {
          root.setUp = !!d.integration.set_up
          root.hotkeyConflicts = (d.integration.conflicts || []).map(c => c.combo.replace(/ /g, ""))
          root.integrationKnown = true
        } else root.integrationKnown = false
        root.missingPackages = (d.deps && d.deps.packages) || []
      }
    }
  }
  Process {
    id: actionProc
    onExited: { root.renaming = ""; root.refreshAll() }
  }
  // level meters: cheap (parec at 16 kHz, 10 Hz peaks) and only while the panel is open
  Process {
    id: micMeter
    running: root.opened && !root.recording && (root.audioMode === "mic" || root.audioMode === "both")
             && root.audioDevices.available
    command: [root.dolly, "audio", "meter", root.sourceFor("mic")]
    stdout: SplitParser { onRead: function(line) { try { root.micLevel = JSON.parse(line).peak } catch (e) {} } }
    onRunningChanged: if (!running) root.micLevel = 0
  }
  Process {
    id: sysMeter
    running: root.opened && !root.recording && (root.audioMode === "system" || root.audioMode === "both")
             && root.audioDevices.available
    command: [root.dolly, "audio", "meter", root.sourceFor("system")]
    stdout: SplitParser { onRead: function(line) { try { root.sysLevel = JSON.parse(line).peak } catch (e) {} } }
    onRunningChanged: if (!running) root.sysLevel = 0
  }
  Timer { id: undoTimer; interval: 8000; onTriggered: root.undoText = "" }

  // ---------------------------------------------------------------- view
  component Label: Text {
    textFormat: Text.PlainText
    color: root.fg
    font.family: root.ff
    font.pixelSize: Style.font.bodySmall
    elide: Text.ElideRight
  }
  component Meter: Rectangle {
    property real level: 0
    height: Style.space(4)
    radius: height / 2
    color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
    Rectangle {
      width: parent.width * Math.min(1, Math.sqrt(parent.level) * 1.4)
      height: parent.height
      radius: parent.radius
      color: parent.level > 0.7 ? root.urgent : root.accent
      Behavior on width { NumberAnimation { duration: Style.duration(90); easing.type: Easing.OutCubic } }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: false
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight)

    content: PanelKeyCatcher {
      anchors.fill: parent
      implicitHeight: col.implicitHeight
      onCloseRequested: root.close()
      onActivateRequested: if (!root.recording) root.start()
      onReturnRequested: if (!root.recording) root.start()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: col
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: Style.space(9)

        // header: title + a prominent Settings button
        Item {
          width: parent.width; height: Math.max(title.implicitHeight, settingsBtn.implicitHeight)
          Label { id: title; anchors.verticalCenter: parent.verticalCenter
                  text: root.recording ? tr.t(hostWidget && hostWidget.paused ? "panel.paused" : "panel.recording")
                                       : tr.t("panel.title")
                  font.pixelSize: Style.font.title; font.bold: true }
          Button { id: settingsBtn; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                   text: tr.t("panel.settings"); iconText: "󰒓"; bordered: true; foreground: root.fg; fontFamily: root.ff
                   onClicked: { root.close(); root.run(["settings"]) } }
        }

        // ---------------- while recording: controls
        Column {
          visible: root.recording
          width: parent.width; spacing: Style.space(8)
          Label { text: (hostWidget ? hostWidget.timerText : "") + (hostWidget && hostWidget.muted ? "  ·  " + tr.t("bar.muted") : "")
                  font.pixelSize: Style.font.title; color: root.urgent }
          Row {
            spacing: Style.space(6)
            Button { text: tr.t(hostWidget && hostWidget.paused ? "panel.resume" : "panel.pause"); iconText: "󰏤"; bordered: true
                     foreground: root.fg; fontFamily: root.ff; onClicked: if (hostWidget) hostWidget.togglePause() }
            Button { text: tr.t(hostWidget && hostWidget.muted ? "panel.unmute" : "panel.mute"); iconText: "󰍭"; bordered: true
                     foreground: root.fg; fontFamily: root.ff; onClicked: root.run(["mute"]) }
            Button { text: tr.t("panel.stop"); iconText: "󰓛"; bordered: true; foreground: root.urgent; fontFamily: root.ff
                     onClicked: { root.close(); if (hostWidget) hostWidget.stopRecording() } }
            Button { text: tr.t("panel.cancel"); iconText: "󰆴"; bordered: true; foreground: root.fg; fontFamily: root.ff
                     onClicked: { root.close(); root.run(["cancel"]) } }
          }
        }

        // ---------------- idle: capture, audio, camera
        Column {
          visible: !root.recording
          width: parent.width; spacing: Style.space(9)

          PanelSectionHeader { text: tr.t("panel.capture"); foreground: root.fg; fontFamily: root.ff }
          Row {
            width: parent.width; spacing: Style.space(6)
            Repeater {
              model: [["screen", "󰍹"], ["window", "󰖯"], ["region", "󰆞"]]
              Rectangle {
                required property var modelData
                readonly property bool on: root.mode === modelData[0]
                width: (col.width - Style.space(12)) / 3; height: Style.space(56)
                color: on ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.14) : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.03)
                border.width: 1
                border.color: on ? root.accent : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.25)
                radius: Style.cornerRadius
                Column {
                  anchors.centerIn: parent; spacing: Style.space(3)
                  Label { anchors.horizontalCenter: parent.horizontalCenter; text: modelData[1]
                          color: parent.parent.on ? root.accent : root.fg; font.pixelSize: Style.font.iconLarge }
                  Label { anchors.horizontalCenter: parent.horizontalCenter; text: tr.t("panel.mode." + modelData[0]) }
                }
                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                            onClicked: { root.mode = modelData[0]; root.remember("mode", modelData[0]) } }
              }
            }
          }

          PanelSectionHeader { text: tr.t("panel.audio"); foreground: root.fg; fontFamily: root.ff }
          ButtonGroup {
            width: parent.width
            options: [{ value: "none", label: tr.t("panel.audio.none") }, { value: "mic", label: tr.t("panel.audio.mic") },
                      { value: "system", label: tr.t("panel.audio.system") }, { value: "both", label: tr.t("panel.audio.both") }]
            value: root.audioMode
            foreground: root.fg; accent: root.accent; fontFamily: root.ff; focusable: false
            onChanged: function(v) { root.audioMode = v; root.remember("audioMode", v) }
          }
          Row {
            visible: root.audioMode === "mic" || root.audioMode === "both"
            width: parent.width; spacing: Style.space(8)
            Dropdown {
              width: parent.width * 0.62; showLabel: false
              options: root.deviceOptions(root.audioDevices.sources || [])
              value: root.micDevice; fontFamily: root.ff
              onChanged: function(v) { root.setConfig(["audio.mic_device=" + v]) }
            }
            Meter { width: parent.width * 0.34; anchors.verticalCenter: parent.verticalCenter; level: root.micLevel }
          }
          Row {
            visible: root.audioMode === "system" || root.audioMode === "both"
            width: parent.width; spacing: Style.space(8)
            Dropdown {
              width: parent.width * 0.62; showLabel: false
              options: root.deviceOptions(root.audioDevices.sinks || [])
              value: root.systemDevice; fontFamily: root.ff
              onChanged: function(v) { root.setConfig(["audio.system_device=" + v]) }
            }
            Meter { width: parent.width * 0.34; anchors.verticalCenter: parent.verticalCenter; level: root.sysLevel }
          }

          PanelSectionHeader { text: tr.t("panel.camera"); foreground: root.fg; fontFamily: root.ff }
          ButtonGroup {
            width: parent.width
            options: [{ value: "off", label: tr.t("panel.camera.off") }, { value: "edit", label: tr.t("panel.camera.edit") },
                      { value: "live", label: tr.t("panel.camera.live") }]
            value: root.cameraMode
            foreground: root.fg; accent: root.accent; fontFamily: root.ff; focusable: false
            onChanged: function(v) { root.cameraMode = v; root.remember("camera", v) }
          }
          Label { width: parent.width; wrapMode: Text.WordWrap; elide: Text.ElideNone; color: root.dim
                  font.pixelSize: Style.font.caption; text: tr.t("panel.camera." + root.cameraMode + "Hint") }
          Row {
            visible: root.cameraMode !== "off"
            width: parent.width; spacing: Style.space(8)
            Dropdown {
              width: parent.width * 0.62; showLabel: false
              options: root.cameras.length ? root.cameras.map(function(c) { return { value: c.device, label: c.name } })
                                           : [{ value: "", label: tr.t("panel.camera.none") }]
              value: root.cameraDevice || (root.cameras.length ? root.cameras[0].device : "")
              fontFamily: root.ff
              onChanged: function(v) { root.setConfig(["camera.device=" + v]) }
            }
            Button { text: tr.t("panel.camera.test"); iconText: "󰄀"; bordered: true; foreground: root.fg; fontFamily: root.ff
                     enabled: root.cameras.length > 0
                     onClicked: root.run(["camera", "preview", "--seconds", "6"]) }
          }

          Toggle { width: parent.width; label: tr.t("panel.keys"); checked: root.keys
                   onClicked: { root.keys = !root.keys; root.remember("keys", root.keys) }
                   foreground: root.fg; fontFamily: root.ff }

          Toggle { width: parent.width; label: tr.t("panel.autoZoom"); checked: root.autoZoom
                   onClicked: root.setConfig(["zoom.enabled=" + (!root.autoZoom)])
                   foreground: root.fg; fontFamily: root.ff }

          Item {
            visible: root.integrationKnown && (!root.setUp || root.hotkeyConflicts.length > 0)
            width: parent.width; height: visible ? setupLabel.implicitHeight + Style.space(8) : 0
            Label { id: setupLabel; anchors.left: parent.left
                    anchors.right: shortcutsBtn.visible ? shortcutsBtn.left : setupBtn.left
                    anchors.rightMargin: Style.space(8); anchors.verticalCenter: parent.verticalCenter
                    wrapMode: Text.WordWrap; elide: Text.ElideNone
                    color: root.setUp ? root.urgent : root.fg
                    text: root.setUp ? tr.t("panel.hotkeyConflict").arg(root.hotkeyConflicts.join(", "))
                                     : (root.missingPackages.length > 0
                                        ? tr.t("panel.setupHintPackages").arg(root.missingPackages.join(", "))
                                        : tr.t("panel.setupHint")) }
            // a hotkey is taken: Fix gives it up, Shortcuts opens Settings where other keys can be chosen
            Button { id: shortcutsBtn; visible: root.setUp; anchors.right: setupBtn.left
                     anchors.rightMargin: Style.space(6); anchors.verticalCenter: parent.verticalCenter
                     text: tr.t("panel.shortcuts"); bordered: true; foreground: root.fg; fontFamily: root.ff
                     onClicked: { root.close(); root.run(["settings", "--section", "shortcuts"]) } }
            Button { id: setupBtn; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                     text: root.setUp ? tr.t("panel.setupFix") : tr.t("panel.setup"); iconText: "󰌌"
                     bordered: true; foreground: root.accent; fontFamily: root.ff
                     onClicked: { if (root.setUp) root.runThenRefresh(["integration", "apply"])
                                  else { root.close(); root.run(["setup"]) } } }
          }

          Item {
            visible: root.missingPackages.length > 0 && (root.setUp || !root.integrationKnown)
            width: parent.width; height: visible ? needsLabel.implicitHeight + Style.space(8) : 0
            Label { id: needsLabel; anchors.left: parent.left; anchors.right: installBtn.left
                    anchors.rightMargin: Style.space(8); anchors.verticalCenter: parent.verticalCenter
                    wrapMode: Text.WordWrap; elide: Text.ElideNone; color: root.urgent
                    text: tr.t("panel.needs").arg(root.missingPackages.join(", ")) }
            Button { id: installBtn; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                     text: tr.t("panel.install"); iconText: "󰏗"; bordered: true; foreground: root.accent; fontFamily: root.ff
                     onClicked: { root.close(); root.run(["deps", "--install"]) } }
          }

          Rectangle {
            width: parent.width; height: Style.space(40)
            color: startMouse.containsMouse ? Qt.lighter(root.urgent, 1.08) : root.urgent
            radius: Style.cornerRadius
            Row {
              anchors.centerIn: parent; spacing: Style.space(10)
              Rectangle { width: Style.space(10); height: width; radius: width / 2; color: Commons.Color.background
                          anchors.verticalCenter: parent.verticalCenter }
              Label { text: tr.t("panel.start"); color: Commons.Color.background; font.pixelSize: Style.font.subtitle; font.bold: true }
            }
            MouseArea { id: startMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: root.start() }
          }
        }

        // ---------------- recent recordings
        Item {
          width: parent.width; height: recentHeader.implicitHeight
          PanelSectionHeader { id: recentHeader; text: tr.t("panel.recent"); foreground: root.fg; fontFamily: root.ff }
          Button { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                   text: tr.t("panel.allRecordings"); iconText: "󰕧"; foreground: root.accent; fontFamily: root.ff
                   onClicked: { root.close(); root.run(["library", "--open"]) } }
        }
        Label { visible: root.recent.length === 0; width: parent.width; wrapMode: Text.WordWrap; elide: Text.ElideNone
                text: tr.t("panel.recentEmpty"); color: root.dim; font.pixelSize: Style.font.caption }
        Repeater {
          model: root.recent
          Column {
            required property var modelData
            width: col.width; spacing: Style.space(4)
            Rectangle {
              width: parent.width; height: Style.space(44)
              radius: Style.cornerRadius
              color: rowMouse.containsMouse ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.06) : "transparent"
              Rectangle {
                id: thumbBox
                x: Style.space(4); anchors.verticalCenter: parent.verticalCenter
                width: Style.space(68); height: Style.space(38); radius: Math.max(2, Style.cornerRadius / 2)
                color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.08); clip: true
                Image { anchors.fill: parent; asynchronous: true; cache: false; fillMode: Image.PreserveAspectCrop
                        sourceSize.width: 160; source: modelData.thumb ? "file://" + modelData.thumb : "" }
                Label { anchors.right: parent.right; anchors.bottom: parent.bottom; anchors.margins: 2
                        text: root.fmtDuration(modelData.duration); font.pixelSize: Style.font.caption; color: "white"
                        style: Text.Outline; styleColor: "#80000000" }
              }
              Column {
                anchors.left: thumbBox.right; anchors.leftMargin: Style.space(10)
                anchors.right: moreBtn.left; anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(2)
                Label { width: parent.width; text: modelData.title; font.bold: modelData.named; visible: root.renaming !== modelData.dir }
                TextField {
                  id: renameField
                  visible: root.renaming === modelData.dir
                  width: parent.width; text: modelData.named ? modelData.title : ""
                  placeholderText: tr.t("panel.renamePlaceholder")
                  onVisibleChanged: if (visible) forceActiveFocus()
                  onAccepted: root.runThenRefresh(["library", "rename", modelData.dir, text || modelData.title])
                  Keys.onEscapePressed: root.renaming = ""
                }
                Row {
                  spacing: Style.space(6)
                  Label { text: modelData.named ? root.fmtDate(modelData.created)
                                                : (modelData.mode + " · " + (modelData.size_text || (Math.round(modelData.bytes / 1e5) / 10 + " MB")))
                          color: root.dim; font.pixelSize: Style.font.caption }
                  Repeater {
                    model: [["exported", modelData.exported], ["shared", modelData.shared], ["vertical", modelData.vertical],
                            ["captions", modelData.captions], ["recovered", modelData.recovered], ["partial", modelData.partial]]
                    Rectangle {
                      required property var modelData
                      visible: modelData[1] === true
                      width: badge.implicitWidth + Style.space(8); height: badge.implicitHeight + Style.space(2)
                      radius: height / 2; color: "transparent"; border.width: 1
                      border.color: modelData[0] === "recovered" || modelData[0] === "partial" ? root.urgent : root.accent
                      Label { id: badge; anchors.centerIn: parent; text: tr.t("panel.badge." + modelData[0])
                              font.pixelSize: Style.font.caption * 0.92; color: parent.border.color }
                    }
                  }
                }
              }
              MouseArea { id: rowMouse; anchors.fill: parent; anchors.rightMargin: moreBtn.width + Style.space(6)
                          hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                          onClicked: { root.close(); root.run(["edit", modelData.dir]) } }
              Button { id: moreBtn; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                       iconText: "󰇘"; foreground: root.fg; fontFamily: root.ff
                       onClicked: root.expanded = root.expanded === modelData.dir ? "" : modelData.dir }
            }
            Flow {
              visible: root.expanded === modelData.dir
              width: parent.width; spacing: Style.space(4)
              Button { text: tr.t("panel.action.open"); iconText: "󰏫"; bordered: true; foreground: root.fg; fontFamily: root.ff
                       onClicked: { root.close(); root.run(["edit", modelData.dir]) } }
              Button { text: tr.t("panel.action.copy"); iconText: "󰆏"; bordered: true; foreground: root.fg; fontFamily: root.ff
                       onClicked: root.run(["library", "copy", modelData.dir]) }
              Button { text: tr.t("panel.action.share"); iconText: "󰒖"; bordered: true; foreground: root.fg; fontFamily: root.ff
                       onClicked: { root.close(); root.run(["edit", modelData.dir, "--view", "share"]) } }
              Button { text: tr.t("panel.action.reveal"); iconText: "󰉋"; bordered: true; foreground: root.fg; fontFamily: root.ff
                       onClicked: root.run(["library", "reveal", modelData.dir]) }
              Button { text: tr.t("panel.action.rename"); iconText: "󰑕"; bordered: true; foreground: root.fg; fontFamily: root.ff
                       onClicked: root.renaming = modelData.dir }
              Button { text: tr.t("panel.action.delete"); iconText: "󰆴"; bordered: true; foreground: root.urgent; fontFamily: root.ff
                       onClicked: { root.expanded = ""; root.undoText = modelData.title; undoTimer.restart()
                                    root.runThenRefresh(["library", "trash", modelData.dir]) } }
            }
          }
        }
        Rectangle {
          visible: root.undoText !== ""
          width: parent.width; height: Style.space(34); radius: Style.cornerRadius
          color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.12)
          Label { anchors.left: parent.left; anchors.leftMargin: Style.space(10); anchors.verticalCenter: parent.verticalCenter
                  anchors.right: undoBtn.left; text: tr.t("panel.trashed") + ": " + root.undoText }
          Button { id: undoBtn; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                   text: tr.t("panel.undo"); foreground: root.accent; fontFamily: root.ff
                   onClicked: { root.undoText = ""; root.runThenRefresh(["library", "undo"]) } }
        }

        // ---------------- instant replay (compact)
        Toggle { visible: !root.recording; width: parent.width; label: tr.t("panel.replayToggle")
                 checked: root.hostWidget ? root.hostWidget.replay : false
                 onClicked: if (root.hostWidget) root.hostWidget.toggleReplay(); foreground: root.fg; fontFamily: root.ff }
        Row {
          width: parent.width; spacing: Style.space(6)
          visible: !root.recording && root.hostWidget ? root.hostWidget.replay : false
          Button { text: tr.t("panel.replaySave").arg(30); foreground: root.fg; fontFamily: root.ff; bordered: true
                   onClicked: { root.close(); root.hostWidget.saveReplay(30) } }
          Button { text: tr.t("panel.replaySave").arg(60); foreground: root.fg; fontFamily: root.ff; bordered: true
                   onClicked: { root.close(); root.hostWidget.saveReplay(60) } }
        }
      }
    }
  }
}

import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Dolly bar widget.
//
// Idle:       a record glyph; left click opens the record panel, right click
//             starts a recording straight away with the saved defaults,
//             middle click opens the last recording in the editor.
// Recording:  red dot + mm:ss timer; any click stops (and opens the editor).
// Phases (from `dolly status --json`): countdown 3-2-1, recording, paused,
// finalizing, rendering/exporting NN % - the same state the panel and the
// editor show.
//
// syncFlash(json): a 40 px magenta square with a green centre on an Overlay
// layer for ~70 ms at the recorded region's top-left. The logger asks for it
// at the start and end of a recording; finalize finds it in the video to line
// events up with frames (dolly/sync.py), and the export trims it out.
//
// Settings: Dolly's own Settings (editor) are the settings UI; ~/.config/dolly/config.json
// is the one source of truth. The widget's shell.json entry (manifest barWidget.schema,
// changed by `omarchy bar set io.github.jagritvats.dolly KEY VALUE` or the panel) is mirrored both
// ways: config.json changes -> updateEntryInline; a widget setting changed elsewhere ->
// `dolly config set --from-bar`. Values that already agree are never written back.
//
// State comes from `dolly status --json` (polled once a second only while
// recording, otherwise every 5 s and on `refresh`). The panel lives in
// RecordPanel.qml and is loaded through a Loader like the built-in clock.
BarWidget {
  id: root
  moduleName: "io.github.jagritvats.dolly"

  readonly property string pluginDir: {
    var u = String(Qt.resolvedUrl("."))
    return u.indexOf("file://") === 0 ? decodeURIComponent(u.slice(7)).replace(/\/$/, "") : u
  }
  readonly property string dolly: pluginDir + "/bin/dolly"

  property bool recording: false
  property bool paused: false
  property bool replay: false
  property int replaySeconds: 0
  property real elapsed: 0
  property string lastDir: ""
  property string backend: ""
  property real startedAt: 0
  property string phase: "idle"
  property int countdownLeft: 0
  property real jobProgress: -1
  property string jobKind: ""
  property bool muted: false
  property var config: ({})

  Commons.Translations { id: tr; directory: Qt.resolvedUrl("i18n") }

  // bar-widget key -> config.json "group.key" (dolly/config.py BAR_KEYS)
  readonly property var barKeys: ({ mode: "capture.mode", countdown: "capture.countdown", fps: "capture.fps",
                                    keys: "capture.keys", audioMode: "audio.mode", camera: "camera.mode",
                                    replaySeconds: "capture.replay_seconds", solidWindows: "privacy.solid_windows" })
  // bar-key -> value from config.json, delivered by `dolly status --json` ("bar")
  property var barCfg: ({})
  property bool _configLoaded: false
  function mirrorConfigToWidget() {
    if (!bar || !bar.shell || typeof bar.shell.updateEntryInline !== "function") return
    var entry = { id: root.moduleName }, changed = false
    for (var k in root.settings) if (k !== "id") entry[k] = root.settings[k]
    for (var bk in root.barKeys) {
      var v = root.barCfg[bk]
      if (v !== undefined && String(entry[bk]) !== String(v)) { entry[bk] = v; changed = true }
    }
    if (changed) { root._mirroring = true; root.settings = entry; bar.shell.updateEntryInline(root.moduleName, entry); root._mirroring = false }
  }
  property bool _mirroring: false
  property var _lastSettings: ({})
  function mirrorWidgetToConfig() {
    // only a change made to the widget entry elsewhere (`omarchy bar set ...`) flows back
    if (!root._configLoaded || root._mirroring) return
    var pairs = []
    for (var bk in root.barKeys) {
      var v = root.settings ? root.settings[bk] : undefined
      var before = root._lastSettings[bk]
      if (v !== undefined && v !== null && before !== undefined && String(v) !== String(before)
          && String(v) !== String(root.barCfg[bk])) pairs.push(bk + "=" + v)
    }
    root._lastSettings = Object.assign({}, root.settings || {})
    if (pairs.length) Quickshell.execDetached([root.dolly, "config", "set", "--from-bar"].concat(pairs))
  }
  // ~/.config/dolly/config.json changes reach the widget through the status poll ("bar")

  readonly property string timerText: {
    var s = Math.max(0, Math.floor(root.elapsed))
    var m = Math.floor(s / 60)
    var ss = s % 60
    return m + ":" + (ss < 10 ? "0" : "") + ss
  }

  function run(args) {
    Quickshell.execDetached([root.dolly].concat(args))
  }

  function refresh() {
    if (!statusProc.running) statusProc.running = true
  }

  // Flags only for what the panel chose; everything else comes from Settings (config.json)
  function startRecording(opts) {
    var o = opts || {}
    var args = ["record"]
    if (o.mode) args.push("--mode", o.mode)
    if (o.audio) args.push("--audio", o.audio)
    if (o.camera) args.push("--camera", o.camera)
    if (o.keys === false) args.push("--no-keys")
    close()
    run(args)
    pollFast.restart()
  }

  function togglePause() { run(["pause"]); Qt.callLater(refresh) }
  function toggleReplay() { run(["replay", root.replay ? "stop" : "start", "--seconds", String(setting("replaySeconds", 60))]); pollFast.restart() }
  function saveReplay(last) { run(last ? ["replay", "save", "--last", String(last)] : ["replay", "save"]) }

  function stopRecording() {
    run(["stop"])
    root.recording = false
    Qt.callLater(refresh)
  }

  function openLast() {
    run(["edit"])
  }

  // ---- panel contract (Bar.findPanelWidget needs open/close/opened)
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (root.recording) stopRecording(); else if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  function injectPanel() {
    var t = panelLoader.item
    if (!t) return
    t.bar = root.bar
    t.settings = root.settings
    t.anchorItem = button
    t.hostWidget = root
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  onBarChanged: injectPanel()
  onSettingsChanged: { injectPanel(); Qt.callLater(mirrorWidgetToConfig) }
  Component.onCompleted: refresh()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("RecordPanel.qml")
    visible: false
    onLoaded: { root.injectPanel(); Qt.callLater(root.injectPanel) }
  }

  Process {
    id: statusProc
    command: [root.dolly, "status", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var s = JSON.parse(text)
          root.recording = s.recording === true
          root.paused = s.paused === true
          root.muted = s.muted === true
          if (s.bar) {
            root.barCfg = s.bar
            if (!root._configLoaded) { root._lastSettings = Object.assign({}, root.settings || {}); root._configLoaded = true }
            root.mirrorConfigToWidget()
          }
          root.replay = s.replay === true
          root.replaySeconds = Number(s.replay_seconds || 0)
          root.lastDir = s.last || ""
          root.backend = s.backend || ""
          root.phase = s.phase || (root.recording ? "recording" : "idle")
          root.countdownLeft = Number(s.countdown || 0)
          var j = (s.jobs || [])[0]
          root.jobProgress = j ? Number(j.progress || 0) : -1
          root.jobKind = j ? String(j.kind || "rendering") : ""
          if (root.recording) {
            root.startedAt = Date.now() / 1000 - Number(s.elapsed || 0)
            root.elapsed = Number(s.elapsed || 0)
          }
        } catch (e) { root.recording = false }
      }
    }
  }

  // 1 s ticks for the timer are computed locally; the status poll only
  // corrects drift and catches recordings started/stopped elsewhere.
  Timer {
    id: tick
    interval: 250
    repeat: true
    running: root.recording && !root.paused
    onTriggered: root.elapsed = Date.now() / 1000 - root.startedAt
  }
  Timer {
    id: pollFast
    interval: 700
    repeat: true
    running: true
    property int n: 0
    onTriggered: {
      root.refresh()
      // poll quickly for ~8 s after a start request (countdown), and while
      // finalizing / rendering, then settle
      var busy = root.phase === "countdown" || root.phase === "finalizing" || root.jobProgress >= 0
      if (++n > 12 && !root.recording && !busy) { n = 0; stop(); pollSlow.start() }
    }
    onRunningChanged: if (running) n = 0
  }
  Timer {
    id: pollSlow
    interval: root.recording ? 2000 : 5000
    repeat: true
    onTriggered: root.refresh()
  }

  // ---- sync marker overlay (see header)
  // Two steps: syncArm maps the surface transparent, syncFlash only lights it. Mapping
  // and lighting a new surface at once was never painted under QT_QUICK_BACKEND=software
  // (VM, Nov 10: layer open ~70 ms, marker in 0 of 632 frames).
  property var flashScreen: null
  property bool flashArmed: false
  PanelWindow {
    id: flash
    property bool lit: false
    visible: false
    screen: root.flashScreen
    anchors { top: true; left: true }
    implicitWidth: 40
    implicitHeight: 40
    color: lit ? "#ff00ff" : "transparent"
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.namespace: "dolly-sync"
    mask: Region {}
    Rectangle { visible: flash.lit; anchors.centerIn: parent; width: parent.width / 2; height: parent.height / 2; color: "#00ff00" }
  }
  Timer {
    id: flashOff; repeat: false
    onTriggered: { flash.lit = false; if (!root.flashArmed) flash.visible = false }
  }
  // an arm without a flash (logger killed in between) never leaves the surface mapped
  Timer { id: flashDisarm; interval: 5000; repeat: false; onTriggered: root.syncDisarm() }

  function placeFlash(o) {
    var x = Number(o.x || 0), y = Number(o.y || 0), size = Number(o.size || 40)
    var target = null
    for (var i = 0; i < Quickshell.screens.length; i++) {
      var sc = Quickshell.screens[i]
      if (x >= sc.x && x < sc.x + sc.width && y >= sc.y && y < sc.y + sc.height) target = sc
    }
    root.flashScreen = target || Quickshell.screens[0]
    flash.margins.left = x - root.flashScreen.x
    flash.margins.top = y - root.flashScreen.y
    flash.implicitWidth = size
    flash.implicitHeight = size
  }

  function syncArm(arg) {
    var o = {}
    try { o = JSON.parse(arg || "{}") } catch (e) {}
    placeFlash(o)
    flash.lit = false
    root.flashArmed = true
    flash.visible = true
    flashDisarm.restart()
    return "ok"
  }

  function syncDisarm() {
    root.flashArmed = false
    flashDisarm.stop()
    if (!flash.lit) flash.visible = false
    return "ok"
  }

  function syncFlash(arg) {
    var o = {}
    try { o = JSON.parse(arg || "{}") } catch (e) {}
    if (!root.flashArmed) placeFlash(o)       // armed: already placed and mapped
    if (o.disarm) { root.flashArmed = false; flashDisarm.stop() }
    flash.visible = true
    flash.lit = true
    flashOff.interval = Number(o.ms || 70)
    flashOff.restart()
    return "ok"
  }

  ShellIpc {
    target: "io.github.jagritvats.dolly"
    function syncFlash(arg: string): string { return root.syncFlash(arg) }
    function syncArm(arg: string): string { return root.syncArm(arg) }
    function syncDisarm(): string { return root.syncDisarm() }
    function refresh(): void { root.broadcast("refresh"); pollFast.restart() }
    function toggle(): void { root.toggle() }
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function record(): void { root.startRecording({}) }
    function stop(): void { root.stopRecording() }
    function last(): void { root.openLast() }
    function pause(): void { root.togglePause() }
    function mute(): void { root.run(["mute"]); Qt.callLater(root.refresh) }
    function openSettings(): void { root.run(["settings"]) }
    function library(): void { root.run(["library", "--open"]) }
    function replay(): void { root.toggleReplay() }
    function saveReplay(): void { root.saveReplay(30) }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // 󰑊 record (idle) / 󰑋 recording / 󰏤 paused; 󰑓 = instant replay buffer running
    text: root.phase === "countdown" ? ("󰑊 " + Math.max(1, root.countdownLeft))
        : root.recording ? ((root.paused ? "󰏤 " : "󰑋 ") + root.timerText + (root.muted ? " 󰍭" : ""))
        : root.phase === "finalizing" ? "󰑊 …"
        : root.jobProgress >= 0 ? ("󰑊 " + Math.round(root.jobProgress * 100) + "%")
        : (root.replay ? "󰑊 󰑓" : "󰑊")
    active: (root.recording && !root.paused) || root.phase === "countdown"
    horizontalMargin: (root.recording || root.replay || root.phase !== "idle" || root.jobProgress >= 0) ? 8.75 : 6
    tooltipText: root.phase === "countdown" ? tr.t("bar.tooltip.countdown")
               : root.phase === "finalizing" ? tr.t("bar.tooltip.finalizing")
               : root.jobProgress >= 0 ? tr.t("bar.tooltip.job").arg(root.jobKind).arg(Math.round(root.jobProgress * 100))
               : root.recording ? tr.t("bar.tooltip.recording")
               : root.replay ? tr.t("bar.tooltip.replay").arg(root.replaySeconds)
               : tr.t("bar.tooltip.idle")
    onPressed: function(b) {
      if (root.phase === "countdown") { root.run(["cancel"]); return }
      // while recording, right-click opens the controls (pause, mute mic, stop, discard)
      if (root.recording) { if (b === Qt.RightButton) { if (panelLoader.item) panelLoader.item.open() } else root.stopRecording() }
      else if (root.replay && b === Qt.RightButton) root.saveReplay(30)
      else if (b === Qt.RightButton) root.startRecording({})
      else if (b === Qt.MiddleButton) root.openLast()
      else if (panelLoader.item) panelLoader.item.toggle()
    }
  }
}

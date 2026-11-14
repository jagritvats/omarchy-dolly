import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import Qt.labs.folderlistmodel
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "." as Compat

// Dolly bar widget.
//
// Idle:       a record glyph; left click opens the record panel, right click
//             starts a recording straight away with the saved defaults,
//             middle click opens the last recording in the editor.
// Recording:  red dot + mm:ss timer; any click stops (and opens the editor).
// Phases: starting (Record was pressed), countdown 3-2-1, recording, paused, finalizing,
// rendering/exporting
// NN % - the same state the panel and the editor show.
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
// State is never polled: the widget watches the small files Dolly keeps in its
// runtime folder (see "state, without polling" below) and starts no process to
// learn it. One `dolly shell-start` runs when the shell loads the plugin. The panel
// lives in RecordPanel.qml and is loaded through a Loader like the built-in clock.
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
  property string backend: ""
  property real startedAt: 0
  property string phase: "idle"
  property int countdownLeft: 0
  property real jobProgress: -1
  property string jobKind: ""
  property bool muted: false
  property var config: ({})

  Compat.DollyTranslations { id: tr; directory: Qt.resolvedUrl("i18n") }

  // bar-widget key -> config.json "group.key" (dolly/config.py BAR_KEYS)
  readonly property var barKeys: ({ mode: "capture.mode", countdown: "capture.countdown", fps: "capture.fps",
                                    keys: "capture.keys", audioMode: "audio.mode", camera: "camera.mode",
                                    replaySeconds: "capture.replay_seconds", solidWindows: "privacy.solid_windows" })
  // bar-key -> value from ~/.config/dolly/config.json (watched, see below)
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
  function applyConfig(c) {
    var view = {}
    for (var bk in root.barKeys) {
      var kp = root.barKeys[bk].split("."), g = c[kp[0]]
      if (g && g[kp[1]] !== undefined) view[bk] = g[kp[1]]
    }
    root.barCfg = view
    if (!root._configLoaded) { root._lastSettings = Object.assign({}, root.settings || {}); root._configLoaded = true }
    root.mirrorConfigToWidget()
  }

  readonly property string timerText: {
    var s = Math.max(0, Math.floor(root.elapsed))
    var m = Math.floor(s / 60)
    var ss = s % 60
    return m + ":" + (ss < 10 ? "0" : "") + ss
  }

  function run(args) {
    Quickshell.execDetached([root.dolly].concat(args))
  }

  function refresh() { reloadAll() }

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
  }

  function togglePause() { run(["pause"]); Qt.callLater(refresh) }
  function toggleReplay() { run(["replay", root.replay ? "stop" : "start", "--seconds", String(setting("replaySeconds", 60))]) }
  function saveReplay(last) { run(last ? ["replay", "save", "--last", String(last)] : ["replay", "save"]) }

  function stopRecording() {
    run(["stop"])
    root.stopRequested = true       // show "finishing" at once; the files confirm it a moment later
    stopGiveUp.restart()
    derive()
  }

  function openLast() {
    run(["edit"])
  }

  // ---- panel contract (Bar.findPanelWidget needs open/close/opened)
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  function open() { if (panelLoader.item) panelLoader.item.open() }
  // `omarchy-shell io.github.jagritvats.dolly open` reaches one bar's button only (one name,
  // one button per monitor), which need not be the bar you are looking at. The shell knows
  // which bar is in front, so ask it; where it cannot tell, open here as before.
  function openInFront() { if (!summonProc.running) summonProc.running = true }
  Process {
    id: summonProc
    command: ["omarchy-shell", "shell", "summon", "io.github.jagritvats.dolly", "{}"]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: { if (text.trim() !== "ok") root.open() } }
  }
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
  Component.onCompleted: { startProc.running = true; startGiveUp.start() }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("RecordPanel.qml")
    visible: false
    onLoaded: { root.injectPanel(); Qt.callLater(root.injectPanel) }
  }

  // ---- state, without polling ---------------------------------------------------------
  // Dolly keeps small files in its runtime folder and replaces them atomically whenever
  // something changes. The widget watches them (the way Omarchy's Agents panel watches its
  // records) and starts no process to learn the state:
  //   state.json    a recorder is up: recorder_pid, started, paused_at, paused_total, muted,
  //                 backend, preroll (true while the countdown runs)
  //   phase.json    phase (countdown | recording | finalizing | ...), left, pid
  //   replay.json   the instant-replay recorder: recorder_pid, seconds
  //   jobs/*.json   one per running export: kind, progress, pid
  //   ~/.config/dolly/config.json   Settings (mirrored into the widget's own entry)
  // While something is shown as running, /proc/<pid>/stat is re-read every few seconds, so
  // a process that died without cleaning up cannot leave the bar stuck. Nothing runs while
  // idle. tests/test_bar_state_files.py keeps the field names here and in the code that
  // writes the files in step.
  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string runDir: (Quickshell.env("XDG_RUNTIME_DIR") || (home + "/.local/state")) + "/dolly"
  readonly property string configPath: Quickshell.env("DOLLY_CONFIG")
      || ((Quickshell.env("XDG_CONFIG_HOME") || (home + "/.config")) + "/dolly/config.json")
  property bool ready: false           // the folders exist: `dolly shell-start` has run
  property var st: null
  property var ph: null
  property var rp: null
  property var jobInfo: null
  property string jobName: ""
  property var deadJobs: ({})
  property bool recorderAlive: true
  property bool phaseAlive: true
  property bool replayAlive: true
  property bool jobAlive: true
  property bool stopRequested: false
  property int retries: 0

  function parseObj(text) {
    try { var o = JSON.parse(text); return (o !== null && typeof o === "object") ? o : null } catch (e) { return null }
  }
  function procAlive(text) { return !/\)\s[ZX]\s/.test(text) }      // gone or a zombie = not running
  function procPath(pid) { return pid ? "/proc/" + Number(pid) + "/stat" : "" }
  // a file caught mid-write reads empty: look again shortly, a few times at most
  function unreadable() { if (root.retries < 4) { root.retries++; retry.restart() } }

  function derive() {
    var st = root.st, ph = root.ph
    var phName = (ph && ph.phase && root.phaseAlive) ? String(ph.phase) : "idle"
    var counting = !!(st && st.preroll === true && phName === "countdown")
    var stopping = phName === "finalizing" || root.stopRequested
    var rec = !!(st && st.recorder_pid && root.recorderAlive && !counting && !stopping)
    var paused = rec && !!st.paused_at
    root.recording = rec
    root.paused = paused
    root.muted = rec && st.muted === true
    root.backend = rec ? String(st.backend || "") : ""
    root.phase = rec ? (paused ? "paused" : "recording")
               : (root.stopRequested && st && phName !== "countdown") ? "finalizing" : phName
    root.countdownLeft = phName === "countdown" ? Number(ph.left || 0) : 0
    if (rec) {
      root.startedAt = Number(st.started || 0) + Number(st.paused_total || 0)
      root.elapsed = (paused ? Number(st.paused_at) : Date.now() / 1000) - root.startedAt
    }
    var rp = root.rp
    root.replay = !!(rp && rp.recorder_pid && root.replayAlive)
    root.replaySeconds = root.replay ? Number(rp.seconds || 0) : 0
    var j = root.jobAlive ? root.jobInfo : null
    root.jobProgress = j ? Number(j.progress || 0) : -1
    root.jobKind = j ? String(j.kind || "rendering") : ""
  }

  function setState(o) {
    root.st = o
    root.recorderAlive = true
    if (o === null) { root.stopRequested = false; stopGiveUp.stop() }
    recProc.path = procPath(o ? o.recorder_pid : 0)
    derive()
  }
  function setPhase(o) {
    root.ph = o
    root.phaseAlive = true
    phProc.path = procPath(o ? o.pid : 0)
    derive()
  }
  function setReplay(o) {
    root.rp = o
    root.replayAlive = true
    rpProc.path = procPath(o ? o.recorder_pid : 0)
    derive()
  }
  function setJob(o) {
    root.jobInfo = o
    root.jobAlive = true
    jobProc.path = procPath(o ? o.pid : 0)
    derive()
  }
  // the first export whose process is still there (like `dolly status`: jobs()[0])
  function pickJob() {
    var m = jobsLoader.item, name = ""
    for (var i = 0; m && i < m.count; i++) {
      var n = m.get(i, "fileName")
      if (n && !root.deadJobs[n]) { name = n; break }
    }
    if (name === root.jobName) return
    root.jobName = name
    jobFile.path = name ? root.runDir + "/jobs/" + name : ""
    if (!name) setJob(null)
  }
  function reloadAll() {
    if (!root.ready) return
    stateFile.reload(); phaseFile.reload(); replayFile.reload(); configFile.reload()
    if (jobFile.path) jobFile.reload()
    pickJob()
  }

  FileView {
    id: stateFile
    path: root.ready ? root.runDir + "/state.json" : ""
    watchChanges: true; printErrors: false
    onFileChanged: reload()
    onLoaded: { var o = root.parseObj(text()); if (o === null) root.unreadable(); else { root.retries = 0; root.setState(o) } }
    onLoadFailed: root.setState(null)
  }
  FileView {
    id: phaseFile
    path: root.ready ? root.runDir + "/phase.json" : ""
    watchChanges: true; printErrors: false
    onFileChanged: reload()
    onLoaded: { var o = root.parseObj(text()); if (o === null) root.unreadable(); else { root.retries = 0; root.setPhase(o) } }
    onLoadFailed: root.setPhase(null)
  }
  FileView {
    id: replayFile
    path: root.ready ? root.runDir + "/replay.json" : ""
    watchChanges: true; printErrors: false
    onFileChanged: reload()
    onLoaded: { var o = root.parseObj(text()); if (o === null) root.unreadable(); else { root.retries = 0; root.setReplay(o) } }
    onLoadFailed: root.setReplay(null)
  }
  FileView {
    id: configFile
    path: root.ready ? root.configPath : ""
    watchChanges: true; printErrors: false
    onFileChanged: reload()
    onLoaded: { var c = root.parseObj(text()); if (c === null) root.unreadable(); else { root.retries = 0; root.applyConfig(c) } }
    onLoadFailed: root.applyConfig({})       // no settings saved yet: nothing to mirror
  }
  Loader {
    id: jobsLoader
    active: root.ready
    sourceComponent: Component {
      FolderListModel {
        folder: "file://" + root.runDir + "/jobs"
        nameFilters: ["*.json"]
        showDirs: false
        sortField: FolderListModel.Name
        onCountChanged: root.pickJob()
        onStatusChanged: root.pickJob()
      }
    }
  }
  FileView {
    id: jobFile
    path: ""
    watchChanges: true; printErrors: false
    onFileChanged: reload()
    onLoaded: { var o = root.parseObj(text()); if (o !== null) root.setJob(o) }
    onLoadFailed: root.setJob(null)
  }
  // is the process behind what the bar shows still there? (read, never started)
  FileView {
    id: recProc; path: ""; printErrors: false
    onLoaded: { var a = root.procAlive(text()); if (a !== root.recorderAlive) { root.recorderAlive = a; root.derive() } }
    onLoadFailed: if (path !== "" && root.recorderAlive) { root.recorderAlive = false; root.derive() }
  }
  FileView {
    id: phProc; path: ""; printErrors: false
    onLoaded: { var a = root.procAlive(text()); if (a !== root.phaseAlive) { root.phaseAlive = a; root.derive() } }
    onLoadFailed: if (path !== "" && root.phaseAlive) { root.phaseAlive = false; root.derive() }
  }
  FileView {
    id: rpProc; path: ""; printErrors: false
    onLoaded: { var a = root.procAlive(text()); if (a !== root.replayAlive) { root.replayAlive = a; root.derive() } }
    onLoadFailed: if (path !== "" && root.replayAlive) { root.replayAlive = false; root.derive() }
  }
  FileView {
    id: jobProc; path: ""; printErrors: false
    onLoaded: if (!root.procAlive(text())) root.jobDied()
    onLoadFailed: if (path !== "") root.jobDied()
  }
  function jobDied() {
    if (!root.jobName) return
    root.deadJobs[root.jobName] = true        // an export that crashed left its file behind
    pickJob()
  }
  Timer {
    id: aliveCheck
    interval: 3000
    repeat: true
    running: root.st !== null || root.ph !== null || root.rp !== null || root.jobInfo !== null
    onTriggered: {
      if (recProc.path) recProc.reload()
      if (phProc.path) phProc.reload()
      if (rpProc.path) rpProc.reload()
      if (jobProc.path) jobProc.reload()
    }
  }
  Timer { id: retry; interval: 80; repeat: false; onTriggered: root.reloadAll() }
  // a Stop that did nothing (the command failed) must not leave "finishing" on the bar
  Timer { id: stopGiveUp; interval: 6000; repeat: false; onTriggered: { root.stopRequested = false; root.derive() } }

  // Once per shell load (start, restart, the reload after `omarchy plugin update`): Dolly
  // makes sure the folders above exist and, after an update, refreshes what Set up wrote.
  Process {
    id: startProc
    command: [root.dolly, "shell-start"]
    onExited: { startGiveUp.stop(); root.ready = true; Qt.callLater(root.reloadAll) }
  }
  Timer { id: startGiveUp; interval: 5000; repeat: false; onTriggered: if (!root.ready) { root.ready = true; Qt.callLater(root.reloadAll) } }

  // the timer text is computed locally from the start time in state.json
  Timer {
    id: tick
    interval: 250
    repeat: true
    running: root.recording && !root.paused
    onTriggered: root.elapsed = Date.now() / 1000 - root.startedAt
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

  // Omarchy 4.0.4 has only IpcHandler. On newer shells, keep ShellIpc's
  // registry lifecycle so socket IPC still works. 4.5 registers handlers; later
  // shells also elect a per-target leader, so detect that API independently.
  // Keep this property off the handler: Quickshell warns about extra change signals there.
  readonly property var ipcRegistry: typeof Commons.IpcRegistry !== "undefined" ? Commons.IpcRegistry : null
  IpcHandler {
    id: ipc
    target: "io.github.jagritvats.dolly"
    enabled: root.ipcRegistry && typeof root.ipcRegistry.leads === "function"
             ? root.ipcRegistry.leads(ipc, root.ipcRegistry.revision) : true
    onTargetChanged: if (root.ipcRegistry && typeof root.ipcRegistry.touch === "function") root.ipcRegistry.touch()
    Component.onCompleted: if (root.ipcRegistry) root.ipcRegistry.register(ipc)
    Component.onDestruction: if (root.ipcRegistry) root.ipcRegistry.unregister(ipc)
    function syncFlash(arg: string): string { return root.syncFlash(arg) }
    function syncArm(arg: string): string { return root.syncArm(arg) }
    function syncDisarm(): string { return root.syncDisarm() }
    function refresh(): void { root.broadcast("refresh"); root.reloadAll() }
    function toggle(): void { root.toggle() }
    function open(): void { root.openInFront() }
    function close(): void { root.close() }
    function show(): void { root.openInFront() }
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
        : (root.phase === "finalizing" || root.phase === "starting") ? "󰑊 …"
        : root.jobProgress >= 0 ? ("󰑊 " + Math.round(root.jobProgress * 100) + "%")
        : (root.replay ? "󰑊 󰑓" : "󰑊")
    active: (root.recording && !root.paused) || root.phase === "countdown"
    horizontalMargin: (root.recording || root.replay || root.phase !== "idle" || root.jobProgress >= 0) ? 8.75 : 6
    tooltipText: (root.phase === "countdown" || root.phase === "starting") ? tr.t("bar.tooltip.countdown", "Dolly · Starting…")
               : root.phase === "finalizing" ? tr.t("bar.tooltip.finalizing", "Dolly · Saving…")
               : root.jobProgress >= 0 ? tr.t("bar.tooltip.job", "Dolly · %1 %2%").arg(root.jobKind).arg(Math.round(root.jobProgress * 100))
               : root.recording ? (root.paused ? tr.t("bar.tooltip.paused", "Dolly · Paused")
                                             : tr.t("bar.tooltip.recording", "Dolly · Recording — click to stop"))
               : root.replay ? tr.t("bar.tooltip.replay", "Dolly · Replay (%1 s)").arg(root.replaySeconds)
               : tr.t("bar.tooltip.idle", "Dolly Recorder")
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

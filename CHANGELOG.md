# Changelog

## 0.5.0 (2026-11-10)

Tested end to end on Omarchy 4.5 in VirtualBox (wf-recorder, software rendering).

First public release: <https://github.com/jagritvats/omarchy-dolly>. The plugin id is now `io.github.jagritvats.dolly` (it was
`jagrit.dolly` in pre-release builds); if you ran a pre-release, remove it with
`omarchy plugin remove jagrit.dolly` and run Set up again after installing.

Omarchy integration
- First-run **Set up** card in the panel: a marketplace install runs no setup, so new
  users got no hotkeys or menu rows. `dolly setup` does the same from a terminal.
- `dolly integration [--json]` and a doctor row: which hotkeys and menu rows Dolly added,
  and any Dolly hotkey that Omarchy now also uses (4.5 took Super+Alt+L); the panel
  offers **Fix**.
- Removing the plugin no longer leaves live hotkeys or menu rows: they only load while
  the plugin is installed.
- The live camera bubble works on Hyprland 0.56 (Lua dispatchers, with the old form
  as fallback); it was tiled into the recording like a normal window.

Recording and timing
- Sync markers use real frame timestamps: wf-recorder writes variable frame rate, so
  clicks, keys and zooms landed ~140 ms late, and the end marker was missed.
- A lone marker is paired with the log mark the clock agrees with (a lost start flash
  had shifted everything by the length of the recording), and a clock estimate is used
  when no marker is found (it was 0, about 3 s off).
- wf-recorder keeps the end of a recording (it dropped its last half second on stop).
- Two-step sync flash survives software rendering; marker colour test is relative.

Auto-zoom and framing
- Zooms frame the text being typed: not the parked pointer, not the middle of
  everything that changed after `clear`, not the pointer while the camera glides.
- No more pumping between ~1.2x and 2x; close-ups far apart are kept, dialogs a click
  opened are framed, results stay on screen a moment.
- Vertical 9:16 keeps the start of long lines and the dialog after a click in frame.

Editor and library
- One editor window: Library, Settings and Edit reuse it instead of opening new ones.
- **Add image…** background picker (theme backgrounds, ~/Pictures, upload, path), also
  as a default in Settings › Look.
- Cursor options that cannot apply to a recording with the real cursor (wf-recorder)
  are greyed out with the reason.
- Sizes read 412 KB / 4.7 MB everywhere (small recordings showed "0 MB" or "1 MB").
- Settings no longer cuts off its right column on smaller windows.

Reliability
- Long recordings no longer run out of memory: activity analysis streams the video
  (10 minutes: 1.8 GB -> 0.24 GB peak; flat in the recording length).
- Recording files are written atomically with a backup; a damaged one is restored.
- Clear one-line errors (and a notification from the bar or a hotkey) instead of
  tracebacks; details in ~/.local/state/dolly/last-error.log.
- Recording into a read-only folder, or a second recording, now says so (was silent).
- Timeouts on external commands; swallowed errors are logged.
- `dolly share export <preset>` no longer crashes after exporting.
- Keys are counted once (Hyprland can report a press twice: "Enter x2").
- Missing packages are reported before disk notes; export progress is one line per
  10 % in logs.

## 0.4.2 (2026-11-10)

First run on a fresh Omarchy 4.0.4 VirtualBox VM (no python packages, no
wf-recorder, gpu-screen-recorder unable to use the virtual GPU) hit dead ends.
- `dolly doctor` crashed on a missing OpenCV (it imported the renderer to count
  workers). It now works without numpy/OpenCV/Pillow, marks them FAIL, and ends with
  "Missing packages: ... Install them: dolly deps --install".
- New `dolly deps [--install] [--json]`: names the Arch packages Dolly still needs
  (python-numpy, python-opencv, python-pillow, ffmpeg, and wf-recorder when
  gpu-screen-recorder cannot capture on this GPU, e.g. "unknown gpu vendor: VMware").
  `--install` opens a floating terminal running Omarchy's `omarchy-pkg-add` (it asks
  for the password there), then `dolly doctor`.
- Recording with packages missing now stops before the picker, countdown and Do Not
  Disturb, with a notification that lists each package and why, and an Install
  button. When gpu-screen-recorder fails for a reason it can never get past here,
  Dolly remembers it (until gpu-screen-recorder is updated) and says to install
  wf-recorder instead of "No screen recorder worked".
- `dolly doctor` checks whether gpu-screen-recorder can capture on this GPU.
- The record panel shows "Needs ..." with an Install button when packages are missing.
- `dolly-setup` asks to install only when run in a terminal; piped or scripted it
  prints the command instead of a hidden prompt. It installs everything missing
  (not only the python packages) and puts the record button on the bar with
  `omarchy bar put` where that exists.
- `dolly --help` no longer shows "log-events ==SUPPRESS==".

Second pass on the VM (Omarchy 4.5, software rendering, 8 GB RAM, small disk):
- Running Dolly no longer writes `__pycache__` into the plugin folder. Omarchy reloads a
  plugin whenever its folder changes, which reset the bar widget over and over
  ("Local plugin changed, reloading" x12). Bytecode now goes to ~/.cache/dolly/pycache.
- The sync marker is drawn in two steps: the overlay is mapped transparent first, then
  only its colour changes, and it stays lit longer (160 ms) when Qt draws in software.
  Mapping and lighting it at once was never painted under `QT_QUICK_BACKEND=software`,
  so recordings lost their exact time base. Older bar widgets still get a flash.
- Disk space is judged in minutes of recording instead of a fixed 2 GB. The rate is
  measured from your last recordings (else estimated). Below about 2 minutes Dolly
  refuses, with Record anyway / Choose folder / Clean up; below 10 minutes it records
  and says how long you have. A recordings folder in memory (tmpfs) is always called
  out. `dolly doctor` shows minutes left, the rate and the filesystem.
  `dolly record --ignore-disk` records anyway.
- `dolly doctor` names a virtual GPU (VirtualBox/VMware vmwgfx, virtio, QXL), and the
  camera bubble's mpv gets `LIBGL_ALWAYS_SOFTWARE=1` there (`DOLLY_SOFTWARE_GL=0/1`).
- The library listed recordings with the same start time in directory order; it now
  falls back to the folder name (newest first).
- Tests: a test that fails half-way no longer leaves its fake recorder running (it held
  a deleted 2.5 GB file in a RAM /tmp); stray test processes are reaped after each test.

Third pass, from real recordings on the VM (commits d9d7429, 47ca50d):
- Auto-zoom frames the text being typed. It could zoom 2x onto empty space while
  you typed somewhere else: the pointer's resting position at the start counted as
  pointer activity, a pointer that the recorder bakes in (and Omarchy hides on each
  key press) looked like typed text, typing shots aimed at the middle of everything
  that changed recently (after `clear`, most of the terminal) instead of the lines
  around the caret, and glide pauses followed a parked pointer.
- Keys whose glyphs are not visible yet use the nearest caret instead of the whole
  window, so typing shots keep their planned zoom instead of dropping to ~1.2x.
- A keystroke counts once even if Hyprland delivers it twice ("Enter x2").
- `dolly export` prints one progress line per 10 % when not in a terminal.
- Missing packages are reported before any disk-space note.
- Audio never uses the "@DEFAULT_SINK@" placeholder as a device name (it dropped the
  system-audio track).

Publishing:
- README: install from the marketplace, required and optional packages, plain
  recording without zoom, background and frame options (including your own image
  via project.json), aspect ratios, virtual machine notes, removal.
- `preview.png` and the README screenshots are real output recorded on Omarchy 4.5 in
  VirtualBox; the remaining harness screenshots are labelled as such.

## 0.4.1 (2026-11-10)

### Auto-zoom: glide between nearby actions
- Actions close to each other (a settings panel, a form, a toolbar) no longer zoom out
  to the whole screen between every click and back in. The camera stays zoomed and
  glides to the next action, moving only as far as needed to show it. It still zooms
  out after a long pause (default 6 s between actions), for a far jump (more than
  about 1.3 screens' worth at the current zoom), when the pointer leaves for somewhere
  else in the pause, and at a workspace switch (a cut).
- Across windows: a window the action itself opened (a dialog, a picker, a menu) is part
  of the same flow and glides. Switching to another app window that was already open is a
  change of context and pulls back, unless it is a short, quick hop (just across the
  border, within 3 s). A tiled window opening, closing or going full screen, or a big
  floating surface such as the scratchpad appearing, pulls back so the new layout is seen.
  A pointer parked elsewhere while typing, or a brief trip to another monitor (up to
  1.5 s), does not break a glide. Dragging or resizing the window a shot is about (or
  the recorded window in window mode) in the pause pulls back to show all of it.
- Short gaps (under 1.4 s between nearby shots) behave as in 0.4.0: the camera heads for
  the next shot early. Only the longer pauses that gliding adds hold the current shot.
- Checked on the 12 synthetic scenarios (tiling, floating drag, workspaces, interruptions,
  dual monitor, window mode, build log, fractional and rotated monitors) in 16:9, 9:16 and
  1:1: every run within the caps, window mode back to 98.7 % after the first version of
  the glide held a zoomed shot while the window was dragged.
- During a glide the camera aims at where the pointer is about to be (the recording is
  known in advance), so the pointer stays in frame on long hops. Hops over about half a
  screen breathe out slightly (never all the way to the whole screen) and back in.
  Pans stay within the scenario cap (1.6 viewport widths/s).
- In the walkthrough recording the five clicks down the Dolly panel were four zoom-outs
  and four zoom-ins; they are now one shot that scrolls down the panel.

### Auto-zoom settings
- Settings › Auto-zoom: Zoom in automatically (on/off), Zoom level (replaces "Zoom
  strength"; old files migrate), Maximum zoom, Zoom in on clicks / typing / windows and
  scrolling output, Between nearby actions (glide or zoom out), Stay zoomed through
  pauses up to N s, Camera motion (calm, normal, quick).
- The editor has the same options per recording (sidebar › Auto-zoom). Changing one
  re-plans the automatic zooms right away; zooms you added or edited stay, and
  automatic ones are trimmed around them. Undo works as usual.
- The record panel has a "Zoom in on the action" toggle.
- With automatic zoom off, manual zooms and "zoom here" markers still zoom.

## 0.4.0 (2026-11-09)

### Fractional scaling and monitors
- Monitor geometry uses Hyprland's real scale (1/120 grid with a clean divisor), so
  logical sizes are exact at 1.6667 and 1.3333 whether `hyprctl` prints 1.6666666 or
  1.67. Transforms 1/3/5/7 swap width and height.
- A region is recorded on the monitor that holds it (not the focused one). A pick that
  spans monitors is clamped to the one holding most of it, or refused with a message.
- Picking uses `omarchy-capture-region smart --match-monitor`; a whole monitor is
  recorded by name (`gpu-screen-recorder -w NAME -s RES`, `wf-recorder -o NAME`), as
  Omarchy's own recorder does.
- Regions snap to an even number of whole physical pixels (encoders rounded odd sizes:
  1003.33 px became 1002 px at 1.6667) and `capture.json` records the physical rect.
- A followed window dragged to another monitor ends the recording cleanly (brief
  excursions are kept).

### Audio and camera
- Audio is one choice: None / Microphone / System audio / Microphone + System, recorded
  as separate tracks on every backend (wf-recorder used to drop the mic when both were
  on). Device pickers and live level meters in the panel. The editor has per-track
  volume and mute, mic noise suppression, ducking of system audio while you speak, and
  loudness normalisation. "Mute mic" (Super+Alt+N or the bar's right-click) records a
  muted span, not a cut.
- Camera bubble, off by default: "Bubble in edit" (preview during the countdown, recorded
  separately, shape/size/position editable afterwards, camera-only ranges) or "Live
  bubble" (on screen, recorded as seen). Circle, rounded or rectangle; theme-accent
  border; shadow; moves away from the cursor.
- Audio tracks and the camera are lined up with the video through the sync-marker fit
  (offset and clock drift). The marker time is now the IPC midpoint, and drift smaller
  than the frame quantisation is treated as noise.

### Omarchy integration
- Bar, panel and editor take colours and fonts from the current Omarchy theme and follow
  `omarchy theme set` live, light themes included. Each recording keeps the theme it was
  made in, so re-exports stay the same after a theme switch.
- Recordings go to `$OMARCHY_SCREENRECORD_DIR/Dolly`, then XDG_VIDEOS_DIR, then ~/Videos.
- Never two recorders: Dolly refuses to start next to Omarchy's recorder, and Omarchy's
  stop key ending a Dolly recording counts as a normal stop.
- Notifications go through `omarchy-notification-send` with strings from `i18n/en.json`
  (also used by the panel through `Commons.Translations`).
- Menu rows under Trigger › Capture for the library and settings; mute and library
  hotkeys with descriptions; a window rule for the live camera bubble.

### Library and settings
- The panel shows the five latest recordings (poster frame, name, length, badges) with
  open / copy / share / show in Files / rename / trash-with-undo.
- The editor has a Library (search, sort, filters, grid/list, multi-select trash, disk
  usage, clean-up of raw files of exported recordings) and a Settings view (Capture,
  Audio, Camera, Look, Auto-zoom, Privacy, Export, Sharing, Storage) with a live preview.
- Settings live in `~/.config/dolly/config.json` (versioned, validated, atomic writes)
  and are mirrored both ways with the bar widget's options.

### Export
- Export workers are fresh processes instead of forks of a threaded parent; the harness
  export that hung at "rendering 0/1789" now runs at about 45 fps.
- Chapter titles (shown for 2.8 s of the finished video, also inside sped-up parts); "Use my Settings look" in the editor; size cap, an extra 9:16 copy and
  "keep raw files" from Settings.

## 0.3.0
Area-of-interest camera, sync markers, faster export, crash safety, window mode,
scenario suite.

## 0.2.0
Share sheet, links from your own storage, instant replay, captions, secret scrub.

## 0.1.0
First version: capture, auto-zoom, editor, export.

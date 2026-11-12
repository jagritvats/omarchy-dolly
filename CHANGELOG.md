# Changelog

## 0.7.1 (2026-11-12)

A recorder that leaves more of the machine to the desktop you are working on, and small
things. Tested on Omarchy 4.5 in VirtualBox (wf-recorder, software rendering); not yet on
real hardware.

Lighter recording (software recorder)
- Where the software recorder is used (virtual machines, machines without a working GPU
  recorder) it now encodes lightly, so more of the machine is left for the desktop and the
  pointer while a take runs. Measured in a 4-CPU virtual machine: the old encoding kept a
  whole CPU busy and at times froze the desktop for a quarter of a second or more; the
  light one takes about two thirds of a CPU, showed no such freezes, and more of what
  happens reaches the file. Raw files are 9 to 13 % larger; exports are unchanged. The GPU
  recorder is not affected.
- Settings > Capture > "Keep the desktop responsive while recording" (on by default) turns
  it off, `DOLLY_WF_PRESET` names a preset, and `dolly doctor` says which is in use.
- Stop returns about half a second sooner with the software recorder (3.1 s where it was
  3.7 s for a short take in a virtual machine).

Small things
- The countdown shows on Omarchy's own on-screen display, where volume and brightness do,
  instead of a notification card every second (cards remain where the shell has no
  on-screen display, or with `DOLLY_COUNTDOWN=cards`).
- Dolly's problem cards ("Nothing was recorded", "Recording stopped: ...") no longer stay
  on screen into the next take; they are dismissed when you press Record, like "Demo
  recorded", without making Record slower.
- Shortcut keycaps pressed close together are all readable: a chord right after Enter
  used to show only while it was fading out. Each keycap now gets at least 0.6 s, and
  the next one waits at most 0.35 s for it.
- The panel's hotkey-conflict card has a **Shortcuts** button that opens Settings at the
  Shortcuts group (`dolly settings --section shortcuts`).
- `omarchy-shell io.github.jagritvats.dolly open` opens the panel on the bar in front, not
  always on the first monitor.
- `dolly doctor` says so when `python-opencv` is still installed from before 0.7.0 and
  how to remove it. Nothing is removed for you.
- README: Super+Ctrl+1 to 9 toggles the panel from the keyboard.

## 0.7.0 (2026-11-11)

Faster, lighter and closer to Omarchy. Tested on Omarchy 4.5 in VirtualBox (wf-recorder,
software rendering); not yet on real hardware.

Stop answers at once
- The "Demo recorded" card no longer waits for the whole recording to be read. It comes
  about 3 s after Stop whatever the length of the take (measured in a VM: 8.5 s before
  for a 10 s take, 21 s for a 45 s take), and most of what is left is the recorder
  shutting down. The sync markers are looked for only at the start and the end of the
  file, and the automatic zooms are worked out afterwards, in the background.
- Stopping on a completely still screen no longer hangs for ten seconds: the software
  recorder only noticed the stop when the screen next repainted, so Dolly now repaints
  one invisible corner.
- The editor opens straight away: it no longer waits for its preview or for the zoom
  plan. Both arrive in the open window ("preparing the preview", "planning zooms").
- Export, share, `dolly run` and the JSON commands wait for the zoom plan, so nothing is
  ever exported or reported half-analysed. The plan is the same as before.
- An analysis cut short by a crash or a logout is finished when the recording is next
  opened or exported, or by `dolly recover`.

Leaves the desktop as it found it
- Do Not Disturb is always switched back off after a take. If the shell answered late
  when Dolly turned it on, Dolly treated it as untouched and left it on.
- No screensaver or lock in the middle of a take: Dolly switches Omarchy's Stay Awake on
  for the length of the recording and off again afterwards (the mug in the bar shows it).
  If you had Stay Awake on yourself, it is left alone. `DOLLY_STAY_AWAKE=0` turns this off.
- If the recorder, or Dolly itself, is killed, the desktop is still put back a few
  seconds later and the take is saved. Anything that could not be undone then is undone
  by the next recording or by `dolly recover`, and `dolly doctor` lists it.

Name
- The plugin is now listed as "Dolly: Screen Recorder", with a description that says what
  it is, so a search for a screen recorder finds it. The id, the `dolly` command and
  the bar button are unchanged.

The bar and the menu
- No polling. The bar button watches Dolly's small state files instead of asking
  `dolly status` every few seconds: nothing of Dolly runs while you are not recording
  (it was 8 to 12 starts a minute, and 42 to 62 while an export ran), and the button
  follows a start, a stop or the countdown at once.
- The panel fills from one command when it opens (it started six).
- The bar button answers a press of Record at once, before the countdown or the first frame.
- One **Demo** entry under Trigger > Capture, with recording, recordings, share,
  instant replay and settings inside it, instead of five rows next to Omarchy's own and
  another under Share. While a take is running the entry is "Stop demo".
- Shortcuts are settings: change any hotkey or switch it off in Settings > Shortcuts or
  with `dolly config set shortcuts.toggle="SUPER + CTRL + D"` (empty = off). Only Dolly's
  marked block in `~/.config/hypr/bindings.lua` is rewritten, without a terminal or a
  password; keys that belong to something else are never taken, and the row says so.
- An update applies itself. After `omarchy plugin update`, Dolly refreshes what Set up
  wrote (menu entry, hotkeys, the `dolly` command) by itself, only for someone who ran
  Set up and did not undo it. `dolly update` does both steps and says what changed.
  A running Omarchy shell keeps the bar button it has loaded: `dolly update` restarts
  the shell when the button changed; after Omarchy's own command a card offers to.
- `dolly integration apply` / `undo` do what `dolly-setup` does to your files, without
  the package step.

Smaller install
- Dolly no longer needs OpenCV. Set up now installs `python-numpy` and `python-pillow`
  only: about 9 MB to download and 55 MB on disk, where it was about 80 MB and 490 MB
  (most of that was what the distribution's OpenCV package depends on).
- Every feature is kept, and exports look the same. Against 0.6.1, on three exports in a
  virtual machine: frames score SSIM 0.994 to 0.999 on average (0.988 for the worst
  frame), files are 0 to 4 % larger, and CPU time is the same within measurement noise
  (-3 to +6 %).
- Nothing to do when you update. If nothing else on your machine uses it, the old package
  can go: `sudo pacman -Rs python-opencv` also removes what was installed only for it
  (vtk, openmpi and a few small libraries; about 430 MB in all). pacman lists them before
  it asks. `opencv` itself stays where another program needs it (kdenlive does).
- `dolly export --json` reports `"engine": "compositor"` (it said `"opencv"`).

For developers
- `dolly/imgops.py` holds the image operations, on NumPy and Pillow. Tests run with OpenCV
  hidden (`DOLLY_TEST_NO_CV2=1`, the default) and CI no longer installs it.
- `harness/bench/` compares two checkouts of Dolly: still frames, zoom plan, export time.

Several monitors
- Checked with a second monitor right of, left of and above the first one, at another
  size and at scale 1.5 and 2: the picture, clicks, typing zoom, 9:16 and 1:1, privacy
  boxes and the sync marker are all measured from the monitor being recorded.
- `dolly run` takes its clean stage on the monitor you are on. It could move the take to
  the other monitor when that one was showing an empty workspace. Afterwards it goes back
  to your monitor first, then your workspace (it could open your workspace on the other
  monitor).
- Stop on a still second monitor is as quick as on the first: the corner that is
  repainted to wake the software recorder is now on the monitor being recorded.
- Unplugging the monitor being recorded ends the take in about 4 s (it took 13). The take
  is kept and says the monitor was disconnected.
- `--json`: a region across two monitors, or a window hanging over a monitor edge, is
  recorded as the part on one monitor, and `warnings` now says so.

Camera
- Window mode: typing in the followed window gets its close-up. A shot was sized to show
  the whole window, so a shot planned at 2.0x played at about 1.0x (since 0.5.0, on any
  number of monitors).
- A notification card sliding in or fading out over the window you are typing in no
  longer pulls the camera to it.

## 0.6.1 (2026-11-11)

Fixes, measured on real takes on Omarchy 4.5 in VirtualBox.

Countdown and starting
- The countdown no longer eats the start of a take: recording and input logging begin
  before it and the clip starts the instant it ends. A 3 s countdown takes 3 s (it took
  6.7 s, and keys pressed in the gap were lost).
- Takes start about 3 s sooner on wf-recorder (virtual machines); `dolly record --for N`
  returns that much earlier.

Camera
- Auto-zoom gets close when the action is inside a floating or small window: typing is no
  longer framed at the size of its window (1.3x became 2.0x on the test scene), and a
  click far from the others gets its own shot.
- A shot no longer plays wider than planned because of what the previous shot left on
  screen.
- 9:16 and 1:1 fill the frame: a window is no longer shown letterboxed with large bands;
  the crop stays with the pointer, the start of the lines or the nearest part of the window.
- A dialog opened by a click stays whole in the vertical crop, button included, even when
  it is low-contrast.

Commands and notifications
- `dolly status`, `mark`, `pause`, `mute`, `cancel` and `replay start/stop` say what
  happened, or why not.
- `dolly stop --json` during a countdown reports the take as cancelled.
- After a recorder crash the notification shows the real saved length instead of 0:00,
  and the saved file is repaired.
- A notification on screen when you press Record is blurred from the first frame.
- `dolly record --fps` takes 30 or 60, like Settings.
- An editor window left over from an earlier session says so and closes.

Docs
- The README understated the packages Set up installs: about 80 MB to download and
  490 MB on disk, not 45 MB. Most of it is what the distribution's OpenCV package
  depends on; removing that dependency is planned.

## 0.6.0 (2026-11-10)

Tested on Omarchy 4.5 in VirtualBox (wf-recorder, software rendering).

For agents and scripts
- `dolly record --window SELECTOR | --monitor NAME | --region X,Y,WxH`, `--for SECONDS`,
  `--wait`, `--no-editor`, `--json`; `dolly stop --json`; `dolly export --json`. No picker,
  no countdown, one JSON object on stdout (errors too, with exit code 1), and it works
  from a shell without session variables. See "For agents and scripts" in the README.
- Starting a take right after another one no longer shows the last take's "Demo
  recorded" card in the new take.
- The start marker no longer fades out, so a take of a still screen keeps its first
  seconds (`--for 5` on a still desktop kept 3.2 s; now 5.2 s).

Privacy by default
- Password managers, keyring and authentication prompts and notification toasts are
  blurred automatically. Each one is a normal item in the editor's privacy lane, marked
  "auto"; removing it is final. Settings > Privacy > "Blur secret windows and
  notifications" turns it off.
- Keys typed into such a window are never logged: no keycaps, no typing zoom, nothing in
  `events.json`.

Scripted demos
- `dolly run demo.toml` performs a storyboard of steps (open, focus,
  type, key, wait, move, click, zoom, say, close), records it and exports it. Typing has
  a human rhythm and is repeatable (seeded); `--dry-run` prints the timed plan and every
  command without touching anything; `--json` for agents. A failed step, the time limit
  or Ctrl+C stops the recording and keeps the take. Uses only what Omarchy ships (wtype,
  Hyprland, grim). Reference: docs/storyboard.md.
- While a storyboard runs, key repeat is switched off and put back afterwards, so a
  stalled machine cannot repeat a typed key.

First run and bug reports
- **Set up** in the panel now finishes everything in one terminal: hotkeys, menu rows
  and the packages Dolly needs, with one password prompt.
- Set up also puts `dolly` on your PATH (a link in `~/.local/bin`, removed by
  `dolly-setup --undo`), so terminals, scripts and agents can call it by name.
- `dolly doctor --report` writes one text file to attach to a bug report: checks,
  versions, settings and logs. No video, no screenshots, no typed keys.
- Issue templates for bugs and ideas; tests run on every push.

Auto-zoom
- A click that only brought a window forward before typing somewhere else no longer
  pulls the zoom toward the pointer.

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

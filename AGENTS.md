# Dolly: notes for contributors and coding agents

The working brief for anyone, a person or a coding agent, who picks Dolly up on an
**Omarchy** machine (a VM or real hardware). Much of it was first built and tested on
a machine with **no Wayland/Hyprland, no GPU, no webcam/mic and no real network
uploads**, so the parts that need real hardware still need running: install it, run
them, collect evidence, fix what breaks, and report. Start with "First 15 minutes".

## What this is

Dolly is an Omarchy 4.x shell plugin plus a Python tool that makes polished
demo recordings. Round 4 (0.4.0) added, on top of what is listed below: exact
fractional-scaling and multi-monitor targets (`dolly/monitors.py`), separate
mic/system audio tracks (`dolly/audio.py`), a camera bubble (`dolly/camera.py`),
a recordings library (`dolly/library.py`), Settings (`dolly/config.py`,
`~/.config/dolly/config.json`), theme-following UI with each recording pinned to
its theme, coexistence with Omarchy's own recorder, and export workers that are
fresh processes (`dolly/render_chunk.py`). See CHANGELOG.md and "Round 4" below.

1. **Capture** (`dolly/capture.py`): screen, window or region at constant
   frame rate, hardware cursor hidden when the backend allows, into a
   crash-safe `raw.mkv`. Optional mic, desktop audio and webcam (v4l2,
   recorded alongside). Pause/resume works on the gpu-screen-recorder path. A
   "zoom here" marker hotkey is available. Window mode records the monitor and
   follows the picked window (moves, resizes, closes).
   Round 3 system behaviour (`dolly/health.py`, `dolly/solid.py`, `dolly/guards.py`):
   - disk check before (2 GB) and during (stops cleanly at 300 MB)
   - Do Not Disturb on while recording (restored)
   - solid windows while recording when Omarchy **Glassy** is on (per window, restored)
   - phases idle/countdown/recording/paused/finalizing/rendering shared by bar, panel and editor
   - one hotkey starts, cancels the countdown, and stops
   - the watchdog stops cleanly on recorder death, low disk, suspend/lock, monitor unplug, window close or logout
   - `dolly recover` handles crashes
   - the editor opens without stealing focus
   - errors point at `dolly doctor`
2. **Instant replay** (`dolly/replay.py`): gpu-screen-recorder keeps the last
   N seconds in RAM, and a rolling event log runs in the logger's memory.
   "Save last 30 s" turns that into a normal project with auto-zoom.
3. **Event logging** (`dolly/logger.py`): cursor via the Hyprland IPC socket
   (125 Hz); clicks and keys via a Hyprland Lua bridge (`hypr/dolly-input.lua`),
   falling back to `/dev/input` (input group), then to none (dwell inference).
   Window state: visible clients polled at 10 Hz plus workspace, special
   workspace, layer (menus, toasts) and monitor events. **Sync markers**
   (`dolly/sync.py`): the bar widget flashes a 40 px magenta/green square for
   about 70 ms at the region's top-left at start and stop. Finalize finds it,
   fits offset + clock drift, and trims it out.
4. **Auto-zoom** (`dolly/zoom.py`, `dolly/aoi.py`): the **area of interest**
   (AOI) is built from frame-difference activity, Hyprland window and layer
   state, typing, and click reactions (dialogs and menus a click opened). It
   ignores the bar clock, spinners, toasts, caret blink and other windows.
   - Typing frames the line from the prompt to the caret, even with the mouse parked elsewhere.
   - Scrolling output frames the whole window.
   - Window open/resize/drag frames the window.
   - Workspace switches **cut**.

   Shots are planned once per segment, with typed text kept at ≥ 15 px at
   1080p. Springs follow the subject with a dead zone, and segments merge or
   pull back as before. `zoom_config.use_aoi=false` gives the old
   cursor-only engine.

   0.4.1 **glide** (`zoom.glides`, `camera_path`): consecutive shots whose
   actions are ≤ `stay_gap` s apart (default 6), within `pan_reach` viewports
   (1.3) and with the pointer not wandering off are one zoomed shot. In the
   pause the camera holds and follows the pointer (aimed 2/omega ahead), then
   moves the least amount needed to show the next action, at most
   `glide_speed` viewports/s. Hops over 0.55 viewport breathe out to ≥ 0.62×
   the zoom. `glide=false` is the round-3 behaviour ("zoom out between").
   Across windows (`_shot_window`, `_relayout_between`): a surface opened
   after the previous action (dialog, menu, new popup) glides; another app
   window that already existed glides only within `switch_reach` (0.6
   viewport) and half of `stay_gap`; a tiled window open/close/fullscreen or a
   floating surface over 25 % of the screen (scratchpad) in the pause pulls
   back. Off-monitor pointer trips up to 1.5 s are ignored. A move/resize of
   the shot's window (or the window-mode target) in the pause pulls back
   (`_moved_between`). Gaps under `bridge_gap` keep the round-3 camera
   (head for the next shot early); only longer glides hold the current one.
   Settings › Auto-zoom and the editor sidebar expose enabled, level,
   triggers (`zoom_clicks`, `follow_typing`, `zoom_windows`), glide,
   `stay_gap` and `motion` (calm 0.75×, normal, quick 1.3× spring speed).
5. **Render** (`dolly/render.py`), CPU only, on NumPy + Pillow (the image operations live in `dolly/imgops.py`; no OpenCV):
   - background, padding, rounded corners, shadow
   - re-drawn cursor (optional motion blur), click ripples, shortcut keycaps
   - speed ramps with a "⏩ 4×" badge (`dolly/timemap.py`)
   - vertical 9:16 / square 1:1 auto-reframe (`dolly/reframe.py`): frames the whole AOI, widening to a letterboxed view when needed (edge springs, jump cuts for big subject switches)
   - cursor: theme size, hidden while typing and off-monitor, I-beam over terminals, grab while dragging
   - parallel chunked export (fresh worker processes, `dolly/render_chunk.py`; concat without re-encode), reuse of unchanged frames, `--quality draft`
   - privacy blur boxes and OCR secret scrub (`dolly/privacy.py`)
   - burned-in captions (`dolly/captions.py`), spotlight, intro/outro title cards
   - webcam bubble that moves away from the cursor
   - mic noise suppression and loudness normalisation
   - output is CFR H.264 via libx264, or GIF
6. **Share** (`dolly/share/`):
   - platform presets with size estimates; two-pass size-capped encoding verified after encode
   - copy the file to the clipboard (wl-copy, `text/uri-list`)
   - "get a link" from the user's **own** storage: S3-compatible (AWS/R2/B2/MinIO) via built-in SigV4, or rclone remotes (Drive/Dropbox/OneDrive via `rclone link`)
   - an opt-in **public** temp host (Litterbox)
   - share history with "delete remote copy"
7. **Editor** (`dolly/editor/`), a local web UI (127.0.0.1, random port, token):
   - timeline lanes for zoom / speed / privacy / clip, with drag, resize, add, split, delete and undo
   - 16:9 / 9:16 / 1:1 preview; draw blur boxes on the preview; "Scrub secrets"; "Auto-detect idle"
   - transcribe; effects, title card and style presets
   - **Share sheet** (presets, estimates, export, Copy file / Copy as GIF / Copy path, Get link, history)
8. **Omarchy plugin** (`manifest.json`, `BarWidget.qml`, `RecordPanel.qml`):
   - bar button: idle 󰑊; countdown 3-2-1 (click cancels); live timer while recording; 󰏤 paused; `󰑊 …` finalizing; `󰑊 42%` rendering; 󰑓 replay armed
   - the `syncFlash` overlay (Overlay layer surface `dolly-sync`, click-through)
   - setting "Solid windows while recording" (auto/on/off)
   - "Record demo" popover with an instant-replay section
   - `bin/dolly-setup` adds menu rows (Trigger > Capture, Trigger > Share) and hotkeys
   - menu `when:` guards are pure shell file tests (no Python): `$XDG_RUNTIME_DIR/dolly/{recording,replay}.pid`, `~/.local/state/dolly/{last,share-ready}`

Recordings: `~/Videos/Dolly/<timestamp>/`, containing:
- `raw.mkv` (older recordings: `raw.mp4`), `webcam.mkv`, `aoi-activity-v8.pkl` (analysis cache)
- `events.raw.jsonl`, `events.json`, `capture.json`, `project.json`
- `shares.json`, `captions.srt`, `recorder.log`
- `exports/`

## Recording what you just built (`dolly run`, for coding agents)

When a change is visible on screen, attach a clip to the pull request (Omarchy's PR
template asks for visual evidence). Do not record by hand: write a storyboard and let
Dolly perform it. Reference: `docs/storyboard.md`; working example:
`examples/terminal-demo.toml`.

1. `dolly run --check` once: it says what this machine can perform (stock Omarchy:
   everything).
2. Write `demo.toml` next to the change. Keep it under 30 seconds: one idea, three to
   six steps. Put slow or noisy preparation (builds, `cd`, `clear`) in `[[setup]]`,
   which is not recorded. After a command that prints, use `wait = "quiet"`, not a
   guessed number of seconds. Use `say` for one short caption per idea.
3. `dolly run demo.toml --dry-run` and read the plan: every command it would run is
   listed. Fix validation errors (they name the line and the step).
4. `dolly run demo.toml --json` and read the result: `ok`, `outputs[].file`,
   `error.where` / `error.why`. On a failure the take up to that step is kept in
   `project`; fix the storyboard and run it again.
5. Look at the export before attaching it (`ffmpeg -i out.mp4 -vf fps=1 f-%02d.png`):
   is the text readable, does the zoom sit on what was typed.
6. Commit `demo.toml` with the change, so the clip can be re-recorded later.

```toml
title  = "New --json flag"
preset = "github"                 # 720p, under GitHub's 10 MB limit
record = "window:term"

[[setup]]
open = "terminal"
as   = "term"
cwd  = "~/dev/project"

[[step]]
say = "The new flag prints machine-readable output"

[[step]]
type  = "mytool status --json"
enter = true

[[step]]
wait = "quiet"

[[cleanup]]
close = "term"
```

A storyboard types into the live desktop: never put secrets in one, and do not run one
you did not write without reading its `--dry-run` plan. Modules: `dolly/storyboard.py`
(parse, validate, plan; runs nothing), `dolly/inject.py` (wtype for keys, Hyprland for pointer, clicks and windows),
`dolly/script.py` (the runner). Tests: `tests/test_storyboard.py`, `tests/test_script_run.py`.

## Layout

```
manifest.json  BarWidget.qml  RecordPanel.qml   Omarchy plugin (id io.github.jagritvats.dolly)
bin/dolly  bin/dolly-setup                      CLI launcher, integration (menu rows + hotkeys, --undo)
hypr/dolly-input.lua                            Lua click/key bridge, loaded only while recording
dolly/  cli capture logger replay events zoom timemap reframe render privacy captions project doctor demo keymap
        aoi (area of interest) sync (markers) health (disk/DND/phases/jobs/focus/other recorders) solid (Glassy)
        guards (menu files) monitors (scale/targets) audio (tracks/mix/meter) camera (bubble) library config i18n
        render_chunk (export worker process) imgops (resize, place, blur, blend, masks: NumPy + Pillow)
i18n/en.json                                    strings for the panel (Commons.Translations) and notifications
dolly/share/  presets encode clipboard config targets  (+ __init__: flow + history)
dolly/editor/server.py + static/index.html
harness/  make_demo.py desktop.html             synthetic recording generator (headless Chromium)
harness/  scene.py scenarios.py metrics.py run_scenarios.py   scenario suite (`make scenarios`)
harness/bench/                                  compare two checkouts: stills, zoom plan, timed export (README.md there)
harness/  dctl wt.py walkthrough.py chrome-wayland  TEST HARNESS ONLY: drive the Omarchy shell harness
tests/  test_zoom test_pipeline test_capture_fallback test_share test_features test_aoi test_system
        test_guards test_scaling test_settings test_library test_av editor_smoke pathcheck
        test_imgops (against frozen OpenCV results) test_no_opencv (cv2 is hidden in every test run)
```

## For agents and scripts (0.6 prototype: `dolly/headless.py`)

To record a demo of your own work from a tool call (same recipe as the README):

```bash
# record a named window for 12 seconds; no click, no countdown, no editor
dolly record --window kitty --for 12 --json
# {"ok": true, "recording": false, "dir": "/home/me/Videos/Dolly/2026-11-10_21-24-56",
#  "raw": ".../raw.mkv", "duration": 12.4, "trim": [0.0, 12.4], "zooms": 2, "blurs": 0, ...}

# or: start, run the thing you want to show, stop
dolly record --window class:chromium --json      # returns once it is recording
./run-my-demo.sh
dolly stop --json                                # the same object as above

# export; "dir" comes from the object above (no dir: the latest recording)
dolly export "$DIR" --json --width 1280
# {"ok": true, "output": ".../2026-11-10_21-24-56.mp4", "bytes": 1070286, "duration": 12.6,
#  "width": 1280, "height": 720, "aspect": "16:9", "format": "mp4"}
```

- Target: `--window SELECTOR` (a class, part of a title, `class:NAME`, `title:TEXT`,
  `address:0x...` or `active`; of several matches the most recently focused wins),
  `--monitor NAME`, or `--region X,Y,WxH` in logical pixels.
- `--for SECONDS` stops and finalizes by itself; `--wait` blocks until someone else stops
  it; with neither, `record` returns as soon as recording has started.
- `--json` prints exactly one JSON object on stdout and never opens the editor
  (`--no-editor` does only the latter).
- It works from a shell with no session variables (ssh, a systemd unit, an agent's tool
  call), as the logged-in user: the Hyprland session is found by itself.
- Errors are the same shape with exit code 1:
  `{"ok": false, "error": "window-not-found", "message": "...", "windows": [...]}`.
  Codes: `window-not-found`, `monitor-not-found`, `bad-region`, `needs-target`,
  `already-recording`, `other-recorder`, `not-recording`, `missing-packages`,
  `folder-not-writable`, `disk-full`, `no-compositor`, `export-failed`.
- Automatic zooms, cursor smoothing and the automatic privacy blurs apply as in a normal
  recording; `dolly edit DIR` opens it in the editor later.
- Code: `dolly/headless.py` (targets, JSON, `--for`), tests in `tests/test_headless.py`.
  `capture.start`/`stop` are unchanged; headless only names the target and waits.

## Hidden stage (0.9.9 beta: `dolly/stage.py`)

A second Hyprland inside the session, with its window output off and a headless output
`STAGE` of its own. `stage.enter()` points a process at it (`DOLLY_STAGE=1`,
`WAYLAND_DISPLAY`, `HYPRLAND_INSTANCE_SIGNATURE`); from then on `logger.on_stage()` is
true and:
- `logger.runtime_dir()` is `$XDG_RUNTIME_DIR/dolly/stage/rt` (state per desktop: the bar
  watches the real desktop's folder only);
- `capture.notify`, the shell calls, Do Not Disturb and Stay Awake do nothing; the
  recorder is always wf-recorder, wrapped in a memory-limited scope;
- the sync marker is the stage's pointer in the top-left corner (`stage.pointer_mark`,
  `sync.detect_pointer_marks`, `fit_pointer`, `pointer_trim`). The start mark is shown
  0.3 s and goes back; the stop mark stays (`stay=True`) and `stage.pointer_home()` takes
  the pointer back after the recorder has gone. `capture.stop` leaves the stage still
  until the recorder is idle (`stage.recorder_drained`, `drain_limit`): wf-recorder drops
  its unencoded frames at the stop signal;
- keys: Hyprland reports a typing tool's keys as codes that say nothing (`wtype` brings
  its own keymap), so on a stage the bridge's keys are logged without a name
  (`keymap.UNNAMED`) unless a `dolly stage type|key` holds them (`inject.Feed.hold`) and
  reports them by name through `script-input.jsonl`, which a stage take always follows;
- a stage take records no microphone and no camera unless asked
  (`cli.apply_record_defaults`); "system audio" is the stage's sink
  (`stage.system_audio_device`), which the stage's programs get through `PULSE_SINK` and
  `PIPEWIRE_NODE`;
- the desktop picture is a generated Quickshell program on the stage's background layer
  (`stage.wallpaper_start`), the theme colour when anything about it fails;
- `inject.Desktop.launch` runs commands through `stage.wrap_command` (no `uwsm-app`
  scope, a private browser profile).
`headless.adopt_session_env` never returns a stage (`stage.real_session`). Cleanup:
`stage.stop`, `stage.sweep` (dead stage, failed start, killed stage), the owner watcher
(`python -m dolly.stage reap PID`). `stage.selftest()` is `dolly doctor --stage`: every
step reported on its own. Tests: `tests/test_stage.py` (fakes of Hyprland, hyprctl,
systemd-run, pactl, grim, wf-recorder, wtype, quickshell). Timing check on a real take:
`python3 harness/stage_timing.py <take>`. For someone with real hardware:
`docs/stage-testing.md`. Only ever run in a VirtualBox VM.

## Privacy by default (0.6 prototype: `dolly/sensitive.py`, `privacy.auto_boxes`)

- At finalize, every password manager, keyring or authentication prompt window
  (`sensitive.SECRET_CLASSES`/`SECRET_TITLES`) and every stretch with notification toasts
  on screen becomes an ordinary privacy box with `"source": "auto"`. Computed once for a
  new project; deleting one in the editor is final. Setting: `privacy.auto` (default on).
- Toasts: Hyprland only sees the shell's notification layer, which stays mapped, so the
  logger polls `omarchy-shell notifications count` and the box is sized from the count.
- Keys typed while such a window has focus are not written by the logger, and
  `strip_secret_keys` drops any that slipped through before `events.json` is written.
- Tests: `tests/test_privacy_auto.py` (synthetic timelines).

## Prerequisites (`bin/dolly doctor` checks all of these)

| Need | Package | Notes |
|---|---|---|
| Omarchy 4.x (Quattro shell) | — | built against omarchy `quattro` branch, Nov 2026 |
| Hyprland Lua config + `hyprctl eval` | ships with Omarchy 4 | click/key logging without the input group |
| ffmpeg with libx264 | `ffmpeg` | export, probing, audio chain (afftdn, loudnorm) |
| gpu-screen-recorder | ships with Omarchy | capture, **pause, instant replay** |
| wf-recorder | `sudo pacman -S wf-recorder` | **install in a VM**: software fallback (no pause/replay, cursor baked in) |
| gdbus (glib2) | ships with Omarchy | stop cleanly on suspend/lock (login1 PrepareForSleep) |
| Dolly bar widget enabled | `omarchy plugin add ... --enable` | sync marker overlay + phases; without it events are timed from the logger start |
| Python 3.10+, numpy, pillow | `bin/dolly deps --install` (omarchy-pkg-add in a floating terminal) | not on a fresh Omarchy; or `bin/dolly-setup` offers a venv. No OpenCV: `dolly/imgops.py` does that work on NumPy + Pillow (tests run with `cv2` hidden) |
| wl-clipboard | ships with Omarchy | "Copy file", link to clipboard |
| tesseract + eng data | `sudo pacman -S tesseract tesseract-data-eng` | optional: "Scrub secrets" |
| rclone | `sudo pacman -S rclone` | optional: links via Drive/Dropbox/OneDrive/... |
| whisper.cpp (`whisper-cli` + ggml model) or `pip install faster-whisper` | AUR `whisper.cpp` | optional: captions; model at `~/.local/share/dolly/whisper/ggml-base.en.bin` or `$DOLLY_WHISPER_MODEL` |
| v4l2 webcam | — | optional; none in a VM is fine |
| slurp, jq, a Chromium browser | ship with Omarchy | region picking, editor window |
| For `make test`/`make demo`: playwright (+ moto, boto3 for share tests) | `pip install --user playwright "moto[server]" boto3` | tests skip what is missing |

`dolly doctor` prints the capture path (gsr / gsr-cpu / wf), the click/key path
(lua / evdev / none), replay and pause availability, the share targets
(default **local-only**), VM and software-rendering detection, and whether a
webcam and transcriber are present. `--json` gives the same as JSON.

## First 15 minutes (in order)

1. `cd` into this repo. `make test` should give 202 passed (about 5-6 min). Share
   and OCR tests skip when moto, rclone or tesseract are missing.
2. `CHROME=$(command -v chromium) make demo` (about 7 min on 2 cores,
   `make demo-fast` for 720p). It produces `demo-out/`:
   - `dolly-demo.mp4`, `-idle-cut`, `-secrets-blurred`, `-vertical`
   - `-github.mp4/.gif`, `-linkedin`, `-capped-1MB`
   - `before-after.mp4`, `software-path.mp4`, `dolly-demo-draft.mp4`
   - `frames/*.png`, `summary.json` (`render_main`/`render_draft`: export fps)

   Compare with "Reference results" below.
3. `make scenarios` (about 12 min) writes `scenarios-out/`:
   - `results.txt`, `results.json`: every metric for 12 scenarios × 3 aspects (incl. 1.6667x, 2x and a rotated monitor), before and after
   - `contact-sheet.png`
   - `clips/*-before-after.mp4`

   It exits non-zero if any area-of-interest metric misses its cap.
4. `bin/dolly doctor | tee $E/doctor.txt`.
5. Install (next section), then work through the test plan, the round-3
   hardware plan and TESTING.md.

## Install (from this local checkout)

```bash
cd /path/to/dolly && git status          # must be committed: the plugin is a clone of HEAD
make clean && omarchy plugin validate .  # scenarios-out/ holds symlinks the validator rejects
omarchy plugin add "file://$PWD" --enable --yes
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly-setup   # menu rows, hotkeys, dep check
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly doctor
```

Hotkeys added by `dolly-setup`. Each is skipped if already bound; Omarchy itself
uses Super+Ctrl+R (reminders) and Super+Ctrl+P (power), so Dolly uses Super+Alt:

| Hotkey | Action |
|---|---|
| Super+Alt+R | record / stop |
| Super+Alt+P | pause / resume |
| Super+Alt+M | "zoom here" marker |
| Super+Alt+N | mute / unmute the microphone track (a muted span, not a cut) |
| Super+Alt+L | recordings library |
| Super+Alt+Shift+R | save the last 30 s of instant replay |

dolly-setup also writes a window rule for the live camera bubble (`DollyCam-S|M|L`:
float, pin, no initial focus). The Omarchy menu is Super+Space: Trigger → Capture
(Record demo, Dolly recordings, Dolly settings, Mute microphone while recording,
Instant replay, Open last) and Trigger → Share → Last demo recording.
`omarchy menu keybindings --print` should list every Dolly binding with its description.

Iterate: commit, then `omarchy plugin update io.github.jagritvats.dolly --yes`. Shell logs:
`qs log -p "$OMARCHY_PATH/shell" --tail 100`.

## Share targets (optional; default is local-only)

Nothing ever uploads until a target exists in `~/.config/dolly/share.json`.
`dolly share init` writes a 0600 template whose examples are all disabled.
Credentials are read from rclone's config, env vars named in the target, an
AWS profile, or inline keys. Inline keys are refused unless the file is 0600.

```json
{"default": "r2", "targets": {
  "r2":     {"type": "s3", "endpoint": "https://ACCOUNT.r2.cloudflarestorage.com", "region": "auto", "bucket": "demos",
             "prefix": "dolly/", "public_base_url": "https://demos.example.dev",
             "access_key_env": "R2_ACCESS_KEY_ID", "secret_key_env": "R2_SECRET_ACCESS_KEY"},
  "b2":     {"type": "s3", "endpoint": "https://s3.us-west-004.backblazeb2.com", "region": "us-west-004", "bucket": "demos",
             "presign_days": 7, "access_key_env": "B2_KEY_ID", "secret_key_env": "B2_APP_KEY"},
  "s3":     {"type": "s3", "region": "eu-west-1", "bucket": "my-demos", "profile": "default"},
  "minio":  {"type": "s3", "endpoint": "http://127.0.0.1:9000", "region": "us-east-1", "bucket": "demos", "profile": "minio"},
  "gdrive": {"type": "rclone", "remote": "gdrive:Dolly", "link_expire": "7d"},
  "temp":   {"type": "litterbox", "time": "24h", "public": true}
}}
```

- S3 without `public_base_url` returns a presigned GET link. SigV4 caps these
  at 7 days.
- Object names get a random suffix so public buckets are not guessable.
- rclone remotes that cannot make links still upload, and the UI says so.
- Litterbox:
  - anonymous, public to anyone with the link, max 1 GB, 1h/12h/24h/72h, no delete
  - its FAQ bans malware, gore, full TV/anime episodes and some file types
  - needs `"public": true`

CLI: `dolly share list|export PRESET [--copy] [--upload [TARGET]]|copy FILE [--path]|upload FILE [--target T]|targets|init|history|delete ID`.

## Real-hardware / VM test plan

`E=~/dolly-evidence/$(date +%F_%H%M); mkdir -p $E`. Evidence bundle after any recording:

```bash
P=$(bin/dolly last); cp $P/{capture.json,events.json,events.raw.jsonl,project.json,recorder.log,shares.json} $E/ 2>/dev/null
ffprobe -v error -show_entries stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate:format=duration,start_time -of json $P/raw.mp4 > $E/raw-probe.json
for t in 1 3 5; do ffmpeg -v error -y -ss $t -i $P/raw.mp4 -frames:v 1 $E/raw-$t.png; done
```

| # | Action | Expected | Evidence |
|---|---|---|---|
| 1 | `bin/dolly doctor` / `--json` | Exit 0. HW: `capture gsr`; VM: `gsr-cpu` or `wf` + "running in a VM". `clicks_keys lua`. `share targets local-only` | doctor.txt/json |
| 2 | `omarchy plugin validate .` + `omarchy plugin add "file://$PWD" --enable --yes` | 󰑊 appears in the bar's right section | `omarchy plugin list --json > $E/plugins.json`, `grim $E/bar.png` |
| 3 | Lua bridge alone: `mkdir -p $XDG_RUNTIME_DIR/dolly; : > $XDG_RUNTIME_DIR/dolly/input.jsonl; hyprctl eval "dofile(\"$PWD/hypr/dolly-input.lua\")"`, click 3×, type "abc", `hyprctl eval 'if dolly_input_stop then dolly_input_stop() end'` | `bridge` line with clicks:3, keys:true; click lines; key lines; clicks still reach apps; after stop nothing more and no "dolly click observer" in `hyprctl binds` | bridge.jsonl, binds-after-stop.json |
| 4 | `bin/dolly record --mode screen`, click/type/Ctrl+S for ~15 s, `bin/dolly stop` | countdown, red bar timer, "Demo recorded" notification, editor opens; events.json has dense moves, clicks, keys | bundle, editor.png |
| 5 | Cursor hidden (gsr) | no system cursor in raw-*.png (wf: cursor present, `cursor.enabled=false`) | raw-*.png |
| 6 | CFR | r_frame_rate == avg_frame_rate for raw and exports | probes |
| 7 | Coordinate mapping at scale 1, a fractional scale and on a second monitor | ripples centred on what you clicked | screenshots, capture.json |
| 8 | Sync | clicks coincide with UI reactions; else measure the offset with the editor's Timing → Events slider and note it | note |
| 9 | Window / region modes | raw.mp4 = picked rect (even dims) | raw-probe.json |
| 10 | Bar widget: left click panel, right click quick record, while recording left click stops / right click pauses (timer freezes, 󰏤), middle click opens last | as described, no QML errors | `qs log ... > $E/qs.log` |
| 11 | **Pause**: Super+Alt+P twice during a recording | gsr pauses (video has no gap), events.raw.jsonl has pause/resume lines, finalized events have the paused span removed (clicks after resume line up) | events.raw.jsonl, events.json, 2-3 frames around the resume |
| 12 | **Mark zoom**: Super+Alt+M while hovering something (no click) | a `mark` event and a zoom segment with reason "marked" there | events.json, project.json |
| 13 | **Instant replay**: panel → "Keep the last minute in memory" (or `bin/dolly replay start`), work 40 s, Super+Alt+Shift+R | 󰑓 in bar; "Replay saved" notification; new `…-replay` project of ~30 s; events aligned (ripples on clicks); `bin/dolly replay stop` removes 󰑓 | replay project bundle, `recorder.log` in `$XDG_RUNTIME_DIR/dolly/replay/` |
| 14 | **Mic**: `bin/dolly record --mic`, speak with background noise, export | AAC track; voice in sync; noise reduced (A/B with editor "Mic noise suppression" off); loudness ~ -16 LUFS (`ffmpeg -i out.mp4 -af ebur128 -f null -`) | export probe, ebur128 summary |
| 15 | **Desktop audio** (`--desktop-audio`): not denoised (denoise only applies to mic recordings) | music unchanged | export |
| 16 | **Webcam**: `bin/dolly record --webcam` (real camera) | `webcam.mkv` recorded; `events.json` `capture.webcam_offset` within ±0.3 s; bubble bottom-left; moves to the other corner when the cursor goes there; clap test: lips/clap match audio | webcam probe (`start_time`), export frames |
| 17 | VM without webcam/mic | record with no flags works; doctor shows webcam warn only | doctor |
| 18 | Editor round-2: lanes (zoom/speed/privacy), "Auto-detect idle", draw a blur box on the preview, "Scrub secrets" on a recording with a fake token in a terminal, 9:16 and 1:1 preview, title card, style preset save/load | preview matches export | screenshots, project.json |
| 19 | **Share sheet → GitHub**: export, "Copy file", paste into a GitHub issue comment box in Chromium and Firefox; also Slack/Discord web upload field | file attaches (it is ≤ 10 MB); if paste does nothing in an app, note which (text/uri-list only) | screenshots |
| 20 | Share → Discord (20 MB), Shorts (9:16), LinkedIn (1:1) | sizes under caps; vertical crop keeps the action in frame | `ls -l exports`, frames |
| 21 | **Real uploads** (your own accounts): configure an R2 or S3 bucket and/or `rclone config` a Drive remote; "Get link" | link copied, opens in a private browser window (presigned/public), history entry; "delete remote" removes the object (link 404s / gone in Drive) | shares.json, browser screenshot |
| 22 | Litterbox only if you accept public hosting: enable `temp`, upload a throwaway clip | URL works without login, expires as configured; delete is refused | shares.json |
| 23 | Notification actions after export: Open / Copy file / Get link / Edit | each works from the Omarchy toast | screenshot |
| 24 | Captions (if whisper installed): `bin/dolly captions` on a mic recording | captions.srt, burned-in captions, `.srt` next to export | files |
| 25 | VM software path (`DOLLY_BACKEND=wf`) | record + export work; pause/replay report "needs gpu-screen-recorder" | recorder.log |
| 26 | Long recording (5+ min), cancel | export completes; cancel removes the folder | timings |

## Round 3: real-hardware plan per scenario

The synthetic suite (`make scenarios`) shows the camera logic works when the
inputs are right. These steps check that the inputs are right on a real
Omarchy desktop. Record each with `bin/dolly record --mode screen`
(`--countdown 0` is fine), then export:
- 16:9: `bin/dolly export --quality fast`
- 9:16: `bin/dolly export --aspect 9:16 --quality fast`

Keep the evidence bundle and the two exports.

Debug view of what Dolly thought mattered (red = what you should have seen
is your call):

```bash
python3 -c "import sys,json;sys.path.insert(0,'.');from dolly import aoi;from dolly.events import Timeline;p=sys.argv[1];tl=Timeline.load(p+'/events.json');a=aoi.build_aoi(tl,aoi.cached_activity(p,tl));json.dump(a.to_json(),open(p+'/aoi.json','w'))" "$(bin/dolly last)"
```

| # | Scenario | Do | Expected | Evidence |
|---|---|---|---|---|
| R1 | **Terminals Omarchy ships**: Alacritty (default), Ghostty, kitty, foot (`omarchy-install-terminal`) | each terminal: click into it, move the mouse to a far corner, type 3 commands, one with long output | 16:9 zooms on the command line (prompt to caret), not on the mouse; 9:16 keeps the line start; text ≥ 15 px at 1080p; I-beam cursor over the terminal; cursor hidden while typing (reappears on mouse move); `events.json` clients carry the terminal `class` (compare with `TERMINALS` in capture.py; add missing classes) | exports, events.json, aoi.json |
| R2 | **Build log** | `yes "$(printf %.0s- {1..100})" \| head -3000` or a real `npm run build` in a half-width terminal | whole window framed while it scrolls, no line-chasing; bar clock / btop / spinners elsewhere don't move the camera | exports |
| R3 | **Tiling** | Super+Return twice, close one with Super+W, drag a split border with the mouse | new window framed after its pop-in, the remaining one after close; no zoom on windows that only make room | exports |
| R4 | **Floating drag** | float a window (Super+T / Omarchy float), Super+drag it across, Super+right-drag resize | camera follows the window, no lag-out; grab cursor while dragging | exports |
| R5 | **Workspaces** | Super+2 / Super+1 (Omarchy: instant switch), then enable workspace animations temporarily and repeat; open the scratchpad (Super+S) | a cut at each switch (no whip-pan), zoomed-in shots resume on the new workspace; scratchpad framed as a window; `events.json` has `workspace` events | exports |
| R6 | **Interruptions** | while typing in a dialog: trigger a notification (`notify-send hi`), open the Omarchy menu (Super+Space) and pick something, right-click context menu | toasts never move the camera; menu framed while open (layer namespace logged as `keyboard: true` - if it shows `false`, add the namespace to `Logger.LAUNCHERS`); DND on during recording, restored after (`omarchy-shell notifications isDnd`) | events.raw.jsonl `layer` lines |
| R7 | **Multi-monitor, mixed fractional scales** | two monitors (e.g. 1.25 and 1.6667 or 1.5); record the 1.6667 one; move the pointer to the other monitor, click there, come back; unplug/replug the other monitor mid-recording | ripples land on what you clicked (logical->physical mapping); no zoom on clicks on the other monitor; cursor fades out while away; hotplug: no crash (`monitor` events logged); unplugging the **recorded** monitor stops cleanly with a notification | capture.json, exports |
| R8 | **Cursor theme size** | `hyprctl setcursor <theme> 32` (or HYPRCURSOR_SIZE), record | drawn cursor matches the real one's size × scale (capture.json `cursor_size`) | export frame vs screenshot |
| R9 | **XWayland** | an X11 app (e.g. `xeyes`, a JetBrains IDE, Steam) | clients have `xwayland: true`, geometry/zoom correct at fractional scale (XWayland scaling may blur; framing must still be right) | events.json |
| R10 | **Sync marker detection** | record 10 s and 5 min | magenta/green square visible in the first/last frames of `raw.mkv` (top-left of the region); `events.json capture.sync.method == "markers2"`, `drift_pct` small (< 0.1); clap/click test: ripple on the click frame | `ffmpeg -ss 0.3 -i raw.mkv -frames:v 1 m.png`, capture.sync |
| R11 | **Window recording mode** | `bin/dolly record --mode window`, pick a floating window, move it, resize it, cover it with another window, close it | export shows only that window as a card following it; covering shows the cover (expected); closing it stops the recording ("window was closed") and the export ends there | capture.json `target`, `stop_reason` |
| R12 | **Suspend / lock / logout** | record, then `systemctl suspend`; again with Super+Escape lock; again log out | each stops cleanly: notification after resume/login, project finalized (`stop_reason` suspend/lock), MKV plays; after a logout run `dolly recover` | capture.json |
| R13 | **Crash / power loss** | `kill -9` the recorder, then separately `kill -9` everything Dolly (`pkill -9 -f dolly; pkill -9 gpu-screen-recorder`) | first: watchdog stops cleanly ("recorder stopped unexpectedly"); second: next `dolly record` or `dolly recover` finalizes the partial `raw.mkv` | capture.json |
| R14 | **Disk full** | `DOLLY_STOP_FREE_MB=999999 bin/dolly record` (simulates low space) and `DOLLY_MIN_FREE_GB=99999 bin/dolly record` | first stops within 1 s with "disk almost full"; second refuses to start with a message naming `dolly doctor` | notifications |
| R15 | **Glassy** (omarchy #18186) | `omarchy-hyprland-window-transparency glassy`; record (bar setting "Solid windows" = auto) for 10 s, open a new window during it; stop. Repeat with setting off; and `kill -9` the recorder mid-way then `dolly recover` | windows solid in the recording (also the one opened mid-way), see-through again right after stop; `omarchy-hyprland-window-transparency` still prints `glassy`; after the crash + recover all windows see-through again; `hyprctl getprop address:0x... opaque` back to the original | screenshots before/during/after, `$XDG_RUNTIME_DIR/dolly/solid.json` absent after |
| R16 | **Hotkey flow + time to first frame** | Super+Alt+R, press again during the countdown, then Super+Alt+R and Super+Alt+R to stop | second press cancels (no folder left); recording starts; `capture.json ttff_ms` (recorder start -> first frame) and `hotkey_to_first_frame_ms` - note both; the editor opens **without taking focus** (keep typing in a terminal while stopping) | capture.json, note |
| R17 | **Theme change mid-session** | open the editor, switch theme (Super+Ctrl+Shift+Space) | editor colours and "Theme:" follow within ~4 s; bar widget restyles | screenshot |
| R18 | **Menu guards** | `time bash -c "<each when: from omarchy-menu.jsonc>"` and Omarchy's `test/shell.d/menu-guard-budget-test.sh` | each ≤ 10 ms, batch ≤ 200 ms; rows flip correctly while recording / replay armed / after deleting the last recording | timings |
| R19 | **Export speed** | `time bin/dolly export --quality high` and `--quality draft` on a 1-min recording | note fps (Reference results: 1080p60 high ~40 fps, draft ~46 fps on 2 vCPU); `DOLLY_WORKERS=1` for comparison | timings |
| R20 | **Exports from the editor** | with the pacman packages (not the venv: `DOLLY_VENV=/nonexistent`), export the same project 3x from the **editor** (Ctrl+E > Export) and once via `bin/dolly export` | every export finishes at roughly R19 speed with no "no progress" line on stderr (run `bin/dolly edit` from a terminal to see the editor's stderr) | timings, stderr |

## Fresh Omarchy 4.0.4 VM findings (Nov 10) and 0.4.2

On Omarchy 4.0.4 in VirtualBox (VMSVGA): gpu-screen-recorder 6.1 fails with
"unknown gpu vendor: VMware, Inc." / "failed to load opengl" for both `gsr` and
`gsr-cpu`, so a VM needs wf-recorder. A fresh install has no python-numpy/pillow (and no python-opencv, which Dolly needed until 0.7.0).
`omarchy bar get` and `omarchy-system` do not exist on 4.0.4 (`omarchy-bar set` is
best-effort in Dolly; Session Keeper falls back to uwsm/systemctl). Quickshell needs
`QT_QUICK_BACKEND=software` in `~/.config/uwsm/env` there.

| # | Check | Expected |
|---|---|---|
| F1 | `bin/dolly doctor` before installing anything | no traceback; FAIL rows for the python modules and "a capture backend"; "Missing packages" + `dolly deps --install` at the end |
| F2 | record from the bar or Super+Alt+R with packages missing | no picker/countdown/DND; one notification naming each package with an Install button; Install opens a floating terminal running `omarchy-pkg-add ...` then `dolly doctor` |
| F3 | `bin/dolly-setup \| tail` (piped) | prints the install command, no hidden prompt; record button on the bar afterwards |
| F4 | record button missing after `omarchy plugin add --enable` | `omarchy bar put io.github.jagritvats.dolly --section right` (or `omarchy plugin enable io.github.jagritvats.dolly --section right`) shows it; note which works on 4.0.4 |
| F5 | `journalctl --user -b \| grep "Local plugin changed"` after a few `dolly` commands | nothing; `~/.cache/dolly/pycache` exists; no `__pycache__` under `~/.config/omarchy/plugins/io.github.jagritvats.dolly` |
| F6 | record 10 s, then `jq .sync <project>/events.json` | `method` is `markers2` (both markers found) under `QT_QUICK_BACKEND=software`; events log has `flash_ms: 160` |
| F7 | `DOLLY_DIR=/tmp/d bin/dolly record` | "Recordings are going to memory" toast; recording works; doctor's disk row says tmpfs and minutes left |
| F8 | `bin/dolly doctor` on VirtualBox | "virtual GPU" row naming VMware SVGA (vmwgfx); camera bubble (live) starts without an mpv crash |

## Round 4: real-hardware plan (fractional scaling, theme, audio, camera, library)

Verified so far only in the Omarchy shell harness (headless sway, `fakehypr`,
llvmpipe; see "Reference results"). These steps need a real Hyprland session.
Keep the evidence bundle for each.

| # | Scenario | Do | Expected | Evidence |
|---|---|---|---|---|
| R21 | **Scale in hyprctl** | `hyprctl monitors -j \| jq '.[] \| {name,width,height,scale,transform,x,y}'` on a 1.6667 and a 1.3333 monitor | note how `scale` is printed (1.6666666 or 1.67). `python3 -c "import sys,json;sys.path.insert(0,'.');from dolly import monitors as M;[print(m['name'],M.effective_scale(m),M.logical_rect(m)) for m in json.load(sys.stdin)]" < <(hyprctl monitors -j)` gives whole logical sizes equal to `hyprctl clients`' maximised window sizes + gaps | monitors.json, the printout |
| R22 | **Region on the unfocused monitor** | focus monitor A, `bin/dolly record --mode region`, pick an area on monitor B (different scale); then a pick spanning A and B (70/30) | capture.json `monitor` = B and `target_info.scale` = B's scale; the spanning pick records the larger part with a "recording the part on …" toast; `raw.mkv` size = `target_info.physical` (even numbers) | capture.json, raw-probe.json |
| R23 | **Whole monitor via the picker** | `--mode region`, click on empty desktop so the picker snaps to the monitor (Omarchy prints `monitor:NAME`) | recorder.log shows `-w NAME -s 0x0` (gsr) or `-o NAME` (wf-recorder); video = physical mode size | recorder.log |
| R24 | **Rotated monitor** | `hyprctl keyword monitor NAME,preferred,auto,1.5,transform,1`, record it, click 4 corners | video is portrait; ripples on the clicked corners (± 1 px at 1080p) | export frames |
| R25 | **Click accuracy at 1, 1.25, 1.5, 1.6667, 2** | one 10 s screen recording per scale, click small targets (a menu item, a checkbox) | ripples centred on them in the 16:9 export (tests/test_scaling.py proves the maths on synthetic video) | frames |
| R26 | **Window dragged across monitors** | `--mode window`, drag the picked window to the other monitor and back within 1 s; again and leave it there | brief trip: kept, the card shows the part still on the monitor; leaving: stop toast "the recorded window moved to another monitor", export ends where it left | capture.json `stop_reason`, export |
| R27 | **XWayland at fractional scale** | an X11 app with `xwayland:force_zero_scaling` true (Omarchy default) and false | `events.json` clients rects match the window on both settings (`capture.json xwayland_zero_scaling`) | events.json |
| R28 | **Theme follow** | open the panel and the editor, `omarchy theme set catppuccin-latte`, then back | bar, panel and editor recolour within ~4 s, editor font = `omarchy font current`; an old recording re-exports with its pinned theme (Settings › Look › Follow theme on) | screenshots, export frames |
| R29 | **Coexistence with Omarchy's recorder** | start Omarchy's recorder (Alt+Print), then Super+Alt+R; separately start Dolly and press Omarchy's stop key / its bar indicator | first: Dolly refuses with "Another screen recording is running"; second: Dolly's recording stops, is saved as normal (`stop_reason stopped-outside`), editor opens, no error toast. | capture.json, toasts |
| R30 | **Audio, four modes, both backends** | Settings or panel: None / Mic / System / Mic + System; play music and speak; once with `DOLLY_BACKEND=wf-recorder` | `tracks/mic.mka` and/or `tracks/system.mka` present; export has both; meters move in the panel while open; voice and music in sync with the video (clap test); Super+Alt+N mid-way gives silence for that span only | tracks/, export, `ffmpeg -i out.mp4 -af ebur128 -f null -` |
| R31 | **Camera, bubble in edit** | panel: Bubble in edit, pick the camera, Start | preview bubble during 3-2-1, gone when recording starts, `webcam.mkv` recorded; editor shows the bubble; change shape/size, drag it, add a camera-only range; export matches; clap test for camera sync | webcam.log, capture.json `webcam_offset`, export |
| R32 | **Camera, live bubble** | panel: Live bubble, Start, Super+drag it, `dolly camera snap`, `dolly camera size` | round bubble (Hyprland `rounding`), theme-accent border, never takes focus, pinned on workspace switch; in the raw video exactly as on screen; no second bubble in the export | screenshots, raw frame |
| R33 | **Camera errors** | camera in use by another app; no camera; user not in `video` | clear toast (busy / not found / permission) and the recording continues without it | toast text |
| R34 | **Library and Settings** | panel Recent: rename, copy video (paste into a browser upload), show in Files (nautilus selects the file), trash + Undo; editor Library: search, filters, clean-up; Settings: change something in each group | everything as labelled; `omarchy bar get io.github.jagritvats.dolly mode` follows Settings › Capture › mode and `omarchy bar set io.github.jagritvats.dolly mode region` shows up in Settings | config.json, shell.json |
| R35 | **MP4 survival on kill** | `ffmpeg -version`; record with `DOLLY_CONTAINER=mp4`, `kill -9` the recorder | gpu-screen-recorder's man page says MP4s survive a kill with FFmpeg 8.1+; Dolly keeps MKV as the default either way. Note the version and whether `dolly recover` was needed | ffmpeg version, recover output |

## 0.4.1: auto-zoom glide and settings (real hardware)

| # | Scenario | Do | Expected | Evidence |
|---|---|---|---|---|
| R36 | **Down a panel or form** | record; open the Dolly panel (or Omarchy's Setup menu, or a long web form); click 5 options top to bottom with 3-5 s between clicks; then click something on the far side of the screen | 16:9 and 9:16: one zoomed shot that scrolls down with the clicks (no zoom out between them); a pull-back before the far click | export, `project.json` segments |
| R38 | **Across windows** | two tiled windows: click in one, then 3 s later in the other far down; click a button that opens a small GTK dialog and click inside it; Super+Return for a new terminal and type in it; Super+S scratchpad and type; with two monitors, flick the pointer to the other one for half a second between two nearby clicks | pull back for the app switch, the new tiled terminal and the scratchpad; glide into the dialog; the monitor flick does not break the glide (a 3 s stay does) | exports, project.json |
| R37 | **Auto-zoom settings** | panel toggle "Zoom in on the action" off, record clicks + typing; then Settings › Auto-zoom: typing only, Zoom out between, Calm, level 1.5; then in the editor sidebar flip Stay and glide / Zoom out on the same recording | off: no zooms except a Super+Alt+M marker; each setting is visible in the export; the editor re-plans in under ~2 s and keeps a manual zoom you added | config.json, project.json, exports |

## Smoothness checklist (watch every export; any "no" is a bug)

- [ ] The thing being typed, clicked, opened or dragged is fully in frame (16:9 and 9:16), including dialogs opened far from the pointer
- [ ] Typing with the mouse parked elsewhere: the camera never visits the mouse
- [ ] Typed text is readable on a phone in 9:16 (≥ 15 px line at 1080p)
- [ ] No hunting: the camera settles; no back-and-forth within a shot; no zoom pumping while a line grows
- [ ] Nearby clicks (a panel, a form): the camera stays zoomed and glides between them; no zoom out and back in
- [ ] Scrolling output: one calm wide shot of the window, not a chase
- [ ] Workspace switch: a cut (or calm zoom-out), never a fast pan across the old screen
- [ ] Toasts, the bar clock, spinners, other windows never pull the camera
- [ ] Cursor: right size, hidden while typing, I-beam over terminals, never drawn while on another monitor, never over the typed line
- [ ] Clicks: ripple on the exact frame the UI reacts (sync); no ripples for clicks on other monitors
- [ ] Window mode: only the window, following it smoothly; ends when it closes
- [ ] Glassy users: recordings show solid windows; their desktop is unchanged afterwards
- [ ] No sync marker visible in any export (first/last frames)
- [ ] Bar, panel and editor agree on the phase (countdown/recording/paused/finalizing/rendering)

## Rollback

```bash
bin/dolly cancel; bin/dolly replay stop; bin/dolly-setup --undo     # menu rows + hotkeys (backups *.dolly-bak)
python3 -c "import sys;sys.path.insert(0,'.');from dolly import solid;print(solid.restore())"   # windows forced solid (Glassy), if any
omarchy-shell notifications isDnd        # Dolly restores DND itself; check it is what you expect
hyprctl eval 'if dolly_input_stop then dolly_input_stop() end'
omarchy plugin remove io.github.jagritvats.dolly --yes
rm -rf ~/.local/share/dolly ${XDG_RUNTIME_DIR}/dolly ~/.local/state/dolly   # optional
# ~/.config/dolly/share.json (credentials!) and ~/Videos/Dolly stay; delete them yourself if wanted
```

## Known risks / likely breakpoints

1. **Lua bridge API** (`hl.bind` function + `non_consuming`/`ignore_mods`,
   `hl.on("input.keyboard.key")`, `sub:remove()`, `hl.unbind`) was verified
   against Hyprland source only. Falls back to evdev/none, which is visible in
   events.raw.jsonl.
2. **Time sync and pauses**: with the bar widget loaded, sync markers fix
   offset + drift (synthetic: < 0.5 frame). Without it, t=0 = logger start
   (expect 0-150 ms skew). Pause relies on gsr SIGUSR2 not leaving a gap;
   check test 11. A pause between the two markers breaks the drift fit's
   linearity (the pause is removed from events; gsr pauses video too, so it
   should hold, so verify with R10 plus a pause).
3. **Replay alignment** assumes gsr's saved clip ends at the signal time.
   SIGRTMIN+n numbering must match the installed gsr (`man gpu-screen-recorder`).
4. **Webcam sync** uses `-timestamps abs -copyts` into MKV and assumes v4l2
   timestamps are CLOCK_MONOTONIC. If `webcam_offset` looks wrong, the code
   falls back to launch time + 0.3 s; fix in `capture.webcam_offset`. `mjpeg`
   input format at 1280x720@30 is assumed.
5. **Clipboard paste of files** depends on the target app reading
   `text/uri-list`. Chromium/Electron and Firefox upload fields usually do;
   file managers want `x-special/gnome-copied-files`, which is not offered.
6. **Size estimates** are heuristic (labelled ≈) and become accurate once a
   master is cached. The cap is enforced by verifying the real file size.
7. **OCR secret scrub** is best-effort: it blurs conservatively (key + value,
   whole time span), but can miss low-contrast or tiny text. The editor lists
   every flagged box to review. Never rely on it alone for real credentials.
8. **QML** never ran in Quickshell: replay/pause states, the instant-replay
   section and Toggle/Button props are modelled on built-in plugins. Expect
   small fixes; check `qs log`.
9. **gpu-screen-recorder flags** (`-cursor no -fm cfr -k h264 -q ultra`,
   replay `-r N -replay-storage ram -o DIR`, `-encoder cpu`): verify with
   `gpu-screen-recorder --help` on the installed version.
10. **Render speed**: CPU. The 1080p60 bench went from 25 to 40 fps on 2 vCPU
    (parallel chunks + reuse); `--quality draft` gives a half-size 30 fps
    preview. Export workers are fresh processes (`dolly/render_chunk.py`), not
    forks: a fork of a process with live threads (the editor's HTTP server, a
    logging handler, a library's thread pool) can inherit a held lock.
    A watchdog still kills workers that land no frame for `DOLLY_STALL_S`
    (default 90 s) and finishes inline with a stderr note. Covered by
    `test_stalled_render_workers_fall_back_inline`; R20 checks it on the real
    package. If you ever see the "no progress" note, report it; `DOLLY_WORKERS=1`
    skips workers entirely.
12. **Area of interest** is tuned on a synthetic desktop. Real risks:
    - apps that animate constantly (video, games, live dashboards) read as "output" when focused
    - very low-contrast dialogs may not register as click reactions
    - the line height is estimated from activity, so check the legibility of 4K and very small fonts

    Escape hatches: edit segments in the editor, or set
    `zoom_config.use_aoi=false` for the old engine.
13. **Hyprland bits never run here**:
    - `hl.exec_cmd(..., {no_initial_focus=true})` via `hyprctl eval`, with fallbacks `dispatch exec [noinitialfocus]` and a focus hand-back
    - per-window `opaque` via `hl.dsp.window.set_prop`, restored with value `"unset"` (falls back to the prior value)
    - `getprop`
    - socket2 event names `workspacev2`, `activespecial`, `openlayer`, `closelayer`, `monitorremovedv2`
    - `hyprctl layers -j` fields
    - layer namespaces Omarchy uses for its menu (assumed to contain "omarchy"/"menu")
14. **Lock detection** uses login1 `Lock` signals plus polling for `hyprlock`.
    Other lockers need adding to `Logger.watchdog`.
15. **Sync overlay** is a Quickshell `PanelWindow` on the Overlay layer with an
    empty input mask. Check it is captured by gsr (it is a normal layer
    surface) and does not flash elsewhere on multi-monitor.
11. **Captions** were never run with a real model here. Only the SRT/remap/burn
    paths and the clean skip are tested.

## Reference results (cloud box, 2 vCPU, Nov 9 2026)

Round 4.1 (0.4.1, Nov 10):
- `make test`: 170 passed (about 5 min). New: glide between nearby inputs (stays zoomed,
  every clicked input in view, pan < 1.6 viewports/s, no reversals), zoom-out-between
  still available, long pause / far jump / pointer wandering pull back, triggers and
  on/off, level and motion, trimmed export equal to the full one inside a glide, across
  windows (other app pulls back, short hop glides, dialog glides, scratchpad-sized popup
  and new tiled window pull back, brief monitor trip keeps the glide), Settings ›
  Auto-zoom and its 0.4.0 migration, editor re-plan keeping manual zooms.
- `make scenarios`: 36/36 within caps; worst cases equal round 4 (AOI in frame min
  98.7 %, pan 0.63, zoom 1.25, accel 2.53, jerk 27.6, sync 0.44 frames, 0 reversals).
- Walkthrough recording re-planned: the five panel clicks are one gliding shot.

Round 4 (0.4.0):
- `make test`: 158 passed (about 5 min), no skips on the cloud box. New:
  - `test_scaling.py` (39): Hyprland scale grid, transforms 1/3/5/7, monitor-for-region,
    spanning picks, `-w NAME`/`-o NAME`, even physical regions, end-to-end click
    position within 1 px at 1, 1.25, 1.5, 1.6667, 2 and a rotated monitor at a negative
    offset, followed window leaving the monitor, window card clipping
  - `test_av.py`: a private PipeWire (null sinks, remap source), four audio modes on
    both backends, track alignment within one frame of ground truth, mix spectrum,
    mute spans, the old wf-recorder single-device bug, the fake camera in edit mode
  - `test_settings.py` (a test per group, bar mirror, migration), `test_library.py`
  - export workers make progress while parent threads hold locks; finished jobs stay
    finished; an outside SIGINT is a normal stop; no stray output files
- `make scenarios`: 12 scenarios × 3 aspects, 36/36 within caps after (see the
  table below for round 3; the new scale-1.6667, scale-2 and rotated scenarios
  have AOI in frame 100 % in all aspects). Worst case over 36 runs equals round 3:
  AOI in frame mean 99.9 % / min 98.7 %, pan 0.63, zoom 1.25, sync 0.44 frames.
  The suite caught a 0.4.0 regression before release: the window card clipped
  Timeline corner boxes as if they were x/y/w/h (window-mode 16:9 fell to 74 %).
- `make demo`: export 43.3 fps 1080p60 high (round 3: 32.6), draft 38.3 fps.
- Shell harness (a separate checkout, its path in `$DOLLY_SHELL_HARNESS`; headless sway + fakehypr):
  the editor export of a 27 s recording took 2 min 42 s with forked workers
  (stuck at 0 for a minute); with worker processes it takes about 40 s.
- Walkthrough (`harness/walkthrough.py`, about 25 min incl. two exports) writes
  `dolly-demo/walkthrough/`: `dolly-walkthrough-16x9.mp4` (87 s, 13 chapter
  titles), `-9x16.mp4`, `walkthrough-raw.mkv` (the 5 min take), 22
  `screenshots/`, `chapters.json`, `notes.json` (caveats: test feeds, harness
  input driver, sway instead of Hyprland window rules).

Round 3:
- `make test`: 90 passed (about 3 min). Round 2 coverage plus:
  - AOI scenarios end to end (terminal typing in 16:9/9:16/1:1, workspace cuts)
  - sync markers with offset + 0.3 % drift (< 1 frame)
  - watchdog stop on recorder death; disk guard; `dolly recover` on a torn MKV + torn event log
  - countdown cancel by the same hotkey
  - Glassy solid-window apply/restore (stop, crash + recover, setting off)
  - menu guards are pure shell, < 10 ms, and track state
  - share-ready guard
- `make scenarios` (about 6 min with cached recordings, about 12 min from
  scratch): 9 scenarios × 3 aspects. Before = round-2 engine (cursor-only,
  no sync correction, cursor always drawn); after = round 3.

  | metric (worst case over 27 runs unless noted) | cap | before | after |
  |---|---|---|---|
  | AOI fully in frame, mean / min | ≥ 95 % | 41.2 % / 0 % | 99.9 % / 98.7 % |
  | pan speed (viewport widths/s) | ≤ 1.6 | 1.79 | 0.63 |
  | zoom speed (ln z/s) | ≤ 1.6 | 1.15 | 1.25 |
  | pan accel p99.5 / jerk p99 | ≤ 10 / ≤ 150 | 4.6 / 33.8 | 2.5 / 31.8 |
  | direction reversals inside a shot | 0 | 4 | 0 |
  | typed line height at 1080p | ≥ 15 px | 17.4 | 15.9 |
  | sync error after correction (frames) | < 1 | 20.0 (long-drift) | 0.44 |
  | cursor drawn over typed text (frames) | 0 | 177 | 0 |
  | cursor drawn while on another monitor (frames) | 0 | 477 | 0 |
  | runs passing all caps | — | 0/27 | 27/27 |
- Export speed, same 37 s demo at 1080p60 "high", speed-ups and blur off, 2 vCPU:
  - round 2: 27.7 fps
  - round 3: 32.6 fps with 2 workers and 381 of 2238 frames reused (29.9 fps with `DOLLY_WORKERS=1`)

  On a 20 s stretch with more static frames: 25.0 to 39.5 fps. `--quality
  draft` (half size, 30 fps): 32 to 46 fps. With 2 cores, decoding and x264
  already kept both busy, so gains were small; more cores scale with the
  worker count.
- `make demo` (37.3 s synthetic, about 9 min):
  - 4 zooms (dialog 1.5×, save toast, list click, terminal 2×)
  - 4 speed-ups: 37.3 s to 30.1 s
  - 2 secrets blurred
  - vertical `frames/09b-vertical-reframe.png` now shows the whole "New task" dialog (round 2 cut it off)
  - GitHub 1.8 MB, 1 MB capped variant, GIF, square, draft preview
- Editor smoke (Playwright): lanes, 9:16 preview incl. letterboxed crops, no JS errors.

## Conventions

- Keep the zoom, reframe and timemap engines pure functions of (events, segments,
  config). Add tests for heuristic changes.
- Third-party QML imports `qs.Commons as Commons`, colors from `Commons.Color`.
- Write only under `~/Videos/Dolly`, `$XDG_RUNTIME_DIR/dolly`,
  `~/.local/share/dolly`, `~/.local/state/dolly`, `~/.config/dolly`, and (only
  via dolly-setup, between markers) the menu extension and `hypr/bindings.lua`.
- Never hard-code credentials; never upload without a configured target.

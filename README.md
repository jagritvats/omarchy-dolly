# Dolly

Polished demo recordings for Omarchy: the camera zooms in on what you are doing,
then you trim, frame and share from a built-in editor.

![Dolly: a framed export, an auto-zoomed export, the record panel and Settings](preview.png)

## What it does

Record your screen. Dolly:
- frames what matters, not just the cursor:
  - the command you are typing, even with the mouse parked in a corner
  - the whole window while a log scrolls
  - the dialog a click opened
  - the window you are dragging
  - a clean cut when you switch workspace
- stays zoomed while you work in one place: going down a settings panel or a form,
  the camera glides from one input to the next instead of zooming out and back in
  each time. A long pause, a far jump or a workspace switch zooms out.
- lets you choose how much of this you want (Settings › Auto-zoom, or per recording
  in the editor): automatic zoom on or off, zoom level, which actions zoom (clicks,
  typing, windows and scrolling output), glide or zoom out between nearby actions,
  how long a pause still counts as nearby, and calm/normal/quick camera motion.
  Zooms you add by hand and "zoom here" markers (Super+Alt+M) always apply.
- does the same in vertical 9:16, square 1:1 and 4:5 exports
- redraws a smooth enlarged cursor with click ripples (when the recorder can leave
  the real cursor out; see "Virtual machines")
- puts the recording on a background from your Omarchy theme, with rounded corners
  and a soft shadow (or none of that: see "Background and frame")
- shows keyboard shortcuts
- adds a camera bubble (off by default): edited afterwards, or live on screen
- records microphone and system audio as separate tracks, with noise suppression,
  ducking and a mute-mic key
- speeds through idle moments
- blurs anything that looks like a secret
- keeps a recordings library: search, filters, rename, trash and undo, clean up raw files

Then the share sheet turns it into what each place wants:
- a ≤ 10 MB MP4 for a GitHub PR, or a GIF
- a Discord-sized file
- a vertical 9:16 cut, or a 1:1 square

You can copy the file straight to the clipboard (Ctrl+V into the upload box),
or get a link from your **own** storage (S3, R2, B2, MinIO, or anything rclone
supports). Sharing is local-only until you set a target up.

## Screenshots

Real output, recorded on Omarchy 4.5 in VirtualBox (software rendering).

| Export: framed on a theme background | Export: auto-zoom on the command being typed |
|---|---|
| ![framed export](docs/vm-export-framed.jpg) | ![zoomed export](docs/vm-export-zoomed.jpg) |
| **Record panel (bar button)** | **Settings, with a live preview of the look** |
| ![record panel](docs/vm-panel.jpg) | ![settings](docs/vm-settings.jpg) |

| Editor: timeline with automatic zooms, auto-zoom options |
|---|
| ![editor](docs/vm-editor.jpg) |

![library](docs/vm-library.jpg)

From the test harness (headless sway, test-pattern camera, synthetic audio), for
features a VM cannot show:

| Editor: camera bubble and audio tracks | Settings in a light theme |
|---|---|
| ![editor with camera bubble](docs/editor-dark.jpg) | ![light theme settings](docs/settings-light.jpg) |

## Install

From the Omarchy plugin marketplace, or:

```bash
omarchy plugin add https://github.com/jagritvats/omarchy-dolly.git --enable
```

The record button appears on the bar. The first time you open its panel, Dolly
checks two things:
- **Set up**: adds the hotkeys (Super+Alt+R to record) and Omarchy menu rows. Omarchy
  runs nothing when a plugin is added, so this is one click in the panel.
- **Install missing packages**: opens a terminal running Omarchy's `omarchy-pkg-add`
  (it asks for your password there).

The same from a terminal:

```bash
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly setup            # hotkeys and menu rows
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly deps --install   # packages Dolly still needs
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly doctor           # what works on this machine
```

Needs Omarchy 4.x (the Quattro shell).

**Required packages** (not in Omarchy's base): `python-numpy`, `python-opencv`,
`python-pillow`. In a virtual machine also `wf-recorder` (gpu-screen-recorder,
which ships with Omarchy, cannot use a virtual GPU). `ffmpeg` ships with Omarchy.

**Optional**: `tesseract` and `tesseract-data-eng` (scrub secrets), `rclone`
(Drive/Dropbox/... links), whisper.cpp or faster-whisper (captions), a camera, `mpv`
(camera preview and live bubble).

## Use

| | |
|---|---|
| Bar button | click: the Dolly panel (capture mode, audio, camera, Start, recent recordings, **Settings**) · right-click: quick record · while recording: click stops, right-click shows pause / mute mic / stop / discard |
| Hotkeys (after **Set up**) | Super+Alt+R record/stop · Super+Alt+P pause · Super+Alt+N mute mic · Super+Alt+M zoom here · Super+Alt+Shift+R save last 30 s (instant replay) · Super+Alt+L recordings (Omarchy 4.5 uses it, so it is skipped there). A combination that is already bound is never taken; if an Omarchy update later claims one, the panel says so and **Fix** re-runs setup. Omarchy's Super+K lists them |
| Menu (Super+Space, after **Set up**) | Trigger → Capture → Record demo / Dolly recordings / Dolly settings / Open last / Instant replay · Trigger → Share → Last demo recording |
| Editor | opens after you stop, without taking focus. Library · Editor · Settings. Timeline lanes for zoom, speed and privacy; 16:9 / 9:16 / 1:1 preview; In/Out trim; camera bubble shape, size and position (drag it); camera-only ranges; per-track volume and mute; blur boxes; Scrub secrets; captions; title card; **Share** (Ctrl+E) |
| Settings | the editor's Settings view (also `dolly settings`): Capture, Audio, Camera, Look, Auto-zoom, Privacy, Export, Sharing, Storage. Stored in `~/.config/dolly/config.json`; the bar widget's own options stay in sync |
| CLI | `dolly record [--mode screen|window|region] [--audio none|mic|system|both] [--camera off|edit|live] · stop · pause · mute · mark · replay · edit · library [--open] · settings · config get/set · export [--aspect 16:9|9:16|1:1|4:5] [--quality draft|fast|high|best] [--gif] · share … · recover · deps · setup · integration · doctor` |

### Plain recording, no zoom

Turn off **Zoom in on the action** in the panel before recording, or make it the
default with `dolly config set zoom.enabled=false`. For a recording you already
made, switch off **Zoom in automatically** in the editor (or press **Reset
zooms**). Pick **Screen** for the whole monitor.

### Background and frame

- Presets: `theme-glow` (default), `theme-dusk`, `theme-aurora`, `theme-ember`,
  `theme-ink` (all from your Omarchy theme colours), `solid`, `wallpaper`.
  Choose one in the editor's Background panel, or set the default with
  `dolly config set look.background=theme-ember`.
- Frame: `look.padding` (0–0.2), `look.radius` (0–40 px), `look.shadow` (0–1),
  `look.background_color` for `solid`. Padding 0, radius 0 and shadow 0 give a
  plain full-bleed video with no background.
- Your own image: **Add image…** in the editor's Background panel picks one from your
  theme's backgrounds, `~/Pictures`, a file dialog or a path (PNG, JPEG or WebP),
  with optional blur. Settings › Look makes it the default for new recordings
  (`look.background=image`, `look.background_image`, `look.background_blur`).
  A custom gradient can be set in a recording's `project.json` with
  `"style": {"background": {"stops": [...], "angle": 135}}`.
- Aspect: 16:9, 9:16, 1:1 (editor and export) and 4:5 (`dolly export --aspect 4:5`).
  Vertical and square exports follow the action. There is no crop of the recorded
  area after recording; use **Region** to record part of the screen.

Recordings go to Omarchy's screen-recording folder (`$OMARCHY_SCREENRECORD_DIR`,
else your Videos folder) under `Dolly/`, unless you pick a folder in Settings ›
Storage. Dolly estimates how many minutes of recording the disk holds, warns when
it is getting short and only refuses with a couple of minutes left (with
**Record anyway**, **Choose folder** and **Clean up** buttons).

Sharing is **local-only by default**. Nothing uploads until you add a target with
`dolly share init` (a 0600 config; credentials come from env, an AWS profile or
rclone, never bundled).

While recording, Dolly:
- turns Do Not Disturb on
- keeps windows solid if you use Omarchy's Glassy look
- puts both back afterwards

It never runs a second recorder next to Omarchy's own: it refuses to start while
Omarchy's recorder runs, and Omarchy's screen-recording key stops a Dolly
recording like Dolly's own key would (saved, editor opens). It stops cleanly
(keeping everything recorded) on suspend, lock, low disk, a crashed recorder or
an unplugged monitor. After a hard crash, `dolly recover` saves the partial
recording.

## Virtual machines

Tested on Omarchy 4.5 in VirtualBox 7.2 (VMSVGA, 3D on).
- Install `wf-recorder`: gpu-screen-recorder does not support virtual GPUs
  ("unknown gpu vendor"). `dolly doctor` says which recorder it will use.
- wf-recorder records the real cursor, so the redrawn cursor and motion blur are
  off for those recordings (the editor greys them out and says why); cursor size
  still sizes click ripples and Smooth still steadies the camera. Pause and instant
  replay need gpu-screen-recorder.
- The cursor you see moving smoothly in the VM window is VirtualBox's own pointer,
  not Dolly.
- If Omarchy's shell (bar, launcher) crash-loops on a VirtualBox/VMware display,
  add `export QT_QUICK_BACKEND=software` to `~/.config/uwsm/env` and log in again.
- `/tmp` is in memory on Omarchy; keep recordings on disk (the default).

## Uninstall

```bash
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly cancel             # if a recording is running
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly replay stop        # if instant replay is on
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly-setup --undo       # menu rows, hotkeys, camera window rule
omarchy plugin remove io.github.jagritvats.dolly --yes
rm -rf ~/.local/share/dolly ~/.cache/dolly ~/.local/state/dolly    # optional: venv, caches, state
```

If you remove the plugin without `--undo`, its hotkeys and menu rows switch
themselves off (they only load while the plugin is installed); `--undo` also
deletes the lines.

Your recordings (`…/Videos/Dolly`) and `~/.config/dolly/` (settings, and
`share.json` with any credentials you added) are kept; delete them yourself if
you want them gone. Packages installed for Dolly stay installed; remove them with
`sudo pacman -Rs` if nothing else uses them.

## Troubleshooting

- `dolly doctor` checks the recorder, packages, disk, hotkeys and the bar widget,
  and says what to do about anything missing.
- Errors show one line (and a notification when started from the bar or a hotkey);
  the full details go to `~/.local/state/dolly/last-error.log`, a running log to
  `~/.local/state/dolly/dolly.log`. Please attach both to a bug report.
- A recording's files are written safely: if one is damaged (power loss), Dolly
  restores the previous version and keeps the broken copy as `*.damaged`.

## Developers

Everything is in this repository:
- `make test`: the test suite (Python, pytest; ~6 minutes). It never touches your
  desktop: notifications, Hyprland and the bar are stubbed.
- `make scenarios` / `make demo`: the synthetic-desktop harness (`harness/`) that
  checks the camera on generated recordings.
- `TESTING.md` and `CLAUDE.md`: the real-hardware test plan (also written so an
  AI coding agent can run it).
- `CHANGELOG.md`: what changed in each version.

Bugs and ideas: <https://github.com/jagritvats/omarchy-dolly/issues> (please attach
`dolly doctor` output and `~/.local/state/dolly/last-error.log`).

## License

MIT, see [LICENSE](LICENSE).

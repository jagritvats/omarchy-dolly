# Installation and compatibility

Checked 2026-11-14. Project: <https://github.com/jagritvats/omarchy-dolly>.
Marketplace: <https://plugins.omarchy.org/plugin.html?id=io.github.jagritvats.dolly>.

**Publication status:** the fixes described here are local, uncommitted changes
on top of v0.7.3 (`d9f547a1134d2d21f6898087acc447e3a7cf3b26`), deployed on the
verification machine. They have **not** been pushed or released. Installing the
current GitHub release does not include them yet. In particular, stock 0.7.3's
widget fails to load on the verified Omarchy 4.0.4 installation.

## 1. Prerequisites

- Omarchy Quattro / Hyprland desktop, logged in as your normal user.
- Python 3.10+ and NumPy/Pillow for the **same interpreter**.
- `gpu-screen-recorder`, `ffmpeg` with libx264, `hyprctl`, `jq`, `slurp` and
  `wl-clipboard` (normally supplied by Omarchy).
- A VM/unsupported GPU may need `wf-recorder`; the real-hardware verification
  used `gpu-screen-recorder`, not a VM or software-rendered desktop.

Optional: OCR (`tesseract` + `tesseract-data-eng`), captions (Whisper), remote
sharing (`rclone` or your own configured storage), camera, `mpv`.

Recordings are local by default. Do not configure sharing or use real secrets
while verifying a new installation.

## 2. Install the plugin

From its marketplace listing, or in an interactive terminal:

```bash
omarchy plugin add https://github.com/jagritvats/omarchy-dolly.git --enable
```

For a deliberately noninteractive, already-reviewed install, append `--yes`.
Plugins execute unsandboxed user code; that flag skips the trust confirmation.
Do not append it without deciding to trust the repository.

Adding the plugin clones and enables it; it does **not** finish dependency and
menu/shortcut setup. Do not use `file://` to clone a checkout and expect
uncommitted patches or untracked compatibility components to be included.

## 3. Complete setup

Click the record button and choose **Set up**, or run:

```bash
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly setup
```

This opens a visible terminal. Let it finish; the parent command returning is
not proof that installation finished. It uses Omarchy's package helper for
missing `python-numpy` / `python-pillow` and asks for your password if necessary.
For direct, terminal-based setup with the interactive dependency choices:

```bash
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly-setup
```

Setup writes only managed integration:

- `~/.config/omarchy/extensions/omarchy-menu.jsonc`: Dolly menu block.
- `~/.config/hypr/bindings.lua`: non-conflicting shortcuts and camera rule.
- `~/.local/bin/dolly`: command link, without replacing a foreign command.
- `${XDG_DATA_HOME:-~/.local/share}/applications/io.github.jagritvats.dolly.desktop`:
  **Dolly Screen Recorder**, searchable by `dolly`, `screen recorder`, or
  `screencast`; Library and Settings actions.
- Dolly state/cache and optional private Python/browser environments.

Changed menu/bindings files receive `.dolly-bak` copies. Declining dependencies
does not undo integration already written. Setup is idempotent.

## 4. Check readiness and Python

```bash
dolly --print-python
dolly doctor
dolly integration status --json
```

The launcher tries dependency-capable interpreters in order:

1. `DOLLY_VENV/bin/python`, default `~/.local/share/dolly/venv/bin/python`.
2. `python3` from PATH (including mise/pyenv-managed Python).
3. `/usr/bin/python3`.

If none can import NumPy/Pillow, doctor/deps/setup still run using an available
Python 3.10+ to explain what is missing. No global PATH or Python configuration
needs changing. This fixes the case where Arch packages exist but mise Python
cannot see them.

For an isolated newer Python, create a venv and install wheels into it:

```bash
python3 -m venv ~/.local/share/dolly/my-python
~/.local/share/dolly/my-python/bin/python -m pip install numpy Pillow
DOLLY_VENV=~/.local/share/dolly/my-python dolly doctor
```

That environment selection applies only to the command. Do not point a
system-site-packages venv at a different Python ABI's packages. After a distro
Python ABI upgrade, recreate a broken venv or use the updated system Python.

On 4.0.4, `plugin list` may show `active:false` for a **working bar widget**;
that field represents whole-bar providers on that shell. Check the visible
button, panel IPC and `dolly doctor`, not that field alone.

## 5. Use and verify

- App launcher: search **Dolly Screen Recorder**. Launch opens the panel; it
  does not silently record.
- Omarchy menu: Trigger → Capture → **Dolly Screen Recorder**.
- `Super+Alt+R`: record/stop; `Super+Alt+P`: pause/resume;
  `Super+Alt+M`: mark a zoom. Setup skips conflicting shortcuts.
- Idle hover text is one line: **Dolly Screen Recorder**.

For a safe end-to-end smoke recording, run the reviewed storyboard included in
this checkout (it opens a dummy terminal on an empty workspace, records and
exports, then restores the original workspace):

```bash
dolly run docs/hardware-smoke.toml --dry-run
dolly run docs/hardware-smoke.toml --json
```

`docs/live-platform-check.py` is an additional **live desktop** check, not a unit
test. It drives the actual bar IPC, pause/resume, real Lua logging and export;
it changes capture defaults temporarily and restores them. Inspect it before
running. It does not activate a mic, camera, or remote uploads.

### Editor preview

The default editor uses a dedicated Dolly browser profile and
`--disable-accelerated-video-decode`. This fixes a black video-to-canvas preview
observed with Chromium 152 on this AMD/NVIDIA Wayland machine. GPU compositing,
capture acceleration and normal Chromium settings remain unchanged. Preview
software decoding can cost more CPU, particularly for 4K/high-frame-rate clips.

To use accelerated decoding where it works:

```bash
DOLLY_EDITOR_ACCELERATED_VIDEO=1 dolly edit /path/to/recording
```

Separate mode-specific profiles prevent Chromium process reuse from silently
discarding changed flags. Explicit `DOLLY_BROWSER` wrappers retain their own
flags/profile behavior. This is a compatibility default, not automatic detection
of every affected GPU/driver combination.

## Version evidence

| Environment | Verification |
|---|---|
| Omarchy 4.0.4-1, Hyprland 0.56.2, Quickshell 0.3.1 | Actual installed bar/panel, app search, screen/window capture, pause/resume, editor preview and export |
| Python 3.14.7 + NumPy 2.5.3 + Pillow 12.3.0 | Actual hardware capture/export and editor |
| mise Python 3.14.8 + NumPy 2.5.4 + Pillow 13.0.0 | Isolated venv, doctor, actual hardware storyboard capture/export; 6/6 steps, no warnings |
| Omarchy 4.5.0, commit `ac2690cd7be4f47e4a255f5e2e78d904d436b068` | Exact-release QML API inspection and exact-release plugin validator passed; **full 4.5 desktop recording not run** |
| Other/future Python or Omarchy versions | Not guaranteed; check dependencies and runtime behavior |

Adapters prefer native APIs and fall back by capability, not a guessed version
number: translations/locale, lazy panel loading and IPC registry methods. In
particular, 4.5's registry exists but lacks methods introduced later, so those
methods are checked individually. A complete 4.5 desktop smoke remains the next
compatibility qualification step; no desktop upgrade was done in this session.

## Updates and removal

Once these patches are released: `dolly update` refreshes setup and may restart
the shell when QML changed. **For the current locally patched installation,
do not update until the fixes are preserved/released**; Git may refuse a dirty
checkout, and replacing it can lose fixes.

```bash
dolly cancel                          # only if you want to discard a running take
dolly replay stop                     # if replay is armed
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly-setup --undo
omarchy plugin remove io.github.jagritvats.dolly --yes
```

Undo removes Dolly's managed launcher/menu/hotkeys/link, not unrelated entries.
Recordings, settings, private environments/browser profiles and dependency
packages are retained. Review and remove them separately if wanted; sharing
configuration may contain credentials and must not be copied into bug reports.

# Dolly: real installed desktop verification — 2026-11-14

## Scope and build

Jagrit requested GitHub/marketplace discovery, installation documentation and
**real non-VM platform testing**, then fixes for old/new shell compatibility,
searchable application naming and an oversized hover tooltip.

- Repository: <https://github.com/jagritvats/omarchy-dolly>.
- Marketplace: <https://plugins.omarchy.org/plugin.html?id=io.github.jagritvats.dolly>.
- Publication evidence: <https://github.com/omacom/omarchy-plugin-marketplace/issues/14607>.
- Base: v0.7.3, `d9f547a1134d2d21f6898087acc447e3a7cf3b26`.
- Source: `/home/jagrit/Projects/omarchy-dolly`.
- Installed: `~/.config/omarchy/plugins/io.github.jagritvats.dolly`.
- Fixes are **local and uncommitted, not published**. No PR, push or release.

## Actual environment

`systemd-detect-virt` returned `none`. Omarchy **4.0.4-1**, Hyprland **0.56.2**,
Quickshell **0.3.1**, gpu-screen-recorder **6.1.0**, Chromium **152**.
Single eDP-1 monitor: **1920×1080**, scale **1.25**. AMD Renoir + NVIDIA GTX
1660 Ti Mobile; gsr actually captured/encoded on **AMD `/dev/dri/card2` via VAAPI**.
Do not describe this run as an NVENC test just because an NVIDIA GPU is present.

## Problems reproduced and repaired

1. **Bar absent despite enabled layout:** `ShellIpc is not a type`; older shell
   also lacked Translations/I18n and native lazy panel properties. Added small
   QML adapters retaining native behavior when available. One shell restart was
   needed to clear a cached failed component. Bar and panel then rendered.
2. **Arch dependencies invisible to mise Python:** setup installed the packages,
   but PATH chose another Python. Launcher/setup now select a dependency-capable
   interpreter without changing global Python configuration.
3. **Wrong window across workspaces:** a named storyboard terminal lost its
   address and matched an unrelated window with identical geometry. The watchdog
   stopped with `window-left-monitor`. Preserve the exact address; filter
   geometric matches to visible workspaces; inspect actual geometry before
   deciding an invisible target left a monitor. Empty export trims now produce
   an actionable error instead of an invalid concat-list message.
4. **Discoverability:** added managed XDG desktop entry, `dolly launch`, launcher
   keywords/actions, and renamed Omarchy's generic Demo entry.
5. **Oversized hover:** one-line idle label and short state-specific tooltips.
6. **Black editor preview:** accelerated video decode produced black canvas
   frames despite normal timecode and valid export. Isolated flags showed that
   disabling video decode alone fixes it. Dolly now uses its own private app
   profile with that narrow flag; normal browser configuration is unchanged.
7. **4.5 static compatibility:** exact 4.5 registry lacks later `leads`/`touch`
   methods. Guard those capabilities independently, not just registry existence.

## Real execution evidence

| Workflow | Result |
|---|---|
| Install/setup | Actual GitHub plugin install and setup; NumPy/Pillow installed; six non-conflicting hotkeys, menu/link/app launcher |
| Bar | Visible right-section widget; panel opens/closes; Escape dismisses; compact tooltip inspected by hover |
| Application search | Actual Apps search for `screen recorder` found Dolly; `gtk-launch` opened panel without recording |
| Window storyboard, Python 3.14.7 | `2026-11-14_14-06-42`: **6/6 steps, no warnings**, raw 18.200 s, H.264 1920×1080 **60/1 nominal and average fps** |
| Window CLI export | Same project: 14.366667 s trimmed MP4, 1920×1080, 60 fps, 1,143,065 bytes; full decode succeeded |
| Real bar screen recording | `2026-11-14_14-13-17`: start/stop and pause/resume through actual widget IPC; Lua source, 88 key events, one mark; `markers2` sync, residual 0.0026 s |
| Screen export | Same project: MP4 1280×720, 7.083 s, 504,907 bytes; full decode succeeded |
| DND | Off before, on during recording, off afterward |
| Actual editor GUI | Playback/time advances, timeline seek/lanes, Settings/Look, Library search/reopen; no app JS/console/HTTP errors in controlled headed browser |
| Editor-button export | `2026-11-14_14-06-42-142046.mp4`: H.264 1280×720 **30/1 nominal and average fps**, 431 frames, 14.366667 s, 671,562 bytes; 20.19 s UI completion; full decode succeeded |
| Preview fix | Actual installed `dolly edit`, not just diagnostic browser: visible terminal footage, replay/reuse and focus-preserving navigation checked |
| New Python real recording | Python **3.14.8**, NumPy **2.5.4**, Pillow **13.0.0**, isolated venv: `2026-11-14_14-43-35`, **6/6 steps, no warnings**, raw 18.73 s; MP4 1920×1080 60/1 nominal/average fps, 14.900 s, 320,686 bytes; full decode succeeded |
| Diagnostics | `dolly doctor --json`: `ok:true`; no Hyprland configuration errors; no shortcut conflicts |
| Review | Independent source review found no material issue; newer-shell runtime remains unqualified |

Pause/resume state transitions were observed and saved; frame-by-frame audiovisual
sync across pause was not qualified. The generated mouse dispatcher click did
**not** appear as a native Lua click event, so this is not a pass for physical
mouse-click/ripple alignment. Storyboard click events were captured through its
explicit script feed. Do not conflate those paths.

Visual review also found repeated `y` input and two I-beam cursor images in a
sampled frame from the earlier `14-06-42` storyboard. The later Python3.14.8
take's sampled frame has the intended text without repetition. This is **not**
proof of a Python-specific cause or a demonstrated fix for those intermittent
typing/cursor artifacts; their reproduction and cursor-hiding qualification
remain open. Successful capture/export is not a blanket visual-polish pass.

### Local evidence locations

- `local-verification/live-ui-20261114/`: bar/panel/hover images and verification.
- `local-verification/live-hardware/`: bar report and sampled exports.
- `local-verification/live-editor/`: GUI evidence, actual installed-app screenshot,
  decoder isolation and launch-fix report. Earlier black-preview report there is
  superseded by `LAUNCH-FIX.md`.
- Dummy projects under `~/Videos/Dolly/` at timestamps above; earlier failed
  `2026-11-14_13-43-08` retained as diagnosis evidence.
- Reproducers: `docs/hardware-smoke.toml`, `docs/live-platform-check.py`.

Recordings contain dummy content. No microphone/camera activation or uploads.
No full application test suite was used to claim platform success. An initial
automated-testing detour was stopped in favor of the explicitly requested live
platform work; its leftover environment/logs are not the acceptance evidence.

## Omarchy 4.5: what is and is not verified

Exact official v4.5.0 commit:
[`ac2690cd7be4f47e4a255f5e2e78d904d436b068`](https://github.com/omacom/omarchy/tree/ac2690cd7be4f47e4a255f5e2e78d904d436b068).
Inspected its `shell/Commons/{ShellIpc,IpcRegistry,Translations,I18n}.qml` and
`shell/Ui/{KeyboardPanel,BarWidget}.qml`. Executed that release's
`bin/omarchy-plugin-validate` against the installed Dolly: exit 0. Rechecked the
legacy panel after the resulting guard fix: rendered and closed normally.

**No full Omarchy 4.5 desktop was installed or run.** This establishes API and
manifest compatibility, not a complete 4.5 recording/GPU/editor qualification.
Python 3.14.8 was tested on the existing 4.0.4 desktop, not a 4.5 desktop.

## Remaining qualification and release work

- Actual 4.5 desktop installation, panel, capture and editor smoke.
- Physical click/ripple alignment; hotkeys themselves; region picker;
  multiple/rotated monitors; long takes; recovery/lock/suspend.
- Mic/system audio, real camera, replay and remote sharing were deliberately
  not tested. No guarantee for future Python/Omarchy/browser/driver versions.
- Preserve/review local changes and release only when authorized. A normal
  upstream update currently does not contain these repairs.

Final desktop state: no recording/replay/jobs, original capture defaults restored,
DND off; healthy Dolly editor available on workspace 2 and user focus restored to
workspace 1. Packages, private environments, dummy projects and evidence retained.

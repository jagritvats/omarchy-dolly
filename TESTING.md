# Dolly: test checklist

Tick each box, and add notes or numbers. Step numbers refer to the test plan
in CLAUDE.md. Keep evidence in `~/dolly-evidence/<date>/`.

Machine: ☐ Omarchy VM on Windows (hypervisor: ______)  ☐ real hardware (GPU: ______)
Omarchy: ______   Hyprland: ______   gpu-screen-recorder: ______   webcam: ______   mic: ______

## Verified in the cloud box (no Hyprland/GPU/webcam/network), re-check locally

- [x] `make test`: see CLAUDE.md Reference results for the count (cloud). Local: [ ] pass [ ] fail ____
- [x] `make scenarios`: 12 scenarios (incl. 1.6667x, 2x, rotated) × 16:9/9:16/1:1, every area-of-interest metric within its cap (cloud). Local: [ ] pass [ ] fail ____
- [x] Menu guards: pure shell, < 10 ms each (cloud, `tests/test_guards.py`). Local Omarchy budget test: [ ] pass [ ] fail ____
- [x] Watchdog/recover/disk guard/countdown cancel/Glassy restore with stubbed gsr + hyprctl (cloud)
- [x] `make demo`: all variants + frames + summary.json (cloud, about 7 min). Local: [ ] pass [ ] fail ____
- [x] Editor smoke incl. share sheet → copy → upload to a local S3 (moto), no JS errors (cloud). Local: [ ] pass [ ] fail
- [x] `omarchy-plugin-validate` passes; menu extension parses (16 Dolly rows) with Omarchy's MenuModel.js (cloud). Local: [ ] pass [ ] fail
- [x] SigV4 presign identical to botocore; S3 upload/GET/DELETE on moto; rclone S3 link + local-remote fallback; Litterbox multipart against a fake host (cloud)
- [x] Capture fallback gsr → wf and replay save/align with stubs (cloud)
- [x] Round 4 (cloud + Omarchy shell harness): fractional scales 1–2 and a rotated monitor at a negative offset, click lands within 1 px (`tests/test_scaling.py`); regions snap to even physical pixels; audio tracks in all 4 modes on both backends with a private PipeWire, aligned within one frame, mix FFT-checked, muted span (`tests/test_av.py`); camera test feed aligned + composited; Settings honoured per group; Library rename/trash/undo/cleanup; export workers progress with threads holding locks in the parent
- [x] Harness (real Omarchy shell b99593e in Quickshell 0.3.2, headless sway at 1.25/1.6667): panel, meters, camera preview, countdown, editor in Chromium, Library, Settings, light/dark theme follow, menu rows (dolly-demo/walkthrough)

## Needs real Hyprland / hardware / network

| # | Check | Pass | Fail | Notes / evidence |
|---|---|---|---|---|
| 1 | doctor: capture ____ clicks ____ replay ____ targets ____ | [ ] | [ ] | doctor.json |
| 2 | Plugin installs, 󰑊 in bar | [ ] | [ ] | bar.png |
| 3 | Lua bridge clicks + keys, non-consuming, clean stop | [ ] | [ ] | bridge.jsonl |
| 4 | Screen recording end to end | [ ] | [ ] | bundle |
| 5 | Cursor hidden (gsr) / baked + Dolly cursor off (wf) | [ ] | [ ] | raw-*.png |
| 6 | CFR raw + exports | [ ] | [ ] | probes |
| 7 | Mapping: scale 1 [ ] fractional [ ] second monitor [ ] | [ ] | [ ] | |
| 8 | Event sync offset: ____ ms | [ ] | [ ] | |
| 9 | Window + region modes | [ ] | [ ] | |
| 10 | Bar widget clicks incl. right-click pause, no QML warnings | [ ] | [ ] | qs.log |
| 11 | Pause/resume (Super+Alt+P): no gap, events line up after resume | [ ] | [ ] | |
| 12 | Mark zoom (Super+Alt+M) → "marked" segment | [ ] | [ ] | |
| 13 | Instant replay: arm, Super+Alt+Shift+R, ~30 s project, aligned events, stop | [ ] | [ ] | |
| 14 | Mic: in sync, noise reduced, ≈ -16 LUFS | [ ] | [ ] | ebur128 |
| 15 | Desktop audio not denoised | [ ] | [ ] | |
| 16 | Webcam: file recorded, offset ____ s, bubble moves away from cursor, clap in sync | [ ] | [ ] | |
| 17 | VM with no webcam/mic still records/exports | [ ] | [ ] | |
| 18 | Editor round 2: lanes, auto idle, blur box, scrub secrets, 9:16/1:1 preview, title, style presets | [ ] | [ ] | |
| 19a | Copy file → paste into GitHub comment (Chromium) | [ ] | [ ] | |
| 19b | … Firefox | [ ] | [ ] | |
| 19c | … Slack / Discord (web or Electron) | [ ] | [ ] | |
| 20 | Discord ≤ 20 MB, Shorts 9:16, LinkedIn 1:1 look right | [ ] | [ ] | |
| 21a | Real S3/R2/B2 upload: link opens logged-out, delete remote works | [ ] | [ ] | |
| 21b | rclone Drive/Dropbox link + delete | [ ] | [ ] | |
| 22 | (optional) Litterbox public temp link | [ ] | [ ] | |
| 23 | Notification actions Open / Copy file / Get link / Edit | [ ] | [ ] | |
| 24 | Captions with whisper (if installed) | [ ] | [ ] | |
| 25 | wf path: pause/replay refuse cleanly | [ ] | [ ] | |
| 26 | 5-min recording export time ____ ; cancel cleans up | [ ] | [ ] | |

## Round 3 on real hardware (CLAUDE.md "Round 3: real-hardware plan per scenario")

| # | Check | Pass | Fail | Notes / evidence |
|---|---|---|---|---|
| R1 | Terminals: Alacritty [ ] Ghostty [ ] kitty [ ] foot [ ]: typed line framed, mouse ignored, I-beam, hidden while typing | [ ] | [ ] | |
| R2 | Build log: whole window, no chasing | [ ] | [ ] | |
| R3 | Tiling open/close/split drag | [ ] | [ ] | |
| R4 | Floating drag + resize followed | [ ] | [ ] | |
| R5 | Workspace switch cuts (instant [ ] / animated [ ]), scratchpad | [ ] | [ ] | |
| R6 | Toasts ignored; Omarchy menu framed (namespace ______ keyboard ____); DND on/restored | [ ] | [ ] | |
| R7 | Mixed fractional monitors (____ / ____): mapping, other-monitor clicks ignored, cursor fades, hotplug ok, recorded monitor unplug stops cleanly | [ ] | [ ] | |
| R8 | Cursor theme size ____ matches | [ ] | [ ] | |
| R9 | XWayland app framed correctly | [ ] | [ ] | |
| R10 | Sync markers: method ____ drift ____ % ; marker not in export | [ ] | [ ] | |
| R11 | Window mode: follows, covered, close stops | [ ] | [ ] | |
| R12 | Suspend [ ] lock [ ] logout+recover [ ] stop cleanly | [ ] | [ ] | |
| R13 | kill -9 recorder → clean stop; kill everything → recover | [ ] | [ ] | |
| R14 | Low disk stop / refuse to start | [ ] | [ ] | |
| R15 | Glassy: solid while recording (incl. new window), restored after stop, cancel and crash+recover; mode still glassy | [ ] | [ ] | |
| R16 | Hotkey cancels countdown; ttff ____ ms; hotkey→first frame ____ ms; editor doesn't steal focus | [ ] | [ ] | |
| R17 | Theme change reaches open editor + bar | [ ] | [ ] | |
| R18 | Menu guards ≤ 10 ms each, rows correct | [ ] | [ ] | |
| R19 | Export fps high ____ draft ____ (1 worker: ____) | [ ] | [ ] | |
| R20 | Arch OpenCV (TBB): exports from the editor finish, no "no progress" note | [ ] | [ ] | |
| R21 | hyprctl scale printed as ____; logical sizes whole | [ ] | [ ] | |
| R22 | Region on the unfocused monitor / spanning pick | [ ] | [ ] | |
| R23 | Whole-monitor pick records by name (-w/-o NAME) | [ ] | [ ] | |
| R24 | Rotated monitor: portrait video, ripples on target | [ ] | [ ] | |
| R25 | Click accuracy at 1 / 1.25 / 1.5 / 1.6667 / 2 | [ ] | [ ] | |
| R26 | Window dragged across monitors (brief / for good) | [ ] | [ ] | |
| R27 | XWayland rects with zero-scaling on and off | [ ] | [ ] | |
| R28 | Theme follow (bar, panel, editor, font); pinned re-export | [ ] | [ ] | |
| R29 | Coexistence with Omarchy's recorder (refuse; outside stop = normal stop) | [ ] | [ ] | |
| R30 | Audio: 4 modes × gsr/wf, meters, sync, mute span | [ ] | [ ] | |
| R31 | Camera bubble in edit: preview, webcam.mkv, editor changes, sync | [ ] | [ ] | |
| R32 | Live bubble: round, accent border, no focus, pinned, single bubble in export | [ ] | [ ] | |
| R33 | Camera errors: busy / missing / permission toasts | [ ] | [ ] | |
| R34 | Library + Settings, widget values in sync both ways | [ ] | [ ] | |
| R35 | ffmpeg ____; MP4 survives kill? | [ ] | [ ] | |

## Quality review (watch the exports critically)

See also the smoothness checklist in CLAUDE.md.

- [ ] Zoom-ins frame the target; no hunting or jitter
- [ ] Idle cut feels natural; ⏩ badge readable; no cut mid-sentence (mic)
- [ ] Vertical crop keeps the cursor and the thing being clicked in frame
- [ ] Blur boxes cover the whole secret for the whole time it is visible
- [ ] Shortcut keycaps / captions readable, not overlapping
- [ ] Webcam bubble never covers the click target
- [ ] GitHub export ≤ 10 MB and still readable

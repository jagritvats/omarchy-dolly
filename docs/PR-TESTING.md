# Draft PR test build — no release

PR: <https://github.com/jagritvats/omarchy-dolly/pull/5>

This branch is for Jagrit's review and Omarchy **4.5 VM** testing. It is not an
approved or released version. Packaging/CI success does not certify desktop
compatibility. The manifest remains 0.7.3; use the Git commit/BUILD-INFO to
identify this development build.

## Download the exact test package

Open the PR's **PR test build → Non-release plugin package** check, then download
its artifact. GitHub may require sign-in. The artifact expires after 30 days;
the PR branch remains available. The zip contains:

- `dolly-pr-5-<commit>.tar.gz`: tracked source only.
- `dolly-pr-5-<commit>.bundle`: offline-cloneable Git repository snapshot.
- `BUILD-INFO.txt`: exact commit and pending 4.5 runtime status.
- `SHA256SUMS`: integrity checks for all four payload files.
- `INSTALL-PR.md`: these instructions.

Extract the downloaded zip into a new directory, open a terminal there, then:

```bash
sha256sum -c SHA256SUMS
git clone ./dolly-pr-5-<commit>.bundle dolly-pr-source
git -C dolly-pr-source rev-parse HEAD
```

Replace `<commit>` with the actual filename suffix. Compare the full commit to
`BUILD-INFO.txt`. Keep `dolly-pr-source` until you remove this installation:
Omarchy records its local origin path. A tar archive alone lacks Git metadata
and cannot be passed directly to `omarchy plugin add`.

## Install on a fresh 4.5 VM

From the extracted package directory:

```bash
omarchy plugin validate ./dolly-pr-source
omarchy plugin add "file://$PWD/dolly-pr-source" --enable
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly setup
dolly doctor
```

Setup opens a visible terminal; let it finish. It may request the package-manager
password for NumPy/Pillow and `wf-recorder`. **A virtual GPU may need wf-recorder**
even though the physical-machine test used gpu-screen-recorder. VM capture may
have a baked-in cursor; pause/replay need a functioning gpu-screen-recorder
backend. Record what doctor actually reports rather than treating those backend
differences as a pass or failure by themselves.

If Dolly is already installed, do not overwrite or reset it blindly. Preserve
local changes and recordings, undo its managed integration and remove the
plugin first if you choose to replace it. Use a fresh VM for the clean-install
check where possible. Do not use `dolly update` for this temporary local-origin
snapshot; obtain a new artifact to test a later PR commit.

### Alternative: clone the PR branch online

```bash
git clone --branch fix/omarchy-compatibility-and-discovery --single-branch \
  https://github.com/vibeforj-afk/omarchy-dolly.git dolly-pr-source
omarchy plugin add "file://$PWD/dolly-pr-source" --enable
~/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly setup
dolly doctor
```

The branch can change; record `git -C dolly-pr-source rev-parse HEAD` with results.

## 4.5 VM review checklist

- [ ] Record Omarchy, Hyprland, Quickshell, Python and backend versions.
- [ ] Clean installation/setup completes; doctor has no required failures.
- [ ] Top-bar button appears; panel labels/layout work; Escape closes it.
- [ ] Search `dolly` and `screen recorder` in Apps; launch opens the panel without
      starting recording. Library/Settings app actions work.
- [ ] Hover text is compact; menu entry is recognizable as Dolly Screen Recorder.
- [ ] Record/stop a dummy window and screen; no wrong-workspace selection or
      premature window-left-monitor stop. Restore DND and original focus.
- [ ] On a capable backend, pause/resume works. Note unsupported VM features.
- [ ] Editor preview has visible footage, seeks/plays, and completes an export.
- [ ] Check final MP4 frames and reported resolution/duration/frame rate.
- [ ] Recheck previously observed intermittent scripted key-repeat/double-cursor
      artifacts; physical-click/ripple alignment is not yet qualified.
- [ ] Review dedicated editor-browser decoding default/CPU use and opt-out
      `DOLLY_EDITOR_ACCELERATED_VIDEO=1` where supported.
- [ ] Jagrit reviews and approves only after testing; keep the PR draft until then.

Safe live repro from the cloned source directory:

```bash
dolly run docs/hardware-smoke.toml --dry-run
dolly run docs/hardware-smoke.toml --json
```

Use dummy content. No camera, microphone, credentials or uploads are needed.
See `docs/HARDWARE-VERIFICATION-2026-11-14.md` for what was actually verified on
physical Omarchy4.0.4 and what remains unverified.

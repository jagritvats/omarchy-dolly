# Panel overflow, Recents jobs and portrait framing

Verified 2026-11-15. Proposed in [draft PR #7](https://github.com/jagritvats/omarchy-dolly/pull/7),
based on released 0.7.4 (`0ee206938a7b6a6352ace6398d27f73bc4eb8924`).
These follow-ups are not part of the current stable release. Abandoned PR #5
was not revived. No dependency packages were added.

## Environment and installation

Actual non-VM Omarchy 4.0.4-1, Hyprland 0.56.2, Quickshell 0.3.1; one
1920×1080 display at 1.25 scale (1536×864 logical pixels). Dolly uses
`/usr/bin/python3`, stock Cairo and Pango. The old NumPy/Pillow system packages,
Dolly runtime venv and Python-3.14.8 verification venv were removed first.

The released recorder installed successfully with:

```sh
omarchy plugin add https://github.com/jagritvats/omarchy-dolly.git --enable --yes
```

The installed commit was checked against v0.7.4. **Set up is still a separate,
optional integration step** for application search, menu rows, hotkeys and the
`dolly` command link; it installs no packages. After proving the plain install
path, that integration was restored. Recordings and user settings were preserved.

## Panel and active jobs

- Native `fittedContentHeight()` retains screen bounds, insets and bar placement.
  A single clipped vertical Flickable scrolls excess content; the scrollbar is
  visible when needed. Header and recording controls remain pinned.
- Controls have keyboard focus/navigation, offscreen focus is revealed, and
  PageUp/Down, arrows and Home/End scroll. Enter on the empty panel no longer
  starts a recording or toggles replay inadvertently.
- Active render/export jobs appear below their recording with status and
  progress. Unknown totals are indeterminate, not a fabricated zero percentage.
- Older active projects are prioritized before the five-row limit. An in-progress
  rename remains reachable and retains draft text, cursor and selection across
  job start/completion, not just progress updates.
- Jobs and rows share a canonical project identity while action paths retain
  their original symlink spelling. Existing v3 caches are migrated without
  probing video or generating posters.
- Existing file watchers and activity-gated liveness checks are reused. There
  is no new idle polling process or per-progress library/device scan.

### Actual versus simulated checks

| Check | Evidence/result |
|---|---|
| Installed popup with expanded Recent actions | Bounded at 380×838 logical px, y=26, bottom=864; scroll reached final row/replay control |
| Real installed export | Recent status changed from indeterminate to 27% and 71%, then cleared on completion; compositor export succeeded |
| Real QML stress component, synthetic layout | 1004px content within 764px viewport; wheel/drag scrolling and focused control/rename reachability checked |
| Synthetic 440px available-height cap | Top/bottom/left/right placements fit; final control reachable; Stop stayed pinned in synthetic recording state |
| Real exports driving an isolated actual-QML component | Dirty rename text/cursor/selection retained across same-project, unrelated-project and older-promoted-project job start/completion |
| Real symlink action paths | Row canonical identity matched live job; original action spelling preserved |
| Cache identity migration during dirty edit | Draft migrated to canonical identity without losing text/selection |

The latter layout/membership cases use labelled synthetic library ordering, not
a claim of changing the user's actual library. No camera/microphone or replay
was activated for these checks, and no existing recording was renamed/trash-moved.

## Auto-zoom/focus: real input recordings, inspected exports

Two isolated dummy-workspace captures tested typing with a parked pointer,
A → newly opened B → A target changes, and three separated GTK controls.
Input was **storyboard script-feed input**, not physical mouse/keyboard input.
Native Lua click timing and physical click/ripple alignment are not certified.

Landscape typing followed the line rather than the parked mouse. Complete
prompt/text/caret bounds were in frame at all four sampled end-of-line moments;
all three clicked controls were fully visible in landscape. Rendered mouse was
hidden during the measured typing intervals. Portrait exposed two defects:
clipped control subjects and zoom-width breathing during a growing line.

### Repaired portrait behavior

- Preserve a bounded click-action rectangle from anticipatory lead-in through
  the reading hold, rather than collapsing to a pointer neighborhood.
- Choose a sufficient, legibility-bounded typing width for the shot instead
  of expanding/contracting as the activity window forgets glyphs.
- Preserve manual/fixed shots and disabled automatic triggers; do not widen
  arbitrary pointer travel or pre-aim across workspace transitions. Existing
  huge-page strip policy and letterbox/readability caps remain.

Before/after exports reused copies of the **same actual recordings**. Existing
project files and settings were not modified for comparison.

| Control, 9:16 | Before: visible at click | After: visible at click | After: minimum over 1.7s hold |
|---|---:|---:|---:|
| Alpha | 51.6% | 100% | 100% |
| Beta | 84.2% | 100% | 100% |
| Gamma | 89.6% | 100% | 100% |

All three full controls and labels fit in inspected actual export frames. The
form's widest crop uses 1.1703 portrait strips (~85.45% card-height fill), rather
than shrinking the whole screen into an unreadable thumbnail.

B's typing zoom previously reversed **0.7564 → 0.9829 → ~0.768**. Afterward it
stayed at ~0.755934 over that measured interval. All four measured typing
intervals had zero meaningful zoom reversals. Complete line bounds remained
100% visible at the sampled checks, with the existing text-legibility floor.

Landscape camera arrays were exactly unchanged across 806 typing frames and
409 form frames. Encoded before/after videos are **not byte/pixel-identical**:
worker/chunk encoding differs (global SSIM 0.991743 typing / 0.988368 form).

Four actual after-fix compositor exports were decoded and inspected: typing
1080×1920 and 1920×1080 (~26.866s), form in both sizes (~13.633s), nominal30fps.
The original typing capture exceeded its raw-duration budget by0.734s and its
runner hit the configured tail limit; this was retained as a run limitation,
not silently labelled a successful bounded recording.

## Focused automated checks

Only the new focused regression files were run locally, not the full suite:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /usr/bin/python3 tests/test_panel_identity.py -v
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/test_reframe_actions.py -v
```

**4 identity/cache tests and 8 portrait action tests passed.** QML syntax,
Python/JSON parsing, plugin validation and Git whitespace checks also passed.
CI status is reported separately on the PR, not inferred from these checks.

## Remaining qualification

- Native Omarchy4.5 panel behavior and physical mixed-monitor interaction are
  not runtime-tested for these new fixes. Smaller/side placements were tested
  in the labelled component harness on4.0.4.
- Pinned Stop geometry was checked in synthetic recording state, not a new
  live Stop activation. Multiple-job crash/removal behavior needs broader coverage.
- Portrait framing depends on an observable, bounded action/reaction rectangle;
  it does not semantically discover arbitrary widget trees or promise to show
  every huge page whole. Very rapid clicks may require a transition.
- Physical input sync, workspace cuts, long takes, camera/audio and uploads were
  outside this verification. No blanket claim for all apps/fonts/settings.

Local raw evidence, scripts, crops and before/after clips are retained in the
ignored `local-verification/` directory, including `p2/` and
`zoom-focus-20261115/`. They are not included in the public source commits.

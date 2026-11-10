"""Area-of-interest camera on generated scenarios (round 3).

Runs the real pipeline (synthetic recording -> finalize with sync markers ->
AOI -> camera/crop) on two scenarios and checks the quality metrics: the
typed line stays in frame with the mouse parked far away (16:9 and 9:16), the
camera never whip-pans, typed text stays legible, the cursor is hidden while
typing, and events are within a frame of the video after sync correction.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def scen(tmp_path_factory):
    import scenarios
    out = tmp_path_factory.mktemp("scen")
    dirs = scenarios.generate(out, ["terminal-typing", "workspaces"], fps=15)
    return {d.name: d for d in dirs}


@pytest.mark.parametrize("aspect", ["16:9", "9:16", "1:1"])
def test_terminal_typing_after_passes_caps(scen, aspect):
    import metrics as M
    r = M.evaluate(scen["terminal-typing"], aspect, M.Engine("after", True))
    assert r["pass"], (r["failed"], r["aoi_misses"])
    assert r["aoi_in_frame_pct"] >= 95 and r["reversals"] == 0 and r["sync_error_frames"] < 1


def test_terminal_typing_before_misses_the_typing(scen):
    import metrics as M
    r = M.evaluate(scen["terminal-typing"], "9:16", M.Engine("before", False))
    assert r["aoi_in_frame_pct"] < 50          # the old engine followed the parked mouse


def test_workspace_switch_cuts_not_whip_pans(scen):
    import metrics as M
    for aspect in ("16:9", "9:16"):
        r = M.evaluate(scen["workspaces"], aspect, M.Engine("after", True))
        assert r["pass"], (aspect, r["failed"])
        assert r["cuts"] >= 2                   # instant switch + slide back


def test_dead_zone_keeps_caret_when_line_is_too_long():
    from dolly.zoom import _dead_zone_rect
    c = _dead_zone_rect(500.0, 0.0, 2000.0, 300.0, anchor=1800.0)
    assert 1800.0 - 300 <= c - 0 and 1800.0 <= c + 300      # caret inside the view
    assert _dead_zone_rect(500.0, 450.0, 600.0, 300.0) == 500.0   # fits: no movement

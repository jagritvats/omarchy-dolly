"""Camera across windows (0.8.1) on generated scenarios: a window that opens is framed when it
has popped in (the camera sets off before it opens), and typing into the Omarchy menu keeps
the whole menu in the 9:16 crop."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def scen(tmp_path_factory):
    import scenarios
    out = tmp_path_factory.mktemp("scenw")
    dirs = scenarios.generate(out, ["tiling", "interruptions"], fps=15)
    return {d.name: d for d in dirs}


def test_a_new_tiled_window_is_in_frame_once_it_has_opened(scen):
    import metrics as M
    r = M.evaluate(scen["tiling"], "16:9", M.Engine("after", True))
    assert r["pass"], (r["failed"], r["aoi_misses"])


def test_typing_into_the_menu_keeps_the_whole_menu_in_the_vertical_crop(scen):
    import metrics as M
    r = M.evaluate(scen["interruptions"], "9:16", M.Engine("after", True))
    assert r["pass"], (r["failed"], r["aoi_misses"])

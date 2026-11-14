"""A click that only focuses a window is not a subject; the typing that follows is."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly.aoi import AOITrack  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.zoom import ZoomConfig, _aoi_actions, focus_only_click  # noqa: E402

W, H = 1920, 1200
CLICK = {"t": 1.0, "type": "click", "x": 1500.0, "y": 1000.0, "button": "left"}
PBOX = (1390.0, 910.0, 1610.0, 1090.0)          # the pointer box the AOI reports after a click


def scene(after_click_rect=PBOX, typing_at=(200.0, 110.0), keys=True, shortcut=False):
    t = [round(0.1 * i, 3) for i in range(60)]              # 6 s, a sample every 0.1 s
    rect, kind, foc = [None] * len(t), [None] * len(t), [None] * len(t)
    for i in range(11, 18):                      # 1.1 .. 1.7 s: pointer activity from the click
        rect[i], kind[i], foc[i] = after_click_rect, "pointer", (CLICK["x"], CLICK["y"])
    if typing_at:
        for i in range(19, 40):                  # typing shows up from 1.9 s
            rect[i], kind[i], foc[i] = (12.0, 80.0, 700.0, 140.0), "typing", typing_at
    ev = [{"t": 0.0, "type": "move", "x": CLICK["x"], "y": CLICK["y"]}, dict(CLICK)]
    if keys:
        mods = ["ctrl"] if shortcut else []
        ev += [{"t": 1.8 + 0.1 * i, "type": "key", "key": "a", "mods": mods} for i in range(8)]
    tl = Timeline.from_dict({"video": {"w": W, "h": H, "fps": 60, "duration": 6.0},
                             "capture": {"scale": 1.0}, "events": ev})
    return tl, AOITrack(t, rect, kind, foc, [], 16.0)


def kinds(tl, aoi):
    return [a.kind for a in _aoi_actions(tl, ZoomConfig(), aoi)]


def test_focus_click_then_typing_elsewhere_drops_the_click():
    tl, aoi = scene()
    assert focus_only_click(tl, aoi, CLICK)
    k = kinds(tl, aoi)
    assert "click" not in k and "type" in k


def test_click_that_opened_something_is_kept():
    tl, aoi = scene(after_click_rect=(1200.0, 700.0, 1700.0, 1100.0))     # a dialog next to the pointer
    assert not focus_only_click(tl, aoi, CLICK)
    assert "click" in kinds(tl, aoi)


def test_click_without_typing_is_kept():
    tl, aoi = scene(keys=False, typing_at=None)
    assert not focus_only_click(tl, aoi, CLICK)
    assert kinds(tl, aoi) == ["click"]


def test_typing_right_where_you_clicked_keeps_the_click():
    tl, aoi = scene(typing_at=(1450.0, 1010.0))                           # clicked a field, typed in it
    assert not focus_only_click(tl, aoi, CLICK)
    assert "click" in kinds(tl, aoi)


def test_a_shortcut_after_the_click_is_not_typing():
    tl, aoi = scene(shortcut=True)
    assert not focus_only_click(tl, aoi, CLICK)


def test_turning_typing_zoom_off_keeps_clicks():
    tl, aoi = scene()
    cfg = ZoomConfig()
    cfg.follow_typing = False
    assert [a.kind for a in _aoi_actions(tl, cfg, aoi)] == ["click"]

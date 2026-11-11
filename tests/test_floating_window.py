"""The action is inside a floating (or small) window: the camera still gets close.

Ground truth, 10 Nov 2026 (Omarchy 4.5 in a VM, a 1100x760 floating terminal and browser on
a 1920x1200 screen): typing played at 1.29x and four clicks at 1.23x, the text unreadable.
Three causes, each tested here:
- the window that just opened (a whole-window subject) was grouped with the typing in it;
- a click far from the others was fitted into the same shallow box;
- a shot was planned for everything on screen during its span, including what the shot
  before it was about (so even a shot planned at 2.0x played at 1.36x).
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly.aoi import AOITrack  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.zoom import (Action, CameraGeometry, ZoomConfig, build_segments, camera_path,  # noqa: E402
                        cluster_actions, smooth_cursor, subject_track)

W, H = 1920, 1200
WIN = (362.0, 172.0, 1462.0, 932.0)             # a floating 1100x760 window


def track(spans, duration=30.0, line_h=16.0):
    """spans: [(t0, t1, kind, rect, focus)] on a 0.1 s grid."""
    t = np.round(np.arange(0, duration, 0.1), 3)
    rect, kind, foc = [None] * len(t), [None] * len(t), [None] * len(t)
    for i, ti in enumerate(t):
        for a, b, k, r, f in spans:
            if a <= ti < b:
                rect[i], kind[i], foc[i] = r, k, f
    return AOITrack(t, rect, kind, foc, [], line_h)


def timeline(events, duration=30.0):
    win = {"address": "0x1", "class": "foot", "title": "t", "rect": [WIN[0], WIN[1], WIN[2] - WIN[0], WIN[3] - WIN[1]],
           "floating": True, "fullscreen": False, "xwayland": False, "workspace": "1"}
    ev = [{"t": 0.0, "type": "move", "x": 1200.0, "y": 800.0},
          {"t": 0.02, "type": "clients", "focus": "0x1", "windows": [win]}] + list(events)
    return Timeline.from_dict({"video": {"w": W, "h": H, "fps": 60, "duration": duration},
                               "capture": {"cursor_baked": True, "scale": 1.0}, "events": ev})


def keys(t0, n, dt=0.08):
    return [{"t": round(t0 + dt * i, 3), "type": "key", "key": "a", "mods": []} for i in range(n)]


def typed(t0, t1, y=210.0):
    return (t0, t1, "typing", (362.0, y - 29, 600.0, y + 29), (530.0, y))


def zoom_at(tl, segs, aoi, t, cfg=None):
    cam = camera_path(tl, segs, CameraGeometry(W, H, 0, 0), 30, 0.0, tl.duration, cfg or ZoomConfig(), aoi=aoi)
    return float(cam.z[int(t * 30)])


# --------------------------------------------------------------- grouping actions into shots
def test_typing_is_not_grouped_with_the_window_it_is_in():
    cfg = ZoomConfig()
    window = Action(2.6, 912, 552, "scroll", WIN)
    line = [Action(4.3 + 0.1 * i, 480, 210, "type", (362, 181, 600, 239)) for i in range(20)]
    groups = cluster_actions([window] + line, W, H, cfg)
    assert [len(g) for g in groups] == [1, 20]
    # ... while the few lines a command prints stay with the command
    out = Action(6.4, 586, 414, "output", (362, 233, 811, 595))
    assert [len(g) for g in cluster_actions(line + [out], W, H, cfg)] == [21]


def test_a_click_far_from_the_others_gets_its_own_shot():
    cfg = ZoomConfig()

    def click(t, x, y):
        return Action(t, x, y, "click", (x - 150, y - 90, x + 150, y + 90))
    groups = cluster_actions([click(16.0, 577, 326), click(18.2, 577, 424), click(20.5, 577, 524),
                              click(22.9, 1085, 843)], W, H, cfg)
    assert [len(g) for g in groups] == [3, 1]


def test_the_cursor_only_engine_groups_as_before():
    """No rects (no screen analysis): two clicks 900 px apart, 1 s apart, were one shot."""
    acts = [Action(1.0, 300, 300, "click"), Action(2.0, 1200, 300, "click")]
    assert len(cluster_actions(acts, W, H, ZoomConfig())) == 1


# ------------------------------------------------------------------------- whole takes
def test_typing_in_a_floating_window_plays_close():
    tl = timeline(keys(4.3, 28) + keys(8.2, 22))
    aoi = track([(2.1, 3.4, "scroll", WIN, None), typed(4.3, 7.6), typed(8.2, 10.8, 262.0),
                 (11.0, 11.5, "output", (362.0, 233.0, 811.0, 595.0), None)])
    segs = build_segments(tl, ZoomConfig(), aoi)
    typing = [s for s in segs if "typing" in s.reason]
    assert typing and min(s.zoom for s in typing) >= 1.8, [(s.start, s.end, s.zoom, s.reason) for s in segs]
    assert zoom_at(tl, segs, aoi, 6.0) >= 1.8 and zoom_at(tl, segs, aoi, 9.5) >= 1.8


def test_a_window_still_settling_does_not_widen_the_typing_shot():
    """The terminal was still redrawing (a whole-window subject) when the first key came:
    the shot was planned at 2.0x and played at 1.36x for all ten seconds of typing."""
    tl = timeline(keys(8.2, 45))
    aoi = track([(6.4, 8.2, "scroll", WIN, None), typed(8.2, 12.6)])
    segs = build_segments(tl, ZoomConfig(), aoi)
    assert zoom_at(tl, segs, aoi, 10.5) >= 1.8, [(s.start, s.end, s.zoom) for s in segs]
    assert zoom_at(tl, segs, aoi, 7.0) <= 1.5          # the window opening is still a wider beat


def test_full_screen_typing_is_unchanged():
    full = (12.0, 38.0, 1908.0, 1188.0)
    tl = timeline(keys(4.3, 30))
    aoi = track([(2.1, 3.4, "scroll", full, None), (4.3, 7.0, "typing", (12.0, 1136.0, 400.0, 1195.0), (274.0, 1166.0))])
    segs = build_segments(tl, ZoomConfig(), aoi)
    assert [round(s.zoom, 2) for s in segs] == [2.0]


# ------------------------------------------------------------------------ what is framed
LOOK = 0.45          # the vertical crop asks for the subject this far ahead (it cancels the spring's lag)


def _subject_kinds(tl, aoi, times):
    st = np.arange(0, tl.duration, 1 / 50)
    cx, cy = smooth_cursor(tl, st, 0.05)
    kinds = subject_track(tl, aoi, st, cx, cy, ZoomConfig(), lookahead=LOOK).kind
    return [kinds[int(round(t * 50))] for t in times]


def test_an_idle_pointer_does_not_take_the_frame_back_from_a_dialog():
    """Click, a dialog opens 400 px away, the pointer rests where it clicked: once the
    dialog had had its moment the frame went back to the pointer and cut the dialog."""
    moves = [{"t": round(1.3 + 0.05 * i, 3), "type": "move", "x": 1200.0 - 47.92 * i, "y": 800.0 - 21.23 * i}
             for i in range(14)]                                        # arrives at 1.95 s
    click = [{"t": 2.0, "type": "click", "x": 577.0, "y": 524.0, "button": "left"}]
    dialog = (2.3, 3.3, "output", (684.0, 616.0, 1188.0, 868.0), None)
    aoi = track([(1.3, 2.3, "pointer", (467.0, 434.0, 687.0, 614.0), (577.0, 524.0)), dialog], 8.0)
    tl = timeline(moves + click, 8.0)
    assert _subject_kinds(tl, aoi, [2.2, 2.6]) == ["output", "output"]
    # the dialog's reading time is over, the pointer moved less than 1.5 s ago (before the dialog)
    assert _subject_kinds(tl, aoi, [3.36, 3.40, 3.44]) == [None, None, None]
    # once the pointer moves again it is a subject like before
    later = [{"t": round(3.9 + 0.05 * i, 3), "type": "move", "x": 577.0 + 40 * i, "y": 524.0 + 20 * i} for i in range(6)]
    tl2 = timeline(moves + click + later, 8.0)
    assert _subject_kinds(tl2, aoi, [4.3]) == ["cursor"]


# ------------------------------------------------------- a countdown recorded as pre-roll
def test_nothing_before_go_is_planned_for():
    """With the countdown recorded (capture["go"]), a window opened during it must not be
    grouped with the first typing of the take, and no shot starts before the take does."""
    from dolly.zoom import extract_actions, take_start
    ev = keys(1.0, 6) + keys(4.3, 28)                       # a few keys during the countdown too
    spans = [(2.1, 3.4, "scroll", WIN, None), typed(1.0, 1.6), typed(4.3, 7.6)]
    plain = timeline(ev)
    assert take_start(plain) == 0.0
    counted = timeline(ev)
    counted.capture["go"] = 4.0
    aoi = track(spans)
    assert take_start(counted) == 4.0
    assert min(a.t for a in extract_actions(counted, ZoomConfig(), aoi)) >= 4.0
    segs = build_segments(counted, ZoomConfig(), aoi)
    assert segs and segs[0].start >= 4.0 and segs[0].zoom >= 1.8, [(s.start, s.end, s.zoom) for s in segs]
    assert any(s.start < 4.0 for s in build_segments(plain, ZoomConfig(), aoi))     # without it: as before

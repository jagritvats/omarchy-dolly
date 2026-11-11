"""Window mode: typing inside the followed window still gets a close-up.

11 Nov 2026 (Omarchy 4.5 in a VM): a typing shot planned at 2.0x played at 1.07x in window
mode, on one monitor or two, and had done so since 0.5.0. While nothing happens the activity
track names the followed window itself, and a shot was sized to show everything named during
its span, so every shot was sized to the whole window. The followed window at rest is where
the camera returns to; it is not something a shot has to fit, follow or wait for.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly.aoi import AOITrack  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.zoom import (CameraGeometry, Segment, ZoomConfig, camera_path, smooth_cursor,  # noqa: E402
                        subject_track)

W, H = 1920, 1200
TILED = (12.0, 38.0, 1908.0, 1188.0)            # a tiled terminal: nearly the whole monitor
FLOAT = (362.0, 172.0, 1462.0, 932.0)           # a floating 1100x760 one
T0, T1 = 4.0, 5.5                               # the typing


def take(win, mode="window"):
    """A window sits still; a command is typed into its first line from T0 to T1."""
    line = (win[0], win[1] + 6, win[0] + 420, win[1] + 70)
    caret = (win[0] + 400, win[1] + 38)
    t = np.round(np.arange(0, 12, 0.1), 3)
    rect, kind, foc, idle = [], [], [], []
    for ti in t:
        if T0 <= ti < T1 + 1.0:
            rect.append(line); kind.append("typing"); foc.append(caret); idle.append(False)
        elif mode == "window":                   # at rest the track names the followed window
            rect.append(win); kind.append("layout"); foc.append(None); idle.append(True)
        else:
            rect.append(None); kind.append(None); foc.append(None); idle.append(False)
    aoi = AOITrack(t, rect, kind, foc, [], 20.0, idle)
    w = {"address": "0x1", "class": "foot", "title": "t", "rect": [win[0], win[1], win[2] - win[0], win[3] - win[1]],
         "floating": False, "fullscreen": False, "xwayland": False, "workspace": "1"}
    ev = [{"t": 0.0, "type": "move", "x": 960.0, "y": 613.0},
          {"t": 0.02, "type": "clients", "focus": "0x1", "windows": [w]}]
    ev += [{"t": round(T0 + 0.05 * i, 3), "type": "key", "key": "a", "mods": []} for i in range(int((T1 - T0) / 0.05))]
    cap = {"cursor_baked": True, "scale": 1.0, "mode": mode}
    if mode == "window":
        cap["target"] = "0x1"
    tl = Timeline.from_dict({"video": {"w": W, "h": H, "fps": 60, "duration": 12.0}, "capture": cap, "events": ev})
    seg = Segment(start=T0 - 0.75, end=T1 + 1.3, zoom=2.0, focus=None, follow=True, source="auto")
    return tl, aoi, [seg], line


def camera(tl, aoi, segs, win=None):
    base = (lambda t: win) if win is not None else None
    return camera_path(tl, segs, CameraGeometry(W, H, 0, 0), 30, 0.0, tl.duration, ZoomConfig(), aoi=aoi, base=base)


def test_typing_in_a_followed_tiled_window_gets_the_planned_close_up():
    tl, aoi, segs, line = take(TILED)
    cam = camera(tl, aoi, segs, TILED)
    i = int((T1 - 0.2) * 30)
    assert cam.z[i] > 1.9                        # it played at 1.07x
    vw, vh = W / cam.z[i], H / cam.z[i]          # ... on the typed line, not the middle of the window
    caret = (line[2] - 20, (line[1] + line[3]) / 2)
    assert cam.cx[i] - vw / 2 <= caret[0] <= cam.cx[i] + vw / 2
    assert cam.cy[i] - vh / 2 <= caret[1] <= cam.cy[i] + vh / 2
    assert cam.cx[i] - vw / 2 < line[0] + 60 and cam.cy[i] - vh / 2 < line[1] + 60    # from the start of the line


def test_window_mode_matches_screen_mode_for_the_same_typing():
    tl_w, aoi_w, segs, _ = take(TILED)
    tl_s, aoi_s, _, _ = take(TILED, mode="screen")
    w, s = camera(tl_w, aoi_w, segs, TILED), camera(tl_s, aoi_s, segs)
    for t in (T0 + 0.5, T1, T1 + 0.8):
        i = int(t * 30)
        assert abs(w.z[i] - s.z[i]) < 0.1 and abs(w.cx[i] - s.cx[i]) < 40 and abs(w.cy[i] - s.cy[i]) < 40


def test_a_small_followed_window_also_zooms_past_its_resting_frame():
    tl, aoi, segs, _ = take(FLOAT)
    cam = camera(tl, aoi, segs, FLOAT)
    rest = float(cam.z[int(1.0 * 30)])           # framing the 1100x760 window: about 1.58x
    assert 1.4 < rest < 1.7
    assert cam.z[int((T1 - 0.2) * 30)] > rest * 1.25


def test_the_camera_rests_on_the_window_again_afterwards():
    tl, aoi, segs, _ = take(FLOAT)
    cam = camera(tl, aoi, segs, FLOAT)
    i = int(11.0 * 30)
    assert abs(cam.z[i] - cam.z[int(1.0 * 30)]) < 0.05
    assert abs(cam.cx[i] - (FLOAT[0] + FLOAT[2]) / 2) < 12 and abs(cam.cy[i] - (FLOAT[1] + FLOAT[3]) / 2) < 12


def test_the_window_at_rest_is_marked_and_does_not_block_what_comes_next():
    tl, aoi, _, line = take(TILED)
    sim_t = np.round(np.arange(0, 12, 0.05), 3)
    cx, cy = smooth_cursor(tl, sim_t, 0.05)
    subj = subject_track(tl, aoi, sim_t, cx, cy, ZoomConfig())
    at = lambda t: int(round(t / 0.05))
    assert subj.idle[at(1.0)] and subj.kind[at(1.0)] == "layout"          # at rest: the window, marked
    assert subj.kind[at(T0 - 0.3)] == "typing" and not subj.idle[at(T0 - 0.3)]   # the typing is anticipated
    assert not subj.idle[at(T0 + 0.5)]
    assert subj.idle[at(11.0)]


def test_a_track_without_the_marks_behaves_as_before():
    """Projects analysed by an older Dolly, and tracks built by hand, carry no marks."""
    tl, aoi, segs, _ = take(TILED)
    old = AOITrack(aoi.t, aoi.rect, aoi.kind, aoi.focus, [], aoi.line_h)
    assert old.idle is None and old.idle_at(1.0) is False
    cam = camera(tl, old, segs, TILED)
    assert cam.z[int((T1 - 0.2) * 30)] < 1.3     # the old behaviour, unchanged for such tracks

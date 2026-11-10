"""Unit tests for the auto-zoom engine (pure logic, no ffmpeg/Hyprland)."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly.events import Timeline  # noqa: E402
from dolly.zoom import (CameraGeometry, Segment, ZoomConfig, build_segments, camera_path,  # noqa: E402
                        cluster_actions, extract_actions, _spring_step)

W, H = 2560, 1600


def tl_from(events, duration=20.0):
    return Timeline.from_dict({"video": {"w": W, "h": H, "fps": 60, "duration": duration}, "events": events})


def path_moves(points, t0=0.0, t1=1.0, n=60):
    """Linear cursor path through points over [t0, t1]."""
    out = []
    for i in range(n + 1):
        u = i / n
        seg = u * (len(points) - 1)
        k = min(int(seg), len(points) - 2)
        f = seg - k
        x = points[k][0] + (points[k + 1][0] - points[k][0]) * f
        y = points[k][1] + (points[k + 1][1] - points[k][1]) * f
        out.append({"t": t0 + (t1 - t0) * u, "type": "move", "x": x, "y": y})
    return out


def click(t, x, y):
    return {"t": t, "type": "click", "x": x, "y": y, "button": "left"}


def key(t, k, mods=()):
    return {"t": t, "type": "key", "key": k, "mods": list(mods)}


# ------------------------------------------------------------------ segments
def test_no_activity_no_segments():
    tl = tl_from(path_moves([(100, 100), (2000, 1200)], 1, 3))
    assert build_segments(tl) == []


def test_single_click_makes_one_segment_with_lead_and_min_length():
    tl = tl_from(path_moves([(100, 100), (800, 600)], 0, 2) + [click(5.0, 800, 600)])
    segs = build_segments(tl)
    assert len(segs) == 1
    s = segs[0]
    cfg = ZoomConfig()
    assert s.start == pytest.approx(5.0 - cfg.lead)
    assert s.end - s.start >= cfg.min_segment
    assert cfg.zoom_min <= s.zoom <= cfg.zoom_max
    assert s.reason == "click"


def test_nearby_clicks_cluster_far_clicks_split():
    ev = [click(2.0, 800, 600), click(3.0, 900, 640), click(4.0, 850, 700)]   # close in time+space
    ev += [click(12.0, 2300, 1400)]                                           # far later
    segs = build_segments(tl_from(ev))
    assert len(segs) == 2
    assert segs[0].reason == "click x3"


def test_clicks_far_apart_in_space_do_not_merge_even_if_close_in_time():
    ev = [click(2.0, 200, 200), click(2.8, 2400, 1450)]
    acts = extract_actions(tl_from(ev), ZoomConfig())
    cl = cluster_actions(acts, W, H, ZoomConfig())
    assert len(cl) == 2


def test_big_cluster_lowers_zoom_but_not_below_min():
    cfg = ZoomConfig()
    ev = [click(2.0, 700, 500), click(2.5, 1500, 1000)]
    segs = build_segments(tl_from(ev), cfg)
    assert len(segs) == 1
    assert segs[0].zoom < cfg.zoom_default
    assert segs[0].zoom >= cfg.zoom_min


def test_typing_anchors_to_clicked_field_and_extends_segment():
    ev = [click(2.0, 1000, 500)] + path_moves([(1000, 500), (1040, 530)], 2.2, 2.5)
    ev += [key(3.0 + 0.15 * i, "a") for i in range(30)]        # typing until 7.35
    segs = build_segments(tl_from(ev))
    assert len(segs) == 1
    assert segs[0].end >= 7.35 + ZoomConfig().hold_typing - 1e-6
    assert "typing" in segs[0].reason


def test_shortcuts_do_not_count_as_typing():
    ev = [key(3.0, "s", ["ctrl"]), key(4.0, "c", ["ctrl"])]
    assert build_segments(tl_from(ev)) == []


def test_segments_never_overlap_and_are_sorted():
    rng = np.random.default_rng(1)
    ev = [click(float(t), float(rng.uniform(0, W)), float(rng.uniform(0, H))) for t in np.sort(rng.uniform(0, 60, 25))]
    segs = build_segments(tl_from(ev, 62))
    for a, b in zip(segs, segs[1:]):
        assert a.end <= b.start + 1e-9
    assert all(s.end > s.start for s in segs)


def test_idle_cursor_ends_segment_early():
    cfg = ZoomConfig(hold_click=10.0, idle_zoom_out=1.0)
    ev = [click(2.0, 800, 600)]
    segs = build_segments(tl_from(ev, 20), cfg)
    assert segs[0].end < 2.0 + 10.0
    assert segs[0].end == pytest.approx(2.0 + 1.0, abs=0.1)


def test_cursor_jump_ends_segment_early():
    cfg = ZoomConfig(hold_click=6.0)
    ev = [click(2.0, 500, 400)] + path_moves([(500, 400), (2400, 1500)], 2.5, 2.9)
    segs = build_segments(tl_from(ev, 20), cfg)
    assert segs[0].end < 3.2


def test_far_apart_consecutive_segments_get_a_zoom_out_gap():
    cfg = ZoomConfig()
    ev = [click(2.0, 300, 300), click(3.6, 2300, 1300)]
    segs = build_segments(tl_from(ev), cfg)
    assert len(segs) == 2
    assert segs[1].start - segs[0].end >= cfg.dip_window - 1e-6


def test_dwell_detection_when_no_click_source():
    ev = path_moves([(100, 100), (1200, 800)], 1.0, 1.5) + path_moves([(1200, 800), (1210, 805)], 3.5, 3.6)
    tl = tl_from(ev)
    tl.capture["click_source"] = "none"
    segs = build_segments(tl)
    assert len(segs) >= 1
    assert segs[0].reason == "dwell"


def test_segment_roundtrip():
    s = Segment(1.234567, 4.5, 1.8, [10.123, 20.987], True, "manual", "", "m1")
    s2 = Segment.from_dict(s.to_dict())
    assert s2.start == pytest.approx(1.235) and s2.focus == [10.1, 21.0] and s2.source == "manual"


# ------------------------------------------------------------------ springs
def test_spring_is_critically_damped_no_overshoot():
    x, v = 0.0, 0.0
    xs = []
    for _ in range(2000):
        x, v = _spring_step(x, v, 1.0, 5.0, 1 / 240)
        xs.append(x)
    xs = np.array(xs)
    assert xs.max() <= 1.0 + 1e-9                     # never overshoots
    assert np.all(np.diff(xs) >= -1e-12)              # monotonic
    assert xs[-1] == pytest.approx(1.0, abs=1e-4)


def demo_timeline():
    ev = path_moves([(1800, 1200), (2380, 210)], 0.5, 1.6)
    ev += [click(2.3, 2380, 210)]
    ev += path_moves([(2380, 210), (1600, 440)], 2.6, 3.2)
    ev += [click(3.6, 1600, 440)] + [key(4.0 + 0.12 * i, "a") for i in range(20)]
    ev += path_moves([(1600, 440), (460, 340)], 9.0, 9.7) + [click(10.0, 460, 340)]
    return tl_from(ev, 16)


def test_camera_path_is_smooth_bounded_and_returns_to_full_view():
    tl = demo_timeline()
    segs = build_segments(tl)
    geom = CameraGeometry(W, H, 0.06 * W, 0.06 * W)
    fps = 60
    p = camera_path(tl, segs, geom, fps, 0, tl.duration)
    assert len(p.z) == int(round(tl.duration * fps))
    assert p.z.min() >= 1.0 - 1e-6
    assert p.z.max() <= ZoomConfig().zoom_max + 1e-6
    assert p.z[0] == pytest.approx(1.0, abs=1e-3)
    assert p.z[-1] < 1.05                             # zoomed back out at the end
    # smoothness: bounded zoom speed and no jitter (velocity sign flips) in pan
    dlogz = np.abs(np.diff(np.log(p.z))) * fps
    assert dlogz.max() < 2.0                          # < ~2 e-folds per second
    k = 1920 * p.z / geom.CW
    pan = np.hypot(np.diff(p.cx), np.diff(p.cy)) * k[1:]
    assert pan.max() < 40                             # px per frame at 1080p
    vx = np.diff(p.cx)
    flips = np.sum((np.sign(vx[1:]) != np.sign(vx[:-1])) & (np.abs(vx[1:]) > 0.5) & (np.abs(vx[:-1]) > 0.5))
    assert flips <= 2 * len(segs) + 2


def test_viewport_stays_inside_canvas():
    tl = demo_timeline()
    segs = build_segments(tl)
    geom = CameraGeometry(W, H, 150, 150)
    p = camera_path(tl, segs, geom, 30, 0, tl.duration)
    vw = geom.CW / p.z
    vh = geom.CH / p.z
    assert np.all(p.cx - vw / 2 >= -geom.px - 1.0)
    assert np.all(p.cx + vw / 2 <= geom.W + geom.px + 1.0)
    assert np.all(p.cy - vh / 2 >= -geom.py - 1.0)
    assert np.all(p.cy + vh / 2 <= geom.H + geom.py + 1.0)


def test_cursor_kept_in_view_while_zoomed():
    tl = demo_timeline()
    segs = build_segments(tl)
    geom = CameraGeometry(W, H, 0, 0)
    fps = 60
    p = camera_path(tl, segs, geom, fps, 0, tl.duration)
    t = np.arange(len(p.z)) / fps
    vw, vh = geom.CW / p.z, geom.CH / p.z
    inside = (np.abs(p.cur_x - p.cx) <= vw / 2) & (np.abs(p.cur_y - p.cy) <= vh / 2)
    # allow brief excursions during fast moves (spring catching up)
    assert inside.mean() > 0.97


def test_path_is_deterministic():
    tl = demo_timeline()
    segs = build_segments(tl)
    geom = CameraGeometry(W, H, 100, 100)
    a = camera_path(tl, segs, geom, 30, 0, tl.duration)
    b = camera_path(tl, segs, geom, 30, 0, tl.duration)
    assert np.array_equal(a.z, b.z) and np.array_equal(a.cx, b.cx)


def test_trimmed_path_matches_full_path():
    """Exporting a trimmed range gives the same camera as the full timeline (settled state)."""
    tl = demo_timeline()
    segs = build_segments(tl)
    geom = CameraGeometry(W, H, 100, 100)
    full = camera_path(tl, segs, geom, 30, 0, tl.duration)
    part = camera_path(tl, segs, geom, 30, 6.0, 10.0)
    i0 = 6 * 30
    assert np.allclose(part.z, full.z[i0:i0 + len(part.z)], atol=2e-3)
    assert np.allclose(part.cx, full.cx[i0:i0 + len(part.cx)], atol=1.5)


def test_manual_fixed_focus_is_respected():
    tl = tl_from(path_moves([(100, 100), (2400, 1500)], 0, 10, 200), 12)
    seg = Segment(2.0, 8.0, 2.0, [600.0, 500.0], False, "manual")
    geom = CameraGeometry(W, H, 0, 0)
    p = camera_path(tl, [seg], geom, 30, 0, 12)
    i = int(6.0 * 30)
    assert p.z[i] == pytest.approx(2.0, abs=0.02)
    assert p.cx[i] == pytest.approx(640.0, abs=45)    # clamped so the viewport stays on screen
    assert p.cy[i] == pytest.approx(500.0, abs=45)


# ------------------------------------------------- glide between nearby inputs (0.4.1)
def panel_walk(pause=4.5, x=2200, ys=(250, 450, 650, 850, 1050), wander=None):
    """A settings panel on the right: the pointer goes down it, clicking one input after
    another with a reading pause between clicks (like the walkthrough)."""
    ev, t = path_moves([(1200, 800), (x, ys[0])], 0.0, 1.0), 1.5
    ev.append(click(t, x, ys[0]))
    for y0, y1 in zip(ys, ys[1:]):
        if wander:
            ev += path_moves([(x, y0), wander, (x, y1)], t + 0.6, t + pause - 0.4, 90)
        else:
            ev += path_moves([(x, y0), (x, y1)], t + pause - 1.2, t + pause - 0.3, 40)
        t += pause
        ev.append(click(t, x, y1))
    return tl_from(ev, t + 6.0), t


def _zoom_during(tl, cfg, t0, t1, fps=30):
    segs = build_segments(tl, cfg)
    p = camera_path(tl, segs, CameraGeometry(W, H, 0, 0), fps, 0, tl.duration, cfg)
    i0, i1 = int(t0 * fps), int(t1 * fps)
    return p, p.z[i0:i1]


def test_glide_stays_zoomed_and_scrolls_between_nearby_inputs():
    tl, t_last = panel_walk()
    cfg = ZoomConfig()
    p, z = _zoom_during(tl, cfg, 2.0, t_last)
    assert z.min() > 1.6, f"zoomed out between inputs (min zoom {z.min():.2f})"
    # the camera scrolled down the panel with the clicks: every clicked input is in view
    fps = 30
    for c in tl.clicks:
        i = int(c["t"] * fps)
        assert abs(c["y"] - p.cy[i]) < (H / p.z[i]) / 2 * 0.9 and abs(c["x"] - p.cx[i]) < (W / p.z[i]) / 2 * 0.9
    assert p.cy[int(t_last * fps)] - p.cy[int(2.0 * fps)] > 250
    # calm: never faster than the scenario cap (1.6 viewport widths/s), no hunting
    vw = W / p.z
    assert (np.abs(np.diff(p.cy)) * fps / vw[1:]).max() < 1.6
    vy = np.diff(p.cy)
    flips = np.sum((np.sign(vy[1:]) != np.sign(vy[:-1])) & (np.abs(vy[1:]) > 0.5) & (np.abs(vy[:-1]) > 0.5))
    assert flips <= 2


def test_zoom_out_between_is_still_available():
    tl, t_last = panel_walk()
    _, z = _zoom_during(tl, ZoomConfig(glide=False), 2.0, t_last)
    assert z.min() < 1.1


def test_long_pause_or_far_jump_pulls_back():
    tl, t_last = panel_walk(pause=9.0)          # longer than stay_gap
    _, z = _zoom_during(tl, ZoomConfig(), 2.0, t_last)
    assert z.min() < 1.1
    ev = [click(2.0, 200, 200), click(6.0, 2400, 1450)]     # opposite corners
    tl = tl_from(path_moves([(200, 200), (2400, 1450)], 4.0, 5.5) + ev, 10)
    _, z = _zoom_during(tl, ZoomConfig(), 2.0, 6.0)
    assert z.min() < 1.1


def test_pointer_wandering_off_in_the_pause_pulls_back():
    tl, t_last = panel_walk(wander=(150, 1500))
    _, z = _zoom_during(tl, ZoomConfig(), 2.0, t_last)
    assert z.min() < 1.25


def test_auto_zoom_off_and_triggers():
    tl = tl_from(path_moves([(100, 100), (800, 600)], 0, 2) + [click(3.0, 800, 600)]
                 + [key(6.0 + i * 0.15, c) for i, c in enumerate("hello")]
                 + [{"t": 9.0, "type": "mark", "x": 1500, "y": 900}], 14)
    assert build_segments(tl, ZoomConfig(enabled=False)) and \
        all(s.reason == "marked" for s in build_segments(tl, ZoomConfig(enabled=False)))
    no_clicks = build_segments(tl, ZoomConfig(zoom_clicks=False))
    assert not any("click" in s.reason for s in no_clicks)
    assert any("typing" in s.reason for s in no_clicks)
    no_typing = build_segments(tl, ZoomConfig(follow_typing=False))
    assert not any("typing" in s.reason for s in no_typing)
    assert any("click" in s.reason for s in no_typing)


def test_zoom_level_and_motion_settings():
    tl = tl_from(path_moves([(100, 100), (800, 600)], 0, 2) + [click(3.0, 800, 600)], 8)
    assert build_segments(tl, ZoomConfig(zoom_default=1.5))[0].zoom == pytest.approx(1.5)
    assert build_segments(tl, ZoomConfig(zoom_default=3.0, zoom_max=3.0))[0].zoom == pytest.approx(3.0)
    segs = build_segments(tl)
    geom = CameraGeometry(W, H, 0, 0)
    calm = camera_path(tl, segs, geom, 30, 0, 8, ZoomConfig(motion="calm"))
    quick = camera_path(tl, segs, geom, 30, 0, 8, ZoomConfig(motion="quick"))
    i = int(2.6 * 30)       # zooming in toward the click
    assert quick.z[i] > calm.z[i]


def test_trimmed_path_matches_full_path_inside_a_glide():
    tl, t_last = panel_walk()
    cfg = ZoomConfig()
    segs = build_segments(tl, cfg)
    geom = CameraGeometry(W, H, 100, 100)
    full = camera_path(tl, segs, geom, 30, 0, tl.duration, cfg)
    t0 = t_last - 1.0
    part = camera_path(tl, segs, geom, 30, t0, t0 + 3.0, cfg)
    i0 = int(round(t0 * 30))
    n = len(part.z) - 10        # the last few frames see the end of the simulated window (as before)
    assert np.allclose(part.z[:n], full.z[i0:i0 + n], atol=2e-3)
    assert np.allclose(part.cy[:n], full.cy[i0:i0 + n], atol=1.5)


# ------------------------------------------- glide across windows (focus, dialogs, layout)
def clients(t, wins, focus):
    """A window snapshot: wins = [(address, x, y, w, h, floating)]."""
    return {"t": t, "type": "clients", "focus": focus,
            "windows": [{"address": a, "class": a, "title": "", "rect": [x, y, w, h], "floating": fl,
                         "fullscreen": False, "workspace": "1"} for a, x, y, w, h, fl in wins]}


TILED = [("A", 0, 0, 1280, 1600, False), ("B", 1280, 0, 1280, 1600, False)]


def _glide_flags(tl, cfg=None):
    from dolly.zoom import auto_focus, glides
    cfg = cfg or ZoomConfig()
    segs = build_segments(tl, cfg)
    return [glides(tl, a, b, auto_focus(tl, a, cfg), auto_focus(tl, b, cfg), cfg) for a, b in zip(segs, segs[1:])]


def two_clicks(p, q, extra=(), t2=5.5, via=None):
    ev = [clients(0.0, TILED, "A")] + path_moves([(1200, 800), p], 0.0, 1.0) + [click(1.5, *p)]
    ev += path_moves([p, via, q] if via else [p, q], 3.0, t2 - 0.5, 60) + [click(t2, *q)] + list(extra)
    return tl_from(ev, t2 + 5)


def test_switching_to_another_open_window_pulls_back_unless_the_hop_is_short():
    assert _glide_flags(two_clicks((300, 400), (700, 1300))) == [True]          # same window, far down: glide
    assert _glide_flags(two_clicks((1100, 400), (1500, 1300))) == [False]       # into the other app: pull back
    assert _glide_flags(two_clicks((1200, 500), (1360, 560), t2=4.2)) == [True]  # just across, quickly: glide
    assert _glide_flags(two_clicks((1200, 500), (1360, 560), t2=6.5)) == [False] # ... after a long look: pull back


def test_dialog_opened_by_the_click_glides_but_a_new_tiled_window_pulls_back():
    dialog = [clients(2.0, TILED + [("D", 700, 400, 500, 300, True)], "D")]
    assert _glide_flags(two_clicks((600, 500), (900, 650), dialog)) == [True]
    big = [clients(2.0, TILED + [("S", 240, 180, 1900, 1200, True)], "S")]      # scratchpad-sized popup
    assert _glide_flags(two_clicks((600, 500), (900, 650), big)) == [False]
    relayout = [clients(2.0, [("A", 0, 0, 1280, 800, False), ("B", 1280, 0, 1280, 1600, False),
                              ("N", 0, 800, 1280, 800, False)], "N")]
    assert _glide_flags(two_clicks((600, 500), (600, 1100), relayout)) == [False]


def test_brief_trip_to_another_monitor_keeps_the_glide():
    # the pointer pops over to a monitor on the left for ~0.6 s in the pause, then comes back
    brief = two_clicks((300, 400), (500, 900), via=(-400, 600), t2=4.8)
    assert _glide_flags(brief) == [True]
    ev = [clients(0.0, TILED, "A")] + path_moves([(1200, 800), (300, 400)], 0.0, 1.0) + [click(1.5, 300, 400)]
    ev += path_moves([(300, 400), (-400, 600)], 2.0, 2.3, 10) + path_moves([(-400, 600), (-420, 640)], 2.3, 5.0, 30)
    ev += path_moves([(-420, 640), (500, 900)], 5.0, 5.4, 10) + [click(5.6, 500, 900)]
    assert _glide_flags(tl_from(ev, 10)) == [False]                              # stayed away ~3 s

"""Fractional scaling, transforms and multi-monitor targets (round 4, part A).

* effective scale / logical size match what Hyprland really uses (1/120 grid,
  clean divisor), for both the float repr hyprctl prints now (1.6666666) and the
  2-decimal form older releases printed (1.67); Omarchy's floor math is 1-4 px
  short at some scales, so a whole-monitor pick is matched with tolerance
* transforms 1/3/5/7 swap width/height (Omarchy swaps only 1/3)
* the target monitor is the one holding the pick, not the focused one; a pick
  spanning monitors is clamped to the one holding most of it, refused when none does
* `monitor:NAME` -> gsr `-w NAME -s RES`, wf `-o NAME`; regions -> `-w WxH+X+Y` / `-g "X,Y WxH"`
* end to end at scales 1, 1.25, 1.5, 1.6667, 2 and a rotated monitor at a
  negative offset: a click logged in global logical coordinates lands on the
  target drawn in a physical-resolution video within 1 px in video space and in
  the composited export; the cursor size, the zoom segment and the click
  reaction (AOI) agree; XWayland clients map the same with zero-scaling on/off
* a followed window dragged to another monitor: brief excursions are kept,
  a lasting move ends the recording and finalize trims to the moment it left
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dolly import gfx  # noqa: E402
from dolly import monitors as M  # noqa: E402


def mon(name, w, h, scale, x=0, y=0, transform=0, focused=False):
    return {"name": name, "width": w, "height": h, "scale": scale, "x": x, "y": y, "transform": transform,
            "focused": focused}


# ------------------------------------------------------------------ scale math
@pytest.mark.parametrize("w,h,printed,exact,logical", [
    (2560, 1600, 1.6666666, 5 / 3, (1536, 960)),
    (2560, 1600, 1.67, 5 / 3, (1536, 960)),          # older hyprctl: 2 decimals
    (2560, 1600, 1.3333334, 4 / 3, (1920, 1200)),
    (1920, 1200, 1.25, 1.25, (1536, 960)),
    (2880, 1800, 1.5, 1.5, (1920, 1200)),
    (3840, 2160, 2.0, 2.0, (1920, 1080)),
    (1920, 1080, 1.0, 1.0, (1920, 1080)),
])
def test_effective_scale_and_logical_size(w, h, printed, exact, logical):
    m = mon("DP-1", w, h, printed)
    assert M.effective_scale(m) == pytest.approx(exact, abs=1e-9)
    assert tuple(M.logical_rect(m)[2:]) == logical


def test_omarchy_floor_is_short_but_matched_with_tolerance():
    m = mon("DP-1", 2560, 1600, 1.3333334)
    assert M.omarchy_geo(m)[2:] == [1919, 1199]            # floor(2560/1.3333334)
    assert M.logical_rect(m)[2:] == [1920, 1200]
    m2 = mon("DP-2", 2560, 1600, 1.67)
    assert M.omarchy_geo(m2)[2:] == [1532, 958]
    # a slurp pick of the whole monitor (real logical size) -> the monitor itself
    t = M.resolve_pick("0,0 1536x960", [m2])
    assert t.kind == "monitor" and t.monitor == "DP-2"
    t = M.resolve_pick("0,0 1919x1199", [m])                # what Omarchy's own rect hint gives
    assert t.kind == "monitor"


@pytest.mark.parametrize("transform,swapped,omarchy_swaps", [(0, False, False), (1, True, True), (2, False, False),
                                                              (3, True, True), (4, False, False), (5, True, False),
                                                              (6, False, False), (7, True, False)])
def test_transform_swap(transform, swapped, omarchy_swaps):
    m = mon("HDMI-A-1", 1920, 1080, 1.5, transform=transform)
    r = M.logical_rect(m)
    assert (r[2], r[3]) == ((720, 1280) if swapped else (1280, 720))
    o = M.omarchy_geo(m)
    assert (o[2] < o[3]) == omarchy_swaps
    assert M.physical_size(m) == ((1080, 1920) if swapped else (1920, 1080))


# ------------------------------------------------------------------ targets
MONS = [mon("eDP-1", 2560, 1600, 1.6666666, 0, 0, focused=True),          # 1536x960
        mon("DP-1", 1920, 1200, 1.25, 1536, 0),                           # 1536x960 at x=1536
        mon("DP-2", 960, 600, 1.5, -400, 0, transform=1)]                 # rotated: 400x640 at x=-400


def test_region_on_unfocused_monitor_names_that_monitor():
    t = M.resolve_pick("1700,100 600x400", MONS)
    assert t.kind == "region" and t.monitor == "DP-1" and t.scale == 1.25
    assert t.gsr_args() == ["-w", "600x400+1700+100"]
    assert t.wf_args() == ["-g", "1700,100 600x400"]
    assert t.expected_video() == (750, 500)


def test_monitor_pick_records_by_name():
    t = M.resolve_pick("monitor:DP-1", MONS)
    assert t.kind == "monitor" and t.region == [1536, 0, 1536, 960]
    assert t.gsr_args() == ["-w", "DP-1", "-s", "0x0"] and t.wf_args() == ["-o", "DP-1"]
    big = M.monitor_target(mon("DP-9", 5120, 2880, 2.0))
    assert big.gsr_args() == ["-w", "DP-9", "-s", "3840x2160"]
    rot = M.resolve_pick("monitor:DP-2", MONS)
    assert rot.region == [-400, 0, 400, 640] and rot.physical == (600, 960)


def test_spanning_region_is_clamped_or_refused():
    t = M.resolve_pick("1336,100 600x400", MONS)        # 200 px on eDP-1, 400 px on DP-1
    assert t.monitor == "DP-1" and t.region == [1536, 100, 400, 400] and "DP-1" in t.note
    with pytest.raises(M.SpanError):
        # a 3-way split: no monitor holds half of it
        M.resolve_pick("-300,0 2200x500", [mon("A", 800, 600, 1.0, -400, 0), mon("B", 800, 600, 1.0, 400, 0),
                                           mon("C", 800, 600, 1.0, 1200, 0)])
    with pytest.raises(M.SpanError):
        M.resolve_pick("5000,5000 100x100", MONS)


def test_pick_target_uses_the_pick_not_the_focused_monitor(monkeypatch):
    from dolly import capture
    monkeypatch.setattr(capture, "notify", lambda *a, **k: None)
    t = capture.pick_target("region", pick="1700,100 600x400", mons=MONS)
    assert t.monitor == "DP-1"
    t = capture.pick_target("screen", mons=MONS)
    assert t.monitor == "eDP-1" and t.kind == "monitor"
    t = capture.pick_target("region", pick="monitor:DP-2", mons=MONS)
    assert t.kind == "monitor" and t.monitor == "DP-2"
    cmd = capture.recorder_cmd("gsr", t, Path("/tmp/x.mkv"), 60, [])
    assert cmd[1:5] == ["-w", "DP-2", "-s", "0x0"]
    cmd = capture.recorder_cmd("wf", t, Path("/tmp/x.mkv"), 60, [])
    assert cmd[1:3] == ["-o", "DP-2"]


# ------------------------------------------------------------------ end to end
CASES = {
    "s1": (mon("DP-1", 800, 500, 1.0, 0, 0), (610, 330)),
    "s1.25": (mon("DP-1", 1280, 800, 1.25, 1920, 0), (1920 + 700, 420)),
    "s1.5": (mon("DP-1", 1440, 900, 1.5, 0, 1080), (733, 1080 + 377)),
    "s1.6667": (mon("DP-1", 1280, 800, 1.6666666, 0, 0), (517, 291)),
    "s2": (mon("DP-1", 1280, 800, 2.0, 0, 0), (451, 133)),
    "rot-neg": (mon("DP-2", 960, 600, 1.5, -400, -120, transform=1), (-400 + 263, -120 + 455)),
}
TARGET = (70, 45)    # logical px half-size of the drawn target (a small dialog)


def _write_video(path, W, H, fps, n, draw):
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr0", "-s", f"{W}x{H}",
                          "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "8",
                          "-pix_fmt", "yuv444p", "-g", "10", str(path)], stdin=subprocess.PIPE)
    for i in range(n):
        p.stdin.write(gfx.data(draw(i)))
    p.stdin.close()
    assert p.wait() == 0


def _red_centre(picture):
    """Middle of the clearly red pixels of a frame: (x, y), or (None, None) without any."""
    w, h = picture.get_width(), picture.get_height()
    above = bytes(v > 150 for v in range(256))
    below = bytes(v < 120 for v in range(256))
    r, g, b = (int.from_bytes(gfx.plane(picture, ch).translate(table), "little")
               for ch, table in (("r", above), ("g", below), ("b", below)))
    red = (r & g & b).to_bytes(w * h, "little")              # 1 where red, row after row
    n = sx = sy = 0
    for y in range(h):
        row = red[y * w:(y + 1) * w]
        k = row.count(1)
        if k:
            n, sy = n + k, sy + k * y
            sx += sum(x for x, on in enumerate(row) if on)
    return (sx / n, sy / n) if n else (None, None)


@pytest.mark.parametrize("case", list(CASES))
def test_click_lands_on_target_end_to_end(tmp_path, case):
    from dolly import capture
    from dolly.events import Timeline
    from dolly.project import Project
    from dolly.render import Compositor, make_layout
    from dolly.zoom import ZoomConfig

    m, (gx, gy) = CASES[case]
    tgt = M.monitor_target(m)
    W, H = tgt.physical
    s = tgt.scale
    lx, ly = tgt.region[0], tgt.region[1]
    # target centre in physical video px (what the compositor shows), sub-pixel exact
    px, py = (gx - lx) * s, (gy - ly) * s
    fps, n, t_click = 20, 50, 1.0
    hx, hy = TARGET[0] * s, TARGET[1] * s

    def grey(v):
        return gfx.rgb((v, v, v))

    def draw(i):
        img = gfx.new(W, H)
        gfx.fill_rect(img, 0, 0, W, H, gfx.rgb((40, 44, 40)))
        if i / fps >= t_click + 0.1:          # the click opens something: a red target appears
            c = gfx.context(img)
            c.rectangle(px - hx, py - hy, 2 * hx, 2 * hy)
            c.clip()
            # a shaded panel, not a flat dim: 90 in the middle, 50 more at its right edge and
            # 25 more at its lower one (the gradient runs 75 levels along (50 / hx, 25 / hy))
            gx, gy = 50 / hx, 25 / hy
            reach = 75 / (gx * gx + gy * gy)
            c.set_source(gfx.linear_gradient(px - gx * reach, py - gy * reach, px + gx * reach, py + gy * reach,
                                             [(0, grey(15)), (1, grey(165))]))
            c.paint()
            gfx.fill_rect(c, px - hx, py - hy, 2 * hx, 14 * s, gfx.rgb((205, 200, 200)))    # title bar
            band = max(1, int(6 * s))
            for y in range(int(py - hy) // (3 * band) * 3 * band, int(py + hy) + 1, 3 * band):
                gfx.fill_rect(c, px - hx, y, 1.2 * hx, band, grey(150))                      # text rows
            c.reset_clip()
            gfx.fill_rect(img, px - 16 * s, py - 9 * s, 32 * s, 18 * s, gfx.rgb((230, 40, 40)))   # the button that was clicked
        return img

    pdir = tmp_path / "p"
    pdir.mkdir()
    _write_video(pdir / "raw.mkv", W, H, fps, n, draw)
    meta = {"backend": "gsr", "region": tgt.region, "scale": s, "monitor": tgt.monitor, "fps": fps,
            "target_info": tgt.to_meta(), "mode": "screen", "cursor_size": 24, "xwayland_zero_scaling": True}
    (pdir / "capture.json").write_text(json.dumps(meta))
    ev = [{"ms": 0, "type": "info", "click_source": "lua"}]
    for k in range(20):                       # pointer approaches the target, then clicks
        ev.append({"ms": 400 + k * 25, "type": "move", "x": gx - 60 + 3 * k, "y": gy - 30 + 1.5 * k})
    ev.append({"ms": 1000, "type": "click", "x": gx, "y": gy, "button": "left"})
    ev.append({"ms": 1000, "type": "clients", "focus": "0xa", "windows": [
        {"address": "0xa", "class": "steam", "title": "x", "xwayland": True, "floating": False, "fullscreen": False,
         "rect": [gx - 100, gy - 80, 300, 200], "workspace": "1"}]})
    (pdir / "events.raw.jsonl").write_text("\n".join(json.dumps(e) for e in ev) + "\n")
    doc = capture.finalize(pdir)

    # 1. video space: the logged click is on the drawn target (within 1 px)
    tl = Timeline.load(pdir / "events.json")
    c = tl.clicks[0]
    assert abs(c["x"] - px) <= 1.0 and abs(c["y"] - py) <= 1.0, (c, px, py)
    # the XWayland window rect maps with the same factors (zero-scaling only changes its content)
    wr = doc["events"][-1]["windows"][0]["rect"] if doc["events"][-1]["type"] == "clients" else \
        next(e for e in doc["events"] if e["type"] == "clients")["windows"][0]["rect"]
    assert wr[0] == pytest.approx((gx - 100 - lx) * s, abs=1.0) and wr[2] == pytest.approx(300 * s, abs=1.0)
    # 2. cursor size: theme size x monitor scale (video px)
    assert doc["capture"]["scale"] == pytest.approx(s, rel=2e-3)
    lay = make_layout(tl.w, tl.h, Project(pdir).load()["style"], tl.capture)
    assert lay.cursor_base == pytest.approx(24 * 1.04 * s, rel=3e-3)
    # 3. the zoom segment and the click reaction (AOI) frame the target
    data = Project(pdir).load()
    seg = next((g for g in data["segments"] if g["start"] <= t_click <= g["end"]), None)
    assert seg is not None, data["segments"]
    from dolly import aoi as aoimod
    act = aoimod.cached_activity(pdir, tl)
    rx = next(r for tc, r in act.reactions if r is not None)
    x0, y0, x1, y1 = rx
    assert x0 - 2 <= px <= x1 + 2 and y0 - 2 <= py <= y1 + 2
    # 4. composited export: the target and the click map to the same output pixel
    style = data["style"]
    style["cursor"]["enabled"] = False
    style["cursor"]["ripple"] = False
    style["keys"]["enabled"] = False
    from dolly.zoom import Segment
    segs = [Segment.from_dict(g) for g in data["segments"]]
    comp = Compositor(tl, style, segs, ZoomConfig.from_dict(data.get("zoom_config")), 0.0, n / fps)
    ts = 1.6
    out = comp.compose_at(ts, draw(int(ts * fps)))
    z, k, vx0, vy0 = comp.affine_at(ts)
    ex, ey = (c["x"] - vx0) * k, (c["y"] - vy0) * k
    cx, cy = _red_centre(out)
    assert cx is not None
    assert abs(cx - ex) <= 1.0 * max(1.0, k) and abs(cy - ey) <= 1.0 * max(1.0, k), (cx, cy, ex, ey, k)


# ------------------------------------------------------------------ leaving the monitor
def test_window_left_monitor_trim_and_logger(monkeypatch):
    from dolly import capture
    from dolly.logger import window_on_region
    region = [0, 0, 1000, 600]
    assert window_on_region([100, 100, 400, 300], region)
    assert window_on_region([800, 100, 400, 300], region)          # half on: still "on"
    assert not window_on_region([900, 100, 400, 300], region)
    # real Timeline snapshots (window rects become corner boxes there)
    from dolly.events import Timeline
    evs = []
    for i in range(30):
        t = i * 0.1
        x = 100 if t < 1.0 else (1200 if t < 1.4 else (100 if t < 2.0 else 1200))   # brief trip, back, then away
        evs.append({"t": t, "type": "window", "address": "0xa", "rect": [x, 100, 400, 300]})
    tl = Timeline.from_dict({"video": {"w": 1000, "h": 600, "fps": 30, "duration": 3.0}, "events": evs})
    assert tl.snapshots[0]["windows"][0]["rect"] == (100, 100, 500, 400)
    assert capture.window_left_at(tl.snapshots, "0xa", region) == pytest.approx(2.0)
    # a big window fully on the monitor is never "left" (x1/y1 are not a width/height)
    big = [{"t": 0.0, "windows": [{"address": "0xb", "rect": (300, 150, 990, 590)}]}]
    assert capture.window_left_at(big, "0xb", region) is None


def test_logger_ends_recording_after_grace(monkeypatch):
    from dolly import logger as lg
    L = lg.Logger.__new__(lg.Logger)
    L.target, L.region = "0xa", [0, 0, 1000, 600]
    calls = []
    L.on_trouble = lambda r, m: calls.append(r)
    clock = [100.0]
    monkeypatch.setattr(lg.time, "monotonic", lambda: clock[0])
    cl = [{"address": "0xa"}]
    away = [{"address": "0xa", "rect": [1200, 0, 400, 300]}]
    home = [{"address": "0xa", "rect": [10, 0, 400, 300]}]
    L._check_target_left(cl, away)
    clock[0] += 1.0
    L._check_target_left(cl, home)            # came back within the grace: nothing
    clock[0] += 1.0
    L._check_target_left(cl, away)
    clock[0] += 1.0
    L._check_target_left(cl, away)
    assert calls == []
    clock[0] += 0.6
    L._check_target_left(cl, away)
    assert calls == ["window-left-monitor"]
    L2 = lg.Logger.__new__(lg.Logger)
    L2.target, L2.region, L2.on_trouble = "0xa", [0, 0, 1000, 600], lambda r, m: calls.append("closed?")
    L2._check_target_left([], [])             # closed windows are handled by closewindow
    assert calls == ["window-left-monitor"]


def test_render_window_card_holds_on_monitor_part():
    """The followed window's card: inside the monitor it is the window itself (a big
    window is not mistaken for an off-monitor one); half dragged off, the part still
    on the monitor; mostly off, it holds the last card."""
    from types import SimpleNamespace
    from dolly.render import Compositor, _clip_box
    assert _clip_box((800, 100, 1200, 400), (0, 0, 1000, 600)) == (800, 100, 1000, 400)
    rects = [(300, 150, 990, 590), (700, 100, 1100, 400), (950, 100, 1350, 400)]
    tl = SimpleNamespace(w=1000, h=600, window_rect_at=lambda t, a: rects[min(2, int(t // 2))])
    me = SimpleNamespace(tl=tl, window_target="0xa")
    me._win_track = Compositor._window_track(me, 0.0, 5.0)

    def card_is(t, rect):
        return all(abs(a - b) <= 0.5 for a, b in zip(Compositor.window_rect(me, t), rect))
    assert card_is(1.0, rects[0])
    assert card_is(3.0, (700, 100, 1000, 400))
    assert card_is(5.0, (700, 100, 1000, 400))


@pytest.mark.parametrize("w,h,scale", [(1920, 1200, 1.25), (2880, 1800, 1.5), (2560, 1600, 1.6666666),
                                       (3840, 2400, 2.0)])
def test_region_snaps_to_even_physical_pixels(w, h, scale):
    """wf-recorder/x264 round odd sizes (a 1003.33 px wide pick at 1.6667 became 1002 px):
    a region starts on a whole physical pixel, spans an even number of them, covers
    the pick and stays on the monitor; capture.json carries the exact physical rect."""
    m = mon("DP-1", w, h, scale, 100, 0)
    s = M.effective_scale(m)
    lw = M.logical_rect(m)
    for pick in ("701,51 602x301", "100,0 51x33", f"{100 + lw[2] - 403},{lw[3] - 199} 403x199"):
        t = M.resolve_pick(pick, [m])
        px, py, pw, ph = (int(v) for v in pick.replace(",", " ").replace("x", " ").split())
        x, y, rw, rh = t.region
        for v in (rw * s, rh * s, (x - 100) * s, y * s):
            assert abs(v - round(v)) < 1e-6
        assert round(rw * s) % 2 == 0 and round(rh * s) % 2 == 0
        assert x <= px and y <= py and x + rw >= min(px + pw, 100 + lw[2]) and y + rh >= min(py + ph, lw[3])
        assert x + rw <= 100 + lw[2] and y + rh <= lw[3] and x >= 100 and y >= 0
        assert tuple(t.physical) == (round(rw * s), round(rh * s))
        assert t.to_meta()["physical_rect"][2:] == list(t.physical)
        assert t.exact == (float(rw), float(rh))         # video px / logical = scale exactly


@pytest.mark.parametrize("backend", ["gsr", "gsr-cpu", "wf"])
@pytest.mark.parametrize("pick", ["monitor:DP-1", "1700,100 600x400"])
def test_recorder_output_is_never_a_monitor_name(backend, pick):
    """A stray video named `DP-1` once appeared in the repo root: a test stub read
    wf-recorder's `-o DP-1` (output selection) as its output file. The real argv keeps
    them apart: gsr writes to `-o PATH`, wf-recorder to `-f PATH`; `-w`/`-o NAME` only
    select what to record."""
    from dolly import capture
    mons = [mon("DP-1", 1920, 1200, 1.25, 1536, 0), mon("eDP-1", 2560, 1600, 1.6666666, 0, 0)]
    t = M.resolve_pick(pick, mons)
    out = Path("/tmp/dolly-test/raw.mkv")
    cmd = capture.recorder_cmd(backend, t, out, 60, [])
    flag = "-f" if backend == "wf" else "-o"
    outs = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == flag and "/" in cmd[i + 1]]
    assert outs == [str(out)], cmd
    names = {m["name"] for m in mons}
    written = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == flag]
    assert not any(w in names for w in written if flag == "-o" and backend != "wf"), cmd
    if backend == "wf" and t.kind == "monitor":
        assert cmd[cmd.index("-o") + 1] == "DP-1" and cmd[cmd.index("-f") + 1] == str(out)

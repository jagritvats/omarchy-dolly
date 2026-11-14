"""Integration tests: finalize mapping, compositor output, export, editor API.

Uses a tiny generated clip (ffmpeg testsrc) so it runs in seconds.
"""
import json
import statistics
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import capture, gfx, project as projmod  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.render import Compositor, export, ffmpeg_bin, make_layout, probe, key_overlay_events  # noqa: E402
from dolly.zoom import ZoomConfig  # noqa: E402
from dolly import keymap  # noqa: E402

W, H, FPS, DUR = 640, 400, 30, 4.0


@pytest.fixture(scope="module")
def proj(tmp_path_factory):
    d = tmp_path_factory.mktemp("proj")
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={FPS}:duration={DUR}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "raw.mp4")], check=True)
    # raw logger output in *logical* coords: region at (100, 50), scale 2 -> logical 320x200
    (d / "capture.json").write_text(json.dumps({"backend": "gsr", "region": [100, 50, 320, 200], "scale": 2.0,
                                                "cursor_baked": False}))
    lines = [{"type": "start", "ms": 0}, {"type": "info", "click_source": "lua", "ms": 1}]
    for i in range(40):
        lines.append({"type": "move", "ms": 100 + i * 25, "x": 150 + i * 3, "y": 100 + i})
    lines.append({"type": "click", "ms": 1200, "x": 250, "y": 140, "button": "left"})
    lines.append({"type": "key", "ms": 2000, "key": "s", "mods": ["ctrl"]})
    lines.append({"type": "window", "ms": 0, "rect": [100, 50, 160, 100], "title": "x"})
    (d / "events.raw.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    capture.finalize(d)
    return d


def test_finalize_maps_logical_to_video_pixels(proj):
    doc = json.loads((proj / "events.json").read_text())
    assert doc["video"]["w"] == W and doc["video"]["h"] == H
    assert abs(doc["video"]["duration"] - DUR) < 0.1
    clk = [e for e in doc["events"] if e["type"] == "click"][0]
    assert clk["t"] == pytest.approx(1.2)
    assert clk["x"] == pytest.approx((250 - 100) * 2) and clk["y"] == pytest.approx((140 - 50) * 2)
    win = [e for e in doc["events"] if e["type"] == "window"][0]
    assert win["rect"] == [0, 0, 320, 200]
    assert doc["capture"]["click_source"] == "lua"
    data = json.loads((proj / "project.json").read_text())
    assert len(data["segments"]) >= 1


def flat(level):
    """A source frame of one grey."""
    f = gfx.new(W, H)
    gfx.fill_rect(f, 0, 0, W, H, gfx.rgb((level, level, level)))
    return f


def off(pixel, level):
    """How far a pixel is from that grey, in levels."""
    return max(abs(v - level) for v in pixel)


def test_compositor_frame_layout(proj):
    tl = Timeline.load(proj / "events.json")
    style = projmod.deep_merge(projmod.DEFAULT_STYLE, {"output": {"width": 640, "height": 360, "fps": 30}})
    comp = Compositor(tl, style, [], ZoomConfig(), 0.0, 1.0)
    out = comp.compose(0, flat(200))
    assert (out.get_width(), out.get_height()) == (640, 360) and not gfx.has_alpha(out)
    lay = comp.lay
    k = lay.OW / lay.geom.CW
    # centre of the screen shows the frame; corner of the canvas shows background
    cy, cx = int(lay.OH / 2), int(lay.OW / 2)
    assert off(gfx.pixel(out, cx, cy), 200) <= 2
    assert off(gfx.pixel(out, 2, 2), 200) > 10
    # the screen's exact corner pixel is rounded off (background, not frame)
    x0, y0 = int(lay.geom.px * k), int(lay.geom.py * k)
    assert off(gfx.pixel(out, x0, y0), 200) > 10


def test_the_screen_has_four_round_corners_and_straight_sides(proj):
    tl = Timeline.load(proj / "events.json")
    tl.capture["cursor_baked"] = True
    style = projmod.deep_merge(projmod.DEFAULT_STYLE, {"output": {"width": 640, "height": 360, "fps": 30},
                                                       "radius": 14, "keys": {"enabled": False}})
    comp = Compositor(tl, style, [], ZoomConfig(), 0.0, 1.0)
    out = comp.compose(0, flat(200))
    lay = comp.lay
    k = lay.OW / lay.geom.CW
    x0, y0 = lay.geom.px * k, lay.geom.py * k
    x1, y1 = x0 + lay.W * k, y0 + lay.H * k
    r = lay.radius_v * k
    assert r > 4
    for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        sx, sy = (1 if cx == x0 else -1), (1 if cy == y0 else -1)
        assert off(gfx.pixel(out, int(cx + sx * 1), int(cy + sy * 1)), 200) > 10          # in the corner: background
        assert off(gfx.pixel(out, int(cx + sx * r), int(cy + sy * r)), 200) <= 2          # past the arc: the screen
    for px, py in ((x0 + 2, (y0 + y1) / 2), (x1 - 3, (y0 + y1) / 2), ((x0 + x1) / 2, y0 + 2), ((x0 + x1) / 2, y1 - 3)):
        assert off(gfx.pixel(out, int(px), int(py)), 200) <= 2                            # the sides are straight


def test_cursor_drawn_unless_baked(proj):
    tl = Timeline.load(proj / "events.json")
    style = projmod.deep_merge(projmod.DEFAULT_STYLE, {"output": {"width": 640, "height": 360, "fps": 30},
                                                       "cursor": {"ripple": False}, "keys": {"enabled": False}})
    a = Compositor(tl, style, [], ZoomConfig(), 2.0, 2.1).compose(0, flat(128))
    tl.capture["cursor_baked"] = True
    b = Compositor(tl, style, [], ZoomConfig(), 2.0, 2.1).compose(0, flat(128))
    changed = gfx.threshold(gfx.difference(a, b), 12)             # 255 where the two differ clearly
    assert sum(gfx.plane(changed, "g")) / 255 > 40                # a cursor's worth of pixels


def test_key_overlay_only_chords():
    tl = Timeline.from_dict({"video": {"w": 10, "h": 10, "fps": 30, "duration": 5}, "events": [
        {"t": 1, "type": "key", "key": "a", "mods": []},
        {"t": 2, "type": "key", "key": "s", "mods": ["ctrl"]},
        {"t": 2.3, "type": "key", "key": "s", "mods": ["ctrl"]},
        {"t": 3, "type": "key", "key": "Return", "mods": []}]})
    ev = key_overlay_events(tl)
    assert [e["caps"] for e in ev] == [["Ctrl", "S"], ["Enter"]]
    assert ev[0]["count"] == 2


def test_keymap_labels():
    assert keymap.label(30) == "a" and keymap.label(28) == "Return" and keymap.MODS[29] == "ctrl"


def test_export_mp4_is_cfr_h264(proj):
    out = export(proj, proj / "exports" / "t.mp4", "mp4", "fast",
                 style_over={"output": {"width": 640, "height": 360, "fps": 30}})
    info = probe(out)
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    assert v["width"] == 640 and v["height"] == 360
    assert v["r_frame_rate"] == "30/1" and v["avg_frame_rate"] == "30/1"
    # frame count == duration * fps (CFR)
    n = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout
    assert int(n) == int(round(DUR * 30))


def test_export_trim_and_gif(proj):
    data = json.loads((proj / "project.json").read_text())
    data["trim"] = [1.0, 2.5]
    (proj / "project.json").write_text(json.dumps(data))
    out = export(proj, proj / "exports" / "t.gif", "gif", "fast",
                 style_over={"output": {"width": 480, "height": 270, "fps": 20}})
    assert out.exists() and out.stat().st_size > 1000
    assert not out.with_suffix(".tmp.mp4").exists()
    data["trim"] = None
    (proj / "project.json").write_text(json.dumps(data))


def test_export_workers_share_the_work_not_the_frames():
    from dolly.render import SHARP_COST, chunk_bounds
    assert chunk_bounds([1.0] * 100, 4) == [0, 25, 50, 75, 100]            # equal frames: split as they always were
    assert chunk_bounds([1.0] * 101, 2) == [0, round(101 / 2), 101]
    assert chunk_bounds([SHARP_COST] * 90, 3) == [0, 30, 60, 90]
    work = [1.0] * 10 + [SHARP_COST] * 40 + [1.0] * 50                     # zoomed in, sharp, for 40 frames early on
    for nw in (2, 3, 4, 6):
        b = chunk_bounds(work, nw)
        assert len(b) == nw + 1 and b[0] == 0 and b[-1] == 100 and b == sorted(b)
        shares = [sum(work[b[k]:b[k + 1]]) for k in range(nw)]
        assert max(shares) - min(shares) <= SHARP_COST                     # no worker is left with the whole stretch
    assert chunk_bounds(work, 2)[1] < 50
    assert chunk_bounds([], 3) == [0, 0, 0, 0]
    assert chunk_bounds([SHARP_COST, 1.0], 6)[-1] == 2


def test_editor_api_roundtrip(proj):
    from http.server import ThreadingHTTPServer
    from dolly.editor.server import Editor, make_handler
    ed = Editor(proj)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ed))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/api/project")            # no token -> 403
        p = json.loads(urllib.request.urlopen(f"{base}/api/project?t={ed.token}").read())
        assert p["video"]["w"] == W and "gradients" in p["theme"]
        segs = [{"id": "m1", "start": 0.5, "end": 3.0, "zoom": 2.2, "focus": [320, 200], "follow": False,
                 "source": "manual", "reason": ""}]
        req = urllib.request.Request(f"{base}/api/save?t={ed.token}", data=json.dumps({"segments": segs}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        assert json.loads(urllib.request.urlopen(req).read())["ok"]
        path = json.loads(urllib.request.urlopen(f"{base}/api/path?fps=30&t={ed.token}").read())
        assert max(path["z"]) == pytest.approx(2.2, abs=0.05)
        r = urllib.request.urlopen(urllib.request.Request(f"{base}/media/raw.mp4?t={ed.token}", headers={"Range": "bytes=0-99"}))
        assert r.status == 206 and len(r.read()) == 100
    finally:
        httpd.shutdown()


def test_logger_lua_bridge_parsing(tmp_path, monkeypatch):
    """The logger turns the Lua bridge's lines into click/key events with held modifiers."""
    from dolly import logger as lg
    L = lg.Logger(tmp_path, keys=True)
    L.on_key(29, True)       # ctrl down
    L.on_key(31, True)       # s
    L.on_key(29, False)
    L.on_key(30, True)       # a
    L.f.close()
    lines = [json.loads(x) for x in (tmp_path / "events.raw.jsonl").read_text().splitlines()]
    assert lines[0]["key"] == "s" and lines[0]["mods"] == ["ctrl"]
    assert lines[1]["key"] == "char" and lines[1]["mods"] == []     # typed text: the time only


def test_recorder_commands():
    from dolly.monitors import Target
    mon = Target("monitor", "DP-1", [0, 0, 1920, 1080], 1.0, (1920.0, 1080.0), physical=(1920, 1080))
    reg = Target("region", "DP-1", [10, 20, 800, 600], 1.0, (800.0, 600.0), physical=(800, 600))
    cmd = capture.recorder_cmd("gsr", mon, Path("/tmp/x.mp4"), 60, [])
    assert cmd[:5] == ["gpu-screen-recorder", "-w", "DP-1", "-s", "0x0"] and "-cursor" in cmd
    assert cmd[cmd.index("-cursor") + 1] == "no"
    assert cmd[cmd.index("-fm") + 1] == "cfr"
    cmd = capture.recorder_cmd("gsr-cpu", reg, Path("/tmp/x.mp4"), 30, ["default_input"])
    assert cmd[1:3] == ["-w", "800x600+10+20"] and cmd[cmd.index("-encoder") + 1] == "cpu" and "-a" in cmd
    cmd = capture.recorder_cmd("wf", reg, Path("/tmp/x.mp4"), 60, [])
    assert cmd[0] == "wf-recorder" and cmd[cmd.index("-g") + 1] == "10,20 800x600" and cmd[cmd.index("-r") + 1] == "60"
    assert capture.recorder_cmd("wf", mon, Path("/tmp/x.mp4"), 60, [])[1:3] == ["-o", "DP-1"]


def test_layout_aspect_and_padding():
    lay = make_layout(2560, 1600, projmod.DEFAULT_STYLE)
    assert lay.OW / lay.OH == pytest.approx(16 / 9, abs=0.01)
    assert lay.geom.CW / lay.geom.CH == pytest.approx(16 / 9, abs=1e-6)
    assert lay.geom.px > 0 and lay.geom.py > 0


# ------------------------------------------------------------------ what the compositor draws
def compositor(events=(), style=None, capture=None, segs=(), dur=6.0, **kw):
    """A compositor over a 640x400 recording, 640x360 out, no keycaps and no drawn cursor unless asked."""
    tl = Timeline.from_dict({"video": {"w": W, "h": H, "fps": 30, "duration": dur},
                             "capture": {"cursor_baked": True, **(capture or {})}, "events": list(events)})
    st = projmod.deep_merge(projmod.DEFAULT_STYLE, {"output": {"width": 640, "height": 360, "fps": 30},
                                                    "keys": {"enabled": False}})
    return Compositor(tl, projmod.deep_merge(st, style), list(segs), ZoomConfig(), 0.0, dur, **kw)


def changed(a, b, x=0, y=0, w=None, h=None):
    """The pixels of that rectangle that differ clearly between two frames: (how many, their
    bounding box (x0, y0, x1, y1) or None)."""
    w, h = (a.get_width() if w is None else int(w)), (a.get_height() if h is None else int(h))
    d = gfx.threshold(gfx.difference(gfx.crop(a, int(x), int(y), w, h), gfx.crop(b, int(x), int(y), w, h)), 12)
    hit = [i for i, v in enumerate(zip(*(gfx.plane(d, ch) for ch in "rgb"))) if any(v)]
    if not hit:
        return 0, None
    cols, rows = [i % w for i in hit], [i // w for i in hit]
    return len(hit), (int(x) + min(cols), int(y) + min(rows), int(x) + max(cols), int(y) + max(rows))


def at(comp, ts, x, y):
    """Where the video point (x, y) is on the output at ts."""
    _, k, vx0, vy0 = comp.affine_at(ts)
    return (x - vx0) * k, (y - vy0) * k


MOVE = [{"t": 0.0, "type": "move", "x": 300, "y": 200}]


def test_click_ripple_is_a_ring_round_the_click():
    comp = compositor(MOVE + [{"t": 2.0, "type": "click", "x": 300, "y": 200, "button": "left"}])
    quiet, ring = comp.compose_at(1.0, flat(60)), comp.compose_at(2.2, flat(60))
    X, Y = at(comp, 2.2, 300, 200)
    n, box = changed(quiet, ring)
    assert n > 200
    assert abs((box[0] + box[2]) / 2 - X) <= 1.5 and abs((box[1] + box[3]) / 2 - Y) <= 1.5     # centred on the click
    assert 40 < box[2] - box[0] < 80 and abs((box[2] - box[0]) - (box[3] - box[1])) <= 1       # and round
    assert changed(quiet, ring, X - 6, Y - 6, 12, 12)[0] == 0                                  # a ring: its middle is clear
    assert gfx.same(comp.compose_at(3.0, flat(60)), quiet)                                     # gone again


def test_spotlight_dims_what_is_far_from_the_cursor():
    from dolly.zoom import Segment
    ev = [{"t": 0.0, "type": "move", "x": 200, "y": 150}]
    segs = [Segment(start=0.5, end=5.5, zoom=2.0, focus=[200, 150], follow=False, source="manual")]
    lit = compositor(ev, {"spotlight": {"enabled": True, "strength": 0.6}}, segs=segs).compose_at(3.0, flat(200))
    plain = compositor(ev, segs=segs)
    ref = plain.compose_at(3.0, flat(200))
    X, Y = at(plain, 3.0, 200, 150)
    assert off(gfx.pixel(lit, int(X), int(Y)), 200) <= 3                       # the spot itself is untouched
    for x, y in ((4, 4), (635, 355), (635, 4)):                                # far from it: 0.6 of the light is gone
        assert all(abs(a - 0.4 * b) <= 4 for a, b in zip(gfx.pixel(lit, x, y), gfx.pixel(ref, x, y)))
    # nothing at 1x: the spotlight belongs to a close-up
    one = compositor(ev, {"spotlight": {"enabled": True, "strength": 0.6}})
    assert gfx.same(one.compose_at(3.0, flat(200)), compositor(ev).compose_at(3.0, flat(200)))


def test_keycaps_show_the_shortcut_where_asked():
    from dolly.render import keycaps_image
    ev = MOVE + [{"t": 2.0, "type": "key", "key": "s", "mods": ["ctrl"]}]
    for where, top in (("bottom", 240), ("top", 0)):
        comp = compositor(ev, {"keys": {"enabled": True, "position": where}})
        quiet, shown = comp.compose_at(1.0, flat(60)), comp.compose_at(2.5, flat(60))
        n, box = changed(quiet, shown)
        assert n > 250 and top <= box[1] and box[3] < top + 120                # in that band
        assert abs((box[0] + box[2]) / 2 - 320) <= 3                           # and in the middle
    caps = keycaps_image(("Ctrl", "S"), 1, 1.0)
    more = keycaps_image(("Ctrl", "S"), 3, 1.0)                                # pressed three times: a count beside them
    assert gfx.has_alpha(caps) and more.get_width() > caps.get_width() and more.get_height() == caps.get_height()
    assert gfx.pixel(caps, 2, 2)[3] < 40                                       # clear at the corner,
    assert gfx.pixel(caps, caps.get_width() // 4, caps.get_height() // 2)[3] > 200     # solid on the first cap
    assert keycaps_image(("Ctrl", "S"), 1, 1.0) is caps                        # kept, not drawn again


def test_captions_chapters_and_the_speed_badge():
    from dolly.timemap import TimeMap
    cues = [{"start": 1.0, "end": 2.0, "text": "Hello from Dolly"}]
    comp = compositor(MOVE, {"chapters": [{"t": 3.0, "text": "Two"}]}, captions=cues,
                      timemap=TimeMap(0.0, 6.0, [{"start": 4.5, "end": 5.5, "speed": 4.0}]))
    quiet = comp.compose_at(0.5, flat(60))
    cap, chap, fast = (comp.compose_at(t, flat(60)) for t in (1.5, 3.5, 5.0))
    n, box = changed(quiet, cap)
    assert n > 500 and box[1] > 280 and abs((box[0] + box[2]) / 2 - 320) <= 3  # the caption: bottom middle
    n, box = changed(quiet, chap)
    assert n > 100 and box[2] < 200 and box[3] < 60                            # the chapter: top left
    # the chapter chip stays 2.8 s of output time, which the speed-up stretches past 5.0 s of
    # the recording: against the frame that has the chip too, only the badge is new
    n, box = changed(chap, fast)
    assert n > 100 and box[0] > 520 and box[3] < 60                            # the fast-forward badge: top right
    half = comp.compose_at(1.04, flat(60))                                     # fading in: fainter, same place
    assert 0 < changed(quiet, half)[0] and not gfx.same(half, cap)


def test_title_card_is_the_plain_canvas_with_the_words_in_its_middle():
    comp = compositor()
    card = comp.title_card("Hello", "from the tests")
    long = comp.title_card("A title too long for one line of this narrow card to hold", "")
    assert (card.get_width(), card.get_height()) == (640, 360) and not gfx.has_alpha(card)
    n, box = changed(comp.bg_plain, card)
    assert n > 300 and 160 < box[0] and box[2] < 480 and 130 < box[1] and box[3] < 230
    n2, box2 = changed(comp.bg_plain, long)
    assert n2 > n and box2[0] >= 30 and box2[2] <= 610                         # wrapped: inside the card,
    assert box2[3] - box2[1] > 40                                              # and more than one line tall
    assert comp.title_card("Hello", "from the tests") is not card              # a frame of its own each time


def test_motion_blur_streaks_a_fast_cursor():
    ev = [{"t": 0.0, "type": "move", "x": 100, "y": 200}] + \
         [{"t": 1.0 + i / 60, "type": "move", "x": 100 + 12 * i, "y": 200} for i in range(40)]

    def moving(blur):
        comp = compositor(ev, {"cursor": {"motion_blur": blur}}, capture={"cursor_baked": False})
        comp.compose_at(1.3, flat(128))
        return comp.compose_at(1.3 + 1 / 30, flat(128))
    bare = compositor(ev).compose_at(1.3 + 1 / 30, flat(128))                  # no cursor drawn at all
    sharp, streak = changed(bare, moving(False)), changed(bare, moving(True))
    assert sharp[0] > 100 and streak[0] > 100
    assert streak[1][0] <= sharp[1][0] - 4                                     # a trail behind it (it moves right),
    assert abs(streak[1][2] - sharp[1][2]) <= 4                                # ending where the cursor is,
    assert abs((streak[1][3] - streak[1][1]) - (sharp[1][3] - sharp[1][1])) <= 5      # and no taller than the cursor


def test_cursor_sprites_arrow_ibeam_and_grab():
    from dolly.render import cursor_sprite, shape_sprite
    for sprite, (hx, hy), height in (cursor_sprite(), shape_sprite("ibeam"), shape_sprite("grab")):
        assert gfx.has_alpha(sprite) and height == 192
        assert 0 < hx < sprite.get_width() and 0 < hy < sprite.get_height()
        assert 8 < gfx.mean(sprite)[3] < 120                                   # mostly clear, a cursor in it
        assert gfx.pixel(sprite, 1, 1)[3] == 0                                 # which does not touch the edge
    arrow, (hx, hy), _ = cursor_sprite()
    assert min(gfx.pixel(arrow, int(hx) + 3, int(hy) + 8)[:3]) > 200           # white just inside the tip,
    assert max(gfx.pixel(arrow, int(hx) + 22, int(hy) + 75)[:3]) < 40          # dark in the body
    ibeam, (hx, hy), _ = shape_sprite("ibeam")
    assert max(gfx.pixel(ibeam, int(hx), int(hy))[:3]) < 40                    # the stem, dark,
    assert min(gfx.pixel(ibeam, int(hx) + 11, int(hy))[:3]) > 200              # with a white line beside it


def test_window_mode_shows_only_the_followed_window():
    win = {"address": "0xa", "class": "app", "title": "t", "rect": [160, 100, 320, 200], "floating": True,
           "fullscreen": False, "workspace": "1"}
    ev = MOVE + [{"t": 0.0, "type": "clients", "focus": "0xa", "windows": [win]}]
    comp = compositor(ev, capture={"mode": "window", "target": "0xa"})
    out = comp.compose_at(2.0, flat(200))
    X, Y = at(comp, 2.0, 320, 200)                                             # the middle of the window
    assert off(gfx.pixel(out, int(X), int(Y)), 200) <= 2
    for vx, vy in ((150, 200), (490, 200), (320, 90)):                         # on the screen, just outside the window
        X, Y = at(comp, 2.0, vx, vy)
        assert 0 <= X < 640 and 0 <= Y < 360 and off(gfx.pixel(out, int(X), int(Y)), 200) > 10
    # below the window lies its shadow: darker than the canvas a little further down
    X, Y = at(comp, 2.0, 320, 303)
    assert int(Y) + 30 < 360
    assert sum(gfx.pixel(out, int(X), int(Y))) < sum(gfx.pixel(out, int(X), int(Y) + 30)) - 50


def test_a_crop_wider_than_the_screen_is_letterboxed_on_a_dimmed_card():
    comp = compositor(aspect="9:16")
    x0, y0, x1, y1 = comp.card
    n = comp.n
    comp.crop.cw, comp.crop.ch = [800.0] * n, [800.0 * (y1 - y0) / (x1 - x0)] * n      # wider than the 640 px screen
    comp.crop.cx, comp.crop.cy = [320.0] * n, [200.0] * n
    out = comp.compose_at(2.0, flat(200))
    assert (out.get_width(), out.get_height()) == (360, 640)
    assert off(gfx.pixel(out, (x0 + x1) // 2, (y0 + y1) // 2), 200) <= 2      # the whole screen in the middle,
    above, canvas = gfx.pixel(out, (x0 + x1) // 2, y0 + 20), gfx.pixel(comp.bg, (x0 + x1) // 2, y0 + 20)
    assert all(abs(a - 0.55 * b) <= 4 for a, b in zip(above, canvas))         # the card round it: the canvas, dimmed
    assert gfx.pixel(out, 3, 3) == gfx.pixel(comp.bg, 3, 3)                    # and outside the card the canvas as it is


def test_frame_reader_hands_out_each_decoded_frame_once(proj):
    from dolly.render import FrameReader
    r = FrameReader(projmod.Project(proj).raw, 30, 0.0, W, H)
    try:
        a = r.at(0.0)
        assert (a.get_width(), a.get_height()) == (W, H) and not gfx.has_alpha(a)
        assert r.at(0.01) is a                          # the same source frame: the same object (frame reuse)
        b = r.at(0.5)
        assert b is not a and not gfx.same(a, b)
        assert r.at(99.0) is not None                   # past the end: the last frame is held
    finally:
        r.close()


def test_render_still_and_numbered_frames_are_pictures_of_the_output(proj, tmp_path):
    from dolly.render import render_still
    over = {"output": {"width": 640, "height": 360, "fps": 30}}
    png = render_still(proj, 1.0, tmp_path / "s.png", style_over=over)
    still = gfx.load(png)
    assert (still.get_width(), still.get_height()) == (640, 360) and not gfx.has_alpha(still)
    assert statistics.pstdev(gfx.plane(gfx.crop(still, 200, 100, 240, 160), "g")) > 20        # the test pattern
    export(proj, tmp_path / "f.mp4", "mp4", "fast", style_over=over, t_range=(0.0, 1.0), frames_dir=tmp_path / "frames",
           frame_every=10)
    frames = sorted((tmp_path / "frames").glob("frame_*.png"))
    assert [f.name for f in frames] == ["frame_00000.png", "frame_00010.png", "frame_00020.png"]
    assert all((gfx.load(f).get_width(), gfx.load(f).get_height()) == (640, 360) for f in frames)


def test_backgrounds_solid_gradient_shadow_and_picture(tmp_path, monkeypatch):
    from dolly.render import build_background
    monkeypatch.setenv("DOLLY_IMAGE_ROOTS", str(tmp_path))

    def canvas(background, **more):
        style = {"background": background, "padding": 0.06, "shadow": 0, "radius": 0,
                 "output": {"width": 640, "height": 360}, **more}
        return build_background(make_layout(640, 400, style), style)
    solid = canvas({"type": "solid", "color": "#204060"})
    assert (solid.get_width(), solid.get_height()) == (640, 360) and not gfx.has_alpha(solid)
    assert all(abs(a - b) <= 1 for a, b in zip(gfx.mean(solid), (0x20, 0x40, 0x60)))
    assert not gfx.same(gfx.crop(solid, 0, 0, 64, 64), gfx.crop(solid, 64, 0, 64, 64))        # dithered, not flat
    assert gfx.same(solid, canvas({"type": "solid", "color": "#204060"}))                     # the same every time
    run = canvas({"type": "gradient", "stops": ["#000000", "#ffffff"], "angle": 90})          # 90 degrees: to the right
    left, right = gfx.mean(gfx.crop(run, 0, 100, 20, 160)), gfx.mean(gfx.crop(run, 620, 100, 20, 160))
    assert max(left) < 20 and min(right) > 200
    shaded = canvas({"type": "solid", "color": "#808080"}, shadow=0.8)
    lay = make_layout(640, 400, {"padding": 0.06, "output": {"width": 640, "height": 360}})
    k = lay.OW / lay.geom.CW
    under = (int(lay.OW / 2), int((lay.geom.py + lay.H) * k) + 3)                             # just below the screen
    assert gfx.pixel(shaded, *under)[1] < 128 - 20 and abs(gfx.pixel(shaded, 3, 3)[1] - 128) <= 6
    red = gfx.new(320, 180)
    gfx.fill_rect(red, 0, 0, 320, 180, gfx.rgb((220, 20, 20)))
    for name in ("red.png", "red.jpg"):
        gfx.save(red, tmp_path / name)
        r, g, b = gfx.pixel(canvas({"type": "wallpaper", "path": str(tmp_path / name), "blur": 0}), 2, 2)
        assert r > 180 and g < 60 and b < 60, name                                            # the picture, filling the canvas
    soft = canvas({"type": "wallpaper", "path": str(tmp_path / "red.png"), "blur": 20})
    assert gfx.pixel(soft, 320, 180)[0] > 180
    gone = canvas({"type": "wallpaper", "path": str(tmp_path / "nothing.png")})               # no such file: the gradient
    assert gfx.pixel(gone, 2, 2)[0] < 180


def test_a_damaged_picture_is_refused_or_drawn_over_never_a_crash(tmp_path, monkeypatch):
    from dolly import images
    from dolly.render import build_background
    monkeypatch.setenv("DOLLY_IMAGE_ROOTS", str(tmp_path))
    red = gfx.new(320, 180)
    gfx.fill_rect(red, 0, 0, 320, 180, gfx.rgb((220, 20, 20)))
    gfx.save(red, tmp_path / "ok.png")
    gfx.save(red, tmp_path / "ok.jpg")
    png, jpg = (tmp_path / "ok.png").read_bytes(), (tmp_path / "ok.jpg").read_bytes()
    assert images.info(tmp_path / "ok.png")["width"] == 320 and images.info(tmp_path / "ok.jpg")["height"] == 180
    (tmp_path / "cut.png").write_bytes(png[:len(png) // 2])                    # a download that stopped half way
    (tmp_path / "flip.png").write_bytes(png[:60] + bytes([png[60] ^ 0xFF]) + png[61:])        # one byte wrong
    (tmp_path / "text.png").write_text("not a picture at all\n" * 40)
    for bad in ("cut.png", "flip.png", "text.png"):
        with pytest.raises(images.ImageError):
            images.check(tmp_path / bad)
    # a JPEG is checked by its header only: one cut short passes the check and is caught, or
    # drawn as far as it goes, when the canvas is made
    (tmp_path / "cut.jpg").write_bytes(jpg[:len(jpg) * 2 // 3])
    style = {"background": {"type": "wallpaper", "path": str(tmp_path / "cut.jpg")}, "padding": 0.06, "shadow": 0,
             "radius": 0, "output": {"width": 640, "height": 360}}
    out = build_background(make_layout(640, 400, style), style)
    assert (out.get_width(), out.get_height()) == (640, 360)
    # an upload is decoded once before it is kept
    assert images.save_upload(png, "mine.png")["width"] == 320
    for data, name in ((png[:len(png) // 2], "a.png"), (b"junk" * 100, "b.jpg"), (jpg[:200], "c.jpg"), (png, "d.gif")):
        with pytest.raises(images.ImageError):
            images.save_upload(data, name)

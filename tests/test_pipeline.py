"""Integration tests: finalize mapping, compositor output, export, editor API.

Uses a tiny generated clip (ffmpeg testsrc) so it runs in seconds.
"""
import json
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import capture, project as projmod  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.render import (Compositor, export, ffmpeg_bin, make_layout, probe, rounded_mask,  # noqa: E402
                          key_overlay_events)
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


def test_rounded_mask_corners():
    m = rounded_mask(100, 60, 12)
    assert m[0, 0] == 0.0 and m[30, 50] == 1.0 and m[0, 50] == 1.0
    assert 0 < m[3, 3] < 1 or m[3, 3] == 0


def test_compositor_frame_layout(proj):
    tl = Timeline.load(proj / "events.json")
    style = projmod.deep_merge(projmod.DEFAULT_STYLE, {"output": {"width": 640, "height": 360, "fps": 30}})
    comp = Compositor(tl, style, [], ZoomConfig(), 0.0, 1.0)
    frame = np.full((H, W, 3), 200, np.uint8)
    out = comp.compose(0, frame)
    assert out.shape == (360, 640, 3)
    lay = comp.lay
    k = lay.OW / lay.geom.CW
    # centre of the screen shows the frame; corner of the canvas shows background
    cy, cx = int(lay.OH / 2), int(lay.OW / 2)
    assert np.abs(out[cy, cx].astype(int) - 200).max() <= 2
    assert np.abs(out[2, 2].astype(int) - 200).max() > 10
    # the screen's exact corner pixel is rounded off (background, not frame)
    x0, y0 = int(lay.geom.px * k), int(lay.geom.py * k)
    assert np.abs(out[y0, x0].astype(int) - 200).max() > 10


def test_cursor_drawn_unless_baked(proj):
    tl = Timeline.load(proj / "events.json")
    style = projmod.deep_merge(projmod.DEFAULT_STYLE, {"output": {"width": 640, "height": 360, "fps": 30},
                                                       "cursor": {"ripple": False}, "keys": {"enabled": False}})
    frame = np.full((H, W, 3), 128, np.uint8)
    a = Compositor(tl, style, [], ZoomConfig(), 2.0, 2.1).compose(0, frame)
    tl.capture["cursor_baked"] = True
    b = Compositor(tl, style, [], ZoomConfig(), 2.0, 2.1).compose(0, frame)
    assert np.abs(a.astype(int) - b.astype(int)).sum() > 1000


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

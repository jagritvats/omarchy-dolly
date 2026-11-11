"""Speed ramps, reframe, privacy/secret detection, captions, audio chain, pause/mark, replay."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import capture, captions, privacy  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.reframe import reframe_path  # noqa: E402
from dolly.render import audio_filter, export, ffmpeg_bin, probe  # noqa: E402
from dolly.timemap import TimeMap, detect_speedups  # noqa: E402
from dolly.zoom import build_segments  # noqa: E402

W, H = 2560, 1600


def tl_from(events, duration=30.0, w=W, h=H):
    return Timeline.from_dict({"video": {"w": w, "h": h, "fps": 30, "duration": duration}, "events": events})


def moves(points, t0, t1, n=40):
    out = []
    for i in range(n + 1):
        u = i / n
        k = min(int(u * (len(points) - 1)), len(points) - 2)
        f = u * (len(points) - 1) - k
        out.append({"t": t0 + (t1 - t0) * u, "type": "move",
                    "x": points[k][0] + (points[k + 1][0] - points[k][0]) * f,
                    "y": points[k][1] + (points[k + 1][1] - points[k][1]) * f})
    return out


# ------------------------------------------------------------------ time map
def test_timemap_roundtrip_and_duration():
    tm = TimeMap(1.0, 21.0, [{"start": 5, "end": 9, "speed": 4}, {"start": 12, "end": 14, "speed": 2}])
    assert tm.duration == pytest.approx(20 - 4 - 2 + 1 + 1)       # 4s@4x -> 1s, 2s@2x -> 1s
    for ts in (1.0, 4.9, 6.0, 9.0, 13.0, 20.0):
        assert tm.to_source(tm.to_output(ts)) == pytest.approx(ts)
    assert tm.speed_at(7) == 4 and tm.speed_at(10) == 1
    taus = np.linspace(0, tm.duration, 200)
    src = [tm.to_source(t) for t in taus]
    assert np.all(np.diff(src) >= -1e-9)                          # monotonic


def test_timemap_clips_segments_to_trim():
    tm = TimeMap(10, 20, [{"start": 5, "end": 12, "speed": 2}])
    assert tm.duration == pytest.approx(1 + 8)


def test_idle_detected_and_microjitter_ignored():
    ev = moves([(100, 100), (800, 600)], 0.5, 1.5)
    ev += [{"t": 2.0, "type": "click", "x": 800, "y": 600, "button": "left"}]
    # resting hand: tiny moves between 3 and 10 s
    ev += [{"t": 3 + i, "type": "move", "x": 800 + (i % 2) * 3, "y": 600} for i in range(7)]
    ev += [{"t": 10.5, "type": "click", "x": 900, "y": 640, "button": "left"}]
    segs = detect_speedups(tl_from(ev, 14))
    idle = [s for s in segs if s["reason"] == "idle"]
    assert idle and idle[0]["start"] == pytest.approx(2.45) and idle[0]["end"] == pytest.approx(10.05)
    assert idle[0]["speed"] == 4.0


def test_visual_change_prevents_idle():
    ev = [{"t": 1.0, "type": "click", "x": 10, "y": 10, "button": "left"},
          {"t": 9.0, "type": "click", "x": 10, "y": 10, "button": "left"}]
    at = np.arange(0, 10, 0.2)
    ad = np.where((at > 3) & (at < 7), 500.0, 0.0)  # terminal printing output
    segs = detect_speedups(tl_from(ev, 10), (at, ad))
    assert all(not (s["start"] < 6 < s["end"]) for s in segs)


def test_long_typing_sped_up_ends_kept():
    ev = [{"t": 2 + 0.15 * i, "type": "key", "key": "a", "mods": []} for i in range(50)]  # 7.35 s burst
    segs = detect_speedups(tl_from(ev, 12))
    ty = [s for s in segs if s["reason"] == "typing"]
    assert ty and ty[0]["start"] == pytest.approx(3.0) and ty[0]["end"] == pytest.approx(2 + 0.15 * 49 - 1.0)
    assert ty[0]["speed"] == 2.0


# ------------------------------------------------------------------ reframe
def demo_tl():
    ev = moves([(1800, 1200), (2380, 210)], 0.5, 1.6) + [{"t": 2.3, "type": "click", "x": 2380, "y": 210}]
    ev += moves([(2380, 210), (1600, 440)], 2.6, 3.2) + [{"t": 3.6, "type": "click", "x": 1600, "y": 440}]
    ev += [{"t": 4.0 + 0.12 * i, "type": "key", "key": "a", "mods": []} for i in range(20)]
    ev += moves([(1600, 440), (460, 340)], 9.0, 9.7) + [{"t": 10.0, "type": "click", "x": 460, "y": 340}]
    return tl_from(ev, 14)


@pytest.mark.parametrize("aspect", [9 / 16, 1.0])
def test_reframe_crop_in_bounds_smooth_and_follows_cursor(aspect):
    tl = demo_tl()
    segs = build_segments(tl)
    p = reframe_path(tl, segs, aspect, 30, 0, tl.duration)
    assert np.all(p.cx - p.cw / 2 >= -0.5) and np.all(p.cx + p.cw / 2 <= W + 0.5)
    assert np.all(p.cy - p.ch / 2 >= -0.5) and np.all(p.cy + p.ch / 2 <= H + 0.5)
    assert np.allclose(p.cw / p.ch, aspect)
    inside = (np.abs(p.cur_x - p.cx) <= p.cw / 2) & (np.abs(p.cur_y - p.cy) <= p.ch / 2)
    assert inside.mean() > 0.95
    step = np.hypot(np.diff(p.cx), np.diff(p.cy)) / p.cw[1:]      # fraction of crop width per frame
    assert step.max() < 0.08
    # zooms in during segments
    assert p.cw.min() < p.cw.max() * 0.95


# ------------------------------------------------------------------ privacy
def test_find_secrets_patterns():
    hits = lambda s: [k for _, _, k in privacy.find_secrets(s)]
    assert "aws-key" in hits("key AKIAIOSFODNN7EXAMPLE here")
    assert "assignment" in hits("API_TOKEN=tok_demo_8f3a9c2e7b1d4f6a0c5e9b2d7f4a1c8e") or \
        "high-entropy" in hits("API_TOKEN=tok_demo_8f3a9c2e7b1d4f6a0c5e9b2d7f4a1c8e")
    assert "email" in hits("contact deploy@tasks.example now")
    assert "jwt" in hits("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
    assert "github-token" in hits("ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8")
    assert hits("Running 12 tests in parallel using 8 processes") == []
    assert hits("http://127.0.0.1:3000/tasks/new") == []
    # OCR glyph confusions still match and keep span alignment
    s = "TOKEN=tok_demo_8f3a9c2e7b1d4féa0c5e9b2d7F4alc8e"
    (a, b, _), = privacy.find_secrets(s)
    assert "tok_demo" in s[a:b] and s[a:b].endswith("8e")


def test_apply_boxes_only_inside_rect_and_time():
    rng = np.random.default_rng(0)
    f = rng.integers(0, 255, (200, 300, 3), dtype=np.uint8)
    boxes = [{"start": 1, "end": 2, "rect": [50, 50, 100, 60], "mode": "pixelate"}]
    assert privacy.apply_boxes(f, boxes, 0.5) is f
    g = privacy.apply_boxes(f, boxes, 1.5)
    assert np.array_equal(g[:40], f[:40]) and not np.array_equal(g[60:100, 60:140], f[60:100, 60:140])
    # pixelated block is (much) lower variance than the noise
    assert g[60:100, 60:140].std() < f[60:100, 60:140].std() * 0.5


def _tesseract_eng():
    import shutil
    import subprocess
    exe = shutil.which("tesseract")
    if not exe:
        return False
    r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True)
    return "eng" in (r.stdout + r.stderr).split()


def _mono_font(px):
    """A real monospace TTF at px: Debian and Arch keep DejaVu in different places."""
    import shutil
    import subprocess
    from PIL import ImageFont
    cands = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
             "/usr/share/fonts/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/liberation/LiberationMono-Regular.ttf"]
    if shutil.which("fc-match"):
        cands.append(subprocess.run(["fc-match", "-f", "%{file}", "monospace"], capture_output=True, text=True).stdout)
    for c in cands:
        if c and Path(c).exists() and c.lower().endswith((".ttf", ".otf")):
            return ImageFont.truetype(c, px)
    try:
        return ImageFont.load_default(px)          # Pillow >= 10.1: scalable built-in font
    except TypeError:
        return None


@pytest.mark.skipif(not _tesseract_eng(), reason="tesseract with English data not installed")
def test_scan_secrets_ocr_end_to_end():
    from PIL import Image, ImageDraw
    font = _mono_font(28)
    if font is None:
        pytest.skip("no scalable monospace font for the OCR fixture")

    def frame(show):
        im = Image.new("RGB", (1280, 400), (26, 27, 38))
        d = ImageDraw.Draw(im)
        d.text((30, 40), "~/code/tasks main > cat .env.local", font=font, fill=(169, 177, 214))
        if show:
            d.text((30, 100), "API_TOKEN=tok_demo_8f3a9c2e7b1d4f6a0c5e9b2d7f4a1c8e", font=font, fill=(169, 177, 214))
        d.text((30, 160), "Running 12 tests in parallel", font=font, fill=(169, 177, 214))
        return np.asarray(im)[..., ::-1].copy()
    frames = [(float(t), frame(2 <= t <= 5)) for t in range(8)]
    boxes = privacy.scan_secrets(None, 8.0, frames=iter(frames))
    assert len(boxes) == 1
    b = boxes[0]
    assert b["start"] <= 1.0 and b["end"] >= 6.0          # covers the gaps between samples
    x, y, w, h = b["rect"]
    assert y < 100 + 30 < y + h and w > 300


# ------------------------------------------------------------------ captions
def test_srt_roundtrip_and_remap(tmp_path):
    cues = [{"start": 1.0, "end": 2.5, "text": "Open the task modal"},
            {"start": 6.0, "end": 7.0, "text": "(sped-up part)"},
            {"start": 12.0, "end": 13.5, "text": "Run the tests"}]
    captions.write_srt(cues, tmp_path / "c.srt")
    back = captions.parse_srt((tmp_path / "c.srt").read_text())
    assert [c["text"] for c in back] == [c["text"] for c in cues] and back[2]["end"] == pytest.approx(13.5)
    tm = TimeMap(0, 20, [{"start": 5, "end": 9, "speed": 4}])
    out = captions.remap(back, tm, offset=2.0)
    assert [c["text"] for c in out] == ["Open the task modal", "Run the tests"]
    assert out[1]["start"] == pytest.approx(2.0 + 12 - 3)


def test_captions_skip_cleanly_without_backend(tmp_path, monkeypatch):
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x200:rate=10:duration=1",
                    "-f", "lavfi", "-i", "sine=duration=1", "-shortest", "-c:v", "libx264", "-c:a", "aac",
                    str(tmp_path / "raw.mp4")], check=True)
    monkeypatch.setattr(captions, "backend", lambda: None)
    with pytest.raises(captions.CaptionsUnavailable, match="whisper"):
        captions.transcribe(tmp_path)


# ------------------------------------------------------------------ audio + full export features
@pytest.fixture(scope="module")
def av_proj(tmp_path_factory):
    d = tmp_path_factory.mktemp("av")
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x400:rate=30:duration=10",
                    "-f", "lavfi", "-i", "sine=f=330:duration=10", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(d / "raw.mp4")], check=True)
    ev = moves([(100, 100), (500, 300)], 0.2, 1.0) + [{"t": 1.5, "type": "click", "x": 500, "y": 300}]
    doc = {"video": {"w": 640, "h": 400, "fps": 30, "duration": 10.0},
           "capture": {"backend": "gsr", "mic": True, "click_source": "lua"}, "events": ev}
    (d / "events.json").write_text(json.dumps(doc))
    captions.write_srt([{"start": 0.5, "end": 2.0, "text": "Hello from Dolly"}], d / "captions.srt")
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:size=640x480:rate=30:duration=10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "webcam.mkv")], check=True)
    data = {"speed": [{"start": 3, "end": 7, "speed": 4}],
            "privacy": [{"start": 0, "end": 10, "rect": [0, 0, 200, 120], "mode": "blur"}],
            "style": {"title": {"enabled": True, "text": "Add task assignees", "intro": 1.0,
                                "outro_text": "Thanks", "outro": 1.0}}}
    (d / "project.json").write_text(json.dumps(data))
    return d


def test_export_with_speed_title_audio_captions(av_proj):
    out = export(av_proj, av_proj / "exports" / "full.mp4", "mp4", "fast",
                 style_over={"output": {"width": 640, "height": 360, "fps": 30}})
    p = probe(out)
    dur = float(p["format"]["duration"])
    assert dur == pytest.approx(1.0 + (10 - 4 + 1) + 1.0, abs=0.12)    # intro + ramped main + outro
    a = [s for s in p["streams"] if s["codec_type"] == "audio"]
    assert a                                                          # audio survived the ramp
    srt = captions.parse_srt(out.with_suffix(".srt").read_text())
    assert srt[0]["start"] == pytest.approx(1.5, abs=0.01)            # shifted by the 1 s intro


@pytest.mark.parametrize("aspect,size", [("9:16", (360, 640)), ("1:1", (480, 480))])
def test_export_reframe_dimensions(av_proj, aspect, size):
    out = export(av_proj, av_proj / "exports" / f"r{size[0]}.mp4", "mp4", "fast", aspect=aspect,
                 style_over={"output": {"width": size[0], "height": size[1], "fps": 15}, "title": {"enabled": False}})
    v = [s for s in probe(out)["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == size


def test_webcam_bubble_drawn_bottom_left(av_proj):
    out = export(av_proj, av_proj / "exports" / "cam.mp4", "mp4", "fast",
                 style_over={"output": {"width": 640, "height": 360, "fps": 15}, "title": {"enabled": False},
                             "speed": {"enabled": False}, "webcam": {"enabled": True, "corner": "bottom-left", "size": 0.25}})
    raw = subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", "5", "-i", str(out), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "bgr24", "-"], capture_output=True).stdout
    f = np.frombuffer(raw, np.uint8).reshape(360, 640, 3)
    bl = f[360 - 80:360 - 20, 20:80].reshape(-1, 3)
    red = (bl[:, 2] > 180) & (bl[:, 1] < 80) & (bl[:, 0] < 80)
    assert red.mean() > 0.5                                   # bubble present in the bottom-left corner
    tr = f[20:80, 560:620].reshape(-1, 3)
    assert ((tr[:, 2] > 180) & (tr[:, 1] < 80)).mean() < 0.05  # and not elsewhere


def test_audio_filter_graph_shape():
    f = audio_filter([(0, 3, 1.0), (3, 7, 8.0), (7, 10, 1.0)], 1.0, 9.5, True, True)
    assert f.count("atrim=") == 4 and "atempo=2.0,atempo=2.0,atempo=2.0" in f and "afftdn" in f
    assert "loudnorm" in f and "adelay=delays=1000" in f and f.endswith("[aout]")


# ------------------------------------------------------------------ pause / mark in finalize
def test_finalize_removes_pauses_and_keeps_marks(tmp_path):
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x200:rate=30:duration=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "raw.mp4")], check=True)
    (tmp_path / "capture.json").write_text(json.dumps({"backend": "gsr", "region": [0, 0, 320, 200], "scale": 1}))
    lines = [{"type": "start", "ms": 0},
             {"type": "click", "ms": 1000, "x": 10, "y": 10},
             {"type": "pause", "ms": 1500},
             {"type": "click", "ms": 2000, "x": 99, "y": 99},     # while paused: dropped
             {"type": "resume", "ms": 4500},
             {"type": "click", "ms": 5000, "x": 20, "y": 20},     # -> 2.0 s after removing the 3 s pause
             {"type": "mark", "ms": 5500, "x": 30, "y": 30}]
    (tmp_path / "events.raw.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    doc = capture.finalize(tmp_path)
    clicks = [e for e in doc["events"] if e["type"] == "click"]
    assert [c["t"] for c in clicks] == [1.0, 2.0]
    marks = [e for e in doc["events"] if e["type"] == "mark"]
    assert marks and marks[0]["t"] == 2.5
    tl = Timeline.from_dict(doc)
    assert tl.marks and any("marked" in s.reason or s.start <= 2.5 <= s.end for s in build_segments(tl))


# ------------------------------------------------------------------ replay (stubbed gsr)
GSR_REPLAY = r'''#!/usr/bin/env bash
out=""; while [[ $# -gt 0 ]]; do case "$1" in -o) out="$2"; shift;; esac; shift; done
save() { ffmpeg -v error -y -f lavfi -i "testsrc2=size=640x400:rate=30:duration=$1" -c:v libx264 -preset ultrafast \
  -pix_fmt yuv420p "$out/Replay_$(date +%H-%M-%S).mp4"; }
trap 'save 4' USR1
trap 'save 3' RTMIN+2
trap 'exit 0' INT TERM
while true; do sleep 0.1; done
'''
HYPRCTL = r'''#!/usr/bin/env bash
case "$*" in
  *monitors*) echo '[{"name":"Virtual-1","focused":true,"x":0,"y":0,"width":640,"height":400,"scale":1.0,"transform":0}]';;
  *cursorpos*) python3 -c "import time;t=time.time()%4;print('{\"x\":%d,\"y\":%d}'%(100+int(t*50),100))";;
  eval*) echo "error"; exit 1;;
  *) echo "{}";;
esac
'''


def test_replay_start_save_stop(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    for n, body in (("gpu-screen-recorder", GSR_REPLAY), ("hyprctl", HYPRCTL)):
        (b / n).write_text(body)
        (b / n).chmod(0o755)
    env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", DOLLY_DIR=str(tmp_path / "v"),
               XDG_RUNTIME_DIR=str(tmp_path / "run"), PYTHONPATH=str(ROOT), HOME=str(tmp_path))
    env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
    (tmp_path / "run").mkdir()
    run = lambda *a, check=True: subprocess.run([sys.executable, "-m", "dolly.cli", *a], env=env, capture_output=True,
                                                text=True, timeout=120, check=check)
    run("replay", "start", "--seconds", "30")
    st = json.loads(run("status", "--json", check=False).stdout)
    assert st["replay"] is True and st["recording"] is False
    time.sleep(2.5)
    pdir = Path(run("replay", "save", "--last", "30").stdout.strip().splitlines()[-1])
    ev = json.loads((pdir / "events.json").read_text())
    assert ev["video"]["duration"] == pytest.approx(3.0, abs=0.2)
    mv = [e for e in ev["events"] if e["type"] == "move"]
    assert mv and all(-0.3 <= e["t"] <= 3.3 for e in mv)                 # aligned to the clip
    assert json.loads((pdir / "capture.json").read_text())["mode"] == "replay"
    run("replay", "stop")
    assert run("replay", "status", check=False).returncode == 1


def test_stalled_render_workers_fall_back_inline(av_proj, monkeypatch, capfd):
    """A worker that hangs must not leave the export spinning: the watchdog (a
    backstop now that workers are fresh processes) kills it and finishes inline."""
    from dolly import render as R
    monkeypatch.setenv("DOLLY_TEST_WORKER_HANG", "1")
    monkeypatch.setattr(R, "STALL_S", 2.0)
    t0 = time.time()
    out = R.export(av_proj, av_proj / "exports" / "stall.mp4", "mp4", "fast", workers=2,
                   style_over={"output": {"width": 320, "height": 180, "fps": 10}, "title": {"enabled": False},
                               "speed": {"enabled": False}})
    assert out.exists() and out.stat().st_size > 1000
    assert time.time() - t0 < 120
    assert "no progress" in capfd.readouterr().err
    v = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                                   "-show_entries", "stream=nb_read_frames", "-of", "json", str(out)],
                                  capture_output=True, text=True).stdout)["streams"][0]
    assert int(v["nb_read_frames"]) > 10


def test_workers_progress_while_parent_threads_hold_locks(av_proj):
    """The harness hang: a parent with live threads (the editor's HTTP server, a logging
    handler mid-emit, a library's thread pool) forked render workers that inherited a held
    lock and waited on it forever. Workers are now fresh interpreters: with a background
    thread holding the logging lock and another busy blurring for the whole export,
    the first frames still land within 10 s and the export completes."""
    import logging
    import threading
    import numpy as np
    from dolly import imgops
    from dolly import render as R
    held = threading.Event()
    release = threading.Event()

    def hold_logging_lock():
        h = logging.StreamHandler()
        logging.getLogger().addHandler(h)
        h.acquire()
        logging._acquireLock() if hasattr(logging, "_acquireLock") else logging._lock.acquire()
        held.set()
        release.wait(120)
        logging._releaseLock() if hasattr(logging, "_releaseLock") else logging._lock.release()
        h.release()

    def busy_blurring():
        a = np.random.rand(600, 600).astype(np.float32)
        while not release.is_set():
            imgops.gaussian_blur(a, 5)
    ts = [threading.Thread(target=hold_logging_lock, daemon=True), threading.Thread(target=busy_blurring, daemon=True)]
    for t in ts:
        t.start()
    held.wait(5)
    first = []
    t0 = time.time()

    def prog(i, n, el):
        if i > 0 and not first:
            first.append(time.time() - t0)
    try:
        out = R.export(av_proj, av_proj / "exports" / "threads.mp4", "mp4", "fast", workers=2, progress=prog,
                       style_over={"output": {"width": 320, "height": 180, "fps": 10}, "title": {"enabled": False},
                                   "speed": {"enabled": False}})
    finally:
        release.set()
    assert first and first[0] < 10.0, first
    assert out.exists() and out.stat().st_size > 1000


def test_chapter_chip_lasts_in_output_time():
    """A chapter that starts inside a 4x fast-forward still shows for 2.8 s of output."""
    from types import SimpleNamespace
    from dolly.render import Compositor
    tm = TimeMap(0.0, 60.0, [{"start": 10.0, "end": 30.0, "speed": 4.0}])
    me = SimpleNamespace(timemap=tm, style={"chapters": [{"t": 12.0, "text": "Two"}]}, CHAPTER_S=Compositor.CHAPTER_S)
    at = lambda ts: Compositor._chapter_at(me, ts)
    assert at(11.5)[0] is None
    assert at(12.0)[0] == 0
    # 2.6 s of output after the start is 10.4 s of source at 4x: still showing
    assert at(12.0 + 2.6 * 4)[0] == 0 and abs(at(12.0 + 2.6 * 4)[1] - 2.6) < 1e-6
    assert at(12.0 + 3.0 * 4)[0] is None
    # without a timemap it is plain source time
    me.timemap = None
    assert at(14.7)[0] == 0 and at(15.0)[0] is None

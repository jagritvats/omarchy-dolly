"""Audio tracks + camera, end to end with a real (private) PipeWire pulse server.

A private dbus + pipewire + wireplumber + pipewire-pulse runs in a temp runtime dir
(never the user's). Fake devices: a "speakers" null sink (system audio is its
monitor) and a "FakeMic" source fed by a 1 kHz tone; system audio is a 440 Hz
tone in 0.3 s bursts.

The stub screen recorder (for both gpu-screen-recorder and wf-recorder) is a tiny
compositor: it draws Dolly's sync marker when the stubbed `omarchy-shell ...
syncFlash` asks for it, and a white square while the system audio is loud. So
the video shows *when* each burst was heard, and the system track, after
finalize's alignment, must put each burst onset on the same frame (within 1).

Covers: all four audio modes on both backends (tracks exist, none dropped),
alignment within one frame, the export mix containing both tones (read off the samples),
muting the mic mid-way (silence in that span, the system tone still there),
the wf-recorder single-device bug, and the camera (test pattern feed): edit
mode records webcam.mkv aligned to the video, the export composites the
bubble; shapes, free position and camera-only ranges in the compositor.
"""
import array
import json
import math
import os
import shutil
import stat
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NEED = ["pipewire", "wireplumber", "pipewire-pulse", "dbus-daemon", "pactl", "parec", "ffmpeg"]
pytestmark = pytest.mark.skipif(any(shutil.which(x) is None for x in NEED),
                                reason="needs pipewire + pipewire-pulse + dbus for a private audio server")

FPS = 30

RECORDER = r'''#!/usr/bin/env python3
# stub screen recorder: marker on request, white square while system audio is loud
import os, subprocess, sys, time, signal
a = sys.argv[1:]
out = a[a.index("-f") + 1] if sys.argv[0].endswith("wf-recorder") else a[a.index("-o") + 1]
W, H, FPS = 640, 400, 30
flag = os.environ["STUB_FLASH_FLAG"]
enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                        "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
                        "-pix_fmt", "yuv420p", "-f", "matroska", out], stdin=subprocess.PIPE)
mon = subprocess.Popen(["parec", "-d", os.environ["STUB_MONITOR"], "--raw", "--format=s16le", "--rate=16000",
                        "--channels=1", "--latency-msec=5"], stdout=subprocess.PIPE)
os.set_blocking(mon.stdout.fileno(), False)
stop = False
def h(*_):
    global stop
    stop = True
signal.signal(signal.SIGINT, h); signal.signal(signal.SIGTERM, h)
def box(img, x0, y0, x1, y1, bgr):                  # fill columns x0..x1-1 of rows y0..y1-1
    for y in range(y0, y1):
        img[(y * W + x0) * 3:(y * W + x1) * 3] = bytes(bgr) * (x1 - x0)
t0 = time.monotonic(); i = 0; level = 0.0
while not stop:
    try:
        buf = mon.stdout.read()
    except Exception:
        buf = None
    if buf:
        x = memoryview(buf[: len(buf) // 2 * 2]).cast("h")[-160:]      # the last 10 ms, 16-bit samples
        if len(x):
            level = max(abs(v) for v in x) / 32768
    img = bytearray(bytes([40]) * (W * H * 3))
    if os.path.exists(flag):
        box(img, 0, 0, 40, 40, (255, 0, 255)); box(img, 10, 10, 30, 30, (0, 255, 0))
    if level > 0.05:
        box(img, 270, 150, 370, 250, (255, 255, 255))
    if i % 15 == 0:
        with open(out + ".frames", "a") as fl:          # ground truth: wall-clock time of frame i
            fl.write(f"{i} {time.time():.6f}\n")
    enc.stdin.write(img); enc.stdin.flush()
    i += 1
    d = t0 + i / FPS - time.monotonic()
    if d > 0:
        time.sleep(d)
mon.kill(); enc.stdin.close(); enc.wait()
'''
SHELL = r'''#!/usr/bin/env bash
# stub omarchy-shell: syncFlash shows the marker for ~70 ms (async, like the real overlay)
if [[ "$1" == "io.github.jagritvats.dolly" && "$2" == "syncFlash" ]]; then
  touch "$STUB_FLASH_FLAG"; ( sleep 0.07; rm -f "$STUB_FLASH_FLAG" ) >/dev/null 2>&1 & echo ok; exit 0
fi
[[ "$1" == "notifications" ]] && { echo off; exit 0; }
exit 0
'''
HYPRCTL = r'''#!/usr/bin/env bash
case "$*" in
  *monitors*) echo '[{"name":"DP-1","focused":true,"x":0,"y":0,"width":640,"height":400,"scale":1.0,"transform":0,"activeWorkspace":{"id":1}}]';;
  *cursorpos*) echo '{"x":300,"y":200}';;
  *activewindow*) echo '{}';;
  *clients*) echo '[]';;
  eval*) exit 1;;
  *) echo "{}";;
esac
'''


@pytest.fixture(scope="module")
def pulse(tmp_path_factory):
    rt = tmp_path_factory.mktemp("pw")
    os.chmod(rt, 0o700)
    env = dict(os.environ, XDG_RUNTIME_DIR=str(rt), DBUS_SESSION_BUS_ADDRESS=f"unix:path={rt}/bus",
               DOLLY_TEST_FIXTURE="1")     # lives for the module: the per-test reaper leaves it alone
    env.pop("PULSE_SERVER", None)
    procs = [subprocess.Popen(["dbus-daemon", "--session", f"--address=unix:path={rt}/bus", "--nofork"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
    time.sleep(0.3)
    for exe in ("pipewire", "wireplumber", "pipewire-pulse"):
        procs.append(subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True))
        time.sleep(0.6)
    server = f"unix:{rt}/pulse/native"
    penv = dict(os.environ, PULSE_SERVER=server, DOLLY_TEST_FIXTURE="1")
    ok = False
    for _ in range(30):
        if subprocess.run(["pactl", "info"], env=penv, capture_output=True).returncode == 0:
            ok = True
            break
        time.sleep(0.2)
    if not ok:
        for p in procs:
            p.kill()
        pytest.skip("private pipewire-pulse did not come up")

    def pa(*a):
        return subprocess.run(["pactl", *a], env=penv, capture_output=True, text=True).stdout.strip()
    pa("load-module", "module-null-sink", "sink_name=fake_speakers", "sink_properties=device.description=FakeSpeakers")
    pa("load-module", "module-null-sink", "sink_name=micfeed", "sink_properties=device.description=MicFeed")
    pa("load-module", "module-remap-source", "master=micfeed.monitor", "source_name=fake_mic",
       "source_properties=device.description=FakeMic")
    pa("set-default-sink", "fake_speakers")
    pa("set-default-source", "fake_mic")
    for _ in range(50):          # wireplumber applies defaults asynchronously (slow VMs)
        if pa("get-default-source") == "fake_mic" and pa("get-default-sink") == "fake_speakers":
            break
        time.sleep(0.1)
    # feeders: 440 Hz bursts (0.3 s every 1.5 s) as "system audio", a steady 1 kHz "voice"
    feeders = [
        subprocess.Popen(["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i",
                          "sine=frequency=440:sample_rate=48000,volume='if(lt(mod(t,1.5),0.3),0.9,0)':eval=frame",
                          "-f", "pulse", "-device", "fake_speakers", "sys"], env=penv, stdin=subprocess.DEVNULL),
        subprocess.Popen(["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
                          "-af", "volume=0.5", "-f", "pulse", "-device", "micfeed", "mic"], env=penv,
                         stdin=subprocess.DEVNULL)]
    time.sleep(0.8)
    yield {"server": server, "pa": pa}
    for p in feeders + procs[::-1]:
        p.kill()


@pytest.fixture()
def env(tmp_path, pulse):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("hyprctl", HYPRCTL), ("gpu-screen-recorder", RECORDER), ("wf-recorder", RECORDER),
                       ("omarchy-shell", SHELL)):
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    e = dict(os.environ)
    e.update({"PATH": f"{bindir}:{e['PATH']}", "DOLLY_DIR": str(tmp_path / "videos"),
              "XDG_RUNTIME_DIR": str(tmp_path / "run"), "DOLLY_OPEN_EDITOR": "0", "PYTHONPATH": str(ROOT),
              "HOME": str(tmp_path), "PULSE_SERVER": pulse["server"], "STUB_FLASH_FLAG": str(tmp_path / "flash"),
              "STUB_MONITOR": "fake_speakers.monitor", "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
              "DOLLY_WEBCAM_INPUT": "lavfi:testsrc2=size=1280x720:rate=30"})
    e.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
    (tmp_path / "run").mkdir()
    return e


def dolly(env, *args, check=True):
    r = subprocess.run([sys.executable, "-m", "dolly.cli", *args], env=env, capture_output=True, text=True,
                       timeout=180)
    if check and r.returncode != 0:
        raise AssertionError(f"dolly {' '.join(args)} failed ({r.returncode}):\n{r.stdout}\n{r.stderr[-3000:]}")
    return r


def record(env, backend, audio, seconds=4.6, camera="off", mute_at=None):
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", str(FPS), "--audio", audio,
              "--camera", camera, "--backend", backend)
    pdir = Path(r.stdout.strip().splitlines()[-1])
    t0 = time.time()
    if mute_at:
        time.sleep(mute_at[0])
        dolly(env, "mute")
        time.sleep(mute_at[1] - mute_at[0])
        dolly(env, "mute")
    time.sleep(max(0.0, seconds - (time.time() - t0)))
    dolly(env, "stop")
    return pdir


def decode_audio(f: Path, sr=16000, start=None) -> list:
    """The file's sound as one channel of samples in -1..1."""
    args = ["ffmpeg", "-v", "error", "-i", str(f), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    out = subprocess.run(args, capture_output=True).stdout
    x = array.array("h")
    x.frombytes(out[: len(out) // 2 * 2])
    return [v / 32768 for v in x]


TONES = (440, 1000)          # the two feeds: system audio and the "voice"


def _tone(x, sr, f0):
    """How strong the tone f0 is in the samples x: the largest value of their spectrum
    (smoothly windowed) within a few hertz of f0. The tracks' clocks differ a little."""
    n = len(x)
    xw = [v * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1))) for i, v in enumerate(x)]
    best = 0.0
    for k in range(-8, 9):
        w = 2 * math.pi * (f0 + 0.5 * k) / sr
        re = math.sumprod(xw, [math.cos(w * i) for i in range(n)])
        im = math.sumprod(xw, [math.sin(w * i) for i in range(n)])
        best = max(best, math.hypot(re, im))
    return best


def band(x, sr, f0):
    """0..1: the tone f0 against the stronger of the two tones the feeds play."""
    if len(x) < 256:
        return 0.0
    return _tone(x, sr, f0) / (max(_tone(x, sr, f) for f in TONES) + 1e-9)


def onsets(env_curve, t, thresh):
    """The times at which the curve rises above the threshold."""
    return [t[i] for i in range(1, len(env_curve)) if env_curve[i] > thresh and not env_curve[i - 1] > thresh]


@pytest.mark.parametrize("backend", ["gsr", "wf-recorder"])
def test_all_audio_modes_record_separate_tracks(env, backend):
    want = {"none": set(), "mic": {"mic"}, "system": {"system"}, "both": {"mic", "system"}}
    for mode, tracks in want.items():
        pdir = record(env, backend, mode, seconds=2.4)
        got = {f.stem for f in (pdir / "tracks").glob("*.mka")} if (pdir / "tracks").exists() else set()
        assert got == tracks, (backend, mode, got)
        ev = json.loads((pdir / "events.json").read_text())
        assert {t["track"] for t in ev["capture"]["audio_tracks"]} == tracks
        meta = json.loads((pdir / "capture.json").read_text())
        assert not meta.get("audio_warnings"), meta.get("audio_warnings")
        for f in (pdir / "tracks").glob("*.mka") if tracks else []:
            x = decode_audio(f)
            assert len(x) > 16000 and band(x, 16000, 1000 if f.stem == "mic" else 440) > 0.5, f


@pytest.mark.parametrize("backend", ["gsr", "wf-recorder"])
def test_tracks_aligned_within_a_frame_and_mixed(env, backend):
    pdir = record(env, backend, "both", seconds=7.5, mute_at=(3.2, 5.4))
    ev = json.loads((pdir / "events.json").read_text())
    assert ev["capture"]["sync"]["method"].startswith("markers"), ev["capture"]["sync"]
    tr = {t["track"]: t for t in ev["capture"]["audio_tracks"]}
    # 1. the time base: a wall-clock instant maps to the video frame drawn at that instant.
    #    The stub logs the wall time of every 15th frame it draws; the system track's
    #    alignment (wall -> logger -> sync-marker fit -> video) must put it on that frame.
    from dolly.audio import start_time
    st = start_time(pdir / tr["system"]["file"])
    frames = [tuple(map(float, l.split())) for l in (pdir / "raw.mkv.frames").read_text().splitlines()]
    errs = []
    for i, wall in frames[2:-2]:
        v_pred = tr["system"]["offset"] + (wall - st) * tr["system"]["scale"]
        errs.append(v_pred - i / FPS)
    if os.environ.get("DOLLY_TEST_DEBUG"):
        print("PDIR", pdir)
    assert max(abs(e) for e in errs) <= 1.0 / FPS, errs            # within one frame
    assert abs(tr["mic"]["offset"] - tr["system"]["offset"] - (start_time(pdir / tr["mic"]["file"]) - st)
               * tr["mic"]["scale"]) < 1e-3                         # both tracks on the same clock
    # 2. the real signal agrees: bursts in the system track land where the stub showed them
    #    (the stub sees them through its own monitor stream and draws on the next frame,
    #    so the video is late by that latency; allow it, plus one frame)
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(pdir / "raw.mkv"), "-vf", "crop=20:20:310:190",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
    v = [sum(out[i:i + 400]) / 400 for i in range(0, len(out) - 399, 400)]       # the 20 x 20 crop of every frame
    vid_on = onsets(v, [i / FPS for i in range(len(v))], 128)
    sr = 16000
    x = decode_audio(pdir / tr["system"]["file"], sr)
    hop = sr // 200
    envl = [max(abs(s) for s in x[i:i + hop]) for i in range(0, len(x) - hop, hop)]
    aud_on = onsets(envl, [tr["system"]["offset"] + i * hop / sr * tr["system"]["scale"] for i in range(len(envl))], 0.05)
    lag = [v_ - max([a for a in aud_on if a <= v_ + 0.05], default=-9) for v_ in vid_on if v_ > aud_on[0] and v_ > 0.5]       # (the first frames may start mid-burst)
    assert len(lag) >= 3 and all(-1.0 / FPS <= d <= 0.2 for d in lag), (lag, vid_on, aud_on)
    # mix: both tones; mic silent while muted (a span, not a cut: the length is unchanged)
    mutes = ev["capture"]["mic_mutes"]
    assert len(mutes) == 1 and 1.8 < mutes[0][1] - mutes[0][0] < 2.6
    out_mp4 = Path(dolly(env, "export", str(pdir), "--quality", "draft", "-q").stdout.strip().splitlines()[-1])
    y = decode_audio(out_mp4, sr)
    trim = json.loads((pdir / "project.json").read_text()).get("trim") or [0, ev["video"]["duration"]]
    assert abs(len(y) / sr - (trim[1] - trim[0])) < 0.3
    trim0 = (json.loads((pdir / "project.json").read_text()).get("trim") or [0])[0]
    a, b = mutes[0][0] - trim0, mutes[0][1] - trim0          # export starts after the sync marker
    pre = y[int(0.8 * sr):int(2.8 * sr)]
    if os.environ.get("DOLLY_TEST_DEBUG"):
        print("MUTE", a, b, "TRIM", trim, "LEN", len(y) / sr, "DUR", ev["video"]["duration"])
        for nm in ("mic", "system"):
            z = decode_audio(pdir / tr[nm]["file"], sr)
            o = tr[nm]["offset"]
            print("TRK", nm, o, [round(max((abs(s) for s in z[max(0, int((k / 2 - o) * sr)):max(0, int(((k + 1) / 2 - o) * sr))]), default=0), 3) for k in range(20)])
        print("ENV", [round(max(abs(s) for s in y[k * sr // 2:(k + 1) * sr // 2]), 3) for k in range(int(len(y) / sr * 2))])
    inside = y[int((a + 0.2) * sr):int((b - 0.2) * sr)]          # >= 1.5 s: holds a system burst
    assert band(pre, sr, 1000) > 0.2 and band(pre, sr, 440) > 0.05
    assert band(inside, sr, 1000) < 0.05 and band(inside, sr, 440) > 0.5


def test_wf_recorder_never_drops_a_source_silently():
    from dolly import capture
    from dolly.monitors import Target
    t = Target("monitor", "DP-1", [0, 0, 100, 100], 1.0, (100.0, 100.0), physical=(100, 100))
    with pytest.raises(ValueError):
        capture.recorder_cmd("wf", t, Path("/tmp/x.mkv"), 60, ["default_output", "default_input"])
    # Dolly itself never asks the recorder for audio: tracks are separate on every backend
    src = (ROOT / "dolly" / "capture.py").read_text()
    assert "recorder_cmd(backend, tgt, raw, a.fps, []," in src


def test_audio_devices_and_meter(env, pulse):
    d = json.loads(dolly(env, "audio", "devices").stdout)
    assert d["default_source"] == "fake_mic" and d["default_sink"] == "fake_speakers"
    assert "fake_mic" in [s["name"] for s in d["sources"]] and not any(s["name"].endswith(".monitor") for s in d["sources"])
    r = dolly(env, "audio", "meter", "fake_mic", "--seconds", "0.6")
    peaks = [json.loads(l)["peak"] for l in r.stdout.splitlines() if l.strip()]
    assert len(peaks) >= 3 and max(peaks) > 0.03     # ffmpeg sine: amplitude 1/8, x0.5


def test_camera_edit_mode_records_aligned_and_composites(env):
    pdir = record(env, "gsr", "none", seconds=3.0, camera="edit")
    assert (pdir / "webcam.mkv").exists()
    ev = json.loads((pdir / "events.json").read_text())
    off = ev["capture"]["webcam_offset"]
    assert ev["capture"]["camera_mode"] == "edit"
    # ground truth: the wall time of the camera's first frame on the stub's frame clock
    from dolly.audio import start_time
    cam_wall = start_time(pdir / "webcam.mkv")                     # lavfi feed: wall-clock pts
    from dolly import num
    fr = [tuple(map(float, l.split())) for l in (pdir / "raw.mkv.frames").read_text().splitlines()]
    expected = num.interp(cam_wall, [wall for _, wall in fr], [i for i, _ in fr]) / FPS
    # 0.1 s: lavfi's wall-clock pts jitter on a loaded VM (seen: 80 ms on a 4 vCPU VirtualBox VM);
    # real cameras are checked by hand within +-0.3 s (AGENTS.md test 16)
    assert abs(off - expected) <= max(1.5 / FPS, 0.1), (off, expected, cam_wall, fr[:2], json.loads((pdir / 'capture.json').read_text()).get('webcam_clock'))
    out = Path(dolly(env, "export", str(pdir), "--quality", "draft", "-q").stdout.strip().splitlines()[-1])
    from dolly import gfx
    fr = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.5", "-i", str(out), "-frames:v", "1", "-f", "rawvideo",
                         "-pix_fmt", "bgr0", "-"], capture_output=True).stdout
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
                                      capture_output=True, text=True).stdout)["streams"][0]
    w, h = probe["width"], probe["height"]
    corner = gfx.crop(gfx.frame_from(bytearray(fr), w, h), int(w * 0.03), int(h * 0.72),
                      int(w * 0.2) - int(w * 0.03), int(h * 0.95) - int(h * 0.72))
    values = b"".join(gfx.plane(corner, ch) for ch in "rgb")
    assert statistics.pstdev(values) > 25          # the test pattern bubble (colour bars), not flat background


def test_bubble_shapes_position_and_camera_only_ranges():
    from dolly import project as projmod
    from dolly.events import Timeline
    from dolly.render import Compositor
    from dolly.zoom import ZoomConfig
    tl = Timeline.from_dict({"version": 2, "video": {"w": 640, "h": 400, "fps": 30, "duration": 10},
                             "capture": {"scale": 1.0}, "events": []})
    st = projmod.deep_merge(projmod.DEFAULT_STYLE, {"webcam": {"enabled": True, "shape": "rect", "size": 0.3,
                                                               "pos": [0.5, 0.5], "auto_move": False,
                                                               "full": [[4.0, 6.0]]}})
    comp = Compositor(tl, st, [], ZoomConfig(), 0, 10)
    x0, y0, w, h, r, u = comp.cam_geometry(1.0)
    OW, OH = comp.lay.OW, comp.lay.OH
    assert abs(w / h - 4 / 3) < 0.02 and abs(x0 + w / 2 - OW / 2) < 2 and abs(y0 + h / 2 - OH / 2) < 2 and u == 0
    g = comp.cam_geometry(5.0)
    assert g[5] == 1 and (g[2], g[3]) == (OW, OH)                  # camera only: full frame
    mid = comp.cam_geometry(3.8)
    assert 0 < mid[5] < 1 and w < mid[2] < OW                       # eased transition, no jump
    from dolly import gfx
    cam = gfx.new(854, 480)
    gfx.fill_rect(cam, 0, 0, 854, 480, gfx.rgb((0, 200, 0)))
    out = comp.compose_at(5.0, gfx.new(640, 400), cam)
    assert gfx.pixel(out, OW // 2, OH // 2)[1] > 150 and gfx.pixel(out, 5, 5)[1] > 150
    st2 = projmod.deep_merge(st, {"webcam": {"shape": "circle", "pos": None, "corner": "top-right", "full": []}})
    c2 = Compositor(tl, st2, [], ZoomConfig(), 0, 10)
    x0, y0, w, h, r, u = c2.cam_geometry(1.0)
    assert w == h and r == h / 2 and x0 > OW / 2 and y0 < OH / 2

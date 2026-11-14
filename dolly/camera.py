"""Camera bubble (off by default).

Modes
  off    no camera
  edit   "Bubble in edit": a live preview only during the countdown so you can frame
         yourself; it closes when recording starts, the camera is recorded to
         webcam.mkv and the bubble is composited at export (shape, size, position
         stay editable).
  live   "Live bubble": an always-on-top, draggable bubble that is part of the
         screen recording (what you see is what you get). No separate camera file,
         so an export never shows two bubbles.

The on-screen bubble follows Omarchy's own WebcamOverlay: an mpv window on the
camera with its own app id (`DollyCam-<size>`), floated, pinned, never focused,
moved to a corner. Hyprland draws the shape: `rounding` (a circle on a square
window), the border in the theme accent and the shadow.

Devices come from /sys/class/video4linux (capture nodes only); the format is the
camera's own list (v4l2-ctl, else ffmpeg -list_formats), preferring 720p30 MJPEG.
Tests and the harness feed a labelled test pattern: DOLLY_WEBCAM_INPUT=
"lavfi:testsrc2=size=1280x720:rate=30" (any ffmpeg lavfi graph).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

MODES = ("off", "edit", "live")
SIZES = {"S": 0.16, "M": 0.22, "L": 0.30}          # bubble diameter / short side of the output
APP_ID = "DollyCam"


def fake_input() -> str | None:
    v = os.environ.get("DOLLY_WEBCAM_INPUT", "")
    return v[len("lavfi:"):] if v.startswith("lavfi:") else None


def devices() -> list[dict]:
    """[{"device": "/dev/video0", "name": "Integrated Camera"}], capture nodes only."""
    if fake_input():
        return [{"device": "lavfi", "name": "Test pattern (DOLLY_WEBCAM_INPUT)", "test": True}]
    out = []
    base = Path(os.environ.get("DOLLY_V4L_SYSFS", "/sys/class/video4linux"))
    for d in sorted(base.glob("video*"), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        try:
            if (d / "index").exists() and (d / "index").read_text().strip() != "0":
                continue                     # metadata node of a camera
            name = (d / "name").read_text().strip()
        except OSError:
            continue
        dev = f"/dev/{d.name}"
        caps = _caps(dev)
        if caps is False:
            continue
        out.append({"device": dev, "name": name})
    return out


def _caps(dev: str):
    """True/False when v4l2-ctl can tell whether the node captures video, None if unknown."""
    if not shutil.which("v4l2-ctl"):
        return None
    try:
        info = subprocess.run(["v4l2-ctl", "--device", dev, "--info"], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return None
    m = re.search(r"Device Caps\s*:.*?\n((?:\s+.*\n)+)", info)
    return bool(m and "Video Capture" in m.group(1)) if m else None


def formats(dev: str) -> list[dict]:
    """[{"fmt": "mjpeg"|"yuyv422"|..., "w", "h", "fps"}] from the camera's own list."""
    out = []
    if shutil.which("v4l2-ctl"):
        try:
            txt = subprocess.run(["v4l2-ctl", "--list-formats-ext", "-d", dev], capture_output=True, text=True,
                                 timeout=3).stdout
        except Exception:
            txt = ""
        fmt, size = None, None
        for line in txt.splitlines():
            m = re.search(r"'(\w+)'", line)
            if m and "[" in line:
                fmt = {"MJPG": "mjpeg", "YUYV": "yuyv422", "H264": "h264"}.get(m.group(1), m.group(1).lower())
            m = re.search(r"Size: Discrete (\d+)x(\d+)", line)
            if m:
                size = (int(m.group(1)), int(m.group(2)))
            m = re.search(r"\((\d+(?:\.\d+)?) fps\)", line)
            if m and fmt and size:
                out.append({"fmt": fmt, "w": size[0], "h": size[1], "fps": float(m.group(1))})
    return out


def pick_format(fmts: list[dict]) -> dict | None:
    """Prefer 1280x720 @ 30 MJPEG; else the largest <= 720p at >= 24 fps; else anything."""
    if not fmts:
        return None

    def score(f):
        return ((f["w"], f["h"]) == (1280, 720), f["fmt"] == "mjpeg", 24 <= f["fps"] <= 30,
                f["h"] <= 720, f["w"] * f["h"], -abs(f["fps"] - 30))
    return max(fmts, key=score)


def input_args(dev: str | None = None) -> tuple[list[str], str]:
    """ffmpeg input args for the camera and the clock its timestamps use."""
    fake = fake_input()
    if fake:
        # a generated test feed has no capture clock: stamp each frame with the wall clock
        # as it leaves the (real-time paced) source, in microseconds (see record_cmd)
        return ["-re", "-f", "lavfi", "-i", fake], "wall"
    dev = dev or (devices() or [{}])[0].get("device")
    if not dev:
        raise CameraError("no camera found")
    f = pick_format(formats(dev)) or {"fmt": "mjpeg", "w": 1280, "h": 720, "fps": 30}
    args = ["-f", "v4l2", "-timestamps", "abs", "-input_format", f["fmt"], "-framerate", str(int(f["fps"])),
            "-video_size", f"{f['w']}x{f['h']}", "-i", dev]
    return args, "mono"


class CameraError(RuntimeError):
    pass


def explain(stderr: str, dev: str | None) -> str:
    s = stderr.lower()
    if "busy" in s:
        return f"The camera {dev or ''} is busy (another app is using it). Close it and try again."
    if "permission" in s:
        return f"No permission to open {dev or 'the camera'}: your account needs to be in the video group (an administrator adds it), then log in again."
    if "no such file" in s or "no camera" in s:
        return "No camera found."
    return (stderr.strip().splitlines() or ["the camera could not be opened"])[-1][:200]


def record_cmd(out: Path, dev: str | None = None) -> tuple[list[str], str]:
    from .ff import ffmpeg_bin
    args, clock = input_args(dev)
    vf = ["-vf", "settb=AVTB,setpts=RTCTIME"] if clock == "wall" else []
    cmd = [ffmpeg_bin(), "-v", "error", "-nostdin", "-y", *args, *vf, "-copyts", "-an", "-c:v", "libx264",
           "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p", "-f", "matroska", str(out)]
    return cmd, clock


def start_recording(pdir: Path, dev: str | None, log) -> tuple[subprocess.Popen | None, str, str | None]:
    """-> (process, clock, error)."""
    try:
        cmd, clock = record_cmd(pdir / "webcam.mkv", dev)
    except CameraError as e:
        return None, "", str(e)
    log.write(f"$ {' '.join(cmd)}\n")
    log.flush()
    errf = pdir / "webcam.log"
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(errf, "w"),
                         start_new_session=True)
    t_end = time.monotonic() + 3.0
    while time.monotonic() < t_end and p.poll() is None and not ((pdir / "webcam.mkv").exists()
                                                                  and (pdir / "webcam.mkv").stat().st_size > 0):
        time.sleep(0.05)
    if p.poll() is not None:
        return None, clock, explain(errf.read_text() if errf.exists() else "", dev)
    return p, clock, None


# ------------------------------------------------------------------ on-screen bubble (mpv)
def bubble_cmd(dev: str | None, size: str = "M", mirror: bool = True, shape: str = "circle") -> list[str]:
    """Omarchy WebcamOverlay-style mpv window; square crop for circle/rounded."""
    fake = fake_input()
    crop = "crop=ih:ih" if shape in ("circle", "rounded") else "crop=ih*4/3:ih"
    vf = f"lavfi=[{crop}{',hflip' if mirror else ''}]"
    src = f"av://lavfi:{fake}" if fake else f"av://v4l2:{dev or (devices() or [{}])[0].get('device', '/dev/video0')}"
    px = int(round(280 * {"S": 0.75, "M": 1.0, "L": 1.35}.get(size, 1.0)))
    return ["mpv", src, "--profile=low-latency", "--untimed", "--no-cache", f"--vf={vf}",
            f"--autofit={px}x{px}", "--force-window=immediate",
            f"--title={APP_ID}", f"--wayland-app-id={APP_ID}-{size}", "--no-border", "--no-audio", "--no-osc",
            "--osd-level=0", "--really-quiet", "--keepaspect-window=yes"]


def _hypr(*args) -> str:
    try:
        return subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return ""


def bubble_window() -> dict | None:
    try:
        for c in json.loads(_hypr("clients", "-j") or "[]"):
            if c.get("title") == APP_ID or str(c.get("class", "")).startswith(APP_ID):
                return c
    except ValueError:
        pass
    return None


def bubble_geometry(region, size: str, corner: str, shape: str = "circle", margin: int = 40) -> list[int]:
    """[x, y, w, h] (logical) for the bubble in a corner of the recorded region."""
    x, y, w, h = region
    d = int(round(min(w, h) * SIZES.get(size, SIZES["M"])))
    bw, bh = (d, d) if shape in ("circle", "rounded") else (int(d * 4 / 3), d)
    bx = x + margin if "left" in corner else x + w - bw - margin
    by = y + h - bh - margin if "bottom" in corner else y + margin
    return [bx, by, bw, bh]


def nearest_corner(win_rect, region) -> str:
    cx, cy = win_rect[0] + win_rect[2] / 2, win_rect[1] + win_rect[3] / 2
    rx, ry, rw, rh = region
    return ("top" if cy < ry + rh / 2 else "bottom") + "-" + ("left" if cx < rx + rw / 2 else "right")


def place_bubble(region, size="M", corner="bottom-right", shape="circle", accent="#7aa2f7", border=3,
                 shadow=True) -> bool:
    """Float, pin, unfocusable, sized and moved; Hyprland draws the shape and border."""
    w = bubble_window()
    if not w:
        return False
    from . import hypr
    addr = f"address:{w['address']}"
    gx, gy, gw, gh = bubble_geometry(region, size, corner, shape)
    ok = True
    if not w.get("floating"):
        ok &= hypr.float_on(addr)
    if not w.get("pinned"):
        ok &= hypr.pin_on(addr)
    ok &= hypr.resize_exact(addr, gw, gh)
    ok &= hypr.move_exact(addr, gx, gy)
    radius = gh // 2 if shape == "circle" else (int(gh * 0.18) if shape == "rounded" else 0)
    col = "rgb(" + accent.lstrip("#") + ")"
    for prop, val in (("rounding", str(radius)), ("border_size", str(border)), ("active_border_color", col),
                      ("inactive_border_color", col), ("no_focus", "1"), ("no_shadow", "0" if shadow else "1"),
                      ("no_dim", "1")):
        hypr.set_prop(addr, prop, val)        # cosmetic: a missing property must not fail placement
    return ok


def snap_bubble(region, size=None, shape="circle", **kw) -> str | None:
    """After a drag: snap to the nearest corner (and optionally change size)."""
    w = bubble_window()
    if not w:
        return None
    corner = nearest_corner([*w["at"], *w["size"]], region)
    if size is None:
        cur = w["size"][1] / max(1, min(region[2], region[3]))
        size = min(SIZES, key=lambda k: abs(SIZES[k] - cur))
    place_bubble(region, size, corner, shape, **kw)
    return corner


def next_size(size: str) -> str:
    order = list(SIZES)
    return order[(order.index(size) + 1) % len(order)] if size in order else "M"


# ------------------------------------------------------------------ alignment
def align(meta: dict, fit, cam: Path) -> dict | None:
    """Camera start -> video seconds through the sync fit (offset + drift)."""
    from .audio import start_time
    st = start_time(cam)
    if st is None:
        return None
    clock = meta.get("webcam_clock", "mono")
    mono = st if clock == "mono" else st - (meta.get("clock_wall", 0) - meta.get("clock_mono", 0))
    log0 = meta.get("logger_t0_mono")
    if log0 is not None:
        off = fit.map(mono - log0)
    elif meta.get("t0_mono") is not None:
        off = mono - meta["t0_mono"]
    else:
        return None
    if not -10 < off < 10:
        return None
    return {"offset": round(off, 4), "scale": round(float(getattr(fit, "b", 1.0) or 1.0), 6)}

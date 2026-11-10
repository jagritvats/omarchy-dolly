#!/usr/bin/env python3
"""Synthetic recording generator for Dolly's test harness.

Produces a project directory that looks exactly like a real Dolly capture:

    <out>/raw.mp4          2560x1600 (1280x800 @2x, like a HiDPI laptop), CFR, no cursor
    <out>/raw-cursor.mp4   same, with a plain system cursor burned in ("before" reference)
    <out>/events.raw.jsonl what the Hyprland logger would write (logical px, ms)
    <out>/events.json      normalized timeline (video px, s)  -> dolly.events

The "desktop" is harness/desktop.html driven deterministically by
window.renderAt(t, x, y). The scripted user opens a modal, types a task title,
picks an assignee from a dropdown, saves with Ctrl+S, ticks the task off, then
switches to a terminal and runs two commands. The cursor follows humanlike
paths (minimum-jerk with slight curvature, overshoot-free, micro-jitter) and
idles between beats.

Usage: python3 harness/make_demo.py OUT_DIR [--fps 30] [--scale 2]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import random
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
LOGICAL_W, LOGICAL_H = 1280, 800


# ------------------------------------------------------------ cursor paths
def min_jerk(u):
    return u ** 3 * (10 - 15 * u + 6 * u * u)


class Script:
    def __init__(self, seed=3):
        self.rng = random.Random(seed)
        self.t = 0.0
        self.x, self.y = 900.0, 600.0
        self.moves: list[tuple[float, float, float]] = [(0.0, self.x, self.y)]
        self.clicks: list[dict] = []
        self.keys: list[dict] = []

    def wait(self, dt, drift=True):
        """Idle; hand resting on the mouse gives tiny occasional nudges."""
        end = self.t + dt
        if drift and dt > 0.8:
            n = int(dt / 0.9)
            for _ in range(n):
                tt = self.t + self.rng.uniform(0.3, 0.8)
                if tt >= end:
                    break
                self._move_to(self.x + self.rng.uniform(-3, 3), self.y + self.rng.uniform(-2, 2),
                              self.rng.uniform(0.12, 0.2), start=tt, curve=0)
        self.t = max(self.t, end)

    def _move_to(self, x, y, dur, start=None, curve=1.0):
        t0 = self.t if start is None else start
        x0, y0 = self.x, self.y
        dist = math.hypot(x - x0, y - y0)
        # perpendicular bow, like a wrist arc
        nx, ny = (-(y - y0) / dist, (x - x0) / dist) if dist > 1 else (0, 0)
        bow = curve * self.rng.uniform(-0.08, 0.12) * dist
        steps = max(2, int(dur * 125))  # ~125 Hz like a real mouse poll via IPC
        for i in range(1, steps + 1):
            u = i / steps
            e = min_jerk(u)
            b = math.sin(math.pi * u) * bow
            jx = self.rng.gauss(0, 0.35) if 0 < u < 1 else 0
            jy = self.rng.gauss(0, 0.35) if 0 < u < 1 else 0
            px = x0 + (x - x0) * e + nx * b + jx
            py = y0 + (y - y0) * e + ny * b + jy
            self.moves.append((t0 + dur * u, px, py))
        self.x, self.y = x, y
        self.t = t0 + dur

    def move(self, x, y, speed=1.0):
        dist = math.hypot(x - self.x, y - self.y)
        # Fitts-ish duration
        dur = (0.22 + 0.11 * math.log2(1 + dist / 18)) / speed
        self._move_to(x, y, dur)

    def click(self, button="left"):
        self.t += self.rng.uniform(0.08, 0.14)
        self.clicks.append({"t": self.t, "x": self.x, "y": self.y, "button": button})
        self.t += 0.09

    def type(self, text, wpm=70, out=None):
        cps = wpm * 5 / 60
        for ch in text:
            self.t += self.rng.uniform(0.55, 1.45) / cps
            if ch == " " and self.rng.random() < 0.25:
                self.t += 0.15
            key = {" ": "space"}.get(ch, ch)
            self.keys.append({"t": self.t, "key": key, "mods": ["shift"] if ch.isupper() else []})
            if out is not None:
                out.append({"t": self.t, "ch": ch})

    def chord(self, mods, key):
        self.t += 0.18
        self.keys.append({"t": self.t, "key": key, "mods": mods})
        self.t += 0.12


def build_script():
    s = Script()
    B: dict = {"title": "Record the launch demo", "assignee": "Priya",
               "titleKeys": [], "cmd1Keys": [], "cmd2Keys": []}
    s.wait(1.2)
    # 1. click "+ New task" (top-right of the app)
    s.move(1194, 107)
    s.wait(0.25, drift=False)
    s.click()
    B["openModal"] = s.t
    s.wait(0.45, drift=False)
    # 2. click the title field and type
    s.move(800, 222)
    s.click()
    B["titleFocus"] = s.t
    s.wait(0.25, drift=False)
    s.move(s.x + 40, s.y + 26, speed=1.6)   # move the mouse out of the way a little
    s.type(B["title"], wpm=78, out=B["titleKeys"])
    s.wait(0.5, drift=False)
    # 3. open assignee dropdown, pick Priya
    s.move(820, 282)
    s.click()
    B["ddOpen"] = s.t
    s.wait(0.35, drift=False)
    s.move(760, 340, speed=1.2)
    s.wait(0.25, drift=False)
    s.move(762, 373, speed=1.4)
    s.wait(0.2, drift=False)
    s.click()
    B["ddPick"] = s.t
    s.wait(0.6)
    # 4. save with Ctrl+S
    s.chord(["ctrl"], "s")
    B["save"] = s.t
    s.wait(1.6)
    # 5. tick the new task off
    s.move(705, 184)
    s.wait(0.2, drift=False)
    s.click()
    B["check"] = s.t
    s.wait(1.2)
    # reading / thinking: a real idle stretch (hand off the mouse) -> idle cut demo
    s.wait(5.0, drift=False)
    # 6. switch to the terminal (big jump left) and run commands
    s.move(232, 172, speed=0.9)
    s.click()
    B["termFocus"] = s.t
    s.wait(0.35, drift=False)
    s.type("bin/rails test test/models/task_test.rb", wpm=95, out=B["cmd1Keys"])
    s.wait(0.25, drift=False)
    s.keys.append({"t": s.t, "key": "Return", "mods": []})
    B["cmd1Enter"] = s.t
    out1 = ['<span class="d">Running 12 tests in parallel using 8 processes</span>',
            '<span class="d">Run options: --seed 40211</span>', '',
            '<span class="g">............</span>', '',
            '<span class="d">Finished in 0.84s, 14.3 runs/s</span>',
            '<span class="g">12 runs, 31 assertions, 0 failures, 0 errors, 0 skips</span>']
    B["out1"] = [{"t": s.t + 0.25 + 0.22 * i, "html": h} for i, h in enumerate(out1)]
    s.t = B["out1"][-1]["t"] + 0.9
    s.type("git commit -am \"Add task assignees\"", wpm=95, out=B["cmd2Keys"])
    s.wait(0.2, drift=False)
    s.keys.append({"t": s.t, "key": "Return", "mods": []})
    B["cmd2Enter"] = s.t
    out2 = ['[main 4f1c2ab] Add task assignees', ' 3 files changed, 48 insertions(+), 6 deletions(-)']
    B["out2"] = [{"t": s.t + 0.2 + 0.15 * i, "html": h} for i, h in enumerate(out2)]
    s.t = B["out2"][-1]["t"] + 0.6
    s.wait(1.4)
    B["backToApp"] = 1e9
    s.wait(0.8)
    return s, B


# ------------------------------------------------------------------ capture
def chrome_path() -> str | None:
    import os
    import shutil
    env = os.environ.get("CHROME")
    if env and Path(env).exists():
        return env
    for name in ("chromium", "google-chrome-stable", "google-chrome", "chromium-browser", "brave"):
        p = shutil.which(name)
        if p:
            return p
    return None  # let playwright use its own downloaded browser


async def capture(out: Path, fps: int, scale: int, s: Script, B: dict):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("make_demo: needs playwright (pip install --user playwright, or in a venv) and a Chromium browser")
    W, H = LOGICAL_W * scale, LOGICAL_H * scale
    duration = s.t
    n = int(math.floor(duration * fps))
    mt = np.array([m[0] for m in s.moves])
    mx = np.array([m[1] for m in s.moves])
    my = np.array([m[2] for m in s.moves])

    def cur(t):
        i = int(np.searchsorted(mt, t, side="right"))
        if i <= 0:
            return mx[0], my[0]
        if i >= len(mt):
            return mx[-1], my[-1]
        return mx[i - 1], my[i - 1]  # logger semantics: last reported position

    raw = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "image2pipe", "-framerate", str(fps), "-i", "-",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p",
                            "-r", str(fps), "-fps_mode", "cfr", str(out / "raw.mp4")], stdin=subprocess.PIPE)
    sig_cache: dict[str, bytes] = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=chrome_path(), args=["--no-sandbox", "--hide-scrollbars", "--disable-background-networking", "--disable-component-update"])
        pg = await b.new_page(viewport={"width": LOGICAL_W, "height": LOGICAL_H}, device_scale_factor=scale)
        await pg.goto((HERE / "desktop.html").as_uri())
        await pg.evaluate(f"window.BEATS = {json.dumps(B)}")
        await pg.wait_for_timeout(400)
        cdp = await pg.context.new_cdp_session(pg)
        await cdp.send("Emulation.setDeviceMetricsOverride", {"width": LOGICAL_W, "height": LOGICAL_H,
                                                              "deviceScaleFactor": scale, "mobile": False})
        await pg.wait_for_timeout(200)
        frames = []
        for i in range(n):
            t = i / fps
            x, y = cur(t)
            sig = await pg.evaluate("([t,x,y]) => window.renderAt(t,x,y)", [t, float(x), float(y)])
            if sig not in sig_cache:
                r = await cdp.send("Page.captureScreenshot", {"format": "png", "optimizeForSpeed": True})
                sig_cache[sig] = base64.b64decode(r["data"])
            raw.stdin.write(sig_cache[sig])
            frames.append(sig)
            if i % 60 == 0:
                sys.stderr.write(f"\r  synth frame {i}/{n} (unique {len(sig_cache)})")
        sys.stderr.write("\n")
        win = await pg.evaluate("window.rectOf('#app')")
        term = await pg.evaluate("window.rectOf('#term')")
        await b.close()
    raw.stdin.close()
    raw.wait()
    return n / fps, (win, term)


def burn_cursor(out: Path, fps: int, scale: int, s: Script):
    """'Before' reference: raw capture with a plain cursor, no zoom/frame."""
    import cv2
    from PIL import Image, ImageDraw
    sz = int(22 * scale)
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    k = sz / 27
    pts = [(2 * k, 1.5 * k), (2 * k, 21 * k), (7 * k, 16.5 * k), (10.4 * k, 24.6 * k), (13.6 * k, 23.2 * k), (10.2 * k, 15.4 * k), (17 * k, 15.2 * k)]
    ImageDraw.Draw(img).polygon(pts, fill=(0, 0, 0, 255), outline=(255, 255, 255, 255))
    arr = np.asarray(img, np.float32) / 255
    mt = np.array([m[0] for m in s.moves])
    W, H = LOGICAL_W * scale, LOGICAL_H * scale
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(out / "raw.mp4"), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                           stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                            "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                            "-pix_fmt", "yuv420p", str(out / "raw-cursor.mp4")], stdin=subprocess.PIPE)
    i = 0
    while True:
        buf = dec.stdout.read(W * H * 3)
        if len(buf) < W * H * 3:
            break
        f = np.frombuffer(buf, np.uint8).reshape(H, W, 3).copy()
        t = i / fps
        j = max(0, int(np.searchsorted(mt, t, side="right")) - 1)
        x, y = int(s.moves[j][1] * scale - 2 * k), int(s.moves[j][2] * scale - 1.5 * k)
        h, w = arr.shape[:2]
        x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
        if x1 > x0 and y1 > y0:
            a = arr[y0 - y:y1 - y, x0 - x:x1 - x]
            roi = f[y0:y1, x0:x1].astype(np.float32)
            f[y0:y1, x0:x1] = (a[..., 2::-1] * 255 * a[..., 3:] + roi * (1 - a[..., 3:])).astype(np.uint8)
        enc.stdin.write(f.tobytes())
        i += 1
    enc.stdin.close()
    enc.wait()
    dec.wait()


def write_events(out: Path, s: Script, duration: float, fps: int, scale: int, win, B=None):
    # raw JSONL, as the real logger writes it (logical coordinates, ms)
    with open(out / "events.raw.jsonl", "w") as f:
        f.write(json.dumps({"type": "start", "ms": 0, "region": [0, 0, LOGICAL_W, LOGICAL_H], "scale": scale,
                            "backend": "synthetic", "click_source": "synthetic"}) + "\n")
        evs = [("move", m[0], {"x": round(m[1], 2), "y": round(m[2], 2)}) for m in s.moves]
        evs += [("click", c["t"], {"x": round(c["x"], 2), "y": round(c["y"], 2), "button": c["button"]}) for c in s.clicks]
        evs += [("key", k["t"], {"key": k["key"], "mods": k["mods"]}) for k in s.keys]
        for kind, t, d in sorted(evs, key=lambda e: e[1]):
            f.write(json.dumps({"type": kind, "ms": round(t * 1000, 1), **d}) + "\n")
    events = [{"t": round(m[0], 4), "type": "move", "x": round(m[1] * scale, 2), "y": round(m[2] * scale, 2)} for m in s.moves]
    events += [{"t": round(c["t"], 4), "type": "click", "x": round(c["x"] * scale, 2), "y": round(c["y"] * scale, 2),
                "button": c["button"]} for c in s.clicks]
    events += [{"t": round(k["t"], 4), "type": "key", "key": k["key"], "mods": k["mods"]} for k in s.keys]
    win, term = win if isinstance(win, tuple) else (win, None)
    if win:
        events.append({"t": 0.0, "type": "window", "rect": [v * scale for v in win], "title": "tasks.local"})
    if win and term:
        # what the logger's client poll reports: two tiled windows, focus moves to the terminal
        wins = [{"address": "0xapp", "class": "chromium", "title": "tasks.local", "rect": [v * scale for v in win],
                 "floating": False, "fullscreen": False, "workspace": "1"},
                {"address": "0xterm", "class": "Alacritty", "title": "~/app", "rect": [v * scale for v in term],
                 "floating": False, "fullscreen": False, "workspace": "1"}]
        events.append({"t": 0.0, "type": "clients", "focus": "0xapp", "windows": wins})
        if B and B.get("termFocus"):
            events.append({"t": round(B["termFocus"], 4), "type": "clients", "focus": "0xterm", "windows": wins})
    events.sort(key=lambda e: e["t"])
    doc = {"version": 1, "video": {"w": LOGICAL_W * scale, "h": LOGICAL_H * scale, "fps": fps, "duration": round(duration, 4)},
           "capture": {"backend": "synthetic", "cursor_baked": False, "click_source": "synthetic",
                       "region": [0, 0, LOGICAL_W, LOGICAL_H], "scale": scale},
           "events": events}
    (out / "events.json").write_text(json.dumps(doc))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--no-before", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    s, B = build_script()
    print(f"script: {s.t:.1f}s, {len(s.moves)} moves, {len(s.clicks)} clicks, {len(s.keys)} keys", file=sys.stderr)
    duration, win = asyncio.run(capture(out, a.fps, a.scale, s, B))
    write_events(out, s, duration, a.fps, a.scale, win, B)
    if not a.no_before:
        burn_cursor(out, a.fps, a.scale, s)
    print(out)


if __name__ == "__main__":
    main()

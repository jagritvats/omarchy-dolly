"""Speed ramps: idle cut and fast-forwarding long typing.

A speed segment {start, end, speed} (source seconds) plays `speed` times
faster. TimeMap maps output time <-> source time over a trimmed range;
everything else (camera, cursor, ripples) keeps working in source time.

detect_speedups() proposes segments from
  * idle stretches: no input AND no visual change for >= idle_min seconds
    (visual change measured on a tiny grayscale proxy of the recording, so a
    terminal printing output while you watch is NOT idle)
  * long typing: sustained typing (key gaps < typing_gap) longer than
    typing_min seconds; the first and last second stay at 1x
"""
from __future__ import annotations

import bisect
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .events import Timeline


@dataclass
class SpeedConfig:
    idle_min: float = 2.2           # seconds of nothing before it counts as idle
    idle_keep: float = 0.45         # keep this much real-time at both ends of an idle stretch
    visual_thresh: float = 40       # changed pixels (of a 320 px wide proxy) counting as "screen changed";
                                    # a new line of text is hundreds, a blinking caret a handful
    typing_min: float = 4.0
    typing_gap: float = 0.7
    typing_keep: float = 1.0
    typing_speed: float = 2.0
    min_segment: float = 0.8

    @classmethod
    def from_dict(cls, d):
        c = cls()
        for k, v in (d or {}).items():
            if hasattr(c, k):
                setattr(c, k, type(getattr(c, k))(v))
        return c


def idle_speed(length: float) -> float:
    return 2.0 if length < 3.5 else 4.0 if length < 8 else 8.0


def visual_activity(raw: Path, fps: float = 5.0, width: int = 320) -> tuple[np.ndarray, np.ndarray]:
    """(times, number of pixels that changed by > 10 levels since the previous sample)."""
    from .render import ffmpeg_bin, probe
    p = probe(raw)
    v = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), {})
    w, h = int(v.get("width", 16)), int(v.get("height", 9))
    hh = max(2, int(round(width * h / w / 2)) * 2)
    # no timeout: decodes the whole recording (time grows with its length)
    out = subprocess.run([ffmpeg_bin(), "-v", "error", "-i", str(raw), "-vf", f"fps={fps},scale={width}:{hh}",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    n = len(out) // (width * hh)
    fr = np.frombuffer(out[: n * width * hh], np.uint8).reshape(n, hh, width).astype(np.int16)
    diff = np.zeros(n)
    if n > 1:
        diff[1:] = (np.abs(np.diff(fr, axis=0)) > 10).sum(axis=(1, 2))
    return np.arange(n) / fps, diff


def cached_activity(project_dir: Path):
    project_dir = Path(project_dir)
    cache = project_dir / "activity-v2.npz"
    from .project import Project
    raw = Project(project_dir).raw
    try:
        if cache.stat().st_mtime >= raw.stat().st_mtime:
            with np.load(cache, allow_pickle=False) as d:      # numbers only, whoever wrote the file
                t, a = np.asarray(d["t"], dtype=float), np.asarray(d["a"], dtype=float)
            if t.ndim == a.ndim == 1 and len(t) == len(a):
                return t, a
    except Exception:
        pass                                                   # missing or damaged: work it out again
    t, a = visual_activity(raw)
    np.savez(cache, t=t, a=a)
    return t, a


def _input_times(tl: Timeline, jitter_px: float = 12.0) -> np.ndarray:
    """Input instants; cursor micro-movements (a resting hand) do not count."""
    ts = [c["t"] for c in tl.clicks] + [k["t"] for k in tl.keys]
    ax, ay = None, None
    for t, x, y in zip(tl.moves_t, tl.moves_x, tl.moves_y):
        if ax is None or abs(x - ax) + abs(y - ay) > jitter_px:
            ts.append(t)
            ax, ay = x, y
    return np.array(sorted(ts))


def detect_speedups(tl: Timeline, activity=None, cfg: SpeedConfig | None = None,
                    t0: float = 0.0, t1: float | None = None) -> list[dict]:
    cfg = cfg or SpeedConfig()
    t1 = tl.duration if t1 is None else t1
    segs: list[dict] = []
    # ---- idle: gaps between "activity instants" (input or visual change)
    inst = list(_input_times(tl))
    if activity is not None:
        at, ad = activity
        inst += list(at[ad > cfg.visual_thresh])
    inst = sorted(t for t in inst if t0 <= t <= t1)
    bounds = [t0] + inst + [t1]
    for a, b in zip(bounds, bounds[1:]):
        if b - a >= cfg.idle_min:
            s, e = a + cfg.idle_keep, b - cfg.idle_keep
            if e - s >= cfg.min_segment:
                segs.append({"start": round(s, 3), "end": round(e, 3), "speed": idle_speed(e - s), "reason": "idle"})
    # ---- long typing bursts
    keys = sorted(k["t"] for k in tl.keys if not (set(k.get("mods", [])) & {"ctrl", "alt", "super"}))
    burst = []
    def flush():
        if len(burst) > 3 and burst[-1] - burst[0] >= cfg.typing_min:
            s, e = burst[0] + cfg.typing_keep, burst[-1] - cfg.typing_keep
            if e - s >= cfg.min_segment and not any(x["start"] < e and s < x["end"] for x in segs):
                segs.append({"start": round(s, 3), "end": round(e, 3), "speed": cfg.typing_speed, "reason": "typing"})
    for t in keys:
        if burst and t - burst[-1] > cfg.typing_gap:
            flush()
            burst = []
        burst.append(t)
    flush()
    segs.sort(key=lambda s: s["start"])
    for i, s in enumerate(segs):
        s["id"] = f"s{i + 1}"
        s["source"] = "auto"
    return segs


class TimeMap:
    """Piecewise-linear output<->source time over [t0, t1] with speed segments."""

    def __init__(self, t0: float, t1: float, segments: list[dict] | None = None):
        self.t0, self.t1 = t0, t1
        segs = sorted((s for s in (segments or []) if s["end"] > s["start"] and float(s.get("speed", 1)) != 1),
                      key=lambda s: s["start"])
        knots = [(t0, 1.0)]
        for s in segs:
            a, b = max(t0, s["start"]), min(t1, s["end"])
            if b <= a or a < knots[-1][0]:
                continue
            knots.append((a, float(s["speed"])))
            knots.append((b, 1.0))
        # source breakpoints and output times
        self.src = [t0]
        self.speed = []
        cur_speed = 1.0
        for t, sp in knots[1:]:
            self.src.append(t)
            self.speed.append(cur_speed)
            cur_speed = sp
        self.src.append(t1)
        self.speed.append(cur_speed)
        self.out = [0.0]
        for i in range(len(self.speed)):
            self.out.append(self.out[-1] + (self.src[i + 1] - self.src[i]) / self.speed[i])

    @property
    def duration(self) -> float:
        return self.out[-1]

    def to_source(self, tau: float) -> float:
        i = min(max(bisect.bisect_right(self.out, tau) - 1, 0), len(self.speed) - 1)
        return min(self.t1, self.src[i] + (tau - self.out[i]) * self.speed[i])

    def to_output(self, ts: float) -> float:
        i = min(max(bisect.bisect_right(self.src, ts) - 1, 0), len(self.speed) - 1)
        return self.out[i] + (ts - self.src[i]) / self.speed[i]

    def speed_at(self, ts: float) -> float:
        i = min(max(bisect.bisect_right(self.src, ts) - 1, 0), len(self.speed) - 1)
        return self.speed[i]

    def pieces(self):
        """[(src_start, src_end, speed)] covering [t0, t1]."""
        return [(self.src[i], self.src[i + 1], self.speed[i]) for i in range(len(self.speed))
                if self.src[i + 1] > self.src[i] + 1e-6]

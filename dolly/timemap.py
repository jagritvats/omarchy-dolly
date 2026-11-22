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
import math
from dataclasses import dataclass
from pathlib import Path

from . import grid
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


def visual_activity(raw: Path, fps: float = 5.0, width: int = 320) -> tuple[list, list]:
    """(times, number of pixels that changed by > 10 levels since the previous sample)."""
    from .ff import ffmpeg_bin, probe
    p = probe(raw)
    v = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), {})
    w, h = int(v.get("width", 16)), int(v.get("height", 9))
    hh = max(2, int(round(width * h / w / 2)) * 2)
    # ffmpeg takes each sample off the next one and marks what is more than 10 apart (tpad
    # repeats the first sample, so it is compared with itself); only the marks are counted
    # here. No timeout: it decodes the whole recording (time grows with its length)
    cmd = [ffmpeg_bin(), "-v", "error", "-filter_threads", "1", "-i", str(raw), "-vf",
           f"fps={fps},scale={width}:{hh},format=gray,tpad=start=1:start_mode=clone,"
           "tblend=all_mode=difference,lut=y='gt(val,10)*255'", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    diff = [float(mask.count(grid.SET)) for mask in grid.read(cmd, width * hh)]
    return [i / fps for i in range(len(diff))], diff


ACTIVITY_CACHE = "activity-v3.json"
OLD_ACTIVITY_CACHES = ("activity-v2.npz",)     # NumPy's file format: never read again, deleted unread


def cached_activity(project_dir: Path):
    """visual_activity of a take, kept next to it as JSON (numbers only, whoever wrote the
    file: a recording folder may come from someone else)."""
    import json
    from . import fsutil
    from .project import Project
    project_dir = Path(project_dir)
    raw = Project(project_dir).raw
    for name in OLD_ACTIVITY_CACHES:
        try:
            (project_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
    cache = project_dir / ACTIVITY_CACHE
    try:
        if cache.stat().st_mtime >= raw.stat().st_mtime:
            doc = json.loads(cache.read_text(encoding="utf-8"))
            t, a = [float(v) for v in doc["t"]], [float(v) for v in doc["a"]]
            if len(t) == len(a):
                return t, a
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        pass                                                   # missing or damaged: work it out again
    t, a = visual_activity(raw)
    try:
        fsutil.atomic_write_text(cache, json.dumps({"t": t, "a": a}, separators=(",", ":")), durable=False)
    except OSError:
        pass                                                   # a read-only folder: the result still stands
    return t, a


def _input_times(tl: Timeline, jitter_px: float = 12.0) -> list:
    """Input instants; cursor micro-movements (a resting hand) do not count."""
    ts = [c["t"] for c in tl.clicks] + [k["t"] for k in tl.keys]
    ax, ay = None, None
    for t, x, y in zip(tl.moves_t, tl.moves_x, tl.moves_y):
        if ax is None or abs(x - ax) + abs(y - ay) > jitter_px:
            ts.append(t)
            ax, ay = x, y
    return sorted(ts)


def detect_speedups(tl: Timeline, activity=None, cfg: SpeedConfig | None = None,
                    t0: float = 0.0, t1: float | None = None) -> list[dict]:
    cfg = cfg or SpeedConfig()
    t1 = tl.duration if t1 is None else t1
    segs: list[dict] = []
    # ---- idle: gaps between "activity instants" (input or visual change)
    inst = _input_times(tl)
    if activity is not None:
        at, ad = activity
        inst += [t for t, d in zip(at, ad) if d > cfg.visual_thresh]
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


PAUSE_MIN = 1.5         # s of nothing (no input, no screen change, no speech) that is a pause to cut
PAUSE_KEEP = 0.35       # s left at each end of a cut pause, so the cut is not abrupt


def detect_pauses(tl: Timeline, activity=None, speech=None, t0: float = 0.0, t1: float | None = None,
                  cfg: SpeedConfig | None = None, min_len: float = PAUSE_MIN, keep: float = PAUSE_KEEP) -> list[dict]:
    """Pauses to cut (0.9.0, "Cut pauses"): stretches with no input, no change on screen and
    nothing said (`speech`: video-time spans of the microphone with sound in them; None when
    there is no sound, then only input and screen count). Returns cuts {start, end, source,
    reason}; what is left at each end keeps the cut from being abrupt."""
    cfg = cfg or SpeedConfig()
    t1 = tl.duration if t1 is None else t1
    inst = _input_times(tl)
    if activity is not None:
        at, ad = activity
        inst += [t for t, d in zip(at, ad) if d > cfg.visual_thresh]
    inst = sorted(t for t in inst if t0 <= t <= t1)
    quiet = []
    for a, b in zip([t0] + inst, inst + [t1]):
        if b - a >= min_len + 2 * keep:
            quiet.append((a, b))
    talk = merge_spans(speech or [])
    cuts = []
    for a, b in quiet:
        for qa, qb in _minus(a, b, talk):
            s_, e_ = qa + keep, qb - keep
            if e_ - s_ >= min_len:
                cuts.append({"start": round(s_, 3), "end": round(e_, 3), "source": "auto", "reason": "pause"})
    for i, c in enumerate(cuts):
        c["id"] = f"x{i + 1}"
    return cuts


def apply_pause_cuts(project_dir, tl: Timeline) -> list[dict]:
    """"Cut pauses" on a recording: its automatic pause cuts are replaced by the pauses found
    now; cuts made by hand, or from a caption line, stay. Returns the recording's cuts."""
    from . import analysis
    from .captions import speech_video_spans
    from .project import Project
    analysis.ensure(project_dir)
    found = detect_pauses(tl, cached_activity(Path(project_dir)), speech_video_spans(Path(project_dir)))
    with analysis.project_lock(project_dir):
        proj = Project(project_dir)
        data = proj.load()
        kept = [c for c in clean_cuts(data.get("cuts")) if not (c["source"] == "auto" and c["reason"] == "pause")]
        data["cuts"] = clean_cuts(kept + found, tl.duration)
        proj.save(data)
    return data["cuts"]


def clean_cuts(cuts, duration: float | None = None) -> list[dict]:
    """The cut list as the editor page or a recording folder gives it, kept to the shape Dolly
    writes (numbers that are numbers, no overlap, an id each)."""
    out = []
    for c in cuts if isinstance(cuts, list) else []:
        if not isinstance(c, dict):
            continue
        try:
            a, b = float(c.get("start")), float(c.get("end"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(a) and math.isfinite(b)) or a < 0 or b - a < 0.05:
            continue
        if duration is not None:
            a, b = min(a, duration), min(b, duration)
            if b - a < 0.05:
                continue
        cid = c.get("id") if isinstance(c.get("id"), str) and c["id"].isalnum() and len(c["id"]) <= 12 else None
        reason = c.get("reason") if c.get("reason") in ("pause", "caption", "manual") else "manual"
        out.append({"id": cid, "start": round(a, 3), "end": round(b, 3),
                    "source": "auto" if c.get("source") == "auto" else "manual", "reason": reason})
        if len(out) >= 2000:
            break
    out.sort(key=lambda c: c["start"])
    merged: list[dict] = []
    for c in out:
        if merged and c["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], c["end"])
            if c["source"] == "manual":
                merged[-1]["source"] = "manual"
        else:
            merged.append(c)
    seen: set = set()
    taken = {c["id"] for c in merged if c["id"]}
    n = 1
    for c in merged:
        if not c["id"] or c["id"] in seen:
            while f"x{n}" in taken:
                n += 1
            c["id"] = f"x{n}"
            taken.add(c["id"])
        seen.add(c["id"])
    return merged


CUT = math.inf          # the speed of a cut: it takes no time in the output


def merge_spans(spans) -> list[tuple[float, float]]:
    """Sorted, merged (start, end) spans; empty and backwards ones dropped."""
    out: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in spans if float(b) > float(a)):
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _minus(a: float, b: float, cuts) -> list[tuple[float, float]]:
    """[a, b] with the cut spans taken out."""
    parts, cur = [], a
    for ca, cb in cuts:
        if cb <= cur or ca >= b:
            continue
        if ca > cur:
            parts.append((cur, ca))
        cur = max(cur, cb)
    if cur < b:
        parts.append((cur, b))
    return parts


class TimeMap:
    """Piecewise-linear output<->source time over [t0, t1] with speed segments and cuts.
    A cut (0.9.0: a caption line cut out, a pause cut) takes no output time: the frames,
    the sound and the captions of that stretch are left out, and a speed-up that overlaps
    it only applies to what is left of it."""

    def __init__(self, t0: float, t1: float, segments: list[dict] | None = None, cuts: list | None = None):
        self.t0, self.t1 = t0, t1
        cut = merge_spans((max(t0, float(c["start"])), min(t1, float(c["end"]))) for c in (cuts or []))
        plan = [(a, b, CUT) for a, b in cut]
        for s in segments or []:
            if s["end"] > s["start"] and float(s.get("speed", 1)) != 1:
                plan += [(a, b, float(s["speed"])) for a, b in _minus(float(s["start"]), float(s["end"]), cut)]
        knots = [(t0, 1.0)]
        for sa, sb, sp in sorted(plan):
            a, b = max(t0, sa), min(t1, sb)
            if b <= a or a < knots[-1][0]:
                continue
            knots.append((a, sp))
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
        if self.speed[i] == CUT:              # only at the very end, a cut up to t1
            return self.src[i + 1]
        return min(self.t1, self.src[i] + (tau - self.out[i]) * self.speed[i])

    def in_cut(self, ts: float) -> bool:
        return self.speed_at(ts) == CUT

    def cuts(self) -> list[tuple[float, float]]:
        return [(self.src[i], self.src[i + 1]) for i in range(len(self.speed)) if self.speed[i] == CUT]

    def to_output(self, ts: float) -> float:
        i = min(max(bisect.bisect_right(self.src, ts) - 1, 0), len(self.speed) - 1)
        return self.out[i] + (ts - self.src[i]) / self.speed[i]

    def speed_at(self, ts: float) -> float:
        i = min(max(bisect.bisect_right(self.src, ts) - 1, 0), len(self.speed) - 1)
        return self.speed[i]

    def pieces(self):
        """[(src_start, src_end, speed)] covering [t0, t1], without the cuts (what is played)."""
        return [(self.src[i], self.src[i + 1], self.speed[i]) for i in range(len(self.speed))
                if self.src[i + 1] > self.src[i] + 1e-6 and self.speed[i] != CUT]

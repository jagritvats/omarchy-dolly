"""Event/video sync via on-screen markers.

At recording start (and again just before stop) the logger asks the shell to
flash a tiny marker for ~2 frames at the top-left corner of the recorded area:
a magenta square with a green centre (an unlikely pair in real UIs), drawn by
an Overlay layer surface (BarWidget.qml `syncFlash`), so it is above every
window and the bar. The logger writes {"type": "sync"} with its own clock at
the moment it asks.

finalize() then
  1. finds the marker frames in the video (only a corner crop is decoded),
  2. fits  t_video = a + b * t_logger  (offset from one marker, offset + drift
     from two),
  3. maps every event through it, and
  4. trims the marker frames out of the default export range.

Display latency: the marker becomes visible on the first frame captured
after the request, i.e. 0..1 frame later. The detected time is the first
marker frame minus half a frame, so the residual error is <= 0.5 frame.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MARKER_LOGICAL = 40          # px (logical) square at the region's top-left
PROBE = 8                    # decoded crop is resized to PROBE x PROBE


@dataclass
class SyncFit:
    a: float = 0.0           # t_video = a + b * t_log
    b: float = 1.0
    markers_video: list = None
    markers_log: list = None
    method: str = "none"     # "markers2" | "markers1" | "none"
    residual: float = 0.0

    def map(self, t_log: float) -> float:
        return self.a + self.b * t_log

    def to_dict(self):
        return {"a": round(self.a, 5), "b": round(self.b, 7), "method": self.method,
                "markers_video": [round(v, 4) for v in (self.markers_video or [])],
                "markers_log": [round(v, 4) for v in (self.markers_log or [])],
                "drift_pct": round((self.b - 1) * 100, 4), "residual": round(self.residual, 4)}


def is_marker(px: np.ndarray) -> bool:
    """px: PROBE x PROBE RGB crop of the marker square."""
    p = px.astype(np.int16)
    edge = np.concatenate([p[0, :], p[-1, :], p[:, 0], p[:, -1]])
    r, g, b = edge[:, 0], edge[:, 1], edge[:, 2]
    # relative, not absolute: the overlay can fade in over a few frames (software rendering:
    # edge red 142 -> 166 -> 192 -> 214) and yuv420 smears colour; magenta = red and blue far
    # above green, green = green far above both
    mag = (r - g > 70) & (b - g > 70) & (np.abs(r - b) < 70) & (r > 110)
    c = p[PROBE // 2 - 1:PROBE // 2 + 1, PROBE // 2 - 1:PROBE // 2 + 1].reshape(-1, 3)
    grn = (c[:, 1] - np.maximum(c[:, 0], c[:, 2]) > 70) & (c[:, 1] > 110)
    return mag.mean() > 0.7 and grn.mean() > 0.7


def detect_markers(raw: Path, scale: float, fps: float, ffmpeg: str = "ffmpeg", frames=None) -> list[tuple[float, float]]:
    """[(t_first_frame, t_last_frame)] runs of marker frames (video seconds).
    `frames` (N x PROBE x PROBE x 3 RGB) overrides decoding (tests)."""
    pts = None
    if frames is None:
        pts = frame_times(raw, ffmpeg)
        size = max(4, int(round(MARKER_LOGICAL * scale)))
        # passthrough: one decoded frame per stored frame (no CFR duplication), so the
        # i-th frame matches the i-th timestamp below
        out = subprocess.run([ffmpeg, "-v", "error", "-i", str(raw), "-map", "0:v:0", "-fps_mode", "passthrough",
                              "-vf", f"crop={size}:{size}:0:0,scale={PROBE}:{PROBE}:flags=area",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
        n = len(out) // (PROBE * PROBE * 3)
        frames = np.frombuffer(out[: n * PROBE * PROBE * 3], np.uint8).reshape(n, PROBE, PROBE, 3)
    runs = []
    cur = None
    for i, f in enumerate(frames):
        if is_marker(f):
            cur = [i, i] if cur is None else [cur[0], i]
        elif cur is not None:
            runs.append(tuple(cur))
            cur = None
    if cur is not None:
        runs.append(tuple(cur))
    if pts is not None and len(pts) >= len(frames):
        # video time starts at the first decoded frame everywhere else (render, AOI), so the
        # timestamps are taken relative to it (a container can start at a non-zero pts)
        p0 = pts[0]
        return [(pts[a] - p0, pts[b] - p0) for a, b in runs]
    return [(a / fps, b / fps) for a, b in runs]


def frame_times(raw: Path, ffmpeg: str = "ffmpeg") -> list[float] | None:
    """Presentation time of every video frame. wf-recorder only writes a frame when the
    screen changes, so frame index / fps drifts from the real time (avg 62.5 vs 60 fps)."""
    import shutil
    probe = shutil.which("ffprobe") or str(Path(ffmpeg).with_name("ffprobe"))
    try:
        out = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "frame=pts_time", "-of", "csv=p=0", str(raw)],
                             capture_output=True, text=True, timeout=120).stdout
        ts = [float(x.strip(",")) for x in out.split() if x.strip(",") not in ("", "N/A")]
        return ts or None
    except Exception:
        return None


# the first frame of the recorder arrives this long after it is launched: 0.14, 0.15 and
# 0.21 s for wf-recorder in a VirtualBox VM (three recordings whose markers were detected);
# with 0.16 the clock fallback lands within 0.01-0.05 s of the marker fit
FIRST_FRAME_LATENCY = 0.16
# a marker pairing that disagrees with the clock estimate by more than this is a mis-pairing
# (the clock is good to a few frames; start and stop markers are seconds apart)
MAX_CLOCK_DISAGREE = 0.5


def clock_offset(meta: dict, lines: list) -> float | None:
    """t_video = a + t_log from the clocks alone, for when no marker is found: the logger's
    start (monotonic) minus the recorder's launch (monotonic) minus its first-frame latency.
    Good to a few frames; assuming a = 0 is off by the whole countdown (~3 s)."""
    start = next((e for e in lines if e.get("type") == "start"), None)
    t_log0, t_rec = (start or {}).get("t0_mono"), meta.get("clock_mono")
    if t_log0 is None or t_rec is None:
        return None
    a = float(t_log0) - float(t_rec) - FIRST_FRAME_LATENCY
    return a if 0.0 <= a < 30.0 else None


def fit(log_marks: list[float], video_runs: list[tuple[float, float]], fps: float,
        clock_a: float | None = None) -> SyncFit:
    """Pair logged marker times with detected runs (first<->first, last<->last).
    Without a usable marker, fall back to the clock estimate `clock_a` when given."""
    if not log_marks or not video_runs:
        if clock_a is not None:
            return SyncFit(clock_a, 1.0, [], list(log_marks), "clock")
        return SyncFit(method="none", markers_video=[], markers_log=list(log_marks))
    v = [r[0] - 0.5 / fps for r in video_runs]
    if len(log_marks) >= 2 and len(v) >= 2:
        l0, l1, v0, v1 = log_marks[0], log_marks[-1], v[0], v[-1]
        if l1 - l0 > 1.0:
            b = (v1 - v0) / (l1 - l0)
            # each marker time is known to +-half a frame, so a drift smaller than that
            # uncertainty over the span is noise: keep b = 1 and split the offset
            # (a 7 s clip at 30 fps would otherwise "drift" 0.5 % and slide 1+ frame)
            if abs(b - 1.0) < 1.0 / fps / (l1 - l0):
                b = 1.0
                a = ((v0 - l0) + (v1 - l1)) / 2
                return SyncFit(a, b, v, list(log_marks), "markers2", float(abs((v0 - l0) - (v1 - l1)) / 2))
            if 0.97 < b < 1.03:          # sane clock drift only
                a = v0 - b * l0
                res = 0.0
                if len(log_marks) > 2 and len(v) == len(log_marks):
                    res = float(max(abs(a + b * l - vv) for l, vv in zip(log_marks, v)))
                return SyncFit(a, b, v, list(log_marks), "markers2", res)
    # one usable marker (the other was not captured, or the two-marker fit was not sane):
    # which logged marker is it? The clock estimate decides; without one, the first.
    if clock_a is not None:
        cands = [(abs((vv - ll) - clock_a), vv, ll) for vv in (v[0], v[-1]) for ll in log_marks]
        err, vv, ll = min(cands)
        if err <= MAX_CLOCK_DISAGREE:
            return SyncFit(vv - ll, 1.0, [vv], [ll], "markers1", float(err))
        return SyncFit(clock_a, 1.0, [], list(log_marks), "clock")
    a = v[0] - log_marks[0]
    return SyncFit(a, 1.0, v[:1], list(log_marks[:1]), "markers1")


def marker_trim(video_runs, fps: float, duration: float) -> tuple[float, float] | None:
    """Default export range that leaves the marker frames out."""
    if not video_runs:
        return None
    t0 = 0.0
    t1 = duration
    first = video_runs[0]
    if first[0] < 10.0:            # the start marker opens the recording: before it is recorder warm-up
        t0 = first[1] + 1.0 / fps
    last = video_runs[-1]
    if last is not first and last[0] > duration - 3.0:
        t1 = last[0] - 0.5 / fps
    return (round(t0, 4), round(t1, 4))

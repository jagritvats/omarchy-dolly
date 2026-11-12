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

import re
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


# ---- bounded scan: the start marker is flashed when the logger starts and the stop marker
# just before the recorder is told to stop, so only the head and the tail of the file can
# hold them. Reading the whole file made Stop take 0.115 s per second recorded.
HEAD_WINDOW = 4.0            # seconds from the first frame (grown to cover the expected start marker)
HEAD_WINDOW_MAX = 12.0
TAIL_WINDOW = 4.0            # last seconds of the file (the wf-recorder stop tail is 1 s)
TAIL_WINDOW_MAX = 20.0       # ... grown when the clock says the stop marker is further from the end
_PTS = re.compile(r"pts_time:\s*(-?[0-9.]+)")


def _probe_window(raw: Path, size: int, ffmpeg: str, head: float | None = None, tail: float | None = None,
                  timeout: float = 120.0, span: tuple[float, float] | None = None):
    """Decode one window of the marker corner: (frames N x PROBE x PROBE x 3, [pts]) with the
    presentation time of every decoded frame in the file's own timeline (showinfo), so frame
    i and time i always belong together. `head`: the first seconds; `tail`: the last seconds;
    `span`: (from, seconds) anywhere in the file."""
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-v", "info", "-copyts"]
    if span is not None:
        cmd += ["-ss", f"{max(0.0, span[0]):.3f}", "-t", f"{max(0.1, span[1]):.3f}"]
    if tail is not None:
        cmd += ["-sseof", f"-{tail:.3f}"]
    if head is not None:
        cmd += ["-t", f"{head:.3f}"]        # an input option: nothing past the window is decoded
    cmd += ["-i", str(raw), "-map", "0:v:0", "-fps_mode", "passthrough",
            "-vf", f"crop={size}:{size}:0:0,scale={PROBE}:{PROBE}:flags=area,showinfo",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    n = len(r.stdout) // (PROBE * PROBE * 3)
    frames = np.frombuffer(r.stdout[: n * PROBE * PROBE * 3], np.uint8).reshape(n, PROBE, PROBE, 3)
    pts = [float(m.group(1)) for line in r.stderr.decode("utf-8", "replace").splitlines()
           if "showinfo" in line and (m := _PTS.search(line))]
    # showinfo can name one more frame than was written (the one that ended the window)
    return frames, (pts[:n] if n and len(pts) >= n else [])


def _runs_of(frames) -> list[tuple[int, int]]:
    runs, cur = [], None
    for i, f in enumerate(frames):
        if is_marker(f):
            cur = [i, i] if cur is None else [cur[0], i]
        elif cur is not None:
            runs.append(tuple(cur))
            cur = None
    if cur is not None:
        runs.append(tuple(cur))
    return runs


def scan_head(raw: Path, scale: float, ffmpeg: str = "ffmpeg", seconds: float = HEAD_WINDOW) -> dict | None:
    """Marker runs in the first `seconds` of the file: {"p0", "end", "runs"} (video seconds from
    the first frame; "end" = how far this window reached). None when it could not be read.
    Safe on a file that is still being written, as long as its first seconds are on disk."""
    size = max(4, int(round(MARKER_LOGICAL * scale)))
    try:
        frames, pts = _probe_window(raw, size, ffmpeg, head=seconds)
    except (OSError, subprocess.SubprocessError):
        return None
    if not len(frames) or len(pts) != len(frames):
        return None
    p0 = pts[0]
    return {"p0": p0, "end": pts[-1] - p0, "runs": [(pts[a] - p0, pts[b] - p0) for a, b in _runs_of(frames)]}


def scan_tail(raw: Path, scale: float, ffmpeg: str = "ffmpeg", seconds: float = TAIL_WINDOW) -> dict | None:
    """Marker runs in the last `seconds` of the file: {"runs"} in the file's own timeline
    (subtract the head's "p0" for video seconds). None when it could not be read."""
    size = max(4, int(round(MARKER_LOGICAL * scale)))
    try:
        frames, pts = _probe_window(raw, size, ffmpeg, tail=seconds)
    except (OSError, subprocess.SubprocessError):
        return None
    if not len(frames) or len(pts) != len(frames):
        return None
    return {"runs": [(pts[a], pts[b]) for a, b in _runs_of(frames)]}


def detect_markers_bounded(raw: Path, scale: float, fps: float, ffmpeg: str = "ffmpeg", *, want_stop: bool = True,
                           head_until: float | None = None, head: dict | None = None,
                           stop_at: float | None = None, duration: float | None = None,
                           log=None) -> list[tuple[float, float]]:
    """The marker runs, reading only the head and the tail of the file. Same result as
    detect_markers() for a start marker in the head window and a stop marker in the tail
    window. `head_until`: where the start marker is expected (clock estimate); `head`: a
    scan_head() result made earlier (while the recorder was still shutting down);
    `stop_at` and `duration`: where the stop marker is expected and how long the file is,
    so the tail window reaches back far enough when the recorder was slow to stop.
    Falls back to the full scan when a window cannot be read, and for short files (the
    two windows would cover them anyway)."""
    from concurrent.futures import ThreadPoolExecutor
    say = log or (lambda _m: None)
    seconds = HEAD_WINDOW if head_until is None else min(HEAD_WINDOW_MAX, max(HEAD_WINDOW, head_until + 2.0))
    if head is not None and head_until is not None and head.get("end", 0.0) < min(head_until + 0.75, seconds - 0.25):
        head = None                        # the early scan stopped before the expected marker
    with ThreadPoolExecutor(max_workers=2) as pool:        # two ffmpeg processes side by side
        f_head = pool.submit(scan_head, raw, scale, ffmpeg, seconds) if head is None else None
        tail_s = TAIL_WINDOW
        if stop_at is not None and duration:
            tail_s = min(TAIL_WINDOW_MAX, max(TAIL_WINDOW, duration - stop_at + 2.0))
        f_tail = pool.submit(scan_tail, raw, scale, ffmpeg, tail_s) if want_stop else None
        head = f_head.result() if f_head else head
        tail = f_tail.result() if f_tail else None
    if head is None:
        say("sync: the head of the video could not be scanned; reading the whole file")
        return detect_markers(raw, scale, fps, ffmpeg)
    runs = list(head["runs"])
    if not want_stop:
        return runs
    if tail is None:
        say("sync: the tail of the video could not be scanned; reading the whole file")
        return detect_markers(raw, scale, fps, ffmpeg)
    for run in ((a - head["p0"], b - head["p0"]) for a, b in tail["runs"]):
        if not any(abs(run[0] - r[0]) < 0.5 / max(fps, 1.0) for r in runs):      # the windows overlap in a short file
            runs.append(run)
    runs.sort()
    merged: list[tuple[float, float]] = []
    for a, b in runs:                      # one run seen by both windows, split at the seam
        if merged and a - merged[-1][1] <= 1.5 / max(fps, 1.0):
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


# ---- the hidden stage's marker: the pointer in the corner (dolly/stage.py pointer_mark).
# No shell lives on a stage to flash the square, but the stage's pointer is Dolly's own and is
# drawn into the video: it jumps to the top-left corner, is shown there a moment and goes back
# (the start mark), or stays there until the recorder has gone (the stop mark).
#
# While it is shown the pointer is not still: it moves a pixel aside and back so the stage
# keeps repainting, and its picture can change under it (11 Nov: near a window's corner
# Hyprland showed the resize pointer, whose white part is cut off one pixel further out; half
# the frames of a mark differed from the empty corner by 58 grey levels and half by 10, and
# every mark was read 0.2 to 0.3 s late). So "the pointer is there" means: the corner differs
# from how it looked just before by more than noise, and "it has gone" means: the corner has
# looked as before for POINTER_GONE_S, not for one frame.
POINTER_MARK_MIN_S = 0.05
POINTER_MARK_MAX_S = 1.6     # 0.3 s on screen; on a busy machine the frame that shows it gone can come late
POINTER_STAY_MAX_S = 20.0    # a stop mark stays to the end of what is read: the file's end, or this long after it
POINTER_GONE_S = 0.1         # the corner as before for this long: the pointer has left
POINTER_CALM_S = 0.1         # ... and it looked the same for this long before the pointer came
POINTER_AFTER_S = 0.2        # the stage repaints this long around a mark (stage.pointer_mark): not in the default range
POINTER_CELLS = 2            # the outermost PROBE cells: the gap around the windows, where only the pointer draws
_ON, _NOISE = 8.0, 3.0       # grey levels: the pointer arriving; what an encoder alone can change


def _corner(frames):
    g = frames.astype(np.float32).mean(axis=3)[:, :POINTER_CELLS, :POINTER_CELLS]
    return g.reshape(len(g), -1)


def _arrivals(flat, pts):
    """Frames at which something came into a corner that had been calm: [(i, step)]."""
    step = np.abs(flat[1:] - flat[:-1]).max(axis=1)
    out = []
    for i in (np.nonzero(step >= _ON)[0] + 1).tolist():
        j = i - 1
        while j > 0 and pts[i - 1] - pts[j - 1] <= POINTER_CALM_S:
            j -= 1
        if float(np.abs(flat[j:i] - flat[i - 1]).max()) < _NOISE:
            out.append((i, float(step[i - 1])))
    return out


def _visit(flat, pts, i: int, step: float):
    """From an arrival at frame i: (last frame with the pointer, first frame of the corner as
    before or None when it never is again, whether the pointer was there most of the time)."""
    here = np.abs(flat[i:] - flat[i - 1]).max(axis=1) >= max(_NOISE, step * 0.12)
    n, last, away = len(flat), i, None
    for k in range(i + 1, n):
        if here[k - i]:
            last, away = k, None
        else:
            away = k if away is None else away
            if pts[k] - pts[away] >= POINTER_GONE_S:
                break
    back = last + 1 if last + 1 < n else None
    seen = here[: last - i + 1]
    return last, back, bool(seen.mean() >= 0.5)


def pointer_runs(frames, pts) -> list[tuple[float, float]]:
    """Stretches where the pointer came into the corner, was shown a moment and left:
    [(t_first, t_last)] in the times given. A change that stays (a window opening), a change
    deeper inside the corner (text) or a single odd frame is not one."""
    if len(frames) < 3 or len(pts) != len(frames):
        return []
    flat = _corner(frames)
    runs, after = [], 0
    for i, step in _arrivals(flat, pts):
        if i <= after:
            continue
        last, back, mostly = _visit(flat, pts, i, step)
        if back is None or not mostly:
            continue
        if POINTER_MARK_MIN_S <= pts[back] - pts[i] <= POINTER_MARK_MAX_S:
            runs.append((pts[i], pts[last]))
            after = back
    return runs


def pointer_open_runs(frames, pts) -> list[tuple[float, float]]:
    """Moments at which the pointer came into the corner and never left again, up to the
    last frame: [(t_on, t_last_frame)]. The stop mark is one: the pointer stays until the
    recorder has gone."""
    if len(frames) < 3 or len(pts) != len(frames):
        return []
    flat = _corner(frames)
    out = []
    for i, step in _arrivals(flat, pts):
        if not (0.1 <= pts[-1] - pts[i] <= POINTER_STAY_MAX_S):
            continue
        last, back, mostly = _visit(flat, pts, i, step)
        # still there at the end: the last frames show it, or it was last seen a moment ago
        if mostly and (back is None or pts[-1] - pts[back] < POINTER_GONE_S):
            out.append((pts[i], pts[-1]))
    return out


def pointer_open_run(frames, pts) -> tuple[float, float] | None:
    """The first of pointer_open_runs(): the pointer came into the corner and the file ends
    with it still there. (t_on, t_last_frame) or None."""
    runs = pointer_open_runs(frames, pts)
    return runs[0] if runs else None


def _nearest(runs, t: float | None, tol: float):
    if t is None or not runs:
        return None
    best = min(runs, key=lambda r: abs(r[0] - t))
    return best if abs(best[0] - t) <= tol else None


def detect_pointer_marks(raw: Path, scale: float, fps: float, ffmpeg: str, log_marks: list[float],
                         clock_a: float | None, duration: float | None, log=None) -> dict:
    """{"start": (t0, t1) | None, "stop": (t0, t1) | None} in video seconds: where the stage's
    pointer marks are in the file. Only the head and the tail are read. The clock says
    roughly where to look. On a stage it can only be wrong one way: the recorder's first
    frame waits for the first change on screen, so the file can begin later than the clock
    assumes and the mark then sits EARLIER in it (11 Nov, a still stage: 0.57 s, where the
    clock said 2.52 s). So the start mark is the pointer's first visit up to 1.6 s after
    the estimate, however early; the stop is looked for where the start says it must be."""
    say = log or (lambda _m: None)
    out = {"start": None, "stop": None}
    if not log_marks:
        return out
    size = max(4, int(round(MARKER_LOGICAL * scale)))
    exp = (clock_a or 0.0) + log_marks[0]
    try:
        frames, pts = _probe_window(raw, size, ffmpeg, head=min(HEAD_WINDOW_MAX, max(HEAD_WINDOW, exp + 2.5)))
    except (OSError, subprocess.SubprocessError):
        return out
    if not len(frames) or len(pts) != len(frames):
        say("sync: the head of the stage take could not be read")
        return out
    p0 = pts[0]
    head_runs = [(a - p0, b - p0) for a, b in pointer_runs(frames, pts)]
    early = [r for r in head_runs if r[0] <= exp + 1.6]
    out["start"] = _nearest(early, exp, 1.6) or (early[0] if early else None)
    if not out["start"]:
        say(f"sync: no start mark near {exp:.2f} s (candidates: {[round(r[0], 2) for r in head_runs][:8]}, "
            f"{len(frames)} frames read)")
    if len(log_marks) < 2:
        return out
    off = out["start"][0] - 0.5 / max(fps, 1.0) - log_marks[0] if out["start"] else clock_a
    exp_stop = off + log_marks[-1] if off is not None else None
    try:
        if exp_stop is not None and duration and duration - exp_stop > TAIL_WINDOW_MAX - 2.5:
            # the file runs on long after the stop mark (the stop waited for a recorder that was
            # far behind): read around where the mark must be, not the whole end of the file
            frames, pts = _probe_window(raw, size, ffmpeg, span=(p0 + exp_stop - 3.0, POINTER_STAY_MAX_S))
        else:
            tail = TAIL_WINDOW
            if exp_stop is not None and duration:
                tail = min(TAIL_WINDOW_MAX, max(TAIL_WINDOW, duration - exp_stop + 2.5))
            frames, pts = _probe_window(raw, size, ffmpeg, tail=tail)
    except (OSError, subprocess.SubprocessError):
        return out
    if len(frames) and len(pts) == len(frames):
        # the stop mark stays in the corner until the file ends; a take made before it did
        # (and one whose recorder outlived the mark) shows the pointer leaving again
        after = out["start"][1] if out["start"] else -1.0
        stays = [(a - p0, b - p0) for a, b in pointer_open_runs(frames, pts) if a - p0 > after]
        # (while the pointer waits in the corner it moves a pixel to and fro: at a low frame
        # rate two such frames look like a short visit of their own, so a visit inside a stay
        # is not a mark)
        visits = [(a - p0, b - p0) for a, b in pointer_runs(frames, pts)
                  if a - p0 > after and not any(s[0] <= a - p0 for s in stays)]
        tol = 0.6 if out["start"] else 1.6
        stop = _nearest(stays, exp_stop, tol) or _nearest(visits, exp_stop, tol)
        if stop:
            out["stop"] = stop
        else:
            say(f"sync: no stop mark near {exp_stop if exp_stop is None else round(exp_stop, 2)} s (stays from "
                f"{[round(r[0], 2) for r in stays][:6]}, visits at {[round(r[0], 2) for r in visits][:6]}, "
                f"{len(frames)} frames read)")
    return out


def fit_pointer(log_marks: list[float], found: dict, fps: float, clock_a: float | None = None) -> SyncFit:
    """The time fit from the stage's pointer marks (each known to be the start or the stop)."""
    start, stop = found.get("start"), found.get("stop")
    half = 0.5 / max(fps, 1.0)
    if start and stop and len(log_marks) >= 2:
        f = fit([log_marks[0], log_marks[-1]], [start, stop], fps, None)
        if f.method == "markers2":
            return f
    if start and log_marks:
        return SyncFit(start[0] - half - log_marks[0], 1.0, [start[0] - half], [log_marks[0]], "markers1")
    if stop and len(log_marks) >= 2:
        return SyncFit(stop[0] - half - log_marks[-1], 1.0, [stop[0] - half], [log_marks[-1]], "markers1")
    if clock_a is not None:
        return SyncFit(clock_a, 1.0, [], list(log_marks), "clock")
    return SyncFit(method="none", markers_video=[], markers_log=list(log_marks))


def pointer_trim(found: dict, fps: float, duration: float) -> tuple[float, float] | None:
    """Default export range that leaves the pointer marks (and the pointer's way back) out."""
    start, stop = found.get("start"), found.get("stop")
    if not start and not stop:
        return None
    f = max(fps, 1.0)
    t0 = start[1] + 2.0 / f + POINTER_AFTER_S if start else 0.0
    t1 = stop[0] - 1.0 / f - POINTER_AFTER_S if stop else duration
    return (round(t0, 4), round(t1, 4)) if t1 > t0 else None


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

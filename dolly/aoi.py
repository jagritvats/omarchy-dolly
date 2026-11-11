"""Area of interest (AOI): what the viewer should be looking at, frame by frame.

The cursor alone is a bad proxy: people type in a terminal with the mouse
parked elsewhere, logs scroll, dialogs open far from the pointer. The AOI
combines, per sample (10 Hz):

  * an **activity map**: where pixels actually changed (frame differences on a
    small grayscale proxy, dilated so glyphs merge into lines/blocks)
  * the **focused window** and the window list (Hyprland events)
  * **typing** (key events), **pointer** activity (moves/clicks) and window
    **layout** changes (open/close/move/resize)

Noise is filtered so the camera never chases it:
  * whole-screen changes (workspace slides, fullscreen toggles, Hyprland
    animations) are *transitions*, not activity -> the camera cuts instead
  * activity outside the focused window (bar clock, spinners, notifications,
    other windows) is ignored unless the pointer is right there
  * tiny isolated changes (blinking caret, cursor-sized flicker) only count
    while the user is typing
  * big continuous changes inside a window (a scrolling build log) mean
    "frame the whole window", not "jitter after each line"

Output: AOITrack with, per sample, a rect (video px) or None, a kind
("typing" | "scroll" | "pointer" | "layout" | None), a focus point (the
freshest change, e.g. the caret) and the transition intervals.
"""
from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .events import Timeline

TERMINAL_CLASSES = {"alacritty", "foot", "footclient", "kitty", "ghostty", "com.mitchellh.ghostty",
                    "org.wezfurlong.wezterm", "wezterm", "xterm", "konsole", "org.gnome.console", "terminal"}


@dataclass
class AOIConfig:
    fps: float = 10.0
    proxy_w: int = 480
    diff_thresh: int = 14
    transition_frac: float = 0.30       # >30 % of the screen changed in one sample = transition
    tiny_area: float = 0.0008           # blobs smaller than this fraction of the screen are "tiny"
    scroll_frac: float = 0.45           # one blob covering > 45 % of its window = a full redraw
    typing_window: float = 1.0          # keys within this many seconds = typing mode
    typing_lookback: float = 1.6        # union activity over this span while typing
    pointer_window: float = 1.2
    layout_window: float = 0.8          # a window that just moved/resized stays the subject this long
    open_window: float = 1.5            # ... and one that just opened
    near_cursor: float = 260.0          # video px; activity this close to an active cursor counts
    margin_lines: float = 1.2           # AOI margin around text activity, in text lines
    panel_area: float = 0.006           # a solid new region this big (of the screen) = a dialog / menu
    panel_max: float = 0.14             # ... and a compact changed region up to this big, right after a click

    @classmethod
    def from_dict(cls, d):
        c = cls()
        for k, v in (d or {}).items():
            if hasattr(c, k):
                setattr(c, k, type(getattr(c, k))(v))
        return c


@dataclass
class Activity:
    """Per-sample blobs from frame differences."""
    t: np.ndarray                                # sample times (s)
    frac: np.ndarray                             # changed fraction of the whole frame
    blobs: list = field(default_factory=list)    # per sample: [(x0, y0, x1, y1, area_frac), ...] video px
    w: int = 0
    h: int = 0
    reactions: list = field(default_factory=list)  # [(t_click, rect | None)]: solid region a click changed


def _color_frame(raw: Path, t: float, pw: int, ph: int):
    from .render import ffmpeg_bin
    try:
        out = subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(raw), "-frames:v",
                              "1", "-vf", f"scale={pw}:{ph}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             timeout=30, capture_output=True).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None              # luminance alone still finds most dialogs
    if len(out) < pw * ph * 3:
        return None
    return np.frombuffer(out[: pw * ph * 3], np.uint8).reshape(ph, pw, 3)


def click_reactions(frames: np.ndarray, fps: float, W: int, H: int, clicks, thresh: int = 14, raw=None) -> list:
    """For each click: the solid region that differs between just before and 0.6 s after
    (a dialog, menu or dropdown it opened, or closed). Dimming overlays barely pass the
    threshold; text-only changes are not solid, so both are ignored."""
    n = len(frames)
    if n == 0:
        return []
    ph, pw = frames.shape[1:]
    sx, sy = W / pw, H / ph
    out = []
    for t in clicks:
        a = int(np.clip(round((t - 0.1) * fps), 0, n - 1))
        b = int(np.clip(round((t + 0.6) * fps), 0, n - 1))
        if b <= a:
            out.append((t, None))
            continue
        fa, fb = frames[a].astype(np.float32), frames[b].astype(np.float32)
        changed = np.abs(fb - fa) > thresh
        if raw is not None:      # colour catches dark-on-dark dialogs that luminance misses
            ca, cb = _color_frame(raw, a / fps, pw, ph), _color_frame(raw, b / fps, pw, ph)
            if ca is not None and cb is not None:
                changed |= (np.abs(cb.astype(np.int16) - ca.astype(np.int16)).max(axis=2) > thresh * 0.6)
        # a modal backdrop dims everything by one factor: drop pixels that just got darker by
        # the dominant ratio, so only the dialog itself remains
        ratio = (fb + 1) / (fa + 1)
        rc = ratio[changed & (fa > 20)]
        if rc.size > 50:
            hist, edges = np.histogram(rc, bins=46, range=(0.2, 2.5))
            k = int(np.argmax(hist))
            mode = (edges[k] + edges[k + 1]) / 2
            if hist[k] > 0.25 * rc.size and abs(mode - 1.0) > 0.12:
                changed &= ~((np.abs(ratio - mode) < 0.07) & (fa > 20))
        m = changed.astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        nl, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        best = None
        for j in range(1, nl):
            x, y, w, h, area = stats[j]
            frac = area / (pw * ph)
            if frac < 0.006 or frac > 0.3 or area / max(w * h, 1) < 0.5:
                continue
            if best is None or area > best[4]:
                best = (x, y, w, h, area)
        if best is not None:
            # parts of the same panel (title bar, field rows) split by unchanged pixels
            bx0, by0, bx1, by1 = best[0], best[1], best[0] + best[2], best[1] + best[3]
            grown = True
            while grown:
                grown = False
                for j in range(1, nl):
                    x, y, w, h, area = stats[j]
                    if area < 4:
                        continue
                    if bx0 <= x and x + w <= bx1 and by0 <= y and y + h <= by1:
                        continue
                    ovx = min(x + w, bx1) - max(x, bx0)
                    ovy = min(y + h, by1) - max(y, by0)
                    gapx = max(bx0 - (x + w), x - bx1, 0)
                    gapy = max(by0 - (y + h), y - by1, 0)
                    # rows above/below within the panel's columns, or columns beside it on its rows
                    stacked = ovx > 0.5 * w and x >= bx0 - 3 and x + w <= bx1 + 3 and gapy <= max(6, 0.6 * (by1 - by0))
                    beside = ovy > 0.5 * h and y >= by0 - 3 and y + h <= by1 + 3 and gapx <= 6 and area / max(w * h, 1) > 0.4
                    if stacked or beside:
                        bx0, by0, bx1, by1 = min(bx0, x), min(by0, y), max(bx1, x + w), max(by1, y + h)
                        grown = True
            best = (bx0, by0, bx1 - bx0, by1 - by0, best[4])
        out.append((t, None if best is None else (best[0] * sx, best[1] * sy, (best[0] + best[2]) * sx,
                                                   (best[1] + best[3]) * sy)))
    return out


class _SparseFrames:
    """Just the proxy frames click_reactions reads, by index (it only needs len/shape/[])."""

    def __init__(self, keep: dict, n: int, ph: int, pw: int):
        self._keep, self._n, self.shape = keep, n, (n, ph, pw)

    def __len__(self):
        return self._n

    def __getitem__(self, i):
        return self._keep[int(i)]


def _click_frame_indices(clicks, fps: float) -> set:
    want = set()
    for t in clicks:
        want.add(max(0, int(round((t - 0.1) * fps))))
        want.add(max(0, int(round((t + 0.6) * fps))))
    return want


def _stream_proxy(raw: Path, fps: float, pw: int, ph: int):
    """Gray proxy frames of the recording, one at a time (never the whole file in memory).
    No timeout: it covers the whole recording; ffmpeg ends at the end of the file."""
    from .render import ffmpeg_bin
    cmd = [ffmpeg_bin(), "-v", "error", "-i", str(raw), "-vf", f"fps={fps},scale={pw}:{ph}:flags=area",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    size = pw * ph
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    n = 0
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            n += 1
            yield np.frombuffer(buf, np.uint8).reshape(ph, pw)
    finally:
        p.stdout.close()
        err = p.stderr.read()
        p.stderr.close()
        rc = p.wait()
    if rc != 0 and n == 0:
        raise subprocess.CalledProcessError(rc, cmd, stderr=err)


def compute_activity(raw: Path, W: int, H: int, cfg: AOIConfig | None = None, frames=None, clicks=()) -> Activity:
    """`frames` (iterable of gray proxy images) overrides decoding (tests)."""
    cfg = cfg or AOIConfig()
    pw = cfg.proxy_w
    ph = max(2, int(round(pw * H / W / 2)) * 2)
    decoded = frames is None
    if frames is None:
        source = _stream_proxy(raw, cfg.fps, pw, ph)
    else:
        frames = np.stack(list(frames))
        ph, pw = frames.shape[1:]
        source = iter(frames)
    sx, sy = W / pw, H / ph
    kern = np.ones((1, 5), np.uint8)  # horizontal only: glyphs merge into words/lines, lines stay apart
    want = _click_frame_indices(clicks, cfg.fps)
    keep: dict = {}
    fracs: list = []
    blobs: list = []
    prev = None
    last_raw = None
    n = 0
    for i, f in enumerate(source):
        n = i + 1
        last_raw = f
        if i in want:
            keep[i] = f.copy()
        cur = f.astype(np.int16)
        if prev is None:
            fracs.append(0.0)
            blobs.append([])
            prev = cur
            continue
        ch = np.abs(cur - prev) > cfg.diff_thresh
        if ch.mean() > 0.05:
            # a modal backdrop / fade dims (or brightens) everything by one factor: that is not
            # activity, the dialog on top of it is
            ratio = (cur + 1.0) / (prev + 1.0)
            rc = ratio[ch & (prev > 20)]
            if rc.size > 200:
                hist, edges = np.histogram(rc, bins=46, range=(0.2, 2.5))
                k = int(np.argmax(hist))
                mode = (edges[k] + edges[k + 1]) / 2
                if hist[k] > 0.4 * rc.size and abs(mode - 1.0) > 0.12:
                    ch &= ~((np.abs(ratio - mode) < 0.07) & (prev > 20))
        m = ch.astype(np.uint8)
        prev = cur
        fr = float(m.mean())
        fracs.append(fr)
        row = []
        if fr:
            md = cv2.dilate(m, kern, iterations=2)
            nl, lab, stats, _ = cv2.connectedComponentsWithStats(md, connectivity=8)
            for j in range(1, nl):
                x, y, w, h, a = stats[j]
                real = int(m[y:y + h, x:x + w].sum())
                row.append((x * sx, y * sy, (x + w) * sx, (y + h) * sy, real / (pw * ph)))
        blobs.append(row)
    # click_reactions clips indices past the end to the last frame
    if n and last_raw is not None:
        keep[n - 1] = last_raw.copy()
        for i in list(want):
            if i >= n:
                keep.setdefault(n - 1, last_raw.copy())
    act = Activity(np.arange(n) / cfg.fps, np.array(fracs, dtype=float), blobs, W, H)
    act.reactions = click_reactions(_SparseFrames(keep, n, ph, pw), cfg.fps, W, H, clicks, cfg.diff_thresh,
                                    raw if decoded else None)
    return act


def cached_activity(project_dir: Path, tl: Timeline, cfg: AOIConfig | None = None) -> Activity:
    import pickle
    project_dir = Path(project_dir)
    from .project import Project
    raw = Project(project_dir).raw
    cache = project_dir / "aoi-activity-v8.pkl"
    if cache.exists() and cache.stat().st_mtime >= raw.stat().st_mtime:
        try:
            return pickle.loads(cache.read_bytes())
        except Exception:
            pass
    act = compute_activity(raw, tl.w, tl.h, cfg, clicks=[c["t"] for c in tl.clicks])
    cache.write_bytes(pickle.dumps(act))
    return act


def estimate_line_h(tl: Timeline, act: Activity, cfg: AOIConfig) -> float:
    """Text line height (video px). Preferred: the line *pitch*, i.e. the
    vertical spacing between rows of new text that appear together (command
    output). Fallback: the height of what changes while typing (caret cell)."""
    scale = float(tl.capture.get("scale", 1.0) or 1.0)
    lo, hi = 9 * scale, 48 * scale
    pitches = []
    tol = 3.5 * scale
    terms = {c.lower() for c in (tl.capture.get("terminals") or [])} | TERMINAL_CLASSES
    for t, blobs in zip(act.t, act.blobs):
        win = tl.focused_window_at(t)
        if win is None or str(win.get("class", "")).lower() not in terms:
            continue          # app list rows / cards have a much larger pitch than text lines
        wr = tuple(win["rect"])
        rows = sorted((b for b in blobs if b[4] < 0.02 and (b[3] - b[1]) < hi
                       and (wr is None or _inside((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, wr))), key=lambda b: b[1])
        # adjacent left-aligned rows of new text: command output, list items
        for a, b in zip(rows, rows[1:]):
            d = b[1] - a[1]
            if lo <= d <= hi and abs(a[0] - b[0]) <= 2 * tol and (a[3] - a[1]) < d and (b[3] - b[1]) < d:
                pitches.append(d)
    if len(pitches) >= 6:
        h, edges = np.histogram(pitches, bins=np.arange(lo, hi + 2 * scale, 2 * scale))
        # the lowest strong peak: rows that skipped a line in a sample show up at 2x the pitch
        strong = np.nonzero(h >= 0.25 * h.max())[0]
        k = int(strong[0]) if len(strong) else int(np.argmax(h))
        near = [p for p in pitches if edges[max(0, k - 1)] <= p <= edges[min(len(edges) - 1, k + 2)]]
        return float(np.median(near))
    keys = sorted(k["t"] for k in tl.keys)
    hs = []
    j = 0
    for i, t in enumerate(act.t):
        while j < len(keys) and keys[j] < t - 0.3:
            j += 1
        if j < len(keys) and keys[j] <= t:
            win = tl.focused_window_at(t)
            wr = tuple(win["rect"]) if win else None
            for (x0, y0, x1, y1, a) in act.blobs[i]:
                h = y1 - y0
                if a < 0.01 and 5 * scale <= h <= 60 * scale and (wr is None or _inside((x0 + x1) / 2, (y0 + y1) / 2, wr)):
                    hs.append(h)
    if len(hs) < 5:
        return 18.0 * scale
    # the caret (a full cell in most terminals) dominates what changes while typing
    return float(np.clip(np.percentile(hs, 75), 10 * scale, 48 * scale))


# ------------------------------------------------------------------ helpers
def transitions(act: Activity, cfg: AOIConfig) -> list[tuple[float, float]]:
    """Intervals where most of the screen changed (workspace slides, fullscreen, ...)."""
    out = []
    on = None
    for t, f in zip(act.t, act.frac):
        if f >= cfg.transition_frac:
            on = t if on is None else on
            end = t
        elif on is not None:
            out.append((on - 1 / cfg.fps, end + 0.5 / cfg.fps))
            on = None
    if on is not None:
        out.append((on - 1 / cfg.fps, end))
    return out


def _union(rects):
    rects = [r for r in rects if r is not None]
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))


def _inside(px, py, r, pad=0.0):
    return r is not None and r[0] - pad <= px <= r[2] + pad and r[1] - pad <= py <= r[3] + pad


def _open_layers(tl: Timeline, t: float) -> list:
    state: dict = {}
    for l in tl.layers:
        if l["t"] > t:
            break
        if l["open"] and l["rect"]:
            state[l["namespace"]] = l
        else:
            state.pop(l["namespace"], None)
    return list(state.values())


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def _clip(r, W, H):
    if r is None:
        return None
    x0, y0, x1, y1 = max(0, r[0]), max(0, r[1]), min(W, r[2]), min(H, r[3])
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


@dataclass
class AOITrack:
    t: np.ndarray
    rect: list            # per sample (x0, y0, x1, y1) or None
    kind: list            # per sample str | None
    focus: list           # per sample (x, y) or None: freshest change (caret / newest output)
    transitions: list     # [(t0, t1)]
    line_h: float         # text line height estimate (video px)

    def at(self, t: float):
        if len(self.t) == 0:
            return None, None, None
        i = int(np.clip(np.searchsorted(self.t, t, side="right") - 1, 0, len(self.t) - 1))
        return self.rect[i], self.kind[i], self.focus[i]

    def in_transition(self, t: float, pad: float = 0.0) -> bool:
        return any(a - pad <= t <= b + pad for a, b in self.transitions)

    def to_json(self):
        r = lambda v: None if v is None else [round(float(x), 1) for x in v]
        return {"t": [round(float(x), 3) for x in self.t], "rect": [r(x) for x in self.rect], "kind": self.kind,
                "focus": [r(x) for x in self.focus], "transitions": self.transitions, "line_h": self.line_h}


def build_aoi(tl: Timeline, act: Activity | None, cfg: AOIConfig | None = None) -> AOITrack:
    cfg = cfg or AOIConfig()
    W, H = tl.w, tl.h
    scale = float(tl.capture.get("scale", 1.0) or 1.0)
    line_h = estimate_line_h(tl, act, cfg) if act is not None else 18.0 * scale
    if act is None:
        return AOITrack(np.zeros(0), [], [], [], [], line_h)
    terms = {c.lower() for c in (tl.capture.get("terminals") or [])} | TERMINAL_CLASSES
    # wf-recorder bakes the pointer into the video; Omarchy hides it on every key press
    baked = bool(tl.capture.get("cursor_baked"))
    ptr_r = 40.0 * scale
    trans = transitions(act, cfg)
    # workspace switches are cuts even when the two workspaces look alike (Omarchy switches
    # instantly; with animations on, the frame-diff interval above covers the slide)
    cur_ws = None
    for w in tl.workspaces:
        if w.get("special"):
            continue
        if w["name"] == cur_ws:      # e.g. the special workspace closing: same workspace again
            continue
        first = cur_ws is None
        cur_ws = w["name"]
        if first and w["t"] < 0.2:
            continue
        a, b = w["t"] - 0.5 / cfg.fps, w["t"] + 1.5 / cfg.fps
        trans.append((a, b))
    trans.sort()
    merged = []
    for a, b in trans:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    trans = merged
    # typing = plain keys (Shift allowed); Super+Return, Ctrl+S... are commands, not typing
    keys = np.array(sorted(k["t"] for k in tl.keys if not (set(k.get("mods", [])) & {"ctrl", "alt", "super"})))
    clicks = np.array(sorted(c["t"] for c in tl.clicks))
    # pointer activity instants: real movement (> 6 px) or clicks
    pmoves = []
    lx = ly = None
    for t, x, y in zip(tl.moves_t, tl.moves_x, tl.moves_y):
        if lx is None:
            lx, ly = x, y           # where the pointer rests when recording starts is not movement
        elif abs(x - lx) + abs(y - ly) > 10 * scale:   # a resting hand jitters a few px
            pmoves.append(t)
            lx, ly = x, y
    pmoves = np.array(pmoves)
    layout_t = np.array(sorted(c["t"] for c in tl.layout_changes if c["reason"] in ("close", "move", "resize",
                                                                                    "fullscreen")))
    open_t = np.array(sorted(c["t"] for c in tl.layout_changes if c["reason"] == "open"))

    def recent(arr, t, span):
        if len(arr) == 0:
            return False
        i = np.searchsorted(arr, t, side="right")
        return i > 0 and t - arr[i - 1] <= span

    target = tl.capture.get("target") if tl.capture.get("mode") == "window" else None
    rects, kinds, foci = [], [], []
    bigflags: list = []
    scroll_until = -1.0
    panels: list = []          # dialogs / menus / popups that opened and are still open
    reactions = sorted(getattr(act, "reactions", []) or [], key=lambda r: r[0])
    ri = 0
    tiny = cfg.tiny_area
    for i, t in enumerate(act.t):
        if any(a <= t <= b + 0.35 for a, b in trans):    # the new screen settling in is not activity
            rects.append(None); kinds.append(None); foci.append(None); bigflags.append(False)
            panels = []
            continue
        win = tl.focused_window_at(t)
        if panels and win is not None and panels[-1]["win"] != win.get("address"):
            panels = []
        panels = [p for p in panels if t - p["t0"] < 30.0]
        while ri < len(reactions) and reactions[ri][0] + 0.15 <= t:
            tc, rr = reactions[ri]
            ri += 1
            if rr is None:
                continue
            hit = next((p for p in panels if _iou(p["rect"], rr) > 0.4), None)
            if hit is not None:
                panels.remove(hit)          # the same region changed back: it closed
            else:
                panels.append({"rect": rr, "t0": tc + 0.15, "win": win.get("address") if win else None})
        if target:   # window mode: the followed window is the frame of reference
            tr = tl.window_rect_at(t, target)
            win = {"rect": tr, "class": win.get("class", "") if win else ""} if tr else win
        wrect = _clip(tuple(win["rect"]) if win else None, W, H)
        cx, cy = tl.cursor_at(t)
        # a dialog drawn dark on a dark page changes only its text and border, so it is not
        # the solid region click_reactions looks for. It still appears as one compact blob
        # in the focused window within a second of the click; the same blob again is it closing.
        if wrect is not None and recent(clicks, t, 1.0):
            for (x0, y0, x1, y1, a) in act.blobs[i]:
                box = (x1 - x0) * (y1 - y0) / (W * H)
                if a < cfg.panel_area or box > cfg.panel_max or not _inside((x0 + x1) / 2, (y0 + y1) / 2, wrect):
                    continue
                br = (float(x0), float(y0), float(x1), float(y1))
                hit = next((p for p in panels if _iou(p["rect"], br) > 0.4), None)
                if hit is None:
                    panels.append({"rect": br, "t0": float(t), "win": win.get("address") if win else None})
                elif t - hit["t0"] > 0.5:
                    panels.remove(hit)          # the same region changed again: it closed
                else:
                    hit["rect"] = _union([hit["rect"], br])     # still fading in
        typing = recent(keys, t, cfg.typing_window)
        pointer = recent(pmoves, t, cfg.pointer_window) or recent(clicks, t, cfg.pointer_window)
        layout = recent(layout_t, t, cfg.layout_window) or recent(open_t, t, cfg.open_window)
        # notification toasts (layers without keyboard focus) are never the subject
        toasts = [l["rect"] for l in _open_layers(tl, t) if not l["keyboard"]]
        # gather recent blobs (lookback depends on mode) that pass the filters
        span = cfg.typing_lookback if typing else 0.6
        lo = np.searchsorted(act.t, t - span)
        kept, freshest, fresh_h, big = [], None, 0.0, False
        now_area, now_rows, now_y0, now_y1 = 0.0, 0, float("inf"), float("-inf")
        for j in range(lo, i + 1):
            for (x0, y0, x1, y1, a) in act.blobs[j]:
                bx, by = (x0 + x1) / 2, (y0 + y1) / 2
                if toasts and any(_inside(bx, by, r, pad=4) for r in toasts):
                    continue
                if baked and not pointer and abs(bx - cx) < ptr_r and abs(by - cy) < ptr_r \
                        and (x1 - x0) < 2 * ptr_r and (y1 - y0) < 2 * ptr_r:
                    continue                        # the recorded pointer hiding/showing while typing
                in_win = _inside(bx, by, wrect)
                near_ptr = pointer and math.hypot(bx - cx, by - cy) < cfg.near_cursor
                if not (in_win or near_ptr):
                    continue                        # clocks, notifications, other windows
                if a < tiny and not typing and not near_ptr:
                    continue                        # blinking caret / spinner while idle
                if wrect is not None and in_win:
                    wa = (wrect[2] - wrect[0]) * (wrect[3] - wrect[1]) / (W * H)
                    if a > cfg.scroll_frac * wa:
                        big = True
                    if j == i:
                        now_area += a
                        now_rows += 1
                        now_y0, now_y1 = min(now_y0, y0), max(now_y1, y1)
                kept.append((x0, y0, x1, y1))
                if j == i or freshest is None:
                    if j >= i - 2:
                        freshest, fresh_h = ((x0 + x1) / 2, (y0 + y1) / 2), y1 - y0
        if wrect is not None and not big:
            # scrolling text: many rows change at once over most of the window's height
            wa = (wrect[2] - wrect[0]) * (wrect[3] - wrect[1]) / (W * H)
            wh = wrect[3] - wrect[1]
            if now_rows >= 6 and (now_y1 - now_y0) > 0.55 * wh and now_area > 0.03 * wa:
                big = True
        bigflags.append(bool(big))
        if big:
            scroll_until = t + 0.6
        if (big or t < scroll_until) and wrect is not None:
            rects.append(wrect); kinds.append("scroll"); foci.append(freshest)
        elif typing and kept:
            u = _union(kept)
            m = cfg.margin_lines * line_h
            x0 = u[0] - m
            if win is not None and wrect is not None and str(win.get("class", "")).lower() in terms:
                x0 = wrect[0]      # a command line reads from the prompt, not from the last few glyphs
            rects.append(_clip((x0, u[1] - m, u[2] + m, u[3] + m), W, H)); kinds.append("typing")
            # the caret is where the newest glyphs are. A window painting itself while the
            # first keys arrive is not glyphs: its middle is not a caret (the camera would
            # frame, and the vertical crop widen for, the middle of the window)
            if freshest is not None and fresh_h <= 4 * line_h:
                foci.append(freshest)
            elif u[3] - u[1] <= 6 * line_h:
                foci.append(freshest or ((u[0] + u[2]) / 2, (u[1] + u[3]) / 2))
            else:
                foci.append(None)
        elif typing and wrect is not None:
            rects.append(wrect); kinds.append("typing"); foci.append(None)
        elif layout and wrect is not None:
            # every window that just changed (a split being dragged resizes both sides)
            recent_ch = [c for c in tl.layout_changes
                         if t - (cfg.open_window if c["reason"] == "open" else cfg.layout_window) <= c["t"] <= t]
            opened = [c for c in recent_ch if c["reason"] == "open"]
            if opened:      # a new window: it is the subject (its neighbours just make room)
                changed = [tl.window_rect_at(t + 0.35, opened[-1]["address"])]
                base = []
            else:           # a split being dragged resizes both sides
                changed = [tl.window_rect_at(t, c["address"]) for c in recent_ch if c["reason"] in ("resize", "move")]
                base = [wrect]
            u = _clip(_union(base + [tuple(r) for r in changed if r is not None]), W, H)
            rects.append(u or wrect); kinds.append("layout"); foci.append(None)
        elif kept and not pointer and any(_inside((k[0] + k[2]) / 2, (k[1] + k[3]) / 2, wrect) for k in kept) \
                and sum((k[2] - k[0]) * (k[3] - k[1]) for k in kept) > tiny * 4 * W * H:
            # new output in the focused window (command results, a page loading)
            ink = [k for k in kept if _inside((k[0] + k[2]) / 2, (k[1] + k[3]) / 2, wrect)]
            u = _union(ink)
            m = cfg.margin_lines * line_h
            x0 = wrect[0] if str(win.get("class", "")).lower() in terms else u[0] - m
            rects.append(_clip((x0, u[1] - m, u[2] + m, u[3] + m), W, H)); kinds.append("output")
            foci.append(None)
        elif pointer:
            pr = (cx - 110, cy - 90, cx + 110, cy + 90)
            near = [k for k in kept if math.hypot((k[0] + k[2]) / 2 - cx, (k[1] + k[3]) / 2 - cy) < cfg.near_cursor * 1.6]
            rects.append(_clip(_union([pr] + near), W, H)); kinds.append("pointer"); foci.append((cx, cy))
        elif target and wrect is not None:
            rects.append(wrect); kinds.append("layout"); foci.append(None)
        else:
            rects.append(None); kinds.append(None); foci.append(None)
        # a dialog / menu that is open frames whatever happens inside it, and right
        # after a click it opened, the click and the dialog together
        if panels and kinds[-1] in ("typing", "pointer", "output", None):
            r = rects[-1]
            for p in panels:
                pr = p["rect"]
                inside = r is not None and _inside((r[0] + r[2]) / 2, (r[1] + r[3]) / 2, pr, pad=line_h)
                fresh = t - p["t0"] < 1.0 and recent(clicks, p["t0"], 0.8)
                if fresh and r is not None and kinds[-1] == "pointer" and not inside \
                        and not _inside(cx, cy, pr, pad=3 * line_h):
                    # the pointer is leaving the dialog it opened: the dialog is the subject
                    rects[-1], kinds[-1], foci[-1] = pr, "output", None
                    continue
                if inside or (fresh and r is not None):
                    rects[-1] = _union([r, pr])
                    if kinds[-1] is None:
                        kinds[-1] = "pointer"
                elif r is None and t - p["t0"] < 0.6:
                    rects[-1], kinds[-1] = pr, "output"
    # scrolling must be sustained: one or two big redraws (a modal popping in over a
    # dimmed page, a page switch) are not a scrolling log
    bf = np.array(bigflags + [False] * (len(kinds) - len(bigflags)), bool)
    i = 0
    while i < len(kinds):
        if kinds[i] == "scroll":
            j = i
            while j < len(kinds) and kinds[j] == "scroll":
                j += 1
            if bf[i:j].sum() < 3:
                for k in range(i, j):
                    rects[k], kinds[k], foci[k] = None, None, None
            i = j
        else:
            i += 1
    return AOITrack(act.t, rects, kinds, foci, trans, line_h)

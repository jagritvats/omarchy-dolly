"""Auto-zoom engine.

Turns an event timeline into

1. **segments**: editable spans ``[start, end]`` during which the camera is
   zoomed in (``zoom``) on a focus point, and
2. a **camera path**: per output frame ``(z, cx, cy)`` in video pixels,
   produced by critically damped springs chasing step targets, so motion is
   always C1-continuous: no jerks, no overshoot, no jitter.

Everything is a pure function of (timeline, segments, config), so the
editor preview, the exporter and the tests agree frame for frame.

Coordinates: the video frame spans ``[0, W] x [0, H]``. The rendered canvas
adds padding ``(px, py)`` (in video pixels) around it. A camera ``(z, cx, cy)``
shows a viewport of size ``(CW / z, CH / z)`` centred on ``(cx, cy)``, where
``CW = W + 2 px`` and ``CH = H + 2 py``. At ``z = 1`` the viewport is exactly
the canvas.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from .events import Timeline


# --------------------------------------------------------------------- config
@dataclass
class ZoomConfig:
    zoom_default: float = 2.0      # zoom used when the action cluster is small
    zoom_min: float = 1.3          # never zoom in less than this (pointless)
    zoom_max: float = 2.5
    cluster_fill: float = 0.62     # cluster bbox may fill this much of the viewport
    lead: float = 0.75             # start zooming this long before the first action
    hold_click: float = 1.7        # stay zoomed this long after the last click
    hold_typing: float = 1.3       # ... after the last keystroke
    min_segment: float = 1.6       # shortest zoom segment
    merge_gap: float = 2.6         # actions closer than this (s) may share a segment
    bridge_gap: float = 1.4        # gap between segments shorter than this: pan, don't zoom out
    idle_zoom_out: float = 3.5     # no activity this long -> end the segment early
    jump_factor: float = 0.9       # cursor leaves focus by this x viewport -> end early
    jump_dip: float = 0.55         # consecutive foci further apart than this x viewport: pull back between them
    dip_window: float = 0.9        # min zoomed-out window (s) inserted between far-apart segments
    safe_area: float = 0.64        # cursor kept inside this fraction of the viewport
    follow: bool = True
    typing_anchor_radius: float = 220.0  # typing anchors to the last click if cursor stayed near
    use_dwell: bool = False        # infer actions from cursor dwell (no click source)
    dwell_time: float = 0.7
    dwell_min_travel: float = 120.0
    # springs (rad/s); critically damped, settle ~= 4.7 / omega seconds
    omega_zoom_in: float = 4.6
    omega_zoom_out: float = 3.6
    omega_pan: float = 4.2
    edge_margin: float = 0.25      # fraction of padding still visible at the edge when zoomed
    final_smooth: float = 0.035    # zero-phase smoothing of the final path (s)
    # area of interest (round 3): frame what changed, not just the cursor
    use_aoi: bool = True           # False = legacy cursor-only engine ("before")
    aoi_fill: float = 0.86         # an AOI rect may fill this much of the viewport
    aoi_lookahead: float = 0.12    # offline: aim at where the AOI is about to be (s)
    min_line_frac: float = 0.0145  # typed text line height >= this x output height (~16 px at 1080p)
    cut_transitions: bool = True   # workspace switches / full-screen changes: cut, never whip-pan
    follow_typing: bool = True     # Settings > Auto-zoom: zoom in on what is being typed
    # Settings > Auto-zoom (0.4.1)
    enabled: bool = True           # False: no automatic zooms (manual segments and "zoom here" marks still apply)
    zoom_clicks: bool = True       # zoom in on clicks
    zoom_windows: bool = True      # zoom on window opens/resizes and scrolling output
    glide: bool = True             # nearby actions: stay zoomed and pan between them (False: zoom out between)
    stay_gap: float = 6.0          # glide: longest pause (s) between two actions that keeps one zoomed shot
    pan_reach: float = 1.3         # glide: next action within this many viewports (per axis) -> pan, else pull back
    glide_speed: float = 1.1       # glide: the camera target moves at most this many viewports per second
    glide_land: float = 0.36       # glide: the next action lands within this central fraction of the view
    switch_reach: float = 0.6      # glide into another, already open app window only for hops up to this (viewports)
    motion: str = "normal"         # calm | normal | quick: spring speed

    @classmethod
    def from_dict(cls, d: dict | None) -> "ZoomConfig":
        cfg = cls()
        for k, v in (d or {}).items():
            if hasattr(cfg, k) and not isinstance(getattr(type(cfg), k, None), property):
                cur = getattr(cfg, k)
                if isinstance(cur, bool) and isinstance(v, str):
                    v = v.strip().lower() in ("1", "true", "on", "yes")
                setattr(cfg, k, type(cur)(v))
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def motion_factor(self) -> float:
        return MOTION.get(self.motion, 1.0)


MOTION = {"calm": 0.75, "normal": 1.0, "quick": 1.3}


@dataclass
class Segment:
    start: float
    end: float
    zoom: float
    focus: list | None = None          # [x, y] in video px; None = auto from actions
    follow: bool = True
    source: str = "auto"               # "auto" | "manual"
    reason: str = ""                   # "click x3", "typing", ...
    id: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = round(d["start"], 3)
        d["end"] = round(d["end"], 3)
        d["zoom"] = round(d["zoom"], 3)
        if d["focus"] is not None:
            d["focus"] = [round(float(d["focus"][0]), 1), round(float(d["focus"][1]), 1)]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(start=float(d["start"]), end=float(d["end"]), zoom=float(d.get("zoom", 1.8)),
                   focus=d.get("focus"), follow=bool(d.get("follow", True)),
                   source=d.get("source", "manual"), reason=d.get("reason", ""), id=str(d.get("id", "")))


# -------------------------------------------------------------------- actions
@dataclass
class Action:
    t: float
    x: float
    y: float
    kind: str  # click | type | dwell | mark | scroll | layout
    rect: tuple | None = None   # (x0, y0, x1, y1): the thing that must be visible
    moving: bool = False        # rect follows something moving (drag): only its size counts for the zoom


def _aoi_of(tl, aoi):
    return aoi if (aoi is not None and len(aoi.t)) else None


def extract_actions(tl: Timeline, cfg: ZoomConfig, aoi=None) -> list[Action]:
    """What the camera should show, filtered by the Auto-zoom triggers (clicks,
    typing, windows/output). "Zoom here" marks are explicit and always kept."""
    acts = _extract_actions(tl, cfg, aoi)
    if not cfg.enabled:
        return [a for a in acts if a.kind == "mark"]
    drop = set()
    if not cfg.zoom_clicks:
        drop |= {"click", "dwell"}
    if not cfg.zoom_windows:
        drop |= {"layout", "scroll", "output"}
    return [a for a in acts if a.kind not in drop] if drop else acts


def _extract_actions(tl: Timeline, cfg: ZoomConfig, aoi=None) -> list[Action]:
    aoi = _aoi_of(tl, aoi) if cfg.use_aoi else None
    if aoi is not None:
        return _aoi_actions(tl, cfg, aoi)
    acts: list[Action] = []
    for c in tl.clicks:
        acts.append(Action(c["t"], c["x"], c["y"], "click"))
    for m in getattr(tl, "marks", []):
        acts.append(Action(m["t"], m["x"], m["y"], "mark"))

    # Typing: plain keys (no ctrl/alt/super) anchor at the last click if the
    # cursor stayed near it (you click a field, then type), else at the cursor.
    clicks = sorted(tl.clicks, key=lambda c: c["t"])
    for k in tl.keys:
        mods = set(k.get("mods", []))
        if mods & {"ctrl", "alt", "super"} or not cfg.follow_typing:
            continue
        if k["key"] in ("Escape",):
            continue
        cx, cy = tl.cursor_at(k["t"])
        ax, ay = cx, cy
        prev = [c for c in clicks if c["t"] <= k["t"]]
        if prev:
            last = prev[-1]
            if (k["t"] - last["t"] < 20.0
                    and math.hypot(cx - last["x"], cy - last["y"]) < cfg.typing_anchor_radius):
                ax, ay = last["x"], last["y"]
        acts.append(Action(k["t"], ax, ay, "type"))

    if cfg.use_dwell or not tl.clicks:
        if cfg.use_dwell or tl.capture.get("click_source") == "none":
            acts.extend(detect_dwells(tl, cfg))

    acts.sort(key=lambda a: a.t)
    return acts


def last_caret(aoi, t: float, horizon: float = 8.0, ahead: float = 2.0):
    """The typing caret nearest to t: the first one seen in the next `ahead` s (the glyphs
    of a key appear a moment after it), else the most recent within `horizon` s, else None."""
    if aoi is None or not len(aoi.t):
        return None
    i0 = int(np.searchsorted(aoi.t, t, side="right")) - 1
    i = max(i0, 0)
    while i < len(aoi.t) and float(aoi.t[i]) - t <= ahead:
        if aoi.kind[i] == "typing" and aoi.focus[i] is not None:
            return aoi.focus[i]
        i += 1
    i = i0
    while i >= 0 and t - float(aoi.t[i]) <= horizon:
        if aoi.kind[i] == "typing" and aoi.focus[i] is not None:
            return aoi.focus[i]
        i -= 1
    return None


def around_caret(r, foc, line_h: float, W: float):
    """Narrow a typing rect to the lines around the caret. The AOI rect is the union of
    everything that changed recently (a `clear`, the previous command's output), so its
    centre can sit far from where the user is typing."""
    fx, fy = float(foc[0]), float(foc[1])
    y0, y1 = max(r[1], fy - 3.0 * line_h), min(r[3], fy + 1.5 * line_h)
    if y1 - y0 < line_h:                       # caret outside the rect: trust the caret
        y0, y1 = fy - 2.0 * line_h, fy + line_h
    x0, x1 = r[0], r[2]
    if x1 - x0 > 0.5 * W:                      # a full-width union: keep the prompt and the caret
        x0, x1 = max(x0, fx - 0.45 * W), min(x1, fx + 0.15 * W)
    if not (x0 <= fx <= x1):
        x0, x1 = min(x0, fx), max(x1, fx)
    return (x0, y0, x1, y1)


def _rect_union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


FOCUS_CLICK_WINDOW = 2.5      # s: typing this soon after a click may be what the click was for
FOCUS_CLICK_DISTANCE = 350.0  # logical px between the click and the caret to call it "elsewhere"


def focus_only_click(tl: Timeline, aoi, c: dict, s: float = 1.0) -> bool:
    """The click changed nothing around the pointer (it focused a window or a field), and
    plain typing starts somewhere else right after: the viewer should watch the typing."""
    t = float(c["t"])
    for dt in (0.15, 0.35, 0.6):
        rr, kind, _ = aoi.at(t + dt)
        if kind in ("scroll", "layout"):
            return False                      # the click rearranged or scrolled something
        if rr is not None and kind == "pointer" and ((rr[2] - rr[0]) > 232 or (rr[3] - rr[1]) > 192):
            return False                      # something changed next to the pointer (menu, dialog, highlight)
    nxt_click = min((float(o["t"]) for o in tl.clicks if float(o["t"]) > t), default=float("inf"))
    for k in tl.keys:
        kt = float(k["t"])
        if kt <= t:
            continue
        if kt - t > FOCUS_CLICK_WINDOW or kt > nxt_click:
            return False
        if set(k.get("mods", [])) & {"ctrl", "alt", "super"} or k["key"] == "Escape":
            return False                      # a shortcut, not typing into what was clicked
        caret = last_caret(aoi, kt + 0.15)
        if caret is None:
            return False                      # cannot tell where the typing is: keep the click
        return math.hypot(float(caret[0]) - c["x"], float(caret[1]) - c["y"]) > FOCUS_CLICK_DISTANCE * s
    return False


def _aoi_actions(tl: Timeline, cfg: ZoomConfig, aoi) -> list[Action]:
    """Actions whose *rect* is what the viewer must see (AOI-aware)."""
    acts: list[Action] = []
    s = float(tl.capture.get("scale", 1.0) or 1.0)

    def ok(t):
        return not aoi.in_transition(t, 0.05)

    for c in tl.clicks:
        if not ok(c["t"]) or not (0 <= c["x"] <= tl.w and 0 <= c["y"] <= tl.h):
            continue          # clicks on another monitor are not ours
        if cfg.follow_typing and focus_only_click(tl, aoi, c, s):
            continue          # it only focused where the typing happens: the typing is the shot
        box = (c["x"] - 150 * s, c["y"] - 50 * s, c["x"] + 150 * s, c["y"] + 50 * s)
        r = box
        # what the click caused (a dialog, a dropdown) next to the pointer; thin changes
        # (a row highlight) need nothing beyond the click box
        for dt in (0.15, 0.35, 0.6):
            rr, kind, _ = aoi.at(c["t"] + dt)
            if rr is not None and kind == "pointer" and (rr[3] - rr[1]) > 0.06 * tl.h:
                r = _rect_union(r, rr)
        if r is not None and ((r[2] - r[0]) > tl.w * 0.9 and (r[3] - r[1]) > tl.h * 0.9):
            r = box
        acts.append(Action(c["t"], (r[0] + r[2]) / 2, (r[1] + r[3]) / 2, "click", r))
    for m in getattr(tl, "marks", []):
        acts.append(Action(m["t"], m["x"], m["y"], "mark"))
    for k in tl.keys:
        mods = set(k.get("mods", []))
        if mods & {"ctrl", "alt", "super"} or k["key"] in ("Escape",) or not ok(k["t"]) or not cfg.follow_typing:
            continue
        r, kind, foc = aoi.at(k["t"] + 0.15)
        if r is None or kind not in ("typing", "scroll"):
            r2, kind2, foc2 = aoi.at(k["t"] + 0.4)
            r, kind, foc = (r2, kind2, foc2) if r2 is not None else (r, kind, foc)
        if r is not None and kind == "typing" and foc is None:
            foc = last_caret(aoi, k["t"] + 0.15)
        if r is None:
            win = tl.focused_window_at(k["t"])
            r = tuple(win["rect"]) if win else None
            foc = foc or last_caret(aoi, k["t"] + 0.15)
            kind = "typing" if r is not None and foc is not None else kind
        if r is not None and kind == "typing" and foc is not None:
            r = around_caret(r, foc, getattr(aoi, "line_h", 0.0) or 18.0, tl.w)
        if r is None:
            x, y = tl.cursor_at(k["t"])
            acts.append(Action(k["t"], x, y, "type"))
            continue
        acts.append(Action(k["t"], (r[0] + r[2]) / 2, (r[1] + r[3]) / 2, "type", tuple(r)))
    # scrolling output: frame the whole window; new output: frame it (sampled twice a second)
    last = -9.0
    for t, r, kind in zip(aoi.t, aoi.rect, aoi.kind):
        if kind in ("scroll", "output") and r is not None and t - last >= 0.5:
            acts.append(Action(float(t), (r[0] + r[2]) / 2, (r[1] + r[3]) / 2, kind, tuple(r)))
            last = t
    # window layout changes (open / resize / move / fullscreen): frame the window after it settles
    target = tl.capture.get("target") if tl.capture.get("mode") == "window" else None
    for ch in tl.layout_changes:
        if ch["reason"] not in ("open", "resize", "move", "fullscreen") or not ok(ch["t"]):
            continue
        if target and ch["address"] == target:
            continue        # window mode: the base framing already follows the window
        r = tl.window_rect_at(ch["t"] + 0.35, ch["address"])
        if r is None:
            continue
        acts.append(Action(ch["t"], (r[0] + r[2]) / 2, (r[1] + r[3]) / 2, "layout", tuple(r),
                           moving=ch["reason"] == "move"))
    acts.sort(key=lambda a: a.t)
    return acts


def detect_dwells(tl: Timeline, cfg: ZoomConfig) -> list[Action]:
    """Cursor came to rest after travelling: likely an interaction point."""
    out: list[Action] = []
    ts, xs, ys = tl.moves_t, tl.moves_x, tl.moves_y
    if len(ts) < 2:
        return out
    seg_start = 0
    for i in range(1, len(ts) + 1):
        end_of_burst = i == len(ts) or ts[i] - ts[i - 1] >= cfg.dwell_time
        if not end_of_burst:
            continue
        travel = math.hypot(xs[i - 1] - xs[seg_start], ys[i - 1] - ys[seg_start])
        if travel >= cfg.dwell_min_travel:
            out.append(Action(ts[i - 1], xs[i - 1], ys[i - 1], "dwell"))
        seg_start = i if i < len(ts) else seg_start
    return out


# ------------------------------------------------------------------- segments
def _fit_zoom(bw: float, bh: float, W: float, H: float, cfg: ZoomConfig) -> float:
    """Largest zoom (<= default) at which a bbox fits in cluster_fill of the viewport."""
    zx = cfg.cluster_fill * W / max(bw, 1.0)
    zy = cfg.cluster_fill * H / max(bh, 1.0)
    return min(cfg.zoom_default, zx, zy)


def cluster_extent(cl: list[Action]):
    """(bw, bh, cx, cy, has_rect) of what a cluster must show."""
    xs0, ys0, xs1, ys1 = [], [], [], []
    mw = mh = 0.0
    for a in cl:
        if a.rect is not None and a.moving:
            mw, mh = max(mw, a.rect[2] - a.rect[0]), max(mh, a.rect[3] - a.rect[1])
            xs0.append(a.x); xs1.append(a.x); ys0.append(a.y); ys1.append(a.y)
        elif a.rect is not None:
            xs0.append(a.rect[0]); ys0.append(a.rect[1]); xs1.append(a.rect[2]); ys1.append(a.rect[3])
        else:
            xs0.append(a.x); xs1.append(a.x); ys0.append(a.y); ys1.append(a.y)
    bw = max(max(xs1) - min(xs0), mw)
    bh = max(max(ys1) - min(ys0), mh)
    return bw, bh, (min(xs0) + max(xs1)) / 2, (min(ys0) + max(ys1)) / 2, any(a.rect is not None for a in cl)


def _fit_cluster(cl: list[Action], W: float, H: float, cfg: ZoomConfig) -> float:
    bw, bh, _, _, has_rect = cluster_extent(cl)
    if not has_rect:
        return _fit_zoom(bw, bh, W, H, cfg)
    return min(cfg.zoom_default, cfg.aoi_fill * W / max(bw, 1.0), cfg.aoi_fill * H / max(bh, 1.0))


def cluster_actions(acts: list[Action], W: float, H: float, cfg: ZoomConfig) -> list[list[Action]]:
    clusters: list[list[Action]] = []
    cur: list[Action] = []
    for a in acts:
        if not cur:
            cur = [a]
            continue
        gap = a.t - cur[-1].t
        # "frame the whole window/screen" moments never merge with close-up actions
        wide_a = a.rect is not None and _fit_cluster([a], W, H, cfg) < cfg.zoom_min
        wide_c = _fit_cluster(cur, W, H, cfg) < cfg.zoom_min
        if wide_a != wide_c:
            clusters.append(cur)
            cur = [a]
            continue
        fits = _fit_cluster(cur + [a], W, H, cfg) >= min(cfg.zoom_min, _fit_cluster(cur, W, H, cfg) - 1e-6)
        same_kind_big = a.kind in ("scroll", "output") and all(c.kind in ("scroll", "output", "type") for c in cur)
        if gap <= cfg.merge_gap and (fits or same_kind_big):
            cur.append(a)
        else:
            clusters.append(cur)
            cur = [a]
    if cur:
        clusters.append(cur)
    return clusters


def _reason(cluster: list[Action]) -> str:
    n_click = sum(a.kind == "click" for a in cluster)
    n_type = sum(a.kind == "type" for a in cluster)
    n_dwell = sum(a.kind == "dwell" for a in cluster)
    n_mark = sum(a.kind == "mark" for a in cluster)
    parts = ["marked"] if n_mark else []
    if n_click:
        parts.append("click" + (f" x{n_click}" if n_click > 1 else ""))
    if n_type:
        parts.append("typing")
    if n_dwell and not parts:
        parts.append("dwell")
    return " + ".join(parts)


def legible_zoom(tl: Timeline, cfg: ZoomConfig, line_h: float | None) -> float:
    """Smallest zoom at which a text line of line_h video px is >= min_line_frac of the output height."""
    if not line_h:
        return 1.0
    return cfg.min_line_frac * tl.h * 1.12 / line_h   # 1.12 ~ canvas padding


def build_segments(tl: Timeline, cfg: ZoomConfig | None = None, aoi=None) -> list[Segment]:
    cfg = cfg or ZoomConfig()
    W, H = tl.w, tl.h
    aoi = _aoi_of(tl, aoi) if cfg.use_aoi else None
    acts = extract_actions(tl, cfg, aoi)
    segs: list[Segment] = []
    for cl in cluster_actions(acts, W, H, cfg):
        has_rect = any(a.rect is not None for a in cl)
        z = _fit_cluster(cl, W, H, cfg)
        if has_rect:
            if any(a.kind == "type" for a in cl) and aoi is not None:
                z = max(z, min(legible_zoom(tl, cfg, aoi.line_h), cfg.zoom_max))
            if z < 1.08:
                continue      # the subject is (nearly) the whole screen: stay wide
            z = float(np.clip(z, 1.0, cfg.zoom_max))
        else:
            z = float(np.clip(z, cfg.zoom_min, cfg.zoom_max))
        first, last = cl[0], cl[-1]
        hold = cfg.hold_typing if last.kind in ("type", "scroll", "layout", "output") else cfg.hold_click
        start = max(0.0, first.t - cfg.lead)
        end = last.t + hold
        _, _, fx, fy, _ = cluster_extent(cl)
        if not has_rect or all(a.kind == "click" for a in cl):
            end = _cut_on_jump_or_idle(tl, cfg, last.t, end, fx, fy, z)
        if end - start < cfg.min_segment:
            end = start + cfg.min_segment
        segs.append(Segment(start, min(end, tl.duration), z, None, cfg.follow, "auto", _reason(cl)))
    if aoi is not None and cfg.cut_transitions:
        segs = _split_at_transitions(segs, aoi.transitions, cfg)
    if aoi is not None:
        segs = _end_at_foreign_moves(tl, segs, acts, cfg)

    # Resolve overlaps created by lead/hold: later segment starts where it starts,
    # earlier one ends there (the spring pans between them).
    segs.sort(key=lambda s: s.start)
    out: list[Segment] = []
    for s in segs:
        if out and s.start < out[-1].end:
            prev = out[-1]
            similar = max(prev.zoom, s.zoom) / min(prev.zoom, s.zoom) < 1.3
            if s.start - prev.start < cfg.min_segment and not similar and aoi is not None:
                # a wide establishing beat right before a close-up: let the close-up win
                prev.end = s.start
                if prev.end - prev.start < 0.6:
                    out.pop()
                out.append(s)
                continue
            if s.start - prev.start < cfg.min_segment:
                # would leave a flicker-short segment: merge into one wider shot
                acts_in = [a for a in acts if prev.start - 0.01 <= a.t <= s.end + 0.01
                           and not (aoi is not None and a.rect is not None and _fit_cluster([a], W, H, cfg) < cfg.zoom_min)]
                z = _fit_cluster(acts_in, W, H, cfg) if acts_in else cfg.zoom_default
                if aoi is not None and z * 1.3 < min(prev.zoom, s.zoom):
                    # the two subjects are far apart: one shot for both would be (nearly) the
                    # whole screen. Keep both close-ups; the camera pans from one to the other.
                    prev.end = s.start
                    if prev.end - prev.start < 0.6:
                        out.pop()
                    out.append(s)
                    continue
                prev.end = max(prev.end, s.end)
                zmin = 1.0 if aoi is not None else cfg.zoom_min
                prev.zoom = float(np.clip(min(prev.zoom, s.zoom, z), zmin, cfg.zoom_max))
                prev.reason = _merge_reason(prev.reason, s.reason)
                continue
            prev.end = s.start
        out.append(s)
    out = [s for s in out if s.zoom >= 1.08 and s.end - s.start > 0.3]
    _insert_dips(tl, out, cfg, aoi)
    # a dip may only shorten segments; enforce ordering once more
    for a, b in zip(out, out[1:]):
        if a.end > b.start:
            a.end = b.start
    for i, s in enumerate(out):
        s.id = f"z{i + 1}"
    return out


def _end_at_foreign_moves(tl: Timeline, segs: list[Segment], acts: list[Action], cfg: ZoomConfig) -> list[Segment]:
    """A window starting to move/resize that the segment is not about ends the shot there
    (window mode: the followed window is dragged -> back to the base framing)."""
    own = {round(a.t, 3) for a in acts if a.kind == "layout"}
    starts = []
    last = -9.0
    for ch in tl.layout_changes:
        if ch["reason"] in ("move", "resize"):
            if ch["t"] - last > 0.5 and round(ch["t"], 3) not in own:
                starts.append(ch["t"])
            last = ch["t"]
    for s in segs:
        for tc in starts:
            if s.start + 0.6 < tc < s.end:
                s.end = tc
                break
    return [s for s in segs if s.end - s.start > 0.3]


def _split_at_transitions(segs: list[Segment], trans, cfg: ZoomConfig) -> list[Segment]:
    """No zoom segment spans a workspace switch / full-screen change."""
    out = []
    for s in segs:
        pieces = [s]
        for (a, b) in trans:
            nxt = []
            for p in pieces:
                if p.end <= a or p.start >= b:
                    nxt.append(p)
                    continue
                if a - p.start >= 0.6:
                    nxt.append(Segment(p.start, a, p.zoom, p.focus, p.follow, p.source, p.reason))
                if p.end - b >= cfg.min_segment * 0.75:
                    nxt.append(Segment(b + 0.25, p.end, p.zoom, p.focus, p.follow, p.source, p.reason))
            pieces = nxt
        out.extend(pieces)
    return out


def _insert_dips(tl: Timeline, segs: list[Segment], cfg: ZoomConfig, aoi=None) -> None:
    """Far-apart consecutive segments get a zoomed-out gap so the camera
    pulls back and pushes in (a 'swoop') instead of a long zoomed pan."""
    for a, b in zip(segs, segs[1:]):
        fa, fb = auto_focus(tl, a, cfg, aoi), auto_focus(tl, b, cfg, aoi)
        if cfg.glide and glides(tl, a, b, fa, fb, cfg, aoi):
            continue          # one zoomed shot that pans between them: no pull-back
        z = min(a.zoom, b.zoom)
        vw = tl.w / z
        if math.hypot(fb[0] - fa[0], fb[1] - fa[1]) < cfg.jump_dip * vw:
            continue
        gap = b.start - a.end
        if gap >= cfg.dip_window:
            continue
        need = cfg.dip_window - gap
        # take it mostly from the tail of `a` (its hold), keep b's lead-in
        take_a = min(need * 0.7, max(0.0, a.end - a.start - cfg.min_segment * 0.6))
        a.end -= take_a
        b.start += min(need - take_a, max(0.0, b.end - b.start - cfg.min_segment * 0.6) * 0.3)


def _merge_reason(a: str, b: str) -> str:
    def parse(r):
        n_click, typing = 0, False
        for part in r.split(" + "):
            if part.startswith("click"):
                n_click += int(part.split("x")[1]) if " x" in part else 1
            elif part == "typing":
                typing = True
        return n_click, typing
    c1, t1 = parse(a)
    c2, t2 = parse(b)
    n = c1 + c2
    parts = (["click" + (f" x{n}" if n > 1 else "")] if n else []) + (["typing"] if t1 or t2 else [])
    return " + ".join(parts) or a


def _cut_on_jump_or_idle(tl: Timeline, cfg: ZoomConfig, t_last: float, end: float,
                         fx: float, fy: float, z: float) -> float:
    """After the last action, zoom out early if the cursor leaves or goes idle."""
    vw, vh = tl.w / z, tl.h / z
    lim = cfg.jump_factor * max(vw, vh) * 0.5 + 0.0
    t = t_last
    last_move = t_last
    prev = tl.cursor_at(t)
    while t < end:
        t += 1 / 60
        x, y = tl.cursor_at(t)
        if abs(x - prev[0]) + abs(y - prev[1]) > 0.5:
            last_move = t
        prev = (x, y)
        if math.hypot(x - fx, y - fy) > lim:
            return t + 0.1
        if t - last_move > cfg.idle_zoom_out:
            return t
    return end


_ACT_CACHE: dict = {}


def _cached_actions(tl, cfg, aoi):
    key = (id(tl), id(aoi), cfg.use_aoi, cfg.use_dwell, cfg.typing_anchor_radius, cfg.enabled, cfg.zoom_clicks,
           cfg.zoom_windows, cfg.follow_typing)
    hit = _ACT_CACHE.get(key)
    if hit is None or hit[0] is not tl:
        if len(_ACT_CACHE) > 16:
            _ACT_CACHE.clear()
        hit = (tl, extract_actions(tl, cfg, aoi))
        _ACT_CACHE[key] = hit
    return hit[1]


def auto_focus(tl: Timeline, seg: Segment, cfg: ZoomConfig, aoi=None) -> tuple[float, float]:
    """Focus for a segment: centre of what its actions must show, else cursor at mid."""
    if seg.focus is not None:
        return float(seg.focus[0]), float(seg.focus[1])
    aoi = _aoi_of(tl, aoi) if cfg.use_aoi else None
    acts = [a for a in _cached_actions(tl, cfg, aoi) if seg.start - 0.05 <= a.t <= seg.end + 0.05]
    if acts:
        # the first beat matters most for where the camera lands; moving subjects use their first rect
        early = [a for a in acts if a.t <= acts[0].t + 1.2] if any(a.moving for a in acts) else acts
        _, _, fx, fy, _ = cluster_extent(early)
        return fx, fy
    return tl.cursor_at((seg.start + seg.end) / 2)


def _seg_actions(tl: Timeline, seg: Segment, cfg: ZoomConfig, aoi=None) -> list:
    aoi = _aoi_of(tl, aoi) if cfg.use_aoi else None
    return [a for a in _cached_actions(tl, cfg, aoi) if seg.start - 0.05 <= a.t <= seg.end + 0.05]


def seg_extent(tl: Timeline, seg: Segment, cfg: ZoomConfig, aoi=None) -> tuple:
    """(x0, y0, x1, y1): what the start of a segment must show."""
    if seg.focus is not None:
        fx, fy = float(seg.focus[0]), float(seg.focus[1])
        return fx, fy, fx, fy
    acts = _seg_actions(tl, seg, cfg, aoi)
    if acts:
        early = [a for a in acts if a.t <= acts[0].t + 1.2]
        bw, bh, cx, cy, _ = cluster_extent(early)
        return cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2
    x, y = tl.cursor_at((seg.start + seg.end) / 2)
    return x, y, x, y


def _shot_window(tl: Timeline, t: float, f) -> str | None:
    """The surface a shot is about at t: an open keyboard layer (launcher, Omarchy menu),
    else the window under its focus point, else the focused window."""
    lay = tl.keyboard_layer_at(t)
    if lay is not None:
        return "layer:" + lay["namespace"]
    w = tl.window_under(t, f[0], f[1]) or tl.focused_window_at(t)
    return w.get("address") if w else None


def _surface_existed(tl: Timeline, t: float, addr: str) -> bool:
    if addr.startswith("layer:"):
        lay = tl.keyboard_layer_at(t)
        return lay is not None and "layer:" + lay["namespace"] == addr
    snap = tl.snapshot_at(t)
    return bool(snap) and any(w.get("address") == addr for w in snap["windows"])


def _moved_between(tl: Timeline, t0: float, t1: float, addrs) -> bool:
    """One of these windows was dragged or resized in (t0, t1] (more than a few pixels)."""
    addrs = {a for a in addrs if a}
    for a in addrs:
        r0, r1 = tl.window_rect_at(t0, a), None
        for ch in tl.layout_changes:
            if t0 < ch["t"] <= t1 and ch["address"] == a and ch["reason"] in ("move", "resize"):
                r1 = tl.window_rect_at(ch["t"], a)
                if r0 is not None and r1 is not None and max(abs(p - q) for p, q in zip(r0, r1)) > 12:
                    return True
    return False


def _relayout_between(tl: Timeline, t0: float, t1: float) -> bool:
    """The screen was rearranged in (t0, t1], so a viewer needs the wide view to follow it:
    a tiled window opened, closed or went full screen (the others moved), or a big floating
    surface appeared or went away (the scratchpad, a large app window). Small floating
    windows (dialogs, pickers, popups) are part of the flow and don't count."""
    for ch in tl.layout_changes:
        if not (t0 < ch["t"] <= t1) or ch["reason"] not in ("open", "close", "fullscreen", "shown", "hidden"):
            continue
        before = ch["reason"] in ("close", "hidden")
        snap = tl.snapshot_at(ch["t"] + (-0.05 if before else 0.35))
        w = next((w for w in (snap or {}).get("windows", []) if w.get("address") == ch["address"]), None)
        if w is None or not w.get("floating"):
            return True
        r = w["rect"]
        if (r[2] - r[0]) * (r[3] - r[1]) > 0.25 * tl.w * tl.h:
            return True
    return False


def glides(tl: Timeline, a: Segment, b: Segment, fa, fb, cfg: ZoomConfig, aoi=None,
           W: float | None = None, H: float | None = None) -> bool:
    """Stay zoomed from shot `a` into the next shot `b`, panning (scrolling) between them,
    instead of pulling back to the whole screen and pushing in again.

    Glide (the default) when all hold:
    - the pause between a's last action and b's first is at most `stay_gap` s
    - b is within `pan_reach` viewports of a on each axis
    - no workspace switch / full-screen change in between
    - the pointer does not wander off somewhere else on screen meanwhile (a pointer parked
      away while typing, or a brief trip off this monitor, does not count)
    - no tiled window opens, closes or goes full screen in the pause (the layout changed),
      and the window the shots are about (or the recorded window) is not dragged or resized
    - switching to another app window that was already open is a change of context: only a
      short, quick hop (`switch_reach` viewports, half of `stay_gap`) glides; otherwise the
      camera pulls back so the viewer sees where the action went. A window the action itself
      opened (a dialog, a menu, a new terminal) is part of the same flow and glides.
    Round-3 bridges (gap under `bridge_gap` s, closer than `jump_dip` viewports) always join.
    With glide off ("zoom out between") only the round-3 rule applies."""
    W = W or tl.w
    H = H or tl.h
    z = max(min(a.zoom, b.zoom), 1.0)
    vw, vh = W / z, H / z
    gap = b.start - a.end
    aoi_ = _aoi_of(tl, aoi) if cfg.use_aoi else None
    if aoi_ is not None and any(a.end - 0.3 <= ta <= b.start + 0.3 for ta, _ in aoi_.transitions):
        return False
    round3 = gap < cfg.bridge_gap and math.hypot(fb[0] - fa[0], fb[1] - fa[1]) < cfg.jump_dip * vw
    if not cfg.glide or round3:
        return round3
    aa, bb = _seg_actions(tl, a, cfg, aoi), _seg_actions(tl, b, cfg, aoi)
    ta = max((x.t for x in aa if x.t <= a.end + 0.05), default=a.end - cfg.hold_click)
    tb = min((x.t for x in bb if x.t >= b.start - 0.05), default=b.start + cfg.lead)
    pause = tb - ta
    if gap >= cfg.bridge_gap and pause > cfg.stay_gap:
        return False
    hx, hy = abs(fb[0] - fa[0]) / vw, abs(fb[1] - fa[1]) / vh
    if hx > cfg.pan_reach or hy > cfg.pan_reach:
        return False
    if _relayout_between(tl, ta, max(tb, ta)):
        return False
    wa, wb = _shot_window(tl, ta, fa), _shot_window(tl, max(tb, ta), fb)
    target = tl.capture.get("target") if tl.capture.get("mode") == "window" else None
    if _moved_between(tl, ta, max(tb, ta), (wa, wb, target)):
        return False                  # the window itself was dragged/resized: show all of it
    if wa and wb and wa != wb and _surface_existed(tl, ta, wb):
        if max(hx, hy) > cfg.switch_reach or pause > cfg.stay_gap / 2:
            return False
    if gap > 0.3:
        # the pointer went somewhere else on screen in the pause: show where it went
        x0, x1 = min(fa[0], fb[0]) - 0.5 * vw, max(fa[0], fb[0]) + 0.5 * vw
        y0, y1 = min(fa[1], fb[1]) - 0.5 * vh, max(fa[1], fb[1]) + 0.5 * vh
        # measured from a's last action; a pointer already parked elsewhere (typing) or on
        # another monitor then does not count
        px, py = tl.cursor_at(ta)
        was_here = tl.cursor_inside(ta)
        away = 0.0
        for t in np.arange(a.end, b.start, 0.1):
            x, y = tl.cursor_at(float(t))
            if not tl.cursor_inside(float(t)):
                if was_here:
                    away += 0.1                 # left for another monitor: nothing to show here,
                    if away > 1.5:              # unless it stays away
                        return False
                continue
            if abs(x - px) + abs(y - py) < 40:
                continue
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                return False
    return True


# ---------------------------------------------------------------- camera path
def smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _softclamp(x, lo, hi, w):
    """Smooth clamp: identity in the middle, saturates softly at lo/hi."""
    if hi <= lo:
        return (lo + hi) / 2
    w = max(min(w, (hi - lo) / 4), 1e-6)

    def sp(v):
        return w * np.logaddexp(0.0, v / w)
    return lo + sp(x - lo) - sp(x - hi)


@dataclass
class CameraGeometry:
    W: float
    H: float
    px: float = 0.0   # padding in video px (each side)
    py: float = 0.0

    @property
    def CW(self):
        return self.W + 2 * self.px

    @property
    def CH(self):
        return self.H + 2 * self.py

    def bounds(self, z, edge_margin):
        """Allowed viewport bounds (x0, y0, x1, y1) at zoom z."""
        a = smoothstep((z - 1.0) / 0.5)
        mx = self.px * (1 - a) + self.px * edge_margin * a
        my = self.py * (1 - a) + self.py * edge_margin * a
        return -mx, -my, self.W + mx, self.H + my

    def clamp(self, z, cx, cy, edge_margin, soft=True):
        x0, y0, x1, y1 = self.bounds(z, edge_margin)
        vw, vh = self.CW / z, self.CH / z
        if soft:
            cx = _softclamp(cx, x0 + vw / 2, x1 - vw / 2, 0.05 * vw)
            cy = _softclamp(cy, y0 + vh / 2, y1 - vh / 2, 0.05 * vh)
        else:
            cx = float(np.clip(cx, x0 + vw / 2, max(x0 + vw / 2, x1 - vw / 2)))
            cy = float(np.clip(cy, y0 + vh / 2, max(y0 + vh / 2, y1 - vh / 2)))
            if x1 - x0 < vw:
                cx = (x0 + x1) / 2
            if y1 - y0 < vh:
                cy = (y0 + y1) / 2
        return cx, cy


def _spring_step(x, v, target, omega, dt):
    """Exact critically damped step toward a constant target."""
    d = x - target
    e = math.exp(-omega * dt)
    k = v + omega * d
    x_new = target + (d + k * dt) * e
    v_new = (v - omega * k * dt) * e
    return x_new, v_new


def _gauss_smooth(arr: np.ndarray, sigma_samples: float) -> np.ndarray:
    if sigma_samples < 0.5 or len(arr) < 3:
        return arr
    r = int(math.ceil(3 * sigma_samples))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma_samples) ** 2)
    k /= k.sum()
    pad = np.pad(arr, r, mode="edge")
    return np.convolve(pad, k, mode="valid")


def smooth_cursor(tl: Timeline, times: np.ndarray, sigma: float, pin_sigma: float = 0.09):
    """Zero-phase smoothed cursor at `times`, pinned exactly onto click points."""
    sim_dt = 1 / 240
    t0, t1 = float(times.min()) - 1.0, float(times.max()) + 1.0
    grid = np.arange(t0, t1 + sim_dt, sim_dt)
    raw = np.array([tl.cursor_at(t) for t in grid])
    sx = _gauss_smooth(raw[:, 0], sigma / sim_dt)
    sy = _gauss_smooth(raw[:, 1], sigma / sim_dt)
    for c in tl.clicks:
        if not (t0 <= c["t"] <= t1):
            continue
        i = int(round((c["t"] - t0) / sim_dt))
        i = min(max(i, 0), len(grid) - 1)
        dx, dy = c["x"] - sx[i], c["y"] - sy[i]
        w = np.exp(-0.5 * ((grid - c["t"]) / pin_sigma) ** 2)
        sx += dx * w
        sy += dy * w
    return np.interp(times, grid, sx), np.interp(times, grid, sy)


@dataclass
class CameraPath:
    fps: float
    t0: float
    z: np.ndarray
    cx: np.ndarray
    cy: np.ndarray
    cur_x: np.ndarray = field(default=None)
    cur_y: np.ndarray = field(default=None)
    cuts: list = field(default_factory=list)   # output frame indices where the camera cuts
    glide: np.ndarray = field(default=None)    # 0..1 per frame: gliding between joined shots

    def to_json(self, decimals=2) -> dict:
        r = lambda a: [round(float(v), decimals) for v in a]
        return {"fps": self.fps, "t0": self.t0, "z": [round(float(v), 4) for v in self.z],
                "cx": r(self.cx), "cy": r(self.cy),
                "cur_x": r(self.cur_x) if self.cur_x is not None else None,
                "cur_y": r(self.cur_y) if self.cur_y is not None else None, "cuts": list(self.cuts)}


# ------------------------------------------------------------ subject track
@dataclass
class SubjectTrack:
    """Per simulation step: the rect that must stay in view (or NaN) and an anchor point."""
    x0: np.ndarray
    y0: np.ndarray
    x1: np.ndarray
    y1: np.ndarray
    ax: np.ndarray      # anchor (caret / newest output); NaN = none
    ay: np.ndarray
    kind: list


READ_HOLD = 0.5     # s a finished subject (command output, typed line) stays framed for reading


def subject_track(tl: Timeline, aoi, sim_t: np.ndarray, curx, cury, cfg: ZoomConfig,
                  lookahead: float | None = None) -> SubjectTrack:
    n = len(sim_t)
    x0, y0, x1, y1 = (np.full(n, np.nan) for _ in range(4))
    ax, ay = np.full(n, np.nan), np.full(n, np.nan)
    kinds: list = [None] * n
    s = float(tl.capture.get("scale", 1.0) or 1.0)
    bx, by = 40 * s, 34 * s
    # anticipation: pointer travel that leads to typing / output elsewhere (the mouse
    # parked out of the way) frames the upcoming subject, not the travelling pointer
    acts = [a for a in _cached_actions(tl, cfg, aoi) if a.rect is not None]
    act_t = np.array([a.t for a in acts]) if acts else np.zeros(0)
    click_t = np.array(sorted(c["t"] for c in tl.clicks)) if tl.clicks else np.zeros(0)
    la = cfg.aoi_lookahead if lookahead is None else lookahead
    last_real = -9.0
    held, held_t = None, -9.0          # last real subject, kept READ_HOLD s for reading
    for i, t in enumerate(sim_t):
        r, kind, foc = aoi.at(t + la)
        if kind in ("typing", "scroll", "layout", "output"):
            last_real = t
        if kind in ("pointer", None) and len(act_t):
            j = int(np.searchsorted(act_t, t))
            # leave the last result on screen ~1 s (reading time) before moving to what's next
            # (a moving pointer is already on its way: follow where it is going)
            horizon = 2.0 if (kind == "pointer" or t - last_real > cfg.hold_typing * 0.8) else 0.6
            if j < len(acts) and acts[j].kind == "click" and acts[j].t - t < 0.6:
                r, kind, foc = acts[j].rect, "pointer", None      # frame the click target before it happens
            elif j < len(acts) and acts[j].t - t < horizon and acts[j].kind in ("type", "scroll", "layout", "output"):
                kc = int(np.searchsorted(click_t, t))
                if kc >= len(click_t) or click_t[kc] > acts[j].t:
                    r, kind, foc = acts[j].rect, "typing" if acts[j].kind == "type" else acts[j].kind, None
        fresh_real = r is not None and kind in ("typing", "scroll", "layout", "output")
        if r is None and held is not None and t - held_t < READ_HOLD and not aoi.in_transition(t):
            r, kind, foc = held          # the result stays the subject while it is being read
        if r is not None and kind == "typing":
            foc = foc if foc is not None else last_caret(aoi, t + la)
            if foc is not None:
                r = around_caret(r, foc, getattr(aoi, "line_h", 0.0) or 18.0, tl.w)
        if fresh_real:
            held, held_t = (r, kind, foc), t
        if r is not None and kind in ("typing", "scroll", "layout", "pointer", "output"):
            x0[i], y0[i], x1[i], y1[i] = r
            kinds[i] = kind
            if kind == "pointer":
                cx, cy = curx[i], cury[i]
                x0[i], y0[i] = min(x0[i], cx - bx), min(y0[i], cy - by)
                x1[i], y1[i] = max(x1[i], cx + bx), max(y1[i], cy + by)
            if foc is not None and kind == "typing":
                ax[i], ay[i] = foc
        elif tl.cursor_inside(t) and not aoi.in_transition(t) and t - tl.last_pointer_before(t, 10 * s) < 1.5:
            cx, cy = curx[i], cury[i]
            x0[i], y0[i], x1[i], y1[i] = cx - bx, cy - by, cx + bx, cy + by
            kinds[i] = "cursor"
    return SubjectTrack(x0, y0, x1, y1, ax, ay, kinds)


def _dead_zone_rect(c: float, lo: float, hi: float, half: float, anchor: float = math.nan) -> float:
    """Move centre c minimally so [lo, hi] sits inside [c - half, c + half].
    Too big to fit: centre on it, but keep the anchor (caret) inside."""
    if hi - lo > 2 * half:
        m = (lo + hi) / 2
        if not math.isnan(anchor):
            m = min(max(m, anchor - half * 0.85), anchor + half * 0.85)
        return m
    if hi > c + half:
        c = hi - half
    if lo < c - half:
        c = lo + half
    return c


def _running_max(a: np.ndarray, w: int) -> np.ndarray:
    if w <= 1:
        return a
    from numpy.lib.stride_tricks import sliding_window_view
    pad = np.pad(a, (w // 2, w - 1 - w // 2), mode="edge")
    return sliding_window_view(pad, w).max(axis=1)


def camera_path(tl: Timeline, segments: list[Segment], geom: CameraGeometry,
                fps: float, t_start: float, t_end: float, cfg: ZoomConfig | None = None,
                cursor_sigma: float = 0.05, aoi=None, base=None) -> CameraPath:
    """aoi: dolly.aoi.AOITrack (None = legacy cursor-only engine).
    base: optional callable t -> (x0, y0, x1, y1) rect framed when no segment is active
    (window-follow recording mode)."""
    cfg = cfg or ZoomConfig()
    aoi = _aoi_of(tl, aoi) if cfg.use_aoi else None
    segs = sorted((s for s in segments if s.end > s.start), key=lambda s: s.start)
    # trim overlaps (user edits may overlap)
    clean: list[Segment] = []
    for s in segs:
        if clean and s.start < clean[-1].end:
            clean[-1] = Segment(**{**clean[-1].__dict__, "end": s.start})
        clean.append(s)
    segs = [s for s in clean if s.end - s.start > 0.05]
    foci = [auto_focus(tl, s, cfg, aoi) for s in segs]
    # Nearby shots are joined (glide): the camera stays zoomed and pans between them.
    # Far-apart ones pull back out so the viewer sees where the action went.
    bridged = [False] * len(segs)
    for j in range(1, len(segs)):
        bridged[j] = glides(tl, segs[j - 1], segs[j], foci[j - 1], foci[j], cfg, aoi, geom.W, geom.H)

    sim_dt = 1 / 240
    lead_in = 3.0  # simulate from before t_start so the state is settled
    sim_t0 = max(0.0, t_start - lead_in)
    if cfg.glide:
        # a joined run of shots carries its framing from shot to shot: a trimmed export
        # replays the run from its first shot so it matches the full export
        k = next((j for j, s in enumerate(segs) if s.end > sim_t0), None)
        while k is not None and k > 0 and bridged[k] and segs[k].start - 0.5 <= sim_t0 + 60.0:
            k -= 1
        if k is not None and segs[k].start < sim_t0 and bridged[min(k + 1, len(segs) - 1)]:
            sim_t0 = max(0.0, segs[k].start - 0.5, t_start - lead_in - 60.0)
    n_sim = int(math.ceil((t_end - sim_t0) / sim_dt)) + 2
    sim_t = sim_t0 + np.arange(n_sim) * sim_dt
    curx, cury = smooth_cursor(tl, sim_t, cursor_sigma)
    subj = subject_track(tl, aoi, sim_t, curx, cury, cfg) if aoi is not None else None

    # ---- base framing (z=1 full screen, or a followed window)
    bz = np.ones(n_sim)
    bxc = np.full(n_sim, geom.W / 2)
    byc = np.full(n_sim, geom.H / 2)
    if base is not None:
        padf = 1 + 2 * (geom.px / max(geom.W, 1))
        for i, t in enumerate(sim_t):
            r = base(t)
            if r is None:
                continue
            rw, rh = max(r[2] - r[0], 16), max(r[3] - r[1], 16)
            bz[i] = max(1.0, min(geom.CW / (rw * padf), geom.CH / (rh * padf)))
            bxc[i], byc[i] = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2

    # ---- target track (z, cx, cy) per sim step, with dead-zone follow
    tz = bz.copy()
    tcx = bxc.copy()
    tcy = byc.copy()
    seg_idx = np.full(n_sim, -1)
    in_gap = np.zeros(n_sim, dtype=bool)
    # dynamic zoom (AOI): the subject must fit; a segment only ever zooms *out* to make it fit
    zneed = None
    if subj is not None:
        sw = np.where(np.isnan(subj.x0), 1.0, subj.x1 - subj.x0)
        sh = np.where(np.isnan(subj.y0), 1.0, subj.y1 - subj.y0)
        zneed = np.minimum(cfg.aoi_fill * geom.CW / np.maximum(sw, 1), cfg.aoi_fill * geom.CH / np.maximum(sh, 1))
        zleg = legible_zoom(tl, cfg, aoi.line_h)
        typing = np.array([k == "typing" for k in subj.kind])
        zneed = np.where(typing, np.maximum(zneed, zleg), zneed)
        zneed = _running_max(zneed, int(0.3 / sim_dt))         # ignore dips shorter than 0.3 s

    # A pause whose subject needs a wider view than the shot (a window being moved or
    # resized, output scrolling past) is not held zoomed: the camera pulls back for it.
    if cfg.glide and zneed is not None:
        big = np.array([k in ("typing", "scroll", "layout", "output", "pointer") for k in subj.kind])
        for j in range(1, len(segs)):
            if not bridged[j]:
                continue
            m = (sim_t >= segs[j - 1].end) & (sim_t < segs[j].start) & big
            if m.sum() * sim_dt > 0.3 and np.percentile(zneed[m], 10) < 0.8 * min(segs[j - 1].zoom, segs[j].zoom):
                bridged[j] = False
    exts = [seg_extent(tl, s, cfg, aoi) for s in segs]
    # A long hop inside a joined run breathes out a little while the pointer travels
    # (never all the way to the whole screen) so both ends stay readable.
    zscale = np.ones(n_sim)
    if cfg.glide:
        for j in range(1, len(segs)):
            if not bridged[j]:
                continue
            a, b = segs[j - 1], segs[j]
            z = max(min(a.zoom, b.zoom), 1.0)
            hop = max(abs(foci[j][0] - foci[j - 1][0]) / (geom.W / z), abs(foci[j][1] - foci[j - 1][1]) / (geom.H / z))
            if hop <= 0.55:
                continue
            # from when the pointer sets off toward b (or just before b) until it has arrived
            p0 = tl.cursor_at(a.end)
            dep = next((float(t) for t in np.arange(a.end, b.start, 1 / 30)
                        if math.hypot(*np.subtract(tl.cursor_at(float(t)), p0)) > 0.12 * geom.W / z), b.start - 0.3)
            w0, w1 = max(a.end - 0.4, dep - 0.15), b.start + cfg.lead + 0.35
            f = max(0.62, 1.0 / (1.0 + 0.55 * (hop - 0.4)))
            zscale[(sim_t >= w0) & (sim_t < w1)] = np.minimum(zscale[(sim_t >= w0) & (sim_t < w1)], f)
    # short gaps (the round-3 bridge) pan early to the next shot, as before; only the longer
    # pauses that glide adds hold the shot we are in
    short = [False] + [segs[j].start - segs[j - 1].end < cfg.bridge_gap and
                       math.hypot(foci[j][0] - foci[j - 1][0], foci[j][1] - foci[j - 1][1])
                       < cfg.jump_dip * geom.W / max(min(segs[j].zoom, segs[j - 1].zoom), 1.0)
                       for j in range(1, len(segs))]
    starts = np.array([s.start for s in segs]) if segs else np.zeros(0)
    for i, t in enumerate(sim_t):
        k = int(np.searchsorted(starts, t, side="right")) - 1
        if k >= 0 and t < segs[k].end:
            seg_idx[i] = k
        elif k + 1 < len(segs) and bridged[k + 1] and k >= 0:
            if cfg.glide and not short[k + 1]:
                # in the pause: hold the shot we are in, following the pointer
                seg_idx[i] = k
                in_gap[i] = True
            else:
                # round-3 bridge: stay zoomed, pan early to the next one
                seg_idx[i] = k + 1

    # plan each shot once: the zoom that fits everything the segment will need to show
    # (offline, so the future is known) -> no zoom changes inside a segment
    seg_fit = {}
    if zneed is not None:
        subj_kinds = np.array([k in ("typing", "scroll", "layout", "pointer", "output") for k in subj.kind])
        for j in range(len(segs)):
            m = (seg_idx == j) & subj_kinds & ~in_gap     # the pause never widens the shot
            if m.any():
                seg_fit[j] = max(1.0, float(np.percentile(zneed[m], 3)))
                if any(subj.kind[i] == "typing" for i in np.nonzero(m)[0][::24]):
                    seg_fit[j] = max(seg_fit[j], min(zleg, segs[j].zoom))

    la = min(0.45, 2.0 / (cfg.omega_pan * cfg.motion_factor))
    lax, lay = smooth_cursor(tl, sim_t + la, cursor_sigma) if cfg.glide and any(bridged) else (curx, cury)
    follow_c = None
    glide_goal = None
    glide_on = np.zeros(n_sim)
    glide_hold = -1.0
    prev_j = -2
    for i in range(n_sim):
        j = int(seg_idx[i])
        if j < 0:
            prev_j = j
            follow_c = None
            continue
        s = segs[j]
        z = max(s.zoom, bz[i] * 1.3) if base is not None else s.zoom
        if j in seg_fit and s.source == "auto":
            z = max(1.0, min(z, seg_fit[j]))
        z = max(1.0, z * zscale[i])
        fx, fy = foci[j]
        vw, vh = geom.CW / z, geom.CH / z
        hx, hy = cfg.safe_area * vw / 2, cfg.safe_area * vh / 2
        if follow_c is None or (j != prev_j and not (cfg.glide and bridged[j] and prev_j == j - 1)):
            follow_c = [fx, fy]
            glide_goal = None
        elif j != prev_j:
            # gliding into the next shot: head for the least move that shows it (a scroll),
            # at a calm capped speed, so a big hop between inputs is never a whip-pan
            e = exts[j]
            gx, gy = cfg.glide_land * vw / 2, cfg.glide_land * vh / 2
            glide_goal = [_dead_zone_rect(follow_c[0], e[0], e[2], gx), _dead_zone_rect(follow_c[1], e[1], e[3], gy)]
        if glide_goal is not None:
            mx, my = cfg.glide_speed * vw * sim_dt, cfg.glide_speed * vh * sim_dt
            dx, dy = glide_goal[0] - follow_c[0], glide_goal[1] - follow_c[1]
            follow_c = [follow_c[0] + float(np.clip(dx, -mx, mx)), follow_c[1] + float(np.clip(dy, -my, my))]
            if abs(dx) <= mx and abs(dy) <= my:
                glide_goal = None
                glide_hold = sim_t[i] + 0.6          # let the springs land before calling it settled
        prev_j = j
        gliding = in_gap[i] or glide_goal is not None
        glide_on[i] = gliding or sim_t[i] < glide_hold
        if s.follow and cfg.follow and gliding and tl.cursor_inside(float(sim_t[i])) \
                and float(sim_t[i]) - tl.last_pointer_before(float(sim_t[i]), 10 * float(tl.capture.get("scale", 1.0) or 1.0)) < 1.5:
            # (a pointer parked out of the way while typing is not a subject)
            # between joined shots, follow where the pointer is about to be: the pan
            # spring trails a moving target by ~2/omega, and offline we know the future
            lo_x, hi_x = sorted((curx[i], lax[i]))
            lo_y, hi_y = sorted((cury[i], lay[i]))
            follow_c[0] = _dead_zone_rect(follow_c[0], lo_x, hi_x, hx)
            follow_c[1] = _dead_zone_rect(follow_c[1], lo_y, hi_y, hy)
        if s.follow and cfg.follow:
            if subj is not None:
                if not math.isnan(subj.x0[i]):
                    follow_c[0] = _dead_zone_rect(follow_c[0], subj.x0[i], subj.x1[i], hx, subj.ax[i])
                    follow_c[1] = _dead_zone_rect(follow_c[1], subj.y0[i], subj.y1[i], hy, subj.ay[i])
            elif not gliding:
                p = (curx[i], cury[i])
                for k, (pc, h) in enumerate(((p[0], hx), (p[1], hy))):
                    d = pc - follow_c[k]
                    if d > h:
                        follow_c[k] += d - h
                    elif d < -h:
                        follow_c[k] += d + h
        cx, cy = geom.clamp(z, follow_c[0], follow_c[1], cfg.edge_margin, soft=False)
        follow_c = [cx, cy]
        tz[i], tcx[i], tcy[i] = z, cx, cy

    # transitions (workspace switch, fullscreen, any whole-screen change): cut, never whip-pan
    cut_steps: list[int] = []
    if aoi is not None and cfg.cut_transitions:
        for (a, b) in aoi.transitions:
            m = (sim_t >= a) & (sim_t <= b)
            tz[m] = bz[m]
            tcx[m] = bxc[m]
            tcy[m] = byc[m]
            k = int(np.searchsorted(sim_t, a))
            if 0 < k < n_sim:
                cut_steps.append(k)

    # Follow targets move in small steps; low-pass them so the pan spring sees
    # a smooth target rather than a staircase (piecewise between cuts).
    def piecewise(arr, sigma):
        out = arr.copy()
        bounds = [0] + cut_steps + [n_sim]
        for a, b in zip(bounds, bounds[1:]):
            if b - a > 2:
                out[a:b] = _gauss_smooth(arr[a:b], sigma)
        return out
    tcx = piecewise(tcx, 0.12 / sim_dt)
    tcy = piecewise(tcy, 0.12 / sim_dt)

    # ---- springs
    mf = cfg.motion_factor
    lz, vz = math.log(max(tz[0], 1.0)), 0.0
    cx, cy = tcx[0], tcy[0]
    vx = vy = 0.0
    out_z = np.empty(n_sim)
    out_x = np.empty(n_sim)
    out_y = np.empty(n_sim)
    cut_set = set(cut_steps)
    for i in range(n_sim):
        tlz = math.log(tz[i])
        if i in cut_set:
            lz, vz, cx, cy, vx, vy = tlz, 0.0, tcx[i], tcy[i], 0.0, 0.0
        # Pre-aim: near z=1 the viewport is the whole canvas, so the centre is
        # free; jump it to the target so the zoom scales straight toward focus.
        if base is None and lz < math.log(1.004) and tlz > lz:
            cx, cy, vx, vy = tcx[i], tcy[i], 0.0, 0.0
        om = (cfg.omega_zoom_in if tlz > lz else cfg.omega_zoom_out) * mf
        lz, vz = _spring_step(lz, vz, tlz, om, sim_dt)
        cx, vx = _spring_step(cx, vx, tcx[i], cfg.omega_pan * mf, sim_dt)
        cy, vy = _spring_step(cy, vy, tcy[i], cfg.omega_pan * mf, sim_dt)
        lz = max(lz, 0.0)
        out_z[i], out_x[i], out_y[i] = lz, cx, cy

    zz = np.exp(out_z)
    # soft clamp to the allowed bounds for the *current* zoom
    fx_ = np.empty(n_sim)
    fy_ = np.empty(n_sim)
    for i in range(n_sim):
        fx_[i], fy_[i] = geom.clamp(zz[i], out_x[i], out_y[i], cfg.edge_margin, soft=True)
    # final zero-phase polish (never across a cut)
    sig = cfg.final_smooth / sim_dt
    zz = np.exp(piecewise(np.log(zz), sig))
    fx_ = piecewise(fx_, sig)
    fy_ = piecewise(fy_, sig)

    n_out = int(round((t_end - t_start) * fps))
    times = t_start + np.arange(n_out) / fps
    # cuts are applied below: no output frame interpolates between two shots
    path = CameraPath(fps, t_start, np.interp(times, sim_t, zz), np.interp(times, sim_t, fx_),
                      np.interp(times, sim_t, fy_))
    for k in cut_steps:
        tc = sim_t[k]
        jj = int(np.searchsorted(times, tc))
        if 0 < jj < n_out:
            path.cuts.append(jj)
            for arr, src in ((path.z, zz), (path.cx, fx_), (path.cy, fy_)):
                arr[jj] = src[min(k + 1, n_sim - 1)] if times[jj] < sim_t[min(k + 1, n_sim - 1)] else arr[jj]
    path.cur_x = np.interp(times, sim_t, curx)
    path.cur_y = np.interp(times, sim_t, cury)
    path.glide = np.interp(times, sim_t, _gauss_smooth(glide_on, 0.25 / sim_dt))
    return path

"""Vertical (9:16) and square (1:1) auto-reframe.

The crop window follows the action using the same ingredients as the
landscape camera: during zoom segments it centres on the segment focus (or
follows the cursor with a safe-area dead zone) and tightens with the segment
zoom; between segments it follows the smoothed cursor (a vertical crop shows
only ~1/3 of a wide screen, so it cannot just sit in the middle). Critically
damped springs + zero-phase smoothing keep it calm. No keyframes needed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .events import Timeline
from .zoom import (CameraGeometry, Segment, ZoomConfig, _aoi_of, _dead_zone_rect, _gauss_smooth, _spring_step,
                   camera_path, smooth_cursor, subject_track)


@dataclass
class CropPath:
    fps: float
    t0: float
    cx: np.ndarray
    cy: np.ndarray
    cw: np.ndarray
    ch: np.ndarray
    cur_x: np.ndarray
    cur_y: np.ndarray
    cuts: list = None


def reframe_path(tl: Timeline, segments: list[Segment], aspect: float, fps: float, t_start: float, t_end: float,
                 cfg: ZoomConfig | None = None, zoom_gain: float = 0.3, safe: float = 0.6,
                 omega: float = 4.2, cursor_sigma: float = 0.05, lookahead: float = 0.22, aoi=None,
                 omega_aoi: float = 4.2) -> CropPath:
    """aspect = crop width / height (9/16 for vertical, 1 for square)."""
    cfg = cfg or ZoomConfig()
    if cfg.use_aoi and _aoi_of(tl, aoi) is not None:
        return _reframe_aoi(tl, segments, aspect, fps, t_start, t_end, cfg, zoom_gain, omega_aoi, cursor_sigma,
                            lookahead, aoi)
    W, H = tl.w, tl.h
    base_h = min(H, W / aspect)        # largest crop of this aspect that fits
    base_w = base_h * aspect
    segs = sorted((s for s in segments if s.end > s.start), key=lambda s: s.start)
    sim_dt = 1 / 120
    sim_t0 = max(0.0, t_start - 3.0)
    n = int(math.ceil((t_end - sim_t0) / sim_dt)) + 2
    st = sim_t0 + np.arange(n) * sim_dt
    cx_, cy_ = smooth_cursor(tl, st, cursor_sigma)
    # offline render: aim at where the cursor is about to be, cancelling the spring lag
    ax_, ay_ = smooth_cursor(tl, st + lookahead, cursor_sigma)
    # The landscape camera already knows where the action is (cluster focus,
    # pre-aim, bridging, pull-backs); the crop centres on it while zoomed and
    # keeps the cursor inside its safe area at all times.
    land = camera_path(tl, segs, CameraGeometry(W, H, 0, 0), 1 / sim_dt, sim_t0, sim_t0 + n * sim_dt, cfg,
                       cursor_sigma=cursor_sigma)
    lz = np.interp(np.arange(n), np.arange(len(land.z)), land.z)
    lg = np.interp(np.arange(n), np.arange(len(land.glide)), land.glide) if land.glide is not None else np.zeros(n)
    lx = np.interp(np.arange(n), np.arange(len(land.cx)), land.cx)
    ly = np.interp(np.arange(n), np.arange(len(land.cy)), land.cy)
    tz = np.ones(n)
    tx = np.empty(n)
    ty = np.empty(n)
    if lg.any():
        # gliding: aim as far ahead as the spring trails (2/omega), like the landscape camera
        gx_, gy_ = smooth_cursor(tl, st + min(0.45, 2.0 / omega), cursor_sigma)
        ax_ = ax_ + (gx_ - ax_) * lg
        ay_ = ay_ + (gy_ - ay_) * lg
    fc = [float(cx_[0]), float(cy_[0])]
    for i in range(n):
        zoomed = lz[i] > 1.05
        z = 1.0 + (lz[i] - 1.0) * zoom_gain * (1.0 - float(lg[i]))   # widen while gliding: a hop crosses
        cw, ch = base_w / z, base_h / z                               # more of a narrow crop
        if zoomed:
            w = float(np.clip((lz[i] - 1.05) / 0.3, 0, 1))  # ease into following the landscape framing
            w *= 1.0 - float(lg[i])   # gliding between joined shots: the narrow crop follows the pointer
            fc = [fc[0] + (lx[i] - fc[0]) * w, fc[1] + (ly[i] - fc[1]) * w]
        # keep where the pointer is and where it is about to be (a glide between joined
        # shots crosses more of a narrow crop than of the landscape view)
        for a, (lo, hi, half) in enumerate(((min(ax_[i], cx_[i]), max(ax_[i], cx_[i]), safe * cw / 2),
                                            (min(ay_[i], cy_[i]), max(ay_[i], cy_[i]), safe * ch / 2))):
            fc[a] = _dead_zone_rect(fc[a], lo, hi, half, ax_[i] if a == 0 else ay_[i])
        fc[0] = float(np.clip(fc[0], cw / 2, W - cw / 2))
        fc[1] = float(np.clip(fc[1], ch / 2, H - ch / 2))
        tz[i], tx[i], ty[i] = z, fc[0], fc[1]
    tx = _gauss_smooth(tx, 0.1 / sim_dt)
    ty = _gauss_smooth(ty, 0.1 / sim_dt)
    lz, vz, x, vx, y, vy = 0.0, 0.0, tx[0], 0.0, ty[0], 0.0
    oz, ox, oy = np.empty(n), np.empty(n), np.empty(n)
    for i in range(n):
        lz, vz = _spring_step(lz, vz, math.log(tz[i]), omega, sim_dt)
        x, vx = _spring_step(x, vx, tx[i], omega, sim_dt)
        y, vy = _spring_step(y, vy, ty[i], omega, sim_dt)
        oz[i], ox[i], oy[i] = lz, x, y
    z = np.exp(np.maximum(oz, 0.0))
    cw, ch = base_w / z, base_h / z
    ox = np.clip(ox, cw / 2, W - cw / 2)
    oy = np.clip(oy, ch / 2, H - ch / 2)
    m = int(round((t_end - t_start) * fps))
    times = t_start + np.arange(m) / fps
    it = lambda a: np.interp(times, st, a)
    return CropPath(fps, t_start, it(ox), it(oy), it(cw), it(ch), it(cx_), it(cy_), [])


TEXT_KINDS = ("typing", "output", "scroll")
# A subject may widen the crop past the full-height strip by at most this factor, so the
# picture still fills 1 / LETTERBOX_MAX of the card's height (bands of about 12 % above and
# below). Anything wider (a whole window, a screen of output) is not shown whole: the crop
# stays a full-height strip on the part of it that matters. In order: where the pointer is
# at work; for text, the start of the lines; for a window, the part nearest to the crop.
LETTERBOX_MAX = 1.3
CUT_HOP = 0.6          # a jump of the crop by more than this many crop widths is a cut, not a pan
POINTER_PAN = 1.1      # following a travelling pointer, the crop moves at most this many widths a second


def text_x(c: float, lo: float, hi: float, half: float, anchor: float = math.nan) -> float:
    """Horizontal centre for text [lo, hi] in a crop of half-width `half`. Text that fits is
    left alone (c). Text wider than the crop is shown from the start of its lines (the prompt,
    the left margin) rather than from its middle, but a caret (`anchor`) always stays in view."""
    if hi - lo <= 2 * half:
        return c
    left = lo + half                       # crop's left edge on the start of the lines
    if not math.isnan(anchor):
        left = max(left, anchor - 0.85 * half)
    return left


def _reframe_aoi(tl, segments, aspect, fps, t_start, t_end, cfg, zoom_gain, omega, cursor_sigma, lookahead, aoi):
    """AOI-aware crop: the whole subject (a dialog, a typed line, what a click changed)
    stays in the crop. A subject a little wider than the full-height strip widens the
    crop, with the background above and below (letterbox), up to LETTERBOX_MAX and only
    as far as typed text stays legible. A subject wider than that (a whole window, a
    screen of output) does not widen it: the strip shows the part of it where the
    pointer is at work, else where its lines start, else the part nearest to where the
    crop already is. Workspace switches cut."""
    W, H = tl.w, tl.h
    base_h = min(H, W / aspect)
    base_w = base_h * aspect
    max_w = float(W)                       # full width, letterboxed
    segs = sorted((s for s in segments if s.end > s.start), key=lambda s: s.start)
    sim_dt = 1 / 120
    sim_t0 = max(0.0, t_start - 3.0)
    n = int(math.ceil((t_end - sim_t0) / sim_dt)) + 2
    st = sim_t0 + np.arange(n) * sim_dt
    cx_, cy_ = smooth_cursor(tl, st, cursor_sigma)
    ax_, ay_ = smooth_cursor(tl, st + lookahead, cursor_sigma)
    land = camera_path(tl, segs, CameraGeometry(W, H, 0, 0), 1 / sim_dt, sim_t0, sim_t0 + n * sim_dt, cfg,
                       cursor_sigma=cursor_sigma, aoi=aoi)
    lz = np.interp(np.arange(n), np.arange(len(land.z)), land.z)
    # feed-forward: a critically damped spring trails a moving target by 2/omega
    subj = subject_track(tl, aoi, st, ax_, ay_, cfg, lookahead=min(0.45, 2.0 / omega))
    # legibility: a typed line must stay >= min_line_frac x the output's short side;
    # the card is ~0.88 of the output width -> cap the crop width while typing
    line_h = aoi.line_h or 18.0
    max_w_text = min(max_w, line_h * 0.88 / (cfg.min_line_frac * 1.12))
    fill_ = 0.9
    seg_of = np.full(n, -1)
    # abutting segments form one shot for the crop (no width change mid-action)
    sid = -1
    prev_end = -9.0
    for sg in segs:
        if sg.start - prev_end > 0.2:
            sid += 1
        prev_end = sg.end
        seg_of[(st >= sg.start) & (st < sg.end)] = sid
    tw = np.empty(n)
    tx = np.empty(n)
    ty = np.empty(n)
    fc = [float(cx_[0]), float(cy_[0])]
    has_a = ~np.isnan(subj.x0)
    sw = np.where(has_a, subj.x1 - subj.x0, 0.0)
    sh = np.where(has_a, subj.y1 - subj.y0, 0.0)
    # a moving subject (window drag) needs slack for the spring's tracking error
    cxs = np.where(has_a, (subj.x0 + subj.x1) / 2, np.nan)
    cys = np.where(has_a, (subj.y0 + subj.y1) / 2, np.nan)
    k = max(1, int(0.1 / sim_dt))

    def translation_speed(lo, hi):
        """Speed at which the whole rect moves (both edges the same way); a rect that
        only grows (new output lines) is not moving."""
        d0 = (np.roll(lo, -k) - np.roll(lo, k)) / (2 * k * sim_dt)
        d1 = (np.roll(hi, -k) - np.roll(hi, k)) / (2 * k * sim_dt)
        v = np.where(np.sign(d0) == np.sign(d1), np.minimum(np.abs(d0), np.abs(d1)), 0.0)
        grow = np.maximum(np.abs(d0), np.abs(d1))
        return np.nan_to_num(v), np.nan_to_num(grow)
    vxs, gxs = translation_speed(subj.x0, subj.x1)
    vys, gys = translation_speed(subj.y0, subj.y1)
    slack = 0.18
    # only a window being dragged or resized moves as a whole; what a click changed or a
    # pointer box that jumps is a new subject, not a moving one (the "speed" of such a jump
    # used to widen the crop to its limit for every click)
    moving_a = np.array([k == "layout" for k in subj.kind])
    redrawn_a = np.array([k == "scroll" for k in subj.kind])   # a dragged window that repaints as it goes
    ex = np.where(moving_a, np.maximum(vxs, 0.5 * gxs),        # a window being resized needs room to grow into
                  np.where(redrawn_a, vxs, 0.0))
    ey = np.where(moving_a | redrawn_a, vys, 0.0)              # (rows of new output grow downwards: no room needed)
    need_a = np.maximum((sw + 2 * slack * ex) / fill_, (sh + 2 * slack * ey) / fill_ * aspect)
    typing_a = np.array([k == "typing" for k in subj.kind])
    # nothing gets wider than keeps ordinary UI text readable on a phone (a whole 4K screen
    # in a 9:16 card is a thumbnail), nor wider than LETTERBOX_MAX strips (large empty bands)
    max_w_ui = min(max_w, max(base_w, 1.3 * max_w_text), LETTERBOX_MAX * base_w)
    own_a = np.maximum(sw / fill_, sh / fill_ * aspect)       # without the room a moving window gets
    # the pointer's surroundings (the row a click lit up, the trail of its travel) come and
    # go by the half second: they never widen the crop past the strip, which just stays with
    # the pointer. Inside the strip they still count, so a zoomed-in crop does not cut the
    # dialog the pointer is working in.
    pointer_a = np.array([k == "pointer" for k in subj.kind])
    too_big = ~typing_a & ~pointer_a & (own_a > max_w_ui)
    need_a = np.where(pointer_a, np.where(own_a <= base_w, own_a, 0.0), need_a)
    # typing with no caret found (keys that show no glyph: a password, a full-screen program)
    # has the whole window as its rect: that is no line to widen for either
    blind_a = typing_a & np.isnan(subj.ax) & (own_a > max_w_text)
    need_a = np.where(typing_a, np.where(blind_a, 0.0, np.minimum(need_a, max_w_text)),
                      np.where(too_big, 0.0, np.minimum(need_a, max_w_ui)))
    # crop width: what the subject needs now and over the next 1.5 s (look-ahead max), so
    # the crop widens *before* a dialog/window needs it and narrows only after; typed text
    # caps it so the text stays legible
    real = np.array([k not in (None, "cursor") for k in subj.kind]) & has_a
    need_r = np.where(real, need_a, 0.0)
    from numpy.lib.stride_tricks import sliding_window_view
    back, fwd = int(0.6 / sim_dt), int(1.5 / sim_dt)
    padded = np.pad(need_r, (back, fwd), mode="edge")
    need_f = sliding_window_view(padded, back + fwd + 1).max(axis=1)[:n]
    from .aoi import TERMINAL_CLASSES
    terms = {c.lower() for c in (tl.capture.get("terminals") or [])} | TERMINAL_CLASSES

    def in_term(t):
        w_ = tl.focused_window_at(t)
        return w_ is not None and str(w_.get("class", "")).lower() in terms
    typ = typing_a & has_a
    for i in np.nonzero(typ)[0][::12]:
        if not in_term(st[i]):
            typ[i:i + 12] = False
    scale = float(tl.capture.get("scale", 1.0) or 1.0)
    pbox = 150.0 * scale                     # what must stay visible around a click

    def pointer_at_work(t):
        return tl.cursor_inside(t) and t - tl.last_pointer_before(t, 10 * scale) < 1.5
    click_t = np.array(sorted(float(c["t"]) for c in tl.clicks))

    def near_click(t):
        j = int(np.searchsorted(click_t, t - 0.6))
        return j < len(click_t) and click_t[j] <= t + 0.6
    tpad = np.pad(typ.astype(np.float64), (int(0.3 / sim_dt), int(0.8 / sim_dt)), mode="edge")
    typ_f = sliding_window_view(tpad, int(0.3 / sim_dt) + int(0.8 / sim_dt) + 1).max(axis=1)[:n] > 0
    for i in range(n):
        z = 1.0 + (lz[i] - 1.0) * zoom_gain
        w_zoom = base_w / z if seg_of[i] >= 0 else base_w
        has = bool(has_a[i])
        w = max(w_zoom, min(need_f[i], max_w))
        if typ_f[i]:
            w = min(w, max(max_w_text, w_zoom))
        ch = w / aspect
        before = fc[0]
        if has:
            kind, half = subj.kind[i], 0.5 * w * 0.94
            sx0, sx1 = float(subj.x0[i]), float(subj.x1[i])
            if sx1 - sx0 <= 2 * half or kind == "typing":
                # it fits; or typed text, shown from the start of its line with the caret in view
                fc[0] = _dead_zone_rect(fc[0], sx0, sx1, half, subj.ax[i])
                if kind in TEXT_KINDS:
                    fc[0] = text_x(fc[0], sx0, sx1, half, subj.ax[i])
            elif sx0 <= ax_[i] <= sx1 and pointer_at_work(float(st[i])):
                # stay with the pointer: around a click, with what was clicked; otherwise
                # (dragging a window or a split, hovering) just the pointer itself
                pb = pbox if near_click(float(st[i])) else 40.0 * scale
                fc[0] = _dead_zone_rect(fc[0], ax_[i] - pb, ax_[i] + pb, half)
            elif kind in TEXT_KINDS:
                fc[0] = text_x(fc[0], sx0, sx1, half)                                  # where its lines start
            else:
                fc[0] = float(np.clip(fc[0], sx0 + w / 2, sx1 - w / 2))                # the nearest part of it
            if kind in ("pointer", "cursor") and abs(fc[0] - before) < CUT_HOP * w:
                # a pointer flying across the screen is followed at a calm speed: the narrow
                # strip would otherwise whip along with it (a jump is left to the cut below)
                lim = POINTER_PAN * w * sim_dt
                fc[0] = before + float(np.clip(fc[0] - before, -lim, lim))
            fc[1] = _dead_zone_rect(fc[1], subj.y0[i], subj.y1[i], 0.5 * ch * 0.94, subj.ay[i])
            # a moving subject (a window being dragged) is centred, so the slack covers
            # tracking error both ways. Not a travelling pointer (followed at its own calm
            # pace above), nor a caret that jumps from one line to another
            for a_, v_, c_ in ((0, vxs[i], cxs[i]), (1, vys[i], cys[i])):
                if v_ > 150 and kind in ("layout", "scroll", "output"):
                    u = min(1.0, (v_ - 150) / 300)
                    fc[a_] = fc[a_] + (c_ - fc[a_]) * u
        fc[0] = float(np.clip(fc[0], w / 2, W - w / 2)) if w <= W else W / 2
        fc[1] = float(np.clip(fc[1], ch / 2, H - ch / 2)) if ch <= H else H / 2
        tw[i], tx[i], ty[i] = w, fc[0], fc[1]
    # jump cuts: when the subject switches to somewhere a crop-width away (a window opened on
    # the other side, focus moved to another window), cut there instead of a long whip-pan.
    # Shorter hops are panned: the strip is narrow, and a dialog that opens beside the
    # click, or the row a click lit up, is no reason to cut.
    cut_steps = []
    la_steps = int(min(0.45, 2.0 / omega) / sim_dt)
    last_cut = -10 ** 9
    i = 1
    while i < n:
        d = abs(tx[i] - tx[i - 1]) + abs(ty[i] - ty[i - 1]) * 0.5
        if d > CUT_HOP * tw[i] and i - last_cut > int(1.5 / sim_dt):
            c = min(n - 1, i + la_steps)
            tx[i:c], ty[i:c], tw[i:c] = tx[i - 1], ty[i - 1], tw[i - 1]
            cut_steps.append(c)
            last_cut = c
            i = c + 1
            continue
        i += 1
    # a cut lands on the framing that holds a moment later: what it cuts to is often still
    # arriving (a pointer on its way to a click), and cutting to where that was would be
    # followed by a slow drift
    settle = int(0.5 / sim_dt)
    for c in list(cut_steps):
        k = min(n - 1, c + settle)
        if any(c < o <= k for o in cut_steps):
            continue
        if abs(tx[k] - tx[c]) + abs(ty[k] - ty[c]) * 0.5 < CUT_HOP * tw[c]:
            tx[c:k], ty[c:k] = tx[k], ty[k]
    # transitions (workspace switch): cut straight to the framing of what comes next on the
    # new workspace when that is known within ~2.5 s, else to the whole screen
    if cfg.cut_transitions:
        for (a, b) in aoi.transitions:
            m = (st >= a) & (st <= b + 0.1)
            nxt = np.nonzero((st > b + 0.1) & (st < b + 2.5) & real)[0]
            if len(nxt):
                k0 = int(nxt[0])
                m = (st >= a) & (st < st[k0])
                tw[m], tx[m], ty[m] = tw[k0], tx[k0], ty[k0]
            else:
                # nothing happens there yet: the focused window (from its left edge when it
                # is wider than the strip); an empty workspace is shown whole
                fw = tl.focused_window_at(b + 0.3)
                fr = fw.get("rect") if fw else None
                if fr is not None and fr[2] - fr[0] > 8:
                    fx = text_x((fr[0] + fr[2]) / 2, fr[0], fr[2], 0.5 * base_w * 0.94)
                    tw[m], tx[m] = base_w, float(np.clip(fx, base_w / 2, W - base_w / 2))
                    ty[m] = H / 2
                else:
                    tw[m], tx[m], ty[m] = max_w, W / 2, H / 2
            kk = int(np.searchsorted(st, a))
            if 0 < kk < n:
                cut_steps.append(kk)
    cut_steps = sorted(set(cut_steps))

    def piecewise(arr, sigma):
        out = arr.copy()
        bounds = [0] + cut_steps + [n]
        for a, b in zip(bounds, bounds[1:]):
            if b - a > 2:
                out[a:b] = _gauss_smooth(arr[a:b], sigma)
        return out
    ty = piecewise(ty, 0.1 / sim_dt)
    # springs on the crop's left/right *edges* (not centre + width): an edge pinned to the
    # subject or the screen border stays pinned while the crop widens or narrows. Edges are
    # derived before smoothing so a pinned edge stays exactly pinned.
    e0_t = piecewise(tx - tw / 2, 0.18 / sim_dt)
    e1_t = piecewise(tx + tw / 2, 0.18 / sim_dt)
    e0, v0, e1, v1, y, vy = e0_t[0], 0.0, e1_t[0], 0.0, ty[0], 0.0
    oe0, oe1, oy = np.empty(n), np.empty(n), np.empty(n)
    cuts = set(cut_steps)
    for i in range(n):
        if i in cuts:
            e0, v0, e1, v1, y, vy = e0_t[i], 0.0, e1_t[i], 0.0, ty[i], 0.0
        e0, v0 = _spring_step(e0, v0, e0_t[i], omega, sim_dt)
        e1, v1 = _spring_step(e1, v1, e1_t[i], omega, sim_dt)
        y, vy = _spring_step(y, vy, ty[i], omega, sim_dt)
        oe0[i], oe1[i], oy[i] = e0, e1, y
    cw = np.maximum(oe1 - oe0, 16.0)
    ox = (oe0 + oe1) / 2
    ch = cw / aspect
    ox = np.where(cw <= W, np.clip(ox, cw / 2, W - cw / 2), W / 2)
    oy = np.where(ch <= H, np.clip(oy, ch / 2, H - ch / 2), H / 2)
    m = int(round((t_end - t_start) * fps))
    times = t_start + np.arange(m) / fps
    it = lambda a: np.interp(times, st, a)  # noqa: E731
    out = CropPath(fps, t_start, it(ox), it(oy), it(cw), it(ch), it(cx_), it(cy_), [])
    for kk in cut_steps:
        jj = int(np.searchsorted(times, st[kk]))
        if 0 < jj < m:
            out.cuts.append(jj)
            for arr, src in ((out.cx, ox), (out.cy, oy), (out.cw, cw), (out.ch, ch)):
                arr[jj] = src[min(kk + 1, n - 1)]
    return out

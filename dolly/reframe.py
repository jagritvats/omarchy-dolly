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
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from . import num
from .events import Timeline
from .zoom import (CameraGeometry, Segment, ZoomConfig, _aoi_of, _dead_zone_rect, _span, _spring_step,
                   camera_path, smooth_cursor, subject_track)


@dataclass
class CropPath:
    """Per output frame, as plain lists of floats."""
    fps: float
    t0: float
    cx: list
    cy: list
    cw: list
    ch: list
    cur_x: list
    cur_y: list
    cuts: list = None


def _at_steps(track, n: int) -> list:
    """A track of the landscape camera, one value per simulation step. The camera is asked
    for at the simulation rate, so it normally has n values already."""
    return list(track) if len(track) == n else num.interp(range(n), range(len(track)), track)


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
    st = [sim_t0 + i * sim_dt for i in range(n)]
    cx_, cy_ = smooth_cursor(tl, st, cursor_sigma)
    # offline render: aim at where the cursor is about to be, cancelling the spring lag
    ax_, ay_ = smooth_cursor(tl, [t + lookahead for t in st], cursor_sigma)
    # The landscape camera already knows where the action is (cluster focus,
    # pre-aim, bridging, pull-backs); the crop centres on it while zoomed and
    # keeps the cursor inside its safe area at all times.
    land = camera_path(tl, segs, CameraGeometry(W, H, 0, 0), 1 / sim_dt, sim_t0, sim_t0 + n * sim_dt, cfg,
                       cursor_sigma=cursor_sigma)
    lz, lx, ly = _at_steps(land.z, n), _at_steps(land.cx, n), _at_steps(land.cy, n)
    lg = _at_steps(land.glide, n) if land.glide is not None else [0.0] * n
    if any(lg):
        # gliding: aim as far ahead as the spring trails (2/omega), like the landscape camera
        gx_, gy_ = smooth_cursor(tl, [t + min(0.45, 2.0 / omega) for t in st], cursor_sigma)
        ax_ = [a + (g - a) * w for a, g, w in zip(ax_, gx_, lg)]
        ay_ = [a + (g - a) * w for a, g, w in zip(ay_, gy_, lg)]
    tz, tx, ty = [], [], []
    fc = [cx_[0], cy_[0]]
    for i in range(n):
        zoomed = lz[i] > 1.05
        z = 1.0 + (lz[i] - 1.0) * zoom_gain * (1.0 - lg[i])          # widen while gliding: a hop crosses
        cw, ch = base_w / z, base_h / z                               # more of a narrow crop
        if zoomed:
            w = num.clip((lz[i] - 1.05) / 0.3, 0.0, 1.0)  # ease into following the landscape framing
            w *= 1.0 - lg[i]          # gliding between joined shots: the narrow crop follows the pointer
            fc = [fc[0] + (lx[i] - fc[0]) * w, fc[1] + (ly[i] - fc[1]) * w]
        # keep where the pointer is and where it is about to be (a glide between joined
        # shots crosses more of a narrow crop than of the landscape view)
        for a, (lo, hi, half) in enumerate(((min(ax_[i], cx_[i]), max(ax_[i], cx_[i]), safe * cw / 2),
                                            (min(ay_[i], cy_[i]), max(ay_[i], cy_[i]), safe * ch / 2))):
            fc[a] = _dead_zone_rect(fc[a], lo, hi, half, ax_[i] if a == 0 else ay_[i])
        fc[0] = num.clip(fc[0], cw / 2, W - cw / 2)
        fc[1] = num.clip(fc[1], ch / 2, H - ch / 2)
        tz.append(z)
        tx.append(fc[0])
        ty.append(fc[1])
    tx = num.gauss_smooth(tx, 0.1 / sim_dt)
    ty = num.gauss_smooth(ty, 0.1 / sim_dt)
    lz, vz, x, vx, y, vy = 0.0, 0.0, tx[0], 0.0, ty[0], 0.0
    oz, ox, oy = [], [], []
    for i in range(n):
        lz, vz = _spring_step(lz, vz, math.log(tz[i]), omega, sim_dt)
        x, vx = _spring_step(x, vx, tx[i], omega, sim_dt)
        y, vy = _spring_step(y, vy, ty[i], omega, sim_dt)
        oz.append(lz)
        ox.append(x)
        oy.append(y)
    z = [math.exp(max(v, 0.0)) for v in oz]
    cw, ch = [base_w / v for v in z], [base_h / v for v in z]
    ox = [num.clip(x, w / 2, W - w / 2) for x, w in zip(ox, cw)]
    oy = [num.clip(y, h / 2, H - h / 2) for y, h in zip(oy, ch)]
    m = int(round((t_end - t_start) * fps))
    times = [t_start + i / fps for i in range(m)]
    it = lambda a: num.interp(times, st, a)
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
    st = [sim_t0 + i * sim_dt for i in range(n)]
    cx_, cy_ = smooth_cursor(tl, st, cursor_sigma)
    ax_, ay_ = smooth_cursor(tl, [t + lookahead for t in st], cursor_sigma)
    land = camera_path(tl, segs, CameraGeometry(W, H, 0, 0), 1 / sim_dt, sim_t0, sim_t0 + n * sim_dt, cfg,
                       cursor_sigma=cursor_sigma, aoi=aoi)
    lz = _at_steps(land.z, n)
    # feed-forward: a critically damped spring trails a moving target by 2/omega
    subj = subject_track(tl, aoi, st, ax_, ay_, cfg, lookahead=min(0.45, 2.0 / omega))
    # legibility: a typed line must stay >= min_line_frac x the output's short side;
    # the card is ~0.88 of the output width -> cap the crop width while typing
    line_h = aoi.line_h or 18.0
    max_w_text = min(max_w, line_h * 0.88 / (cfg.min_line_frac * 1.12))
    fill_ = 0.9
    seg_of = [-1] * n
    # abutting segments form one shot for the crop (no width change mid-action)
    sid = -1
    prev_end = -9.0
    for sg in segs:
        if sg.start - prev_end > 0.2:
            sid += 1
        prev_end = sg.end
        for i in _span(st, sg.start, sg.end):
            seg_of[i] = sid
    has_a = [v == v for v in subj.x0]                         # a subject at this step (NaN: none)
    step = max(1, int(0.1 / sim_dt))

    def translation_speed(lo, hi):
        """Speed at which the whole rect moves (both edges the same way); a rect that
        only grows (new output lines) is not moving. Next to a step without a subject
        (NaN) it neither moves nor grows."""
        over = 2 * step * sim_dt
        d0 = [(a - b) / over for a, b in zip(num.roll(lo, -step), num.roll(lo, step))]
        d1 = [(a - b) / over for a, b in zip(num.roll(hi, -step), num.roll(hi, step))]
        v = [min(abs(a), abs(b)) if num.sign(a) == num.sign(b) else 0.0 for a, b in zip(d0, d1)]
        grow = [max(abs(a), abs(b)) if a == a and b == b else 0.0 for a, b in zip(d0, d1)]
        return v, grow
    # a moving subject (window drag) needs slack for the spring's tracking error
    vxs, gxs = translation_speed(subj.x0, subj.x1)
    vys, _ = translation_speed(subj.y0, subj.y1)
    slack = 0.18
    # nothing gets wider than keeps ordinary UI text readable on a phone (a whole 4K screen
    # in a 9:16 card is a thumbnail), nor wider than LETTERBOX_MAX strips (large empty bands)
    max_w_ui = min(max_w, max(base_w, 1.3 * max_w_text), LETTERBOX_MAX * base_w)
    need_r = []                                # the crop width each step's subject asks for
    for i, kind in enumerate(subj.kind):
        if kind in (None, "cursor") or not has_a[i]:
            need_r.append(0.0)
            continue
        sw, sh = subj.x1[i] - subj.x0[i], subj.y1[i] - subj.y0[i]
        own = max(sw / fill_, sh / fill_ * aspect)            # without the room a moving window gets
        if kind == "pointer":
            # the pointer's surroundings (the row a click lit up, the trail of its travel) come
            # and go by the half second: they never widen the crop past the strip, which just
            # stays with the pointer. Inside the strip they still count, so a zoomed-in crop
            # does not cut the dialog the pointer is working in.
            need = own if own <= base_w else 0.0
        else:
            # only a window being dragged or resized moves as a whole; what a click changed or
            # a pointer box that jumps is a new subject, not a moving one (the "speed" of such
            # a jump used to widen the crop to its limit for every click)
            if kind == "layout":
                ex, ey = max(vxs[i], 0.5 * gxs[i]), vys[i]    # a window being resized needs room to grow into
            elif kind == "scroll":                            # a dragged window that repaints as it goes
                ex, ey = vxs[i], vys[i]                       # (rows of new output grow downwards: no room needed)
            else:
                ex = ey = 0.0
            need = max((sw + 2 * slack * ex) / fill_, (sh + 2 * slack * ey) / fill_ * aspect)
        if kind == "typing":
            # typing with no caret found (keys that show no glyph: a password, a full-screen
            # program) has the whole window as its rect: that is no line to widen for either
            blind = subj.ax[i] != subj.ax[i] and own > max_w_text
            need = 0.0 if blind else min(need, max_w_text)
        elif kind != "pointer" and own > max_w_ui:
            need = 0.0
        else:
            need = min(need, max_w_ui)
        need_r.append(need)
    # crop width: what the subject needs now and over the next 1.5 s (look-ahead max), so
    # the crop widens *before* a dialog/window needs it and narrows only after; typed text
    # caps it so the text stays legible
    real = [kind not in (None, "cursor") and has for kind, has in zip(subj.kind, has_a)]
    need_f = num.running_max(need_r, int(0.6 / sim_dt), int(1.5 / sim_dt))
    from .aoi import TERMINAL_CLASSES
    terms = {c.lower() for c in (tl.capture.get("terminals") or [])} | TERMINAL_CLASSES

    def in_term(t):
        w_ = tl.focused_window_at(t)
        return w_ is not None and str(w_.get("class", "")).lower() in terms
    typ = [kind == "typing" and has for kind, has in zip(subj.kind, has_a)]
    for i in [i for i, on in enumerate(typ) if on][::12]:
        if not in_term(st[i]):
            typ[i:i + 12] = [False] * len(typ[i:i + 12])
    scale = float(tl.capture.get("scale", 1.0) or 1.0)
    pbox = 150.0 * scale                     # what must stay visible around a click

    def pointer_at_work(t):
        return tl.cursor_inside(t) and t - tl.last_pointer_before(t, 10 * scale) < 1.5
    click_t = sorted(float(c["t"]) for c in tl.clicks)

    def near_click(t):
        j = bisect_left(click_t, t - 0.6)
        return j < len(click_t) and click_t[j] <= t + 0.6
    typ_f = num.running_max(typ, int(0.3 / sim_dt), int(0.8 / sim_dt))
    tw, tx, ty = [], [], []
    fc = [cx_[0], cy_[0]]
    for i in range(n):
        z = 1.0 + (lz[i] - 1.0) * zoom_gain
        w_zoom = base_w / z if seg_of[i] >= 0 else base_w
        w = max(w_zoom, min(need_f[i], max_w))
        if typ_f[i]:
            w = min(w, max(max_w_text, w_zoom))
        ch = w / aspect
        before = fc[0]
        if has_a[i]:
            kind, half = subj.kind[i], 0.5 * w * 0.94
            sx0, sx1 = subj.x0[i], subj.x1[i]
            if sx1 - sx0 <= 2 * half or kind == "typing":
                # it fits; or typed text, shown from the start of its line with the caret in view
                fc[0] = _dead_zone_rect(fc[0], sx0, sx1, half, subj.ax[i])
                if kind in TEXT_KINDS:
                    fc[0] = text_x(fc[0], sx0, sx1, half, subj.ax[i])
            elif sx0 <= ax_[i] <= sx1 and pointer_at_work(st[i]):
                # stay with the pointer: around a click, with what was clicked; otherwise
                # (dragging a window or a split, hovering) just the pointer itself
                pb = pbox if near_click(st[i]) else 40.0 * scale
                fc[0] = _dead_zone_rect(fc[0], ax_[i] - pb, ax_[i] + pb, half)
            elif kind in TEXT_KINDS:
                fc[0] = text_x(fc[0], sx0, sx1, half)                                  # where its lines start
            else:
                fc[0] = num.clip(fc[0], sx0 + w / 2, sx1 - w / 2)                      # the nearest part of it
            if kind in ("pointer", "cursor") and abs(fc[0] - before) < CUT_HOP * w:
                # a pointer flying across the screen is followed at a calm speed: the narrow
                # strip would otherwise whip along with it (a jump is left to the cut below)
                lim = POINTER_PAN * w * sim_dt
                fc[0] = before + num.clip(fc[0] - before, -lim, lim)
            fc[1] = _dead_zone_rect(fc[1], subj.y0[i], subj.y1[i], 0.5 * ch * 0.94, subj.ay[i])
            # a moving subject (a window being dragged) is centred, so the slack covers
            # tracking error both ways. Not a travelling pointer (followed at its own calm
            # pace above), nor a caret that jumps from one line to another
            if kind in ("layout", "scroll", "output"):
                for a_, v_, lo, hi in ((0, vxs[i], sx0, sx1), (1, vys[i], subj.y0[i], subj.y1[i])):
                    if v_ > 150:
                        u = min(1.0, (v_ - 150) / 300)
                        fc[a_] = fc[a_] + ((lo + hi) / 2 - fc[a_]) * u
        fc[0] = num.clip(fc[0], w / 2, W - w / 2) if w <= W else W / 2
        fc[1] = num.clip(fc[1], ch / 2, H - ch / 2) if ch <= H else H / 2
        tw.append(w)
        tx.append(fc[0])
        ty.append(fc[1])
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
            tx[i:c], ty[i:c], tw[i:c] = [tx[i - 1]] * (c - i), [ty[i - 1]] * (c - i), [tw[i - 1]] * (c - i)
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
            tx[c:k], ty[c:k] = [tx[k]] * (k - c), [ty[k]] * (k - c)
    # transitions (workspace switch): cut straight to the framing of what comes next on the
    # new workspace when that is known within ~2.5 s, else to the whole screen
    if cfg.cut_transitions:
        for (a, b) in aoi.transitions:
            kk = bisect_left(st, a)
            stop = bisect_right(st, b + 0.1)                           # the steps from a to b + 0.1
            k0 = next((i for i in range(stop, bisect_left(st, b + 2.5)) if real[i]), None)
            if k0 is not None:
                stop, held = k0, (tw[k0], tx[k0], ty[k0])              # ... up to what comes next, and its framing
            else:
                # nothing happens there yet: the focused window (from its left edge when it
                # is wider than the strip); an empty workspace is shown whole
                fw = tl.focused_window_at(b + 0.3)
                fr = fw.get("rect") if fw else None
                if fr is not None and fr[2] - fr[0] > 8:
                    fx = text_x((fr[0] + fr[2]) / 2, fr[0], fr[2], 0.5 * base_w * 0.94)
                    held = (base_w, num.clip(fx, base_w / 2, W - base_w / 2), H / 2)
                else:
                    held = (max_w, W / 2, H / 2)
            for i in range(kk, stop):
                tw[i], tx[i], ty[i] = held
            if 0 < kk < n:
                cut_steps.append(kk)
    cut_steps = sorted(set(cut_steps))

    def piecewise(arr, sigma):
        out = list(arr)
        bounds = [0] + cut_steps + [n]
        for a, b in zip(bounds, bounds[1:]):
            if b - a > 2:
                out[a:b] = num.gauss_smooth(arr[a:b], sigma)
        return out
    ty = piecewise(ty, 0.1 / sim_dt)
    # springs on the crop's left/right *edges* (not centre + width): an edge pinned to the
    # subject or the screen border stays pinned while the crop widens or narrows. Edges are
    # derived before smoothing so a pinned edge stays exactly pinned.
    e0_t = piecewise([x - w / 2 for x, w in zip(tx, tw)], 0.18 / sim_dt)
    e1_t = piecewise([x + w / 2 for x, w in zip(tx, tw)], 0.18 / sim_dt)
    e0, v0, e1, v1, y, vy = e0_t[0], 0.0, e1_t[0], 0.0, ty[0], 0.0
    oe0, oe1, oy = [], [], []
    cuts = set(cut_steps)
    for i in range(n):
        if i in cuts:
            e0, v0, e1, v1, y, vy = e0_t[i], 0.0, e1_t[i], 0.0, ty[i], 0.0
        e0, v0 = _spring_step(e0, v0, e0_t[i], omega, sim_dt)
        e1, v1 = _spring_step(e1, v1, e1_t[i], omega, sim_dt)
        y, vy = _spring_step(y, vy, ty[i], omega, sim_dt)
        oe0.append(e0)
        oe1.append(e1)
        oy.append(y)
    cw = [max(b - a, 16.0) for a, b in zip(oe0, oe1)]
    ch = [w / aspect for w in cw]
    ox = [num.clip((a + b) / 2, w / 2, W - w / 2) if w <= W else W / 2 for a, b, w in zip(oe0, oe1, cw)]
    oy = [num.clip(y, h / 2, H - h / 2) if h <= H else H / 2 for y, h in zip(oy, ch)]
    m = int(round((t_end - t_start) * fps))
    times = [t_start + i / fps for i in range(m)]
    it = lambda a: num.interp(times, st, a)  # noqa: E731
    out = CropPath(fps, t_start, it(ox), it(oy), it(cw), it(ch), it(cx_), it(cy_), [])
    for kk in cut_steps:
        jj = bisect_left(times, st[kk])
        if 0 < jj < m:
            out.cuts.append(jj)
            k1 = min(kk + 1, n - 1)
            out.cx[jj], out.cy[jj], out.cw[jj], out.ch[jj] = ox[k1], oy[k1], cw[k1], ch[k1]
    return out

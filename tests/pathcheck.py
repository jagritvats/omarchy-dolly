#!/usr/bin/env python3
"""Plot + metrics for a project's camera path (z, cx, cy, cursor).

    python3 tests/pathcheck.py PROJECT_DIR OUT.png [--fps 60]
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import gfx  # noqa: E402
from dolly import project as projmod  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.render import make_layout, resolve_segments  # noqa: E402
from dolly.zoom import ZoomConfig, camera_path  # noqa: E402


def steps(a):
    """The change from each value to the next."""
    return [y - x for x, y in zip(a, a[1:])]


def metrics(path, geom):
    z, cx, cy = path.z, path.cx, path.cy
    fps = path.fps
    # what the viewer perceives is output px: k of them per video px at zoom z
    OW = 1920
    k = [OW * v / geom.CW for v in z]
    # screen-space motion of a canvas point at the viewport centre = pan speed
    pan_px = [math.hypot(dx, dy) * kk for dx, dy, kk in zip(steps(cx), steps(cy), k[1:])]    # output px / frame
    logz = [math.log(v) for v in z]
    dz = [abs(d) * fps for d in steps(logz)]                       # log-zoom / s
    acc = [abs(d) * fps for d in steps(pan_px)]                    # px/frame/s
    jerk_z = [abs(d) * fps ** 3 for d in steps(steps(steps(logz)))]
    # direction reversals of pan (jitter)
    vx = steps(cx)
    rev = sum((a > 0) != (b > 0) and abs(a) > 0.05 and abs(b) > 0.05 for a, b in zip(vx, vx[1:]))
    return {"max_pan_px_per_frame": max(pan_px, default=0.0), "max_dlogz_per_s": max(dz, default=0.0),
            "max_pan_accel": max(acc, default=0.0), "max_jerk_logz": max(jerk_z, default=0.0),
            "pan_reversals": rev, "zoom_range": [min(z), max(z)]}


def plot(out, t, panels, spans, marks, legend):
    """Line charts one under the other over the same time axis.
    panels: [(label, [(values, colour, width, alpha), ...])]; spans: [(t0, t1)] shaded in
    the first panel (the zoom segments); marks: times drawn as thin lines (the clicks);
    legend: [(text, colour)]."""
    W, H, left, right, top, bottom, gap = 1120, 640, 64, 16, 12, 30, 12
    img = gfx.new(W, H)
    gfx.fill_rect(img, 0, 0, W, H, (1, 1, 1))
    c = gfx.context(img)
    font, ink = gfx.font(None, 12), gfx.rgb("#444444")
    t0, t1 = t[0], max(t[-1], t[0] + 1e-9)
    ph = (H - top - bottom - gap * (len(panels) - 1)) / len(panels)

    def X(v):
        return left + (v - t0) / (t1 - t0) * (W - left - right)
    for i, (label, lines) in enumerate(panels):
        y0 = top + i * (ph + gap)
        lo, hi = min(min(v) for v, *_ in lines), max(max(v) for v, *_ in lines)
        lo, hi = lo - 0.05 * (hi - lo) - 1e-9, hi + 0.05 * (hi - lo) + 1e-9
        for a, b in spans if i == 0 else ():
            gfx.fill_rect(c, X(a), y0, X(b) - X(a), ph, gfx.rgb("#bb9af7"), 0.15)
        for m in marks:
            gfx.fill_rect(c, X(m), y0, 0.6, ph, gfx.rgb("#e0af68"), 0.7)
        for values, colour, width, alpha in lines:
            for j, (tt, v) in enumerate(zip(t, values)):
                (c.line_to if j else c.move_to)(X(tt), y0 + ph - (v - lo) / (hi - lo) * ph)
            c.set_source_rgba(*gfx.rgb(colour), alpha)
            c.set_line_width(width)
            c.stroke()
        c.rectangle(left + 0.5, y0 + 0.5, W - left - right - 1, ph - 1)
        c.set_source_rgba(*ink, 0.5)
        c.set_line_width(1)
        c.stroke()
        gfx.draw_text(c, f"{hi:.4g}", left - 5, y0, font, ink, anchor="ra")
        gfx.draw_text(c, label, left - 5, y0 + ph / 2, font, ink, anchor="rm")
        gfx.draw_text(c, f"{lo:.4g}", left - 5, y0 + ph, font, ink, anchor="rd")
    for j in range(11):
        v = t0 + (t1 - t0) * j / 10
        gfx.draw_text(c, f"{v:.1f} s", X(v), H - bottom + 6, font, ink, anchor="ma")
    for j, (text, colour) in enumerate(legend):
        gfx.draw_text(c, text, W - right - 8, top + 6 + 16 * j, font, gfx.rgb(colour), anchor="ra")
    gfx.save(img, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("out")
    ap.add_argument("--fps", type=float, default=60)
    a = ap.parse_args()
    proj = projmod.Project(a.dir)
    data = proj.load()
    tl = Timeline.load(proj.events)
    zcfg = ZoomConfig.from_dict(data.get("zoom_config"))
    segs = resolve_segments(tl, data, zcfg)
    lay = make_layout(tl.w, tl.h, data["style"], tl.capture)
    p = camera_path(tl, segs, lay.geom, a.fps, 0, tl.duration, zcfg)
    m = metrics(p, lay.geom)
    print(json.dumps(m, indent=2))
    t = [i / a.fps for i in range(len(p.z))]
    cam, cursor = "#7aa2f7", "#f7768e"
    plot(a.out, t, [("zoom", [(p.z, cam, 1.5, 1.0)]),
                    ("x", [(p.cur_x, cursor, 0.8, 0.6), (p.cx, cam, 1.5, 1.0)]),         # camera over the cursor
                    ("y", [(p.cur_y, cursor, 0.8, 0.6), (p.cy, cam, 1.5, 1.0)])],
         [(s.start, s.end) for s in segs], [c["t"] for c in tl.clicks], [("camera", cam), ("cursor", cursor)])


if __name__ == "__main__":
    main()

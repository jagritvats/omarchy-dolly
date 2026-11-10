#!/usr/bin/env python3
"""Plot + metrics for a project's camera path (z, cx, cy, cursor).

    python3 tests/pathcheck.py PROJECT_DIR OUT.png [--fps 60]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import project as projmod  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.render import make_layout, resolve_segments  # noqa: E402
from dolly.zoom import ZoomConfig, camera_path  # noqa: E402


def metrics(path, geom):
    z, cx, cy = path.z, path.cx, path.cy
    fps = path.fps
    # viewport left/top edges in output px (what the viewer perceives)
    OW = 1920
    k = OW * z / geom.CW
    vx0 = (cx - geom.CW / (2 * z))
    vy0 = (cy - geom.CH / (2 * z))
    # screen-space motion of a canvas point at the viewport centre = pan speed
    pan_px = np.hypot(np.diff(cx), np.diff(cy)) * k[1:]           # output px / frame
    dz = np.abs(np.diff(np.log(z))) * fps                          # log-zoom / s
    acc = np.abs(np.diff(pan_px)) * fps                            # px/frame/s
    jerk_z = np.abs(np.diff(np.log(z), 3)) * fps ** 3
    # direction reversals of pan (jitter)
    vx = np.diff(cx)
    rev = int(np.sum((np.sign(vx[1:]) != np.sign(vx[:-1])) & (np.abs(vx[1:]) > 0.05) & (np.abs(vx[:-1]) > 0.05)))
    return {"max_pan_px_per_frame": float(pan_px.max()), "max_dlogz_per_s": float(dz.max()),
            "max_pan_accel": float(acc.max()), "max_jerk_logz": float(jerk_z.max()),
            "pan_reversals": rev, "zoom_range": [float(z.min()), float(z.max())]}


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
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t = np.arange(len(p.z)) / a.fps
    fig, ax = plt.subplots(3, 1, figsize=(14, 8), sharex=True)
    ax[0].plot(t, p.z, color="#7aa2f7")
    for s in segs:
        ax[0].axvspan(s.start, s.end, color="#bb9af7", alpha=0.15)
    ax[0].set_ylabel("zoom")
    ax[1].plot(t, p.cx, label="cam x", color="#7aa2f7")
    ax[1].plot(t, p.cur_x, label="cursor x", color="#f7768e", alpha=0.6, lw=0.8)
    ax[1].legend(loc="upper right")
    ax[2].plot(t, p.cy, label="cam y", color="#7aa2f7")
    ax[2].plot(t, p.cur_y, label="cursor y", color="#f7768e", alpha=0.6, lw=0.8)
    for c in tl.clicks:
        for a_ in ax:
            a_.axvline(c["t"], color="#e0af68", lw=0.6, alpha=0.7)
    ax[2].set_xlabel("s")
    fig.tight_layout()
    fig.savefig(a.out, dpi=80)


if __name__ == "__main__":
    main()

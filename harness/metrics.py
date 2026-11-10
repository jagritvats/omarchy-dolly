"""Automatic quality metrics for a rendered camera against scenario ground truth.

All numbers are computed from the exact camera/crop path the exporter uses
(Compositor), at the output frame rate, against truth.json written by the
scenario generator. Velocities are in *viewport widths per second* (how fast
content slides across the frame), zoom speed in ln-units per second.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dolly import project as projmod
from dolly.events import Timeline
from dolly.render import Compositor, load_aoi
from dolly.zoom import Segment, ZoomConfig, build_segments

ANTICIPATE = 0.8      # s: a camera may move to the next subject this early (zoom lead-in)
ASPECT_OUT = {"16:9": (1280, 720), "9:16": (608, 1080), "1:1": (720, 720)}

# caps every scenario must meet (after)
CAPS = {
    "aoi_in_frame_pct": 95.0,      # AOI fully visible in >= this % of frames that have an AOI
    "pan_speed": 1.6,              # viewport widths / s
    "zoom_speed": 1.6,             # ln(z) / s
    "pan_accel": 10.0,             # viewport widths / s^2
    "pan_jerk": 150.0,             # viewport widths / s^3 (p99)
    "reversals": 0,                # inside a zoom segment
    "min_line_frac": 0.0140,       # typed line height / output short side (~15 px at 1080p)
    "sync_error_frames": 1.0,
    "cursor_over_text": 0,
    "cursor_outside_drawn": 0,
}


@dataclass
class Engine:
    name: str              # "before" | "after"
    use_aoi: bool


def _visible_rect(comp: Compositor, ts):
    """Video-px rect visible in the output (the screen card in reframe mode)."""
    z, k, vx0, vy0 = comp.affine_at(ts)
    if comp.reframe:
        x0, y0, x1, y1 = comp.card
    else:
        x0, y0, x1, y1 = 0, 0, comp.lay.OW, comp.lay.OH
    return (vx0 + x0 / k, vy0 + y0 / k, vx0 + x1 / k, vy0 + y1 / k), k


def _contains(outer, inner, tol):
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol and
            inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def _reversals(v, amp, seg_mask, dead=0.03, min_amp=0.03, where=None):
    """Direction flips (per axis) inside segments while the camera is moving: both legs must
    travel >= min_amp viewport widths and the camera must not have come to rest in between
    (|v| < dead for ~0.15 s). Moving to a new subject after settling is a new beat."""
    n = 0
    for axis in range(v.shape[1]):
        sign, travel, still = 0, 0.0, 0
        for i in range(len(v)):
            if not seg_mask[i]:
                sign, travel = 0, 0.0
                continue
            x = v[i, axis]
            sg = 1 if x > dead else (-1 if x < -dead else 0)
            if sg == 0:
                still += 1
                if still > 4:           # came to rest (~0.15 s): a new move is a new beat, not a whip back
                    sign, travel = 0, 0.0
                continue
            still = 0
            if sign == 0:
                sign, travel = sg, abs(amp[i, axis])
            elif sg == sign:
                travel += abs(amp[i, axis])
            else:
                if travel > min_amp:
                    # count only if the new direction also travels far enough
                    j, t2 = i, 0.0
                    while j < len(v) and seg_mask[j] and (1 if v[j, axis] > dead else (-1 if v[j, axis] < -dead else 0)) in (sg, 0):
                        t2 += abs(amp[j, axis])
                        j += 1
                    if t2 > min_amp:
                        n += 1
                        if where is not None:
                            where.append((i, axis))
                sign, travel = sg, abs(amp[i, axis])
    return n


def make_compositor(pdir: Path, aspect: str, engine: Engine, fps: float = 30.0, size=None):
    """The exact compositor an export would use, for the 'before' (cursor-only, no sync
    correction, no cursor hiding) or 'after' engine."""
    pdir = Path(pdir)
    proj = projmod.Project(pdir)
    data = proj.load()
    tl = Timeline.load(proj.events)
    zcfg = ZoomConfig.from_dict(data.get("zoom_config"))
    zcfg.use_aoi = engine.use_aoi
    w, h = size or ASPECT_OUT[aspect]
    style = projmod.deep_merge(data["style"], {"output": {"fps": fps, "width": w, "height": h}})
    if not engine.use_aoi:
        tl = _unsynced_timeline(pdir, tl)     # "before" had no sync markers
    aoi = load_aoi(pdir, tl, zcfg)
    segs = build_segments(tl, zcfg, aoi) if not engine.use_aoi else \
        [Segment.from_dict(s) for s in data["segments"]]
    trim = data.get("trim") or [0.0, tl.duration]
    t0, t1 = float(trim[0]), float(trim[1])
    comp = Compositor(tl, style, segs, zcfg, t0, t1, aspect=aspect, aoi=aoi)
    return comp, tl, segs, (t0, t1)


def evaluate(pdir: Path, aspect: str = "16:9", engine: Engine = Engine("after", True), fps: float = 30.0) -> dict:
    pdir = Path(pdir)
    truth = json.loads((pdir / "truth.json").read_text())
    comp, tl, segs, (t0, t1) = make_compositor(pdir, aspect, engine, fps)
    n = int(round((t1 - t0) * fps))
    frames = truth["frames"]
    tfps = truth["fps"]
    W, H = tl.w, tl.h
    short = min(comp.lay.OW, comp.lay.OH)
    vis, ks, cx, cy, vw = [], [], [], [], []
    aoi_total = aoi_in = 0
    text_frames, line_fracs = 0, []
    cursor_over_text = 0
    cursor_out_drawn = 0
    cursor_unit = comp.lay.cursor_base * comp.cursor_size
    misses = []
    for i in range(n):
        ts = t0 + i / fps
        (r, k) = _visible_rect(comp, ts)
        vis.append(r)
        ks.append(k)
        cx.append((r[0] + r[2]) / 2)
        cy.append((r[1] + r[3]) / 2)
        vw.append(r[2] - r[0])
        fr = frames[min(len(frames) - 1, int(round(ts * tfps)))]
        if fr["aoi"] is not None and not fr["transition"]:
            aoi_total += 1
            screen_vis = (max(r[0], 0), max(r[1], 0), min(r[2], W), min(r[3], H))
            ok_ = _contains(screen_vis, fr["aoi"], tol=0.004 * W)
            if not ok_:
                # anticipation is allowed: framing the *next* subject up to ANTICIPATE s early
                fi = int(round(ts * tfps))
                for k in range(1, int(ANTICIPATE * tfps) + 1):
                    nf = frames[min(len(frames) - 1, fi + k)]
                    if nf["kind"] != fr["kind"] and nf["aoi"] is not None:
                        ok_ = _contains(screen_vis, nf["aoi"], tol=0.004 * W)
                        break
            if ok_:
                aoi_in += 1
            else:
                misses.append(round(ts, 2))
        if fr.get("typing") and fr.get("line_h"):
            text_frames += 1
            line_fracs.append(fr["line_h"] * k / short)
            a = comp.cursor_alpha(ts) if comp.draw_cursor else 0.0
            if a > 0.05 and fr.get("text_rect"):
                x, y = tl.cursor_at(ts)
                cr = (x - 0.1 * cursor_unit, y - 0.1 * cursor_unit, x + 0.75 * cursor_unit, y + cursor_unit)
                tr = fr["text_rect"]
                if cr[0] < tr[2] and cr[2] > tr[0] and cr[1] < tr[3] and cr[3] > tr[1]:
                    cursor_over_text += 1
        if not fr.get("cursor_in", True) and comp.draw_cursor and comp.cursor_alpha(ts) > 0.05:
            cursor_out_drawn += 1
    cx, cy, vw = np.array(cx), np.array(cy), np.array(vw)
    cuts = set(getattr(comp.crop if comp.reframe else comp.path, "cuts", None) or [])
    ok = np.ones(n, bool)
    for c in cuts:
        ok[max(0, c - 3):min(n, c + 3)] = False
    dt = 1 / fps
    vx = np.gradient(cx) / dt / vw
    vy = np.gradient(cy) / dt / vw
    vz = np.abs(np.gradient(np.log(vw)) / dt)
    pan = np.hypot(vx, vy)
    ax = np.gradient(vx) / dt
    ay = np.gradient(vy) / dt
    acc = np.hypot(ax, ay)
    jerk = np.hypot(np.gradient(ax) / dt, np.gradient(ay) / dt)
    seg_mask = np.zeros(n, bool)
    for sgm in segs:
        a = int((sgm.start + 0.4 - t0) * fps)
        b = int((sgm.end - 0.4 - t0) * fps)
        if b > a:
            seg_mask[max(0, a):max(0, min(n, b))] = True
    seg_mask &= ok
    seg_mask &= vz < 0.12          # pans only: a crop widening from a pinned edge is a zoom, not a reversal
    amp = np.stack([vx * dt, vy * dt], 1)
    where = []
    rev = _reversals(np.stack([vx, vy], 1), amp, seg_mask, where=where)
    sel = ok if ok.any() else np.ones(n, bool)
    res = {
        "scenario": truth["name"], "aspect": aspect, "engine": engine.name, "frames": n,
        "aoi_frames": aoi_total,
        "aoi_in_frame_pct": round(100.0 * aoi_in / aoi_total, 1) if aoi_total else 100.0,
        "pan_speed": round(float(pan[sel].max()), 3) if n else 0.0,
        "zoom_speed": round(float(vz[sel].max()), 3) if n else 0.0,
        "pan_accel": round(float(np.percentile(acc[sel], 99.5)), 2) if n else 0.0,
        "pan_jerk": round(float(np.percentile(jerk[sel], 99)), 1) if n else 0.0,
        "reversals": int(rev),
        "cuts": len(cuts),
        "typing_frames": text_frames,
        "min_line_frac": round(float(min(line_fracs)), 4) if line_fracs else None,
        "min_line_px_1080": round(float(min(line_fracs)) * 1080, 1) if line_fracs else None,
        "cursor_over_text": cursor_over_text,
        "cursor_outside_drawn": cursor_out_drawn,
        "aoi_misses": misses[:12],
        "reversal_at": [(round(t0 + i / fps, 2), "xy"[a]) for i, a in where][:10],
    }
    res.update(sync_error(pdir, truth, engine))
    res["pass"], res["failed"] = check(res)
    return res


def _unsynced_timeline(pdir: Path, tl: Timeline) -> Timeline:
    """What the pre-round-3 finalize produced: logger clock taken as video time."""
    doc = json.loads((pdir / "events.json").read_text())
    s = doc["capture"].get("sync") or {}
    a, b = float(s.get("a", 0.0)), float(s.get("b", 1.0))
    if a == 0 and b == 1:
        return tl
    # invert t = a + b * t_log  ->  t_log = (t - a) / b
    for e in doc["events"]:
        e["t"] = (e["t"] - a) / b
    doc["capture"]["sync"] = {}
    return Timeline.from_dict(doc)


def sync_error(pdir: Path, truth: dict, engine: Engine) -> dict:
    doc = json.loads((pdir / "events.json").read_text())
    s = doc["capture"].get("sync") or {}
    a, b = float(s.get("a", 0.0)), float(s.get("b", 1.0))
    clicks = sorted([e for e in doc["events"] if e["type"] == "click"
                     and 0 <= e["x"] <= truth["W"] and 0 <= e["y"] <= truth["H"]], key=lambda e: e["t"])
    true = sorted(truth["clicks"], key=lambda c: c["t"])
    if not clicks or len(clicks) != len(true):
        return {"sync_error_frames": None, "sync_method": s.get("method")}
    errs = []
    for e, c in zip(clicks, true):
        t = e["t"] if engine.use_aoi else (e["t"] - a) / b
        errs.append(abs(t - c["t"]))
    return {"sync_error_frames": round(max(errs) * truth["fps"], 2), "sync_method": s.get("method"),
            "drift_pct": s.get("drift_pct")}


def check(res: dict):
    failed = []
    if res["aoi_frames"] and res["aoi_in_frame_pct"] < CAPS["aoi_in_frame_pct"]:
        failed.append("aoi_in_frame")
    for k in ("pan_speed", "zoom_speed", "pan_accel", "pan_jerk"):
        if res[k] > CAPS[k]:
            failed.append(k)
    if res["reversals"] > CAPS["reversals"]:
        failed.append("reversals")
    if res["min_line_frac"] is not None and res["min_line_frac"] < CAPS["min_line_frac"]:
        failed.append("legibility")
    if res["sync_error_frames"] is not None and res["sync_error_frames"] >= CAPS["sync_error_frames"]:
        failed.append("sync")
    if res["cursor_over_text"] > CAPS["cursor_over_text"]:
        failed.append("cursor_over_text")
    if res["cursor_outside_drawn"] > CAPS["cursor_outside_drawn"]:
        failed.append("cursor_outside")
    return not failed, failed


def debug_frames(pdir, aspect, engine, times, out_png, fps=30.0):
    """Composite frames at `times` with the truth AOI outlined (red) - diagnostics only."""
    import cv2
    pdir = Path(pdir)
    truth = json.loads((pdir / "truth.json").read_text())
    proj = projmod.Project(pdir)
    data = proj.load()
    tl = Timeline.load(proj.events)
    zcfg = ZoomConfig.from_dict(data.get("zoom_config"))
    zcfg.use_aoi = engine.use_aoi
    style = projmod.deep_merge(data["style"], {"output": {"fps": fps, "width": ASPECT_OUT[aspect][0],
                                                           "height": ASPECT_OUT[aspect][1]}})
    if not engine.use_aoi:
        tl = _unsynced_timeline(pdir, tl)
    aoi = load_aoi(pdir, tl, zcfg)
    segs = build_segments(tl, zcfg, aoi) if not engine.use_aoi else [Segment.from_dict(s) for s in data["segments"]]
    trim = data.get("trim") or [0.0, tl.duration]
    comp = Compositor(tl, style, segs, zcfg, float(trim[0]), float(trim[1]), aspect=aspect, aoi=aoi)
    import subprocess
    tiles = []
    for t in times:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(proj.raw), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True).stdout
        frame = np.frombuffer(raw, np.uint8).reshape(tl.h, tl.w, 3)
        img = comp.compose_at(t, frame)
        fr = truth["frames"][min(len(truth["frames"]) - 1, int(round(t * truth["fps"])))]
        z, k, vx0, vy0 = comp.affine_at(t)
        if fr["aoi"]:
            a = fr["aoi"]
            cv2.rectangle(img, (int((a[0] - vx0) * k), int((a[1] - vy0) * k)), (int((a[2] - vx0) * k), int((a[3] - vy0) * k)),
                          (60, 60, 255), 2)
        cv2.putText(img, f"{t:.2f}s {fr.get('kind') or ''}", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        tiles.append(img)
    h = max(t.shape[0] for t in tiles)
    row = np.concatenate([np.pad(t, ((0, h - t.shape[0]), (0, 4), (0, 0))) for t in tiles], 1)
    cv2.imwrite(str(out_png), row)


def trace(pdir, aspect, engine=Engine("after", True), fps=30.0):
    """(times, cx, cy, vw) of the visible rect, for debugging."""
    pdir = Path(pdir)
    proj = projmod.Project(pdir)
    data = proj.load()
    tl = Timeline.load(proj.events)
    zcfg = ZoomConfig.from_dict(data.get("zoom_config"))
    zcfg.use_aoi = engine.use_aoi
    style = projmod.deep_merge(data["style"], {"output": {"fps": fps, "width": ASPECT_OUT[aspect][0],
                                                           "height": ASPECT_OUT[aspect][1]}})
    aoi = load_aoi(pdir, tl, zcfg)
    segs = [Segment.from_dict(s) for s in data["segments"]]
    trim = data.get("trim") or [0.0, tl.duration]
    comp = Compositor(tl, style, segs, zcfg, float(trim[0]), float(trim[1]), aspect=aspect, aoi=aoi)
    ts = np.arange(float(trim[0]), float(trim[1]), 1 / fps)
    rs = np.array([_visible_rect(comp, t)[0] for t in ts])
    return ts, rs, comp, aoi, segs

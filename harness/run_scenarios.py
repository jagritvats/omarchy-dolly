#!/usr/bin/env python3
"""`make scenarios`: generate the scenario suite, score every scenario with the old
(cursor-only) and new (area-of-interest) camera in 16:9, 9:16 and 1:1, render
before/after clips and a contact sheet, and fail if any new-engine metric misses
its cap.

    python3 harness/run_scenarios.py OUT [--only NAME ...] [--no-clips] [--fps 30]

OUT/
  projects/<scenario>/        synthetic recordings (raw.mp4, events, truth.json, project.json)
  results.json                every metric, before and after, per aspect
  contact-sheet.png           key frame per scenario: after 16:9 | after 9:16 | before 9:16 | after 1:1
  clips/                      before/after clips (terminal typing, corrected vertical crop, workspaces, ...)
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import metrics as M  # noqa: E402
import scenarios as SC  # noqa: E402
from dolly import imgops  # noqa: E402
from dolly import project as projmod  # noqa: E402
from scene import blit_text  # noqa: E402
from dolly.render import export, ffmpeg_bin  # noqa: E402

ASPECTS = ("16:9", "9:16", "1:1")
BEFORE, AFTER = M.Engine("before", False), M.Engine("after", True)
CLIPS = [  # (scenario, aspect, name)
    ("terminal-typing", "16:9", "terminal-typing"),
    ("terminal-typing", "9:16", "terminal-typing-vertical"),
    ("workspaces", "9:16", "dialog-vertical"),
    ("workspaces", "16:9", "workspaces"),
    ("build-log", "9:16", "build-log-vertical"),
    ("floating-drag", "9:16", "floating-drag-vertical"),
    ("interruptions", "16:9", "interruptions"),
    ("window-mode", "16:9", "window-mode"),
]
CLIP_SIZE = {"16:9": (1280, 720), "9:16": (608, 1080), "1:1": (720, 720)}


def make_before_project(pdir: Path, dst: Path) -> Path:
    """A copy that the pre-round-3 pipeline would have produced: logger clock as video
    time (no sync markers), cursor-only zoom/reframe, no cursor hiding."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for f in ("raw.mp4", "raw.mkv"):
        if (pdir / f).exists():
            (dst / f).symlink_to((pdir / f).resolve())
    for f in ("capture.json", "events.raw.jsonl"):
        if (pdir / f).exists():
            shutil.copy(pdir / f, dst / f)
    doc = json.loads((pdir / "events.json").read_text())
    s = doc["capture"].get("sync") or {}
    a, b = float(s.get("a", 0.0)), float(s.get("b", 1.0))
    for e in doc["events"]:
        e["t"] = round((e["t"] - a) / b, 4)
    doc["capture"]["sync"] = {}
    (dst / "events.json").write_text(json.dumps(doc))
    from dolly.events import Timeline
    from dolly.zoom import ZoomConfig, build_segments
    data = projmod.Project(pdir).load()
    data["zoom_config"] = {**(data.get("zoom_config") or {}), "use_aoi": False}
    tl = Timeline.load(dst / "events.json")
    data["segments"] = [x.to_dict() for x in build_segments(tl, ZoomConfig.from_dict(data["zoom_config"]))]
    data["trim"] = None
    projmod.Project(dst).save(data)
    return dst


def key_times(truth: dict, n: int = 1) -> list[float]:
    """Middle of the longest stretches of a meaningful subject."""
    pref = ("typing", "dialog", "scroll", "menu", "window", "open", "output", "click", "popup", "resize")
    runs, cur = [], None
    for f in truth["frames"]:
        k = f["kind"] if f["kind"] in pref else None
        if cur and k == cur[0]:
            cur[2] = f["t"]
        else:
            if cur:
                runs.append(cur)
            cur = [k, f["t"], f["t"]] if k else None
    if cur:
        runs.append(cur)
    runs.sort(key=lambda r: -(r[2] - r[1]) * (1.5 if r[0] in ("typing", "dialog", "scroll", "menu") else 1.0))
    return [round(r[1] + (r[2] - r[1]) * 0.6, 2) for r in runs[:n]] or [truth["duration"] / 2]


def still(comp, raw: Path, t: float, w: int, h: int):
    out = subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{t:.3f}", "-i", str(raw), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True).stdout
    frame = np.frombuffer(out, np.uint8).reshape(h, w, 3)
    return comp.compose_at(t, frame)


def label(img, text, sub=""):
    img = img.copy()
    imgops.fill_rect(img, (0, 0), (img.shape[1], 34 if not sub else 58), (20, 16, 16))
    blit_text(img, 10, 8, text, 15, (245, 245, 250))
    if sub:
        blit_text(img, 10, 35, sub, 12, (180, 190, 210))
    return img


def fit_h(img, h):
    return imgops.resize(img, (int(img.shape[1] * h / img.shape[0]), h))


def contact_sheet(projects: list[Path], out_png: Path, results: dict):
    rows = []
    H = 360
    for pdir in projects:
        truth = json.loads((pdir / "truth.json").read_text())
        t = key_times(truth)[0]
        from dolly.events import Timeline
        tl = Timeline.load(pdir / "events.json")
        raw = projmod.Project(pdir).raw
        tiles = []
        for asp, eng, tag in (("16:9", AFTER, "after 16:9"), ("9:16", AFTER, "after 9:16"),
                              ("9:16", BEFORE, "before 9:16"), ("1:1", AFTER, "after 1:1")):
            comp, _, _, (t0, t1) = M.make_compositor(pdir, asp, eng)
            tt = min(max(t, t0), t1 - 0.05)
            img = still(comp, raw, tt, tl.w, tl.h)
            r = results[pdir.name][eng.name][asp]
            sub = f"AOI in frame {r['aoi_in_frame_pct']}%"
            tiles.append(label(fit_h(img, H), f"{tag} @ {tt:.1f}s", sub))
        head = np.full((H, 250, 3), (28, 22, 22), np.uint8)
        y = 40
        for line in [pdir.name] + _desc(pdir.name):
            blit_text(head, 12, y - 15, line, 15 if y == 40 else 11, (245, 245, 250) if y == 40 else (190, 196, 214))
            y += 30 if y == 40 else 22
        row = np.concatenate([head] + [np.pad(x, ((0, 0), (0, 6), (0, 0)), constant_values=12) for x in tiles], 1)
        rows.append(row)
    wmax = max(r.shape[1] for r in rows)
    rows = [np.pad(r, ((0, 8), (0, wmax - r.shape[1]), (0, 0)), constant_values=12) for r in rows]
    imgops.write_image(out_png, np.concatenate(rows, 0))


def _desc(name):
    return {"terminal-typing": ["typing, mouse parked", "far away; clock +", "spinner noise"],
            "build-log": ["fast-scrolling log:", "frame the window"],
            "tiling": ["open / close /", "drag-resize tiles"],
            "floating-drag": ["Super+drag and", "resize a floating", "window"],
            "workspaces": ["dialog, instant +", "slide workspace", "switch, scratchpad"],
            "interruptions": ["toasts, context", "menu, Omarchy menu"],
            "dual-monitor": ["DP-1 at 1.667x;", "pointer visits the", "1.25x monitor;", "hotplug"],
            "window-mode": ["window recording:", "moves, resizes,", "covered, closes"],
            "long-drift": ["150 s, logger clock", "+270 ms, 0.3 % fast"]}.get(name, [])


def clips(projects: dict, out: Path, fps: float = 30.0) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for scen, asp, name in CLIPS:
        if scen not in projects:
            continue
        pdir = projects[scen]
        w, h = CLIP_SIZE[asp]
        before = make_before_project(pdir, out.parent / "work" / f"{scen}-before")
        so = {"output": {"width": w, "height": h, "fps": fps}, "keys": {"enabled": True}}
        b = export(before, out / f"{name}-before.mp4", "mp4", "fast", style_over=so, aspect=asp)
        a = export(pdir, out / f"{name}-after.mp4", "mp4", "fast", style_over=so, aspect=asp)
        st = "hstack" if asp != "16:9" else "vstack"
        lab = ("drawtext=text='{}':x=12:y=12:fontsize=26:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=8"
               if _has_drawtext() else "null")
        subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-i", str(b), "-i", str(a), "-filter_complex",
                        f"[0:v]{lab.format('before')}[l];[1:v]{lab.format('after')}[r];[l][r]{st}=inputs=2:shortest=1",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                        str(out / f"{name}-before-after.mp4")], check=True)
        made += [b, a, out / f"{name}-before-after.mp4"]
        print(f"  clip {name}: {out / (name + '-before-after.mp4')}", file=sys.stderr)
    return made


_DT = None


def _has_drawtext():
    global _DT
    if _DT is None:
        r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        _DT = " drawtext " in r
    return _DT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--no-clips", action="store_true")
    ap.add_argument("--fps", type=int, default=30)
    a = ap.parse_args()
    out = Path(a.out).resolve()
    proj_root = out / "projects"
    t_all = time.time()
    print("== generating scenarios", file=sys.stderr)
    dirs = SC.generate(proj_root, a.only, a.fps)
    projects = {d.name: d for d in dirs}
    print("== scoring (before = cursor-only engine, after = area of interest)", file=sys.stderr)
    results: dict = {}
    failed = []
    for d in dirs:
        results[d.name] = {"before": {}, "after": {}}
        for eng in (BEFORE, AFTER):
            for asp in ASPECTS:
                r = M.evaluate(d, asp, eng)
                results[d.name][eng.name][asp] = r
                if eng is AFTER and not r["pass"]:
                    failed.append((d.name, asp, r["failed"]))
    caps = dict(M.CAPS)
    # summary table
    cols = ("aoi_in_frame_pct", "pan_speed", "zoom_speed", "pan_accel", "pan_jerk", "reversals", "cuts",
            "min_line_px_1080", "sync_error_frames", "cursor_over_text", "cursor_outside_drawn")
    lines = [f"{'scenario':16s} {'aspect':6s} {'engine':6s} " + " ".join(f"{c[:10]:>10s}" for c in cols)]
    for name, rr in results.items():
        for eng in ("before", "after"):
            for asp in ASPECTS:
                r = rr[eng][asp]
                lines.append(f"{name:16s} {asp:6s} {eng:6s} " + " ".join(
                    f"{'-' if r[c] is None else r[c]:>10}" for c in cols) + ("" if r["pass"] else "  FAIL " + ",".join(r["failed"])))
    table = "\n".join(lines)
    print(table)
    agg = {}
    for eng in ("before", "after"):
        rs = [results[n][eng][asp] for n in results for asp in ASPECTS]
        agg[eng] = {
            "aoi_in_frame_pct_mean": round(float(np.mean([r["aoi_in_frame_pct"] for r in rs])), 1),
            "aoi_in_frame_pct_min": min(r["aoi_in_frame_pct"] for r in rs),
            "max_pan_speed": max(r["pan_speed"] for r in rs), "max_zoom_speed": max(r["zoom_speed"] for r in rs),
            "max_pan_accel_p995": max(r["pan_accel"] for r in rs), "max_pan_jerk_p99": max(r["pan_jerk"] for r in rs),
            "reversals": sum(r["reversals"] for r in rs),
            "min_line_px_1080": min((r["min_line_px_1080"] for r in rs if r["min_line_px_1080"]), default=None),
            "max_sync_error_frames": max((r["sync_error_frames"] for r in rs if r["sync_error_frames"] is not None), default=None),
            "cursor_over_text_frames": sum(r["cursor_over_text"] for r in rs),
            "cursor_outside_drawn_frames": sum(r["cursor_outside_drawn"] for r in rs),
            "passing": f"{sum(r['pass'] for r in rs)}/{len(rs)}"}
    print(json.dumps(agg, indent=2))
    (out / "results.json").write_text(json.dumps({"caps": caps, "aggregate": agg, "scenarios": results}, indent=1))
    (out / "results.txt").write_text(table + "\n")
    print("== contact sheet", file=sys.stderr)
    contact_sheet(dirs, out / "contact-sheet.png", results)
    if not a.no_clips:
        print("== clips", file=sys.stderr)
        clips(projects, out / "clips", a.fps)
    print(f"== done in {time.time() - t_all:.0f}s: {out}", file=sys.stderr)
    if failed:
        print("FAILED (after engine): " + "; ".join(f"{n} {asp}: {','.join(f)}" for n, asp, f in failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

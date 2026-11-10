"""`dolly demo`: end-to-end synthetic run that needs no Hyprland.

1. harness/make_demo.py renders a scripted desktop session in headless
   Chromium into raw.mp4 + events.json (same format as a real capture) and
   raw-cursor.mp4 (what a plain screen recorder gives). The fake terminal shows
   a fake API token and e-mail, and there is a 5 s idle stretch.
2. auto-zoom, idle/typing speed-up detection and the OCR secret scan run on it.
3. Renders the variants below, builds before/after and extracts key frames.

Output dir (default ./demo-out):
    dolly-demo.mp4                  polished 1080p60 (auto-zoom, cursor, frame)
    dolly-demo-idle-cut.mp4         + idle cut / typing speed-ups with the 4x badge
    dolly-demo-secrets-blurred.mp4  + OCR-flagged token and e-mail blurred
    dolly-demo-vertical.mp4         9:16 auto-reframe with intro title card (Shorts/Reels)
    dolly-demo-github.mp4 / .gif    share presets: two-pass size-capped under GitHub's 10 MB
    dolly-demo-linkedin.mp4         1:1 square reframe preset
    before-after.mp4                raw capture vs Dolly
    software-path.mp4               wf-recorder path (cursor baked into the capture)
    frames/*.png, summary.json
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import project as projmod
from .render import cli_progress, export, ffmpeg_bin

ROOT = Path(__file__).resolve().parents[1]


def sh(cmd, **kw):
    print("$", " ".join(str(c) for c in cmd), file=sys.stderr)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)   # dev tool: steps run as long as they need


def step(msg):
    print(f"\n== {msg}", file=sys.stderr)


def run_demo(out: str | None, fast: bool = False):
    from . import share
    from .events import Timeline
    from .privacy import scan_secrets
    from .timemap import cached_activity, detect_speedups
    from .zoom import ZoomConfig, build_segments
    maker = ROOT / "harness" / "make_demo.py"
    if not maker.exists():
        raise SystemExit("dolly demo renders a scripted desktop with the development harness, "
                         "which is not part of the release; record a real demo with `dolly record`")
    out = Path(out or "demo-out").resolve()
    out.mkdir(parents=True, exist_ok=True)
    proj = out / "project"
    t_all = time.time()
    if not (proj / "raw.mp4").exists():
        step("synthetic recording")
        sh([sys.executable, maker, proj, "--fps", "30"])
    p = projmod.Project(proj)
    data = p.load()
    tl = Timeline.load(p.events)
    step("analysis")
    from .render import load_aoi
    zc = ZoomConfig.from_dict(data.get("zoom_config"))
    segs = build_segments(tl, zc, load_aoi(proj, tl, zc))       # area-of-interest camera
    speed = detect_speedups(tl, cached_activity(proj))
    try:
        privacy = scan_secrets(p.raw, tl.duration)
    except RuntimeError as e:  # tesseract missing
        print(f"  secret scan skipped: {e}", file=sys.stderr)
        privacy = []
    data.update({"segments": [s.to_dict() for s in segs], "speed": speed, "privacy": privacy, "trim": None})
    p.save(data)
    for s in segs:
        print(f"  zoom  {s.id}: {s.start:6.2f}-{s.end:6.2f}s {s.zoom:.2f}x {s.reason}", file=sys.stderr)
    for s in speed:
        print(f"  speed {s['id']}: {s['start']:6.2f}-{s['end']:6.2f}s {s['speed']:g}x {s['reason']}", file=sys.stderr)
    for b in privacy:
        print(f"  blur  {b['id']}: {b['start']:6.2f}-{b['end']:6.2f}s {b['label']}", file=sys.stderr)

    w, fps = (1280, 30) if fast else (1920, 60)
    base = {"output": {"width": w, "height": w * 9 // 16, "fps": fps}}
    off = {"speed": {"enabled": False}, "privacy": {"enabled": False}}
    summary = {"duration_raw": tl.duration, "zooms": len(segs), "speedups": speed, "secrets": privacy, "files": {}}

    def rec(name, f):
        summary["files"][name] = {"file": Path(f).name, "bytes": Path(f).stat().st_size}

    step("main render")
    st_main: dict = {}
    mp4 = export(proj, out / "dolly-demo.mp4", "mp4", "high", progress=cli_progress, style_over={**base, **off},
                 stats=st_main)
    rec("main", mp4)
    summary["render_main"] = st_main
    step("draft preview (half size, 30 fps)")
    st_draft: dict = {}
    rec("draft", export(proj, out / "dolly-demo-draft.mp4", "mp4", "draft", progress=cli_progress,
                        style_over={**base, **off}, stats=st_draft))
    summary["render_draft"] = st_draft
    step("idle cut")
    idle = export(proj, out / "dolly-demo-idle-cut.mp4", "mp4", "high", progress=cli_progress,
                  style_over={**base, "privacy": {"enabled": False}})
    rec("idle_cut", idle)
    step("secrets blurred")
    sec = export(proj, out / "dolly-demo-secrets-blurred.mp4", "mp4", "high", progress=cli_progress,
                 style_over={**base, "speed": {"enabled": False}})
    rec("secrets_blurred", sec)
    step("vertical 9:16")
    vert = export(proj, out / "dolly-demo-vertical.mp4", "mp4", "high", progress=cli_progress, aspect="9:16",
                  style_over={"output": {"width": 1080, "height": 1920, "fps": 30},
                              "title": {"enabled": True, "text": "Assign tasks in two clicks", "subtitle": "tasks.local",
                                        "intro": 1.8}})
    rec("vertical", vert)
    step("share presets")
    for pid in ("github", "github-gif", "linkedin"):
        r = share.share_preset(proj, pid, progress=cli_progress, log=lambda m: print(m, file=sys.stderr))
        dst = out / f"dolly-demo-{pid.replace('github-gif', 'github')}.{r['preset']['format']}"
        shutil.copy(r["file"], dst)
        rec(pid, dst)
        summary["files"][pid]["report"] = r["report"]
        summary["files"][pid]["cap_mb"] = r["preset"].get("cap_mb")

    step("tight cap: two-pass at 1 MB (shows the size targeting binding)")
    from .share import encode, presets
    master = share.render_master(proj, presets.get("github"))
    capped = out / "dolly-demo-capped-1MB.mp4"
    rep = encode.target_size_mp4(master, capped, 1_000_000, 1280, 720, 30, log=lambda m: print(m, file=sys.stderr))
    rec("capped_1MB", capped)
    summary["files"]["capped_1MB"]["report"] = rep

    step("software path (cursor baked in)")
    sw = out / "software-path"
    shutil.rmtree(sw, ignore_errors=True)
    sw.mkdir()
    shutil.copy(proj / "raw-cursor.mp4", sw / "raw.mp4")
    ev = json.loads((proj / "events.json").read_text())
    ev["capture"].update({"backend": "wf", "cursor_baked": True})
    (sw / "events.json").write_text(json.dumps(ev))
    swp = projmod.Project(sw)
    sd = swp.load()
    sd["segments"] = data["segments"]
    sd["style"]["cursor"]["enabled"] = False
    swp.save(sd)
    rec("software_path", export(sw, out / "software-path.mp4", "mp4", "fast", progress=cli_progress,
                                style_over={"output": {"width": 1280, "height": 720, "fps": 30}}))

    ff = ffmpeg_bin()
    step("before / after + frames")
    sh([ff, "-v", "error", "-y", "-i", proj / "raw-cursor.mp4", "-i", mp4, "-filter_complex",
        "[0:v]fps=30,scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2:color=0x0e0e14,"
        "drawtext=text='Before - raw capture':x=24:y=24:fontsize=26:fontcolor=white:box=1:boxcolor=0x0e0e14@0.7:boxborderw=10[a];"
        "[1:v]fps=30,scale=960:540,drawtext=text='After - Dolly':x=24:y=24:fontsize=26:fontcolor=white:box=1:boxcolor=0x0e0e14@0.7:boxborderw=10[b];"
        "[a][b]hstack=inputs=2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", out / "before-after.mp4"])
    fr = out / "frames"
    shutil.rmtree(fr, ignore_errors=True)
    fr.mkdir()
    grab = lambda src, t, name: sh([ff, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", fr / name])
    grab(mp4, 0.6, "01-start.png")
    for i, s in enumerate(segs[:3]):
        grab(mp4, min(s.end - 0.3, s.start + 1.6), f"0{2 + i}-{s.id}-zoomed.png")
    if tl.clicks:
        grab(mp4, tl.clicks[0]["t"] + 0.12, "05-click-ripple.png")
    ks = [k for k in tl.keys if set(k["mods"]) & {"ctrl", "alt", "super"}]
    if ks:
        grab(mp4, ks[0]["t"] + 0.4, "06-shortcut-overlay.png")
    from .timemap import TimeMap
    tm = TimeMap(0, tl.duration, speed)
    if speed:
        s0 = max(speed, key=lambda s: s["speed"])
        grab(idle, tm.to_output((s0["start"] + s0["end"]) / 2), "07-idle-cut-badge.png")
    if privacy:
        t_sec = 24.0 if tl.duration > 25 else tl.duration / 2
        grab(mp4, t_sec, "08a-terminal-before-blur.png")
        grab(sec, t_sec, "08b-terminal-secrets-blurred.png")
    vt = 1.8 + tm.to_output(10.6)
    grab(vert, 0.8, "09a-vertical-title.png")
    grab(vert, 1.8 + tm.to_output(6.5), "09b-vertical-reframe.png")     # the dialog, fully in the 9:16 crop
    grab(vert, vt, "09d-vertical-after-save.png")
    grab(vert, 1.8 + tm.to_output(24.0), "09c-vertical-terminal.png")
    grab(out / "dolly-demo-linkedin.mp4", 6.0, "10-square.png")
    grab(out / "before-after.mp4", 6, "11-before-after.png")
    summary["elapsed_s"] = round(time.time() - t_all)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\ndemo done in {summary['elapsed_s']}s -> {out}", file=sys.stderr)
    for k, v in summary["files"].items():
        print(f"  {k:16s} {v['file']:34s} {v['bytes'] / 1e6:7.2f} MB", file=sys.stderr)
    print(out)

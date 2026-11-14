"""The plain export: the take as it was recorded, by ffmpeg alone.

Everything that draws (dolly/render.py) needs Python's cairo module. A stock Omarchy has
it, but a machine where python-cairo was removed must still record and give a video, so
this module imports nothing outside the standard library.

What it keeps of the project:
- the range: the default trim (sync markers and countdown left out) or the one you set
- privacy boxes: each enabled box is covered with a solid rectangle for its time span
  (the styled export blurs; a filled box needs no pixel maths and shows nothing)
- the audio tracks, mixed like the styled export (mute spans, ducking, loudness)

What it leaves out: zoom, the redrawn cursor and click ripples, keycaps, the background
and padding, speed-ups, captions, title cards, the camera bubble recorded for the editor,
and the 9:16 / 1:1 framing. The recording itself is not changed: the styled export of the
same take is there as soon as python-cairo is installed again.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import project as projmod
from .events import Timeline
from .ff import QUALITY, ffmpeg_bin, probe

FILL = "0x14141c"            # the solid colour a private area is covered with
EDGE = 0.05                  # s a cover starts early and ends late (a frame and a half at 30 fps)
LEFT_OUT = "zoom, cursor effects and the background"


def cover_filters(boxes, t0: float, t1: float, w: int, h: int) -> list[str]:
    """One drawbox filter per enabled privacy box that touches [t0, t1], in the clock of the
    cut video (0 = t0). Only numbers go into the filter: nothing from the project file
    is handed to ffmpeg as text. A box that cannot be read stops the export: showing what
    was meant to be hidden is the one thing this must not do."""
    out = []
    for i, b in enumerate(boxes or []):
        try:
            if not b.get("enabled", True):
                continue
            a, e = float(b["start"]) - t0, float(b["end"]) - t0
            x, y, bw, bh = (int(round(float(v))) for v in b["rect"])
        except (AttributeError, KeyError, TypeError, ValueError):
            raise RuntimeError(f"privacy box {i + 1} of this recording cannot be read; nothing was exported") from None
        x0, y0, x1, y1 = max(0, x), max(0, y), min(w, x + bw), min(h, y + bh)
        if x1 - x0 < 2 or y1 - y0 < 2 or e + EDGE <= 0 or a - EDGE >= t1 - t0:
            continue
        a, e = max(0.0, a - EDGE), min(t1 - t0, e + EDGE)
        out.append(f"drawbox=x={x0}:y={y0}:w={x1 - x0}:h={y1 - y0}:color={FILL}:t=fill:"
                   f"enable='between(t,{a:.3f},{e:.3f})'")
    return out


def _video(raw: Path) -> dict:
    p = probe(raw)
    v = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"w": int(v.get("width") or 0), "h": int(v.get("height") or 0),
            "audio": any(s.get("codec_type") == "audio" for s in p.get("streams", []))}


def _progress_line(frac: float, done: bool = False):
    if sys.stderr.isatty():
        sys.stderr.write(f"\r  exporting {frac * 100:3.0f} %" + ("\n" if done else ""))
        sys.stderr.flush()


def _run(cmd: list[str], total: float, on_progress) -> None:
    """Run ffmpeg (started with -progress pipe:1) and report how far it is."""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, text=True)
        try:
            for line in proc.stdout:                  # no timeout: it encodes the whole take
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        on_progress(min(1.0, max(0.0, int(line.split("=", 1)[1]) / 1e6 / max(total, 1e-6))))
                    except ValueError:
                        pass                          # "N/A" before the first frame
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0:
            err.seek(0)
            tail = err.read().decode("utf-8", "replace").strip()[-300:]
            raise RuntimeError(f"ffmpeg could not write the export: {tail} (disk full? see `dolly doctor`)")


def export(project_dir: str | Path, out_path: str | Path | None = None, gif: bool = False,
           quality: str | None = None, width: int | None = None, fps: float | None = None,
           notify: bool = False, progress=None, lacking: list[str] | None = None) -> Path:
    """Write the plain export of a recording and return its path. `lacking`: the packages
    whose absence made this the export (the card then offers to install them)."""
    proj = projmod.Project(project_dir)
    data = proj.load()
    style = data["style"]
    tl = Timeline.load(proj.events)
    v = _video(proj.raw)
    w, h = v["w"] or int(tl.w), v["h"] or int(tl.h)
    trim = data.get("trim") or [0.0, tl.duration]
    t0, t1 = max(0.0, float(trim[0])), min(float(tl.duration), float(trim[1]))
    total = t1 - t0
    if total < 0.05 and w and h:
        raise projmod.EmptyTrim(f"no video remains in the selected trim ({t0:g}–{t1:g} s); "
                                "adjust the trim in the editor or record another take")
    if not w or not h:
        raise RuntimeError("nothing to export: this recording has no video in its range")
    out_cfg = style.get("output", {})
    quality = quality or out_cfg.get("quality") or "high"
    out_fps = float(fps or out_cfg.get("fps") or 60)
    if quality == "draft":                           # like the styled draft: small and 30 fps
        out_fps = min(out_fps, 30.0)
        width = width or (int(w * min(1.0, 960 / max(w, h))) if max(w, h) > 960 else None)
    preset, crf = QUALITY.get(quality, QUALITY["high"])
    boxes = data.get("privacy") if style.get("privacy", {}).get("enabled", True) else []
    # wf-recorder writes full-range video; the export is limited range like the styled one
    # (what players and upload sites expect of H.264): converted, not merely relabelled,
    # and first, so the covers are drawn in the colours the file is written in
    vf = ["scale=out_range=tv", *cover_filters(boxes, t0, t1, w, h)]
    if w % 2 or h % 2:
        vf.append(f"crop={w - w % 2}:{h - h % 2}:0:0")
    if width and int(width) < w:
        vf.append(f"scale={int(width) - int(width) % 2}:-2:flags=lanczos")
    fmt = "gif" if gif else "mp4"
    proj.exports.mkdir(exist_ok=True)
    if out_path is None:
        out_path = proj.exports / f"{proj.name}-{time.strftime('%H%M%S')}.{fmt}"
    out_path = Path(out_path)
    part = out_path.with_name(f".{out_path.stem}.part.mp4")
    ff = ffmpeg_bin()
    cmd = [ff, "-hide_banner", "-nostats", "-v", "error", "-y", "-progress", "pipe:1",
           "-ss", f"{t0:.4f}", "-t", f"{total:.4f}", "-i", str(proj.raw)]
    tracks = [t for t in (tl.capture.get("audio_tracks") or []) if (proj.dir / t["file"]).exists()] if not gif else []
    graph = "[0:v]" + ",".join(vf) + "[vout]"
    if tracks:
        from .audio import mix_filter
        au = style.get("audio", {})
        settings = {"duck": au.get("duck", True), "normalize": au.get("normalize", True),
                    "mic": {"denoise": au.get("denoise", True), **(au.get("mic") or {})},
                    "system": dict(au.get("system") or {})}
        for t in tracks:
            cmd += ["-f", "matroska", "-i", str(proj.dir / t["file"])]
        graph += ";" + mix_filter(tracks, 1, [(t0, t1, 1.0)], 0.0, total, settings,
                                  tl.capture.get("mic_mutes") or [], au.get("rnnoise_model"))
        cmd += ["-filter_complex", graph, "-map", "[vout]", "-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]
    elif v["audio"] and not gif:                     # an older take with its sound inside the video file
        cmd += ["-filter_complex", graph, "-map", "[vout]", "-map", "0:a:0", "-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-filter_complex", graph, "-map", "[vout]"]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-color_range", "tv",
            "-profile:v", "high",
            "-r", f"{out_fps:g}", "-fps_mode", "cfr", "-g", str(int(out_fps * 2)), "-movflags", "+faststart",
            "-f", "mp4", str(part)]
    from .health import Job, check_disk
    msg = check_disk(proj.exports, need=256 << 20)
    if msg:
        raise RuntimeError(msg)
    job = Job("exporting", proj.name, str(out_path))

    def on_progress(frac: float):
        job.update(frac)
        (progress or _progress_line)(frac)
    try:
        _run(cmd, total, on_progress)
        if gif:
            g = style.get("gif", {})
            gw, gfps = min(w, int(g.get("width", 800))), int(g.get("fps", 15))
            # no timeout: a GIF encode of the whole export (time grows with its length)
            subprocess.run([ff, "-v", "error", "-y", "-i", str(part), "-vf",
                            f"fps={gfps},scale={gw}:-2:flags=lanczos,split[a][b];"
                            "[a]palettegen=stats_mode=full:max_colors=192[p];"
                            "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
                            str(out_path)], check=True)
        else:
            part.replace(out_path)
    finally:
        part.unlink(missing_ok=True)
        job.done()
    if progress:
        progress(1.0)
    else:
        _progress_line(1.0, done=True)
    if notify:
        try:
            from .share import notify_export
            notify_export(proj.dir, out_path, lacking=lacking)
        except Exception:
            from . import log
            log.swallowed("export card")
    return out_path

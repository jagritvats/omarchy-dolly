"""Size-targeted encoding.

target_size_mp4(): first tries a quality (CRF) encode; if that already fits
under the cap it is kept (better quality per byte than forcing a bitrate).
Otherwise it runs libx264 two-pass at the bitrate that lands at ~95 % of the
cap, verifies the real size and retries with a corrected bitrate. If the
needed bitrate is too low for the resolution to look decent it steps the
resolution / frame rate down first.

gif_under_cap(): palette GIF, stepping width and fps down until it fits.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from ..ff import ffmpeg_bin, probe

MB = 1_000_000
# bits per pixel per frame below which screen recordings start to look smeary
# at libx264 -preset medium (empirical, text-heavy content)
BPP_FLOOR = 0.012


def media_info(path: Path) -> dict:
    p = probe(path)
    v = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), {})
    has_a = any(s.get("codec_type") == "audio" for s in p.get("streams", []))
    num, den = (v.get("avg_frame_rate") or "30/1").split("/")
    return {"w": int(v.get("width", 0)), "h": int(v.get("height", 0)),
            "fps": float(num) / max(float(den), 1.0), "duration": float(p.get("format", {}).get("duration", 0) or 0),
            "audio": has_a}


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def _ff(args: list, cwd=None):
    # no timeout: a share-preset encode of the whole video (time grows with its length)
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", *map(str, args)], check=True, cwd=cwd)


def crf_encode(src: Path, dst: Path, w: int, h: int, fps: float, crf: int = 22, preset: str = "medium",
               audio_kbps: int = 128):
    info = media_info(src)
    a = ["-c:a", "aac", "-b:a", f"{audio_kbps}k"] if info["audio"] else ["-an"]
    _ff(["-i", src, "-vf", f"scale={w}:{h}:flags=lanczos,fps={fps}", "-c:v", "libx264", "-preset", preset,
         "-crf", crf, "-pix_fmt", "yuv420p", "-profile:v", "high", "-fps_mode", "cfr", "-g", int(fps * 2),
         *a, "-movflags", "+faststart", dst])


def plan_ladder(w: int, h: int, fps: float, duration: float, target_bytes: int, audio_kbps: int) -> list[tuple]:
    """(w, h, fps, video_kbps) candidates from best to most reduced."""
    total_kbps = target_bytes * 8 / 1000 / max(duration, 0.1) * 0.97  # ~3 % mux overhead
    v_kbps = total_kbps - (audio_kbps if audio_kbps else 0)
    out = []
    for scale in (1.0, 0.75, 0.6, 0.5, 0.4):
        for f in sorted({fps, min(fps, 30), min(fps, 24), min(fps, 15)}, reverse=True):
            ww, hh = _even(w * scale), _even(h * scale)
            out.append((ww, hh, f, v_kbps, v_kbps * 1000 / (ww * hh * f)))
    return out


def two_pass(src: Path, dst: Path, w: int, h: int, fps: float, v_kbps: float, audio_kbps: int,
             preset: str = "medium"):
    info = media_info(src)
    with tempfile.TemporaryDirectory(prefix="dolly2p-") as tmp:
        log = str(Path(tmp) / "pass")
        vf = f"scale={w}:{h}:flags=lanczos,fps={fps}"
        common = ["-vf", vf, "-c:v", "libx264", "-preset", preset, "-b:v", f"{int(v_kbps)}k",
                  "-maxrate", f"{int(v_kbps * 1.5)}k", "-bufsize", f"{int(v_kbps * 2)}k",
                  "-pix_fmt", "yuv420p", "-profile:v", "high", "-fps_mode", "cfr", "-g", int(fps * 2),
                  "-passlogfile", log]
        _ff(["-i", src, *common, "-pass", 1, "-an", "-f", "null", "-"], cwd=tmp)
        a = ["-c:a", "aac", "-b:a", f"{audio_kbps}k"] if info["audio"] and audio_kbps else ["-an"]
        _ff(["-i", src, *common, "-pass", 2, *a, "-movflags", "+faststart", dst], cwd=tmp)


def target_size_mp4(src: Path, dst: Path, cap_bytes: int, w: int, h: int, fps: float,
                    audio_kbps: int = 96, crf: int = 22, log=print) -> dict:
    """Encode src -> dst at (w, h, fps) under cap_bytes. Returns a report dict."""
    src, dst = Path(src), Path(dst)
    info = media_info(src)
    a_kbps = audio_kbps if info["audio"] else 0
    goal = int(cap_bytes * 0.95)
    # 1. quality encode; keep if it fits
    crf_encode(src, dst, w, h, fps, crf=crf, audio_kbps=max(a_kbps, 96))
    size = dst.stat().st_size
    if size <= goal:
        return {"method": "crf", "crf": crf, "size": size, "w": w, "h": h, "fps": fps, "attempts": 1}
    # 2. two-pass at the right bitrate, stepping resolution down if bpp too low
    ladder = plan_ladder(w, h, fps, info["duration"], goal, a_kbps)
    choice = next((c for c in ladder if c[4] >= BPP_FLOOR), ladder[-1])
    ww, hh, ff, v_kbps, bpp = choice
    attempts = 1
    for attempt in range(3):
        attempts += 1
        two_pass(src, dst, ww, hh, ff, v_kbps, a_kbps)
        size = dst.stat().st_size
        log(f"  two-pass {ww}x{hh}@{ff:g} {v_kbps:.0f} kb/s -> {size / MB:.2f} MB (goal {goal / MB:.2f})")
        if size <= cap_bytes and size >= goal * 0.85:
            break
        if size <= cap_bytes and attempt == 2:
            break
        v_kbps *= goal / size * (0.98 if size > goal else 1.0)
    if size > cap_bytes:
        raise RuntimeError(f"could not get under {cap_bytes / MB:.1f} MB (got {size / MB:.2f} MB)")
    return {"method": "two-pass", "size": size, "w": ww, "h": hh, "fps": ff, "video_kbps": round(v_kbps),
            "bpp": round(bpp, 4), "attempts": attempts}


GIF_LADDER = [(1.0, 15, 192), (1.0, 12, 192), (0.9, 12, 160), (0.8, 12, 128), (0.8, 10, 128), (0.7, 10, 96),
              (0.6, 10, 96), (0.6, 8, 64), (0.5, 8, 64), (0.4, 8, 48)]


def gif_encode(src: Path, dst: Path, width: int, fps: float, colors: int = 192):
    vf = (f"fps={fps},scale={width}:-2:flags=lanczos,split[a][b];[a]palettegen=stats_mode=full:max_colors={colors}[p];"
          f"[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    _ff(["-i", src, "-vf", vf, "-loop", 0, dst])


def gif_under_cap(src: Path, dst: Path, cap_bytes: int | None, width: int, fps: float, log=print) -> dict:
    tried = 0
    for scale, f, colors in GIF_LADDER:
        f = min(f, fps)
        w = _even(width * scale)
        gif_encode(src, dst, w, f, colors)
        tried += 1
        size = dst.stat().st_size
        log(f"  gif {w}px @{f}fps -> {size / MB:.2f} MB")
        if cap_bytes is None or size <= cap_bytes:
            return {"method": "gif", "size": size, "w": w, "fps": f, "attempts": tried}
    raise RuntimeError(f"GIF still over {cap_bytes / MB:.1f} MB at {w}px/{f}fps; shorten or trim the clip")


# --------------------------------------------------------------- estimates
# bits per (pixel * frame) of a CRF-22 1080p60 screen recording; calibrated on
# the demo (1080p60 CRF17 = 5.9 MB / 32.7 s -> 0.0036). Smaller frames and
# lower fps carry more bits per pixel-frame, hence the exponents below.
BITS_PPF_1080P60 = 0.0052
GIF_BYTES_PER_PIXEL_FRAME = 0.045


def _bits_ppf(w, h, fps, ref=BITS_PPF_1080P60):
    return ref * (1920 * 1080 / (w * h)) ** 0.35 * (60 / fps) ** 0.5


def estimate(preset: dict, duration: float, master: Path | None = None, audio: bool = True) -> dict:
    """Rough output size before encoding (refined from a cached master when present)."""
    w, h, fps = preset["width"], preset["height"], preset["fps"]
    cap = preset.get("cap_mb")
    if preset["format"] == "gif":
        est = GIF_BYTES_PER_PIXEL_FRAME * w * h * min(fps, 15) * duration
    else:
        ref = BITS_PPF_1080P60
        if master and Path(master).exists():
            mi = media_info(Path(master))
            if mi["duration"] > 0 and mi["w"]:
                measured = Path(master).stat().st_size * 8 / (mi["w"] * mi["h"] * mi["fps"] * mi["duration"])
                # back to 1080p60-equivalent, and CRF14 master -> CRF22 output (~0.45x)
                ref = measured / _bits_ppf(mi["w"], mi["h"], mi["fps"], 1.0) * 0.4  # measured: CRF22 medium output ~= 0.4x the CRF13 master
        mult = 2.2 if preset.get("quality") == "best" else 1.0
        est = _bits_ppf(w, h, fps, ref) * w * h * fps * duration / 8 * mult + (12_000 * duration if audio else 0)
    fits = True
    if cap:
        capb = cap * MB
        if est > capb * 0.95:
            est = capb * 0.95
            fits = None  # will be squeezed to fit (two-pass / GIF ladder)
    over_duration = bool(preset.get("max_duration") and duration > preset["max_duration"])
    return {"bytes": int(est), "fits": fits, "over_duration": over_duration}

"""Optional local captions.

Backends (first available):
  1. whisper.cpp CLI (`whisper-cli`, AUR whisper.cpp) + a ggml model:
     $DOLLY_WHISPER_MODEL or ~/.local/share/dolly/whisper/ggml-base.en.bin
  2. faster-whisper (python), model name $DOLLY_WHISPER_SIZE (default "base")
If neither is installed, captions are skipped with a clear message.

Cues are stored as captions.srt (source time) in the project; export burns them
in (styled pill at the bottom) and writes a time-remapped .srt next to the video.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


class CaptionsUnavailable(RuntimeError):
    pass


def _fmt(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)) % 1000:03d}"


def write_srt(cues: list[dict], path: Path):
    lines = []
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{_fmt(c['start'])} --> {_fmt(c['end'])}", c["text"].strip(), ""]
    Path(path).write_text("\n".join(lines))


def parse_srt(text: str) -> list[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip().replace("\r", "")):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        st = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        en = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        body = block[m.end():].strip()
        if body:
            cues.append({"start": st, "end": en, "text": " ".join(body.splitlines())})
    return cues


def remap(cues: list[dict], timemap, offset: float = 0.0) -> list[dict]:
    """Source-time cues -> output-time cues (dropping cues fully inside sped-up stretches)."""
    out = []
    for c in cues:
        a, b = max(c["start"], timemap.t0), min(c["end"], timemap.t1)
        if b <= a:
            continue
        if timemap.speed_at((a + b) / 2) > 1.01:
            continue
        out.append({"start": timemap.to_output(a) + offset, "end": timemap.to_output(b) + offset, "text": c["text"]})
    return out


def backend() -> str | None:
    if shutil.which("whisper-cli") and _model_path():
        return "whisper.cpp"
    try:
        import faster_whisper  # noqa: F401
        return "faster-whisper"
    except ImportError:
        return None


def _model_path() -> str | None:
    p = os.environ.get("DOLLY_WHISPER_MODEL") or str(Path.home() / ".local/share/dolly/whisper/ggml-base.en.bin")
    return p if Path(p).exists() else None


def transcribe(project_dir: Path, language: str | None = None) -> list[dict]:
    from .render import ffmpeg_bin, probe
    project_dir = Path(project_dir)
    from .project import Project
    raw = Project(project_dir).raw
    if not any(s.get("codec_type") == "audio" for s in probe(raw).get("streams", [])):
        raise CaptionsUnavailable("the recording has no audio track (record with --mic)")
    be = backend()
    if not be:
        raise CaptionsUnavailable("no transcriber: install whisper.cpp (whisper-cli + a ggml model) "
                                  "or `pip install faster-whisper`")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        # no timeouts here: decoding and transcribing take time proportional to the recording
        subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-i", str(raw), "-ac", "1", "-ar", "16000", str(wav)],
                       check=True)
        if be == "whisper.cpp":
            args = ["whisper-cli", "-m", _model_path(), "-f", str(wav), "-osrt", "-of", str(Path(tmp) / "out")]
            if language:
                args += ["-l", language]
            subprocess.run(args, check=True, capture_output=True)
            cues = parse_srt((Path(tmp) / "out.srt").read_text())
        else:
            from faster_whisper import WhisperModel
            model = WhisperModel(os.environ.get("DOLLY_WHISPER_SIZE", "base"), device="cpu", compute_type="int8")
            segs, _ = model.transcribe(str(wav), language=language, vad_filter=True)
            cues = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segs]
    write_srt(cues, project_dir / "captions.srt")
    return cues


def load(project_dir: Path) -> list[dict]:
    f = Path(project_dir) / "captions.srt"
    return parse_srt(f.read_text()) if f.exists() else []


@lru_cache(maxsize=128)
def caption_image(text: str, max_w: int, scale: float, font_file: str | None):
    size = int(30 * scale)
    font = ImageFont.truetype(font_file, size) if font_file else ImageFont.load_default()
    # greedy wrap to max 2 lines
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.getlength(trial) <= max_w - 40 * scale or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    lines = lines[-2:]
    lh = int(size * 1.3)
    tw = int(max(font.getlength(l) for l in lines)) + int(36 * scale)
    th = lh * len(lines) + int(20 * scale)
    img = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, tw - 1, th - 1), radius=int(14 * scale), fill=(14, 14, 20, 205))
    for i, l in enumerate(lines):
        d.text((tw / 2, int(10 * scale) + lh * i + lh / 2), l, font=font, fill=(245, 246, 252, 255), anchor="mm")
    arr = np.asarray(img, np.float32) / 255.0
    return np.concatenate([arr[..., :3][..., ::-1] * arr[..., 3:4], arr[..., 3:4]], axis=2)

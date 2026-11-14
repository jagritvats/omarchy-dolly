"""Finding ffmpeg and asking ffprobe about a file.

No third-party imports: the recorder, the library and the plain export use these on a
machine without Python's cairo module (nothing to draw with, still something to record).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# x264 preset and crf per export quality (the styled export and the plain one)
QUALITY = {"draft": ("ultrafast", 26), "fast": ("veryfast", 21), "high": ("veryfast", 17),
           "best": ("slow", 15), "master": ("veryfast", 13)}


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # the ffmpeg that the imageio-ffmpeg Python package carries, when one is importable
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover
        raise RuntimeError("ffmpeg not found (package ffmpeg, which Omarchy ships)") from e


def probe(path: Path) -> dict:
    ffprobe = shutil.which("ffprobe") or ffmpeg_bin().replace("ffmpeg", "ffprobe")
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                              "stream=index,codec_type,width,height,r_frame_rate,avg_frame_rate,start_time:format=duration,start_time",
                              "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffprobe did not answer within 60 s for {path.name}") from None
    try:
        return json.loads(out.stdout or "{}")
    except ValueError:
        return {}

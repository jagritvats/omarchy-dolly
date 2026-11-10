"""Sync markers in variable-frame-rate video (wf-recorder writes a frame only when the
screen changes): marker times must come from frame timestamps, not frame index / fps."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import sync  # noqa: E402

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                reason="needs ffmpeg + ffprobe")


def _vfr_with_marker(path: Path):
    # 90 frames: the first 60 at 30 fps (0-2 s), the last 30 at 60 fps (2-2.5 s);
    # the marker (magenta square, green centre) is on frames 70-75 -> t = 2.167 s
    box = ("drawbox=x=0:y=0:w=40:h=40:color=0xff00ff:t=fill:enable='between(n,70,75)',"
           "drawbox=x=10:y=10:w=20:h=20:color=0x00ff00:t=fill:enable='between(n,70,75)',"
           "setpts='if(lt(N,60),N/30,2+(N-60)/60)/TB'")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x202020:s=320x240:r=60",
                    "-frames:v", "90", "-vf", box, "-fps_mode", "passthrough", "-c:v", "libx264", "-preset",
                    "ultrafast", "-qp", "0", "-pix_fmt", "yuv444p", "-f", "matroska", str(path)], check=True)


def test_marker_time_uses_frame_timestamps(tmp_path):
    raw = tmp_path / "raw.mkv"
    _vfr_with_marker(raw)
    times = sync.frame_times(raw)
    assert times is not None and len(times) == 90
    runs = sync.detect_markers(raw, 1.0, 60.0)
    assert len(runs) == 1
    t0, t1 = runs[0]
    # index/fps would give 70/60 = 1.17 s: a full second off
    assert abs(t0 - (2 + 10 / 60)) < 0.02 and abs(t1 - (2 + 15 / 60)) < 0.02, runs


def test_two_markers_on_vfr_fit_both():
    log = [0.25, 6.17]
    runs = [(3.409, 3.45), (9.325, 9.36)]
    f = sync.fit(log, runs, 62.5)
    assert f.method == "markers2" and abs(f.map(6.17) - 9.317) < 0.02

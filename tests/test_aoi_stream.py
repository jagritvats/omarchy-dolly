"""Activity analysis streams the proxy decode (flat memory for long recordings) and gives the
same result as analysing all frames at once."""
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import aoi  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _clip(path: Path):
    # 6 s at 10 fps: a box appears at 2 s (a "dialog" a click at 1.8 s opens), text-like changes later
    vf = ("drawbox=x=400:y=200:w=300:h=200:color=white:t=fill:enable='gte(t,2)',"
          "drawtext=text='%{n}':x=40:y=40:fontsize=24:fontcolor=white")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x202020:s=1280x800:r=10",
                    "-t", "6", "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", str(path)],
                   check=True)


def test_streamed_matches_all_frames(tmp_path):
    raw = tmp_path / "raw.mkv"
    try:
        _clip(raw)
    except subprocess.CalledProcessError:
        pytest.skip("ffmpeg without drawtext")
    cfg = aoi.AOIConfig()
    streamed = aoi.compute_activity(raw, 1280, 800, cfg, clicks=[1.8, 5.95])
    frames = list(aoi._stream_proxy(raw, cfg.fps, cfg.proxy_w, 300))
    whole = aoi.compute_activity(raw, 1280, 800, cfg, frames=frames, clicks=[1.8, 5.95])
    assert len(streamed.t) == len(whole.t) == len(frames)
    assert np.allclose(streamed.frac, whole.frac) and streamed.blobs == whole.blobs
    # the dialog the click opened (whole.reactions has no colour pass: compare geometry presence)
    assert streamed.reactions[0][1] is not None
    assert streamed.reactions[1][0] == 5.95          # a click at the very end is clipped, not an error


def test_sparse_frames_keep_only_click_frames():
    want = aoi._click_frame_indices([1.8, 30.0], 10.0)
    assert want == {17, 24, 299, 306}


def test_color_frame_timeout_is_not_fatal(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 30)
    monkeypatch.setattr(subprocess, "run", slow)
    assert aoi._color_frame(Path("/nonexistent.mkv"), 1.0, 480, 300) is None

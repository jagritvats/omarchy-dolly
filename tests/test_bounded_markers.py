"""Stop must not read the whole recording to find two markers: they can only be in the
head (the logger starts with the take) and in the tail (flashed just before the recorder
stops). The bounded scan gives the same runs as the full scan."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import sync  # noqa: E402

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                reason="needs ffmpeg + ffprobe")


def _clip(path: Path, frames: int, marks, fps: int = 60):
    """A dark clip with the sync marker (magenta square, green centre) on the given frame ranges."""
    en = "+".join(f"between(n,{a},{b})" for a, b in marks) or "0"
    box = (f"drawbox=x=0:y=0:w=40:h=40:color=0xff00ff:t=fill:enable='{en}',"
           f"drawbox=x=10:y=10:w=20:h=20:color=0x00ff00:t=fill:enable='{en}'")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x202020:s=320x240:r={fps}",
                    "-frames:v", str(frames), "-vf", box, "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
                    "-pix_fmt", "yuv444p", "-f", "matroska", str(path)], check=True)


@pytest.fixture(scope="module")
def long_clip(tmp_path_factory):
    raw = tmp_path_factory.mktemp("bounded") / "raw.mkv"
    _clip(raw, 14 * 60, [(30, 37), (800, 807)])          # 14 s: markers at 0.5 s and 13.33 s
    return raw


def _close(a, b):
    return len(a) == len(b) and all(abs(x[0] - y[0]) < 1e-3 and abs(x[1] - y[1]) < 1e-3 for x, y in zip(a, b))


def test_same_runs_as_the_full_scan(long_clip):
    full = sync.detect_markers(long_clip, 1.0, 60.0)
    assert len(full) == 2 and abs(full[0][0] - 0.5) < 0.02 and abs(full[1][0] - 800 / 60) < 0.02
    assert _close(sync.detect_markers_bounded(long_clip, 1.0, 60.0, head_until=0.5), full)
    assert _close(sync.detect_markers_bounded(long_clip, 1.0, 60.0), full)        # no clock estimate either


def test_only_the_head_and_the_tail_are_decoded(long_clip, monkeypatch):
    seen = []
    real = sync._probe_window

    def probe(raw, size, ffmpeg, head=None, tail=None, timeout=120.0):
        frames, pts = real(raw, size, ffmpeg, head=head, tail=tail, timeout=timeout)
        seen.append((head, tail, len(frames)))
        return frames, pts
    monkeypatch.setattr(sync, "_probe_window", probe)
    monkeypatch.setattr(sync, "detect_markers", lambda *a, **k: pytest.fail("the whole file was read"))
    runs = sync.detect_markers_bounded(long_clip, 1.0, 60.0, head_until=0.5)
    assert len(runs) == 2
    assert sorted((h is not None, t is not None) for h, t, _ in seen) == [(False, True), (True, False)]
    assert sum(n for _, _, n in seen) <= (sync.HEAD_WINDOW + sync.TAIL_WINDOW) * 60 + 4      # of 840 frames


def test_no_stop_marker_logged_means_only_the_head(long_clip, monkeypatch):
    seen = []
    real = sync._probe_window
    monkeypatch.setattr(sync, "_probe_window", lambda raw, size, ffmpeg, head=None, tail=None, timeout=120.0:
                        (seen.append((head, tail)) or real(raw, size, ffmpeg, head=head, tail=tail)))
    runs = sync.detect_markers_bounded(long_clip, 1.0, 60.0, want_stop=False, head_until=0.5)
    assert len(runs) == 1 and abs(runs[0][0] - 0.5) < 0.02
    assert all(tail is None for _, tail in seen)


def test_a_window_that_cannot_be_read_falls_back_to_the_full_scan(long_clip, monkeypatch):
    notes = []
    monkeypatch.setattr(sync, "scan_tail", lambda *a, **k: None)
    full = sync.detect_markers(long_clip, 1.0, 60.0)
    runs = sync.detect_markers_bounded(long_clip, 1.0, 60.0, head_until=0.5, log=notes.append)
    assert _close(runs, full) and any("whole file" in n for n in notes)
    monkeypatch.undo()
    monkeypatch.setattr(sync, "scan_head", lambda *a, **k: None)
    assert _close(sync.detect_markers_bounded(long_clip, 1.0, 60.0, head_until=0.5, log=notes.append), full)


def test_a_late_start_marker_widens_the_head_window(tmp_path):
    raw = tmp_path / "raw.mkv"
    _clip(raw, 16 * 60, [(390, 397), (920, 927)])         # start marker at 6.5 s (a slow recorder start)
    full = sync.detect_markers(raw, 1.0, 60.0)
    assert _close(sync.detect_markers_bounded(raw, 1.0, 60.0, head_until=6.5), full)
    # without knowing where to look, the default head window stops short of it
    assert len(sync.detect_markers_bounded(raw, 1.0, 60.0)) == 1


def test_an_early_head_scan_is_reused_only_if_it_reached_the_marker(long_clip, monkeypatch):
    head = sync.scan_head(long_clip, 1.0)
    assert head and head["runs"] and head["end"] > 3.5
    calls = []
    real = sync.scan_head
    monkeypatch.setattr(sync, "scan_head", lambda *a, **k: (calls.append(1) or real(*a, **k)))
    full = sync.detect_markers(long_clip, 1.0, 60.0)
    assert _close(sync.detect_markers_bounded(long_clip, 1.0, 60.0, head_until=0.5, head=head), full)
    assert calls == []                                    # the early scan was enough
    short = {"p0": head["p0"], "end": 0.2, "runs": []}    # stopped before the expected marker
    assert _close(sync.detect_markers_bounded(long_clip, 1.0, 60.0, head_until=0.5, head=short), full)
    assert calls == [1]


def test_a_short_take_is_not_counted_twice(tmp_path):
    raw = tmp_path / "raw.mkv"
    _clip(raw, 150, [(20, 27), (120, 127)])               # 2.5 s: head and tail windows both cover all of it
    full = sync.detect_markers(raw, 1.0, 60.0)
    assert len(full) == 2
    assert _close(sync.detect_markers_bounded(raw, 1.0, 60.0, head_until=0.33), full)


def test_times_are_measured_from_the_first_frame(long_clip):
    head = sync.scan_head(long_clip, 1.0)
    tail = sync.scan_tail(long_clip, 1.0)
    assert head["runs"] and abs(head["runs"][0][0] - 0.5) < 0.02
    assert tail["runs"] and abs(tail["runs"][0][0] - head["p0"] - 800 / 60) < 0.02


def test_a_recorder_that_was_slow_to_stop_is_still_covered(tmp_path):
    """The stop marker is 7 s before the end of the file (the recorder took that long to
    shut down): the clock says where it is, and the tail window reaches back to it."""
    raw = tmp_path / "raw.mkv"
    _clip(raw, 20 * 60, [(30, 37), (780, 787)])           # markers at 0.5 s and 13.0 s of 20 s
    full = sync.detect_markers(raw, 1.0, 60.0)
    assert len(full) == 2
    assert _close(sync.detect_markers_bounded(raw, 1.0, 60.0, head_until=0.5, stop_at=13.0, duration=20.0), full)
    assert len(sync.detect_markers_bounded(raw, 1.0, 60.0, head_until=0.5)) == 1      # not without the clock

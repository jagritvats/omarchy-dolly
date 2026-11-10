"""sync.fit picks the marker pairing the clock agrees with (a lone stop marker is not the start)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import sync  # noqa: E402

LOG = [0.4984, 26.6552]          # start, stop (logger seconds) of a real VM recording


def test_lone_stop_marker_is_paired_with_the_stop_log_mark():
    f = sync.fit(LOG, [(29.85, 30.083)], 60.0, clock_a=3.266)
    assert f.method == "markers1" and abs(f.a - (29.85 - 0.5 / 60 - 26.6552)) < 1e-6
    assert abs(f.a - 3.266) < 0.2


def test_lone_start_marker_still_works():
    f = sync.fit(LOG, [(3.7, 3.85)], 60.0, clock_a=3.266)
    assert f.method == "markers1" and abs(f.a - (3.7 - 0.5 / 60 - 0.4984)) < 1e-6


def test_marker_that_matches_nothing_falls_back_to_the_clock():
    f = sync.fit(LOG, [(12.0, 12.1)], 60.0, clock_a=3.266)       # a magenta thing in the video
    assert f.method == "clock" and f.a == 3.266


def test_two_markers_unchanged():
    f = sync.fit(LOG, [(3.7, 3.85), (29.85, 30.08)], 60.0, clock_a=3.266)
    assert f.method == "markers2"


def test_without_clock_keeps_old_behaviour():
    f = sync.fit(LOG, [(3.7, 3.85)], 60.0)
    assert f.method == "markers1" and abs(f.a - (3.7 - 0.5 / 60 - 0.4984)) < 1e-6

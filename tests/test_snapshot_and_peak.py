"""Two small measurements done with the standard library alone: how much of the screen
changed between two snapshots (a storyboard's `wait = "quiet"`), and the level of a block
of sound (the panel's meters)."""
import random
import stat
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import audio  # noqa: E402
from dolly.inject import Desktop, changed_fraction, snapshot  # noqa: E402


def pixels(n, rng, lo=0, hi=255):
    return bytes(rng.randint(lo, hi) for _ in range(3 * n))


def one_by_one(a, b, n, thresh):
    """The definition, pixel by pixel: the mean of red, green and blue differs by more than thresh."""
    return sum(abs(sum(a[3 * i:3 * i + 3]) - sum(b[3 * i:3 * i + 3])) > 3 * thresh for i in range(n)) / n


@pytest.mark.parametrize("thresh", [0, 12, 40, 200])
def test_changed_fraction_is_the_count_of_changed_pixels(thresh):
    rng = random.Random(thresh)
    w, h = 37, 23
    a = pixels(w * h, rng)
    for b in (pixels(w * h, rng),                                             # another picture
              bytes(min(255, max(0, v + rng.randint(-20, 20))) for v in a),   # the same one, a little off
              a):
        assert changed_fraction(snapshot(w, h, a), snapshot(w, h, b), thresh) == one_by_one(a, b, w * h, thresh)


def test_the_threshold_is_exact_and_pixels_do_not_spill_into_their_neighbours():
    grey = lambda v, n=1: bytes([v, v, v]) * n  # noqa: E731
    base = snapshot(4, 1, grey(50, 4))
    assert changed_fraction(base, snapshot(4, 1, grey(50, 4))) == 0.0
    assert changed_fraction(base, snapshot(4, 1, grey(62, 4))) == 0.0                 # 12 levels: not more than 12
    assert changed_fraction(base, snapshot(4, 1, grey(62, 3) + bytes([62, 62, 63]))) == 0.25
    assert changed_fraction(base, snapshot(4, 1, grey(38, 4))) == 0.0                 # darker counts the same way
    assert changed_fraction(base, snapshot(4, 1, bytes([38, 38, 37]) + grey(38, 3))) == 0.25
    # the largest differences there are, side by side, in both directions
    a, b = grey(0) + grey(255) + grey(0) + grey(255), grey(255) + grey(0) + grey(0) + grey(255)
    assert changed_fraction(snapshot(4, 1, a), snapshot(4, 1, b)) == 0.5
    assert changed_fraction(snapshot(2, 2, a), snapshot(2, 2, b), 254) == 0.5
    assert changed_fraction(snapshot(2, 2, a), snapshot(2, 2, b), 255) == 0.0


def test_what_cannot_be_compared_counts_as_changed():
    a = snapshot(2, 2, bytes(12))
    assert changed_fraction(a, None) == 1.0 and changed_fraction(None, a) == 1.0
    assert changed_fraction(a, snapshot(4, 1, bytes(12))) == 1.0                       # another shape
    assert changed_fraction(snapshot(0, 0, b""), snapshot(0, 0, b"")) == 1.0
    with pytest.raises(ValueError):
        snapshot(2, 2, bytes(11))                                                      # a pixel short


def test_screen_sample_reads_what_grim_prints(tmp_path, monkeypatch):
    shot = tmp_path / "shot.ppm"
    grim = tmp_path / "bin" / "grim"
    grim.parent.mkdir()
    grim.write_text(f'#!/bin/sh\nprintf \'%s \' "$@" > "{tmp_path}/args"\ncat "{shot}"\n')
    grim.chmod(grim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{grim.parent}:/usr/bin:/bin")
    rng = random.Random(3)
    w, h = 48, 27
    first = pixels(w * h, rng, 0, 60)                     # dark, line-end bytes (10) among the pixels
    shot.write_bytes(b"P6\n%d %d\n255\n" % (w, h) + first)
    desk = Desktop()
    a = desk.screen_sample((10, 20, 192, 108))
    assert a[:2] == (w, h) and (tmp_path / "args").read_text().split() == ["-t", "ppm", "-s", "0.25", "-g", "10,20", "192x108", "-"]
    second = bytearray(first)
    second[3 * 100:3 * 400] = bytes([200]) * 900          # 300 of the 1296 pixels light up
    shot.write_bytes(b"P6\n%d %d\n255\n" % (w, h) + bytes(second))
    b = desk.screen_sample((10, 20, 192, 108))
    assert changed_fraction(a, b) == 300 / (w * h)
    assert changed_fraction(b, desk.screen_sample((10, 20, 192, 108))) == 0.0
    shot.write_bytes(b"P6\n%d %d\n255\n" % (w, h) + first[:-1])
    assert desk.screen_sample() is None                   # cut short: no snapshot, the caller waits instead
    shot.write_bytes(b"")
    assert desk.screen_sample() is None


def test_peak_of_a_block_of_sound():
    block = lambda *v: struct.pack("<%dh" % len(v), *v)  # noqa: E731
    assert audio.peak(b"") == 0.0
    assert audio.peak(block(0, 0, 0)) == 0.0
    assert audio.peak(block(100, -8192, 4096)) == 0.25                 # the loudest, whichever way it points
    assert audio.peak(block(16384, -3)) == 0.5
    assert audio.peak(block(32767)) == 32767 / 32768
    assert audio.peak(block(-32768, 5)) == 1.0
    assert audio.peak(block(1000, 2000) + b"\x7f") == 2000 / 32768     # a block cut off in the middle of a sample

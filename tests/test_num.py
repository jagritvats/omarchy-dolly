"""dolly/num.py: the list helpers that stand in for NumPy in the camera planning.

Each expectation is worked out by hand or is what NumPy's documentation says the call
gives; the comments name the NumPy call. Checked against NumPy 2.5 itself when the module
was written (random and awkward inputs, a throwaway script): arange, linspace, interp,
percentile, clip, roll, sign, running_max and logaddexp gave the same bits, and gauss_smooth differed by at most 3e-15
of the value (NumPy adds the products in another order).
"""
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dolly import num

ROOT = Path(__file__).resolve().parents[1]
NAN, INF = math.nan, math.inf


def close(a, b, tol=1e-12):
    assert len(a) == len(b), (len(a), len(b))
    for x, y in zip(a, b):
        assert abs(x - y) <= tol * max(1.0, abs(y)), (x, y)


# ------------------------------------------------------------------ ranges
def test_arange_counts_like_numpy():
    assert num.arange(3) == [0.0, 1.0, 2.0]                       # np.arange(3.0)
    assert num.arange(1.0, 2.0, 0.25) == [1.0, 1.25, 1.5, 1.75]   # stop is not included
    assert num.arange(0.0, 1.0, 0.3) == [0.0, 0.3, 0.6, 0.8999999999999999]   # ceil(1 / 0.3) = 4 values
    assert num.arange(2.0, 2.0, 0.1) == [] and num.arange(3.0, 2.0, 0.1) == []
    assert num.arange(5.0, 3.0, -1.0) == [5.0, 4.0]
    assert num.arange(0.5, 0.6, 1.0) == [0.5]


def test_arange_rounds_like_numpy():
    # NumPy takes its step from the first two values: value i is start + i * ((start + step) - start)
    got = num.arange(0.1, 1.0, 0.1)
    d = (0.1 + 0.1) - 0.1
    assert got == [0.1, 0.1 + 0.1] + [0.1 + i * d for i in range(2, 9)]
    assert len(got) == 9
    # a grid of the camera simulation, 240 steps a second from 3.7 s. The count is the
    # division rounded up, and that division can land a hair above a whole number: NumPy
    # gave 101 values here, not 100, and so does this
    t = num.arange(3.7, 3.7 + 100 / 240, 1 / 240)
    assert len(t) == math.ceil((3.7 + 100 / 240 - 3.7) / (1 / 240)) == 101
    assert t[0] == 3.7 and abs(t[99] - (3.7 + 99 / 240)) < 1e-12
    assert len(num.arange(0.0, 2.0, 0.5)) == 4 and len(num.arange(0.0, 2.0 + 1e-9, 0.5)) == 5


def test_linspace_ends_exactly():
    assert num.linspace(0.0, 1.0, 5) == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert num.linspace(2.0, 3.0, 1) == [2.0] and num.linspace(2.0, 3.0, 0) == []
    v = num.linspace(0.0, 0.3, 4)
    assert v[-1] == 0.3 and v[1] == 1 * (0.3 / 3) and len(v) == 4
    assert num.linspace(1.0, -1.0, 3) == [1.0, 0.0, -1.0]


# ------------------------------------------------------------------ single values
def test_clip_at_and_beyond_the_ends():
    assert num.clip(0.5, 0.0, 1.0) == 0.5
    assert num.clip(-2.0, 0.0, 1.0) == 0.0 and num.clip(7.0, 0.0, 1.0) == 1.0
    assert num.clip(0.0, 0.0, 1.0) == 0.0 and num.clip(1.0, 0.0, 1.0) == 1.0
    assert num.clip(0.5, 2.0, 1.0) == 1.0              # np.clip with lo > hi gives hi
    assert math.isnan(num.clip(NAN, 0.0, 1.0))         # and passes NaN through
    assert num.clip(INF, 0.0, 1.0) == 1.0 and num.clip(-INF, 0.0, 1.0) == 0.0


def test_sign():
    assert [num.sign(v) for v in (-3.5, 0.0, 2.0, -0.0)] == [-1.0, 0.0, 1.0, 0.0]
    assert math.isnan(num.sign(NAN))
    assert num.sign(NAN) != num.sign(NAN)               # so "same direction" is False for unknown speeds


def test_logaddexp_does_not_overflow():
    assert num.logaddexp(0.0, 0.0) == math.log(2.0)
    assert abs(num.logaddexp(1.0, 2.0) - math.log(math.exp(1.0) + math.exp(2.0))) < 1e-15
    assert num.logaddexp(0.0, 1000.0) == 1000.0 and num.logaddexp(1000.0, 0.0) == 1000.0    # exp(1000) overflows
    assert num.logaddexp(0.0, -1000.0) == 0.0
    assert math.isnan(num.logaddexp(NAN, 1.0))
    # zoom._softclamp: w * logaddexp(0, v / w) is v far above 0 and 0 far below
    w = 0.5
    assert abs(w * num.logaddexp(0.0, 40.0 / w) - 40.0) < 1e-12 and w * num.logaddexp(0.0, -40.0 / w) < 1e-30


# ------------------------------------------------------------------ look-up

def test_interp_between_at_and_beyond_the_points():
    xp, fp = [0.0, 1.0, 3.0], [10.0, 20.0, 0.0]
    assert num.interp(0.5, xp, fp) == 15.0
    assert num.interp(2.0, xp, fp) == 10.0
    assert num.interp(0.0, xp, fp) == 10.0 and num.interp(1.0, xp, fp) == 20.0 and num.interp(3.0, xp, fp) == 0.0
    assert num.interp(-5.0, xp, fp) == 10.0 and num.interp(99.0, xp, fp) == 0.0       # held at the ends
    assert num.interp(-INF, xp, fp) == 10.0 and num.interp(INF, xp, fp) == 0.0
    assert math.isnan(num.interp(NAN, xp, fp))
    assert num.interp([0.5, 99.0, 1.0], xp, fp) == [15.0, 0.0, 20.0]                  # a sequence gives a list
    assert num.interp([], xp, fp) == []
    assert isinstance(num.interp(1, [0, 2], [0, 4]), float) and num.interp(1, [0, 2], [0, 4]) == 2.0


def test_interp_edge_cases_of_numpy():
    assert num.interp(5.0, [2.0], [7.0]) == 7.0 and num.interp(NAN, [2.0], [7.0]) == 7.0   # one point: always it
    # a repeated x (a cut in a track): the value after the step at it, the line before it
    assert num.interp(1.0, [0.0, 1.0, 1.0, 2.0], [0.0, 10.0, 50.0, 60.0]) == 50.0
    assert num.interp(0.5, [0.0, 1.0, 1.0, 2.0], [0.0, 10.0, 50.0, 60.0]) == 5.0
    # an infinite value next to a finite one does not turn its whole stretch into NaN
    assert num.interp(0.0, [0.0, 1.0], [3.0, INF]) == 3.0
    assert num.interp(0.5, [0.0, 1.0], [3.0, INF]) == INF
    with pytest.raises(ValueError):
        num.interp(1.0, [], [])
    # a NaN in fp (no subject at that step) only spoils its own two stretches
    got = num.interp([0.5, 1.5, 2.5], [0.0, 1.0, 2.0, 3.0], [1.0, NAN, 3.0, 4.0])
    assert math.isnan(got[0]) and math.isnan(got[1]) and got[2] == 3.5


def test_interp_resamples_a_track_as_the_planner_does():
    # 240 Hz simulation read at 60 fps output times
    sim_t = [i / 240 for i in range(481)]
    z = [1.0 + 0.5 * t for t in sim_t]
    times = [i / 60 for i in range(120)]
    got = num.interp(times, sim_t, z)
    close(got, [1.0 + 0.5 * t for t in times])
    assert num.interp(2.5, sim_t, z) == z[-1]


def test_percentile_is_numpys_linear_one():
    a = [15.0, 20.0, 35.0, 40.0, 50.0]
    assert num.percentile(a, 0) == 15.0 and num.percentile(a, 100) == 50.0
    assert num.percentile(a, 50) == 35.0
    assert abs(num.percentile(a, 40) - 29.0) < 1e-12  # 0.4 * 4 = 1.6 -> 20 + 0.6 * 15
    assert abs(num.percentile(a, 10) - 17.0) < 1e-12  # 0.4 of the way from 15 to 20
    assert num.percentile([3.0, 1.0, 2.0], 50) == 2.0                 # order does not matter
    assert num.percentile([7.0], 3) == 7.0
    assert math.isnan(num.percentile([], 10))
    assert num.percentile([1.0, 2.0], 75) == 1.75
    assert num.percentile((v for v in (4.0, 2.0)), 50) == 3.0          # any iterable


# ------------------------------------------------------------------ whole lists
def test_roll():
    a = [0, 1, 2, 3, 4]
    assert num.roll(a, 2) == [3, 4, 0, 1, 2]          # np.roll(a, 2)
    assert num.roll(a, -1) == [1, 2, 3, 4, 0]
    assert num.roll(a, 0) == a and num.roll(a, 5) == a and num.roll(a, 7) == [3, 4, 0, 1, 2]
    assert num.roll([], 3) == [] and a == [0, 1, 2, 3, 4]


def test_gauss_smooth_boundaries_are_those_of_the_old_one():
    a = [0.0, 0.0, 10.0, 0.0, 0.0]
    assert num.gauss_smooth(a, 0.49) == a and num.gauss_smooth(a, 0.49) is not a        # under 0.5: left alone
    assert num.gauss_smooth([1.0, 5.0], 3.0) == [1.0, 5.0]                              # under 3 values: left alone
    # sigma 0.5: radius ceil(1.5) = 2, weights exp(-2 i^2) / their sum
    k = [math.exp(-2.0 * i * i) for i in (-2, -1, 0, 1, 2)]
    k = [v / sum(k) for v in k]
    close(num.gauss_smooth(a, 0.5), [10 * k[4], 10 * k[3], 10 * k[2], 10 * k[1], 10 * k[0]])
    # the ends are held (np.pad mode="edge"): a step keeps its levels, a constant stays constant
    assert num.gauss_smooth([3.0] * 9, 2.0) == pytest.approx([3.0] * 9, abs=1e-12)
    step = num.gauss_smooth([0.0] * 30 + [1.0] * 30, 2.0)
    assert step[0] == pytest.approx(0.0, abs=1e-9) and step[-1] == pytest.approx(1.0, abs=1e-9)
    assert step[29] + step[30] == pytest.approx(1.0, abs=1e-12) and all(y >= x for x, y in zip(step, step[1:]))
    # the first value: weights of everything left of it land on a[0]
    ramp = [float(i) for i in range(10)]
    r = 2
    k = [math.exp(-0.5 * (i / 0.6) ** 2) for i in range(-r, r + 1)]
    k = [v / sum(k) for v in k]
    assert num.gauss_smooth(ramp, 0.6)[0] == pytest.approx(k[3] * 1.0 + k[4] * 2.0, abs=1e-12)
    assert num.gauss_smooth(ramp, 0.6)[5] == pytest.approx(5.0, abs=1e-12)              # a line stays a line inside


def test_gauss_smooth_keeps_length_and_input():
    a = [math.sin(i / 7) for i in range(500)]
    before = list(a)
    out = num.gauss_smooth(a, 28.8)                   # 0.12 s at 240 Hz: the pan target's low-pass
    assert len(out) == 500 and a == before
    assert max(out) < max(a) and min(out) > min(a)


def test_running_max_windows():
    a = [1.0, 3.0, 2.0, 5.0, 4.0]
    assert num.running_max(a, 1, 1) == [3.0, 3.0, 5.0, 5.0, 5.0]
    assert num.running_max(a, 0, 0) == a and num.running_max([], 2, 2) == []
    assert num.running_max(a, 0, 2) == [3.0, 5.0, 5.0, 5.0, 4.0]         # looking ahead only
    assert num.running_max(a, 2, 0) == [1.0, 3.0, 3.0, 5.0, 5.0]         # looking back only
    assert num.running_max(a, 10, 10) == [5.0] * 5                       # wider than the list
    assert num.running_max([2.0], 3, 4) == [2.0]
    # zoom._running_max(a, w): pad (w // 2, w - 1 - w // 2); for w = 4 that is 2 back, 1 ahead
    assert num.running_max(a, 4 // 2, 4 - 1 - 4 // 2) == [3.0, 3.0, 5.0, 5.0, 5.0]


def test_running_max_against_the_plain_definition():
    import random
    rnd = random.Random(3)
    a = [rnd.random() for _ in range(257)]
    for before, after in ((0, 1), (3, 0), (36, 35), (72, 180), (5, 300)):
        want = [max(a[max(0, i - before):i + after + 1]) for i in range(len(a))]
        assert num.running_max(a, before, after) == want, (before, after)


def test_nothing_changes_its_argument_or_needs_numpy():
    a = [3.0, 1.0, 2.0]
    for fn in (lambda: num.percentile(a, 50), lambda: num.gauss_smooth(a, 1.0), lambda: num.running_max(a, 1, 1),
               lambda: num.roll(a, 1)):
        fn()
        assert a == [3.0, 1.0, 2.0]
    assert num.interp(1.5, (1.0, 2.0), (10.0, 20.0)) == 15.0             # tuples are fine too
    src = (ROOT / "dolly" / "num.py").read_text()
    assert "import numpy" not in src and "from numpy" not in src


# ------------------------------------------------------------------ without NumPy and Pillow
def test_num_and_gfx_import_where_numpy_and_pillow_are_missing():
    code = ("import sys\n"
            "from dolly import num, gfx\n"
            "assert num.interp(0.5, [0.0, 1.0], [0.0, 2.0]) == 1.0\n"
            "for m in ('numpy', 'PIL'):\n"
            "    try:\n"
            "        __import__(m)\n"
            "    except ImportError:\n"
            "        continue\n"
            "    raise SystemExit(m + ' was importable')\n"
            "print('ok', gfx.available())\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "tests" / "no_numpy"), str(ROOT)]))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0 and r.stdout.startswith("ok"), r.stderr[-600:]

"""The few numeric helpers the camera planning needs, on plain lists of floats.

They stand in for the NumPy calls zoom.py, reframe.py and timemap.py made, and behave the
same at the edges (the docstrings say how), so the plan a recording gets does not change.
Lists go in and new lists come out; nothing here changes its argument. What had no trap in
it is not here: np.zeros(n) is [0.0] * n, np.where a comprehension, np.exp math.exp.

Two things NumPy did that plain Python does not, to keep in mind where values can be NaN:
min() and max() are not symmetric (max(nan, 1) is nan, max(1, nan) is 1), and a NaN breaks
sorting. clip() and interp() below pass a NaN through as NumPy did; running_max() and
percentile() want none. A sorted look-up is bisect, a running total itertools.accumulate.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from itertools import accumulate

try:
    from math import sumprod as _dot          # Python 3.12
except ImportError:                            # pragma: no cover
    from operator import mul

    def _dot(a, b):
        return sum(map(mul, a, b))


# ------------------------------------------------------------------ ranges
def arange(start: float, stop: float | None = None, step: float = 1.0) -> list:
    """np.arange for floats: start, start + step, ... up to but not including stop;
    ceil((stop - start) / step) values, none when that is not positive. As NumPy, value i
    is start + i * d with d = (start + step) - start, which is what its last digits are."""
    if stop is None:
        start, stop = 0.0, start
    n = math.ceil((stop - start) / step)
    if n <= 0:
        return []
    start = float(start)
    if n == 1:
        return [start]
    second = start + step
    d = second - start
    return [start, second] + [start + i * d for i in range(2, n)]


def linspace(start: float, stop: float, n: int) -> list:
    """np.linspace: n values from start to stop, both included (the last is exactly stop)."""
    if n <= 0:
        return []
    if n == 1:
        return [float(start)]
    step = (stop - start) / (n - 1)
    out = [i * step + start for i in range(n)]
    out[-1] = float(stop)
    return out


# ------------------------------------------------------------------ single values
def clip(x: float, lo: float, hi: float) -> float:
    """np.clip for one value: lo below lo, hi above hi (hi when lo > hi); NaN stays NaN."""
    return min(max(x, lo), hi)


def sign(x: float) -> float:
    """np.sign: -1.0, 0.0 or 1.0, and NaN for NaN."""
    if x != x:
        return math.nan
    return float((x > 0) - (x < 0))


def logaddexp(a: float, b: float) -> float:
    """np.logaddexp: log(exp(a) + exp(b)) without overflow for large values."""
    if a == b:
        return a + math.log(2.0)
    d = a - b
    if d > 0:
        return a + math.log1p(math.exp(-d))
    if d <= 0:
        return b + math.log1p(math.exp(d))
    return d                                        # NaN


# ------------------------------------------------------------------ look-up
def interp(x, xp, fp):
    """np.interp: the line through the points (xp, fp), xp rising, read at x (one value
    gives a float, a sequence a list). Before the first point it is fp[0] and after the
    last fp[-1]; at a point it is exactly that point's value, at a repeated xp the last
    one's; a NaN x gives NaN."""
    n = len(xp)
    if n == 0:
        raise ValueError("interp needs at least one point")
    one = not hasattr(x, "__iter__")
    first, last, f0, f1 = xp[0], xp[-1], float(fp[0]), float(fp[-1])
    out = []
    for v in ((x,) if one else x):
        if n == 1:
            r = f0
        elif v != v:
            r = math.nan
        elif v > last:
            r = f1
        elif v < first:
            r = f0
        else:
            j = bisect_right(xp, v) - 1
            if j == n - 1 or xp[j] == v:
                r = float(fp[j])
            else:
                slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j])
                r = slope * (v - xp[j]) + fp[j]
                if r != r:                           # an infinite neighbour: come from the other side
                    r = slope * (v - xp[j + 1]) + fp[j + 1]
                    if r != r and fp[j] == fp[j + 1]:
                        r = float(fp[j])
        out.append(r)
    return out[0] if one else out


def percentile(a, q: float) -> float:
    """np.percentile with its default method: the value q percent of the way through the
    sorted values, on the line between the two it falls between. NaN for no values."""
    s = sorted(a)
    n = len(s)
    if n == 0:
        return math.nan
    at = (n - 1) * (q / 100.0)
    lo = min(max(math.floor(at), 0), n - 1)
    hi = min(lo + 1, n - 1)
    t = at - lo
    d = s[hi] - s[lo]
    return float(s[hi] - d * (1 - t) if t >= 0.5 else s[lo] + d * t)


# ------------------------------------------------------------------ whole lists
def roll(a, k: int) -> list:
    """np.roll: every value moved k places to the right, the ones pushed off the end coming
    back in at the start (a negative k moves left)."""
    a = list(a)
    if not a:
        return a
    k %= len(a)
    return a[-k:] + a[:-k] if k else a


def gauss_smooth(a, sigma: float) -> list:
    """Zero-phase Gaussian smoothing of a track, `sigma` in samples, as zoom.py did it
    with NumPy: a kernel 3 sigma to each side, the track held at its first and last value
    beyond its ends. Left as it is (a copy) when sigma < 0.5 or it has fewer than 3 values."""
    a = list(a)
    n = len(a)
    if sigma < 0.5 or n < 3:
        return a
    r = int(math.ceil(3 * sigma))
    k = [math.exp(-0.5 * (i / sigma) ** 2) for i in range(-r, r + 1)]
    total = math.fsum(k)
    k = [v / total for v in k]
    p = [a[0]] * r + a + [a[-1]] * r
    m = 2 * r + 1
    return [_dot(k, p[i:i + m]) for i in range(n)]


def running_max(a, before: int, after: int) -> list:
    """For every i the largest of a[i - before] .. a[i + after], the list held at its ends:
    what sliding_window_view(np.pad(a, (before, after), mode="edge"), before + after + 1)
    .max(axis=1) gave. zoom._running_max(a, w) is running_max(a, w // 2, w - 1 - w // 2)."""
    a = list(a)
    n, w = len(a), before + after + 1
    if w <= 1 or n == 0:
        return a
    p = [a[0]] * before + a + [a[-1]] * after
    # largest so far within blocks of w, from the left and from the right: a window is the
    # end of one block and the start of the next
    fwd, back = [], []
    for i in range(0, len(p), w):
        block = p[i:i + w]
        fwd += accumulate(block, max)
        back += list(accumulate(reversed(block), max))[::-1]
    return list(map(max, back[:n], fwd[w - 1:w - 1 + n]))

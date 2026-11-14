"""Small grey pictures and masks for the screen analysis, as plain bytes.

A picture here is a bytes object, one byte a pixel, row after row; a mask is such a picture
with 255 where it is set and 0 elsewhere. ffmpeg makes them, scaled down, and marks what
changed between two of them (aoi.py, timemap.py: read() below takes them off its pipe).

Nothing here loops over pixels in Python. Each step is one call that runs over the whole
buffer in C: bytes.count and bytes.find, a regular expression, a table looked up for every
pixel, or the arithmetic of Python's integers, which are as long as they need to be (a
mask read as one number can be shifted, and two of them combined, in one operation).
Python itself only handles what comes out: a count, the runs of set pixels, the regions.

No NumPy, no cairo: the analysis runs wherever ffmpeg does.
"""
from __future__ import annotations

import re
import subprocess
import sys
from functools import lru_cache

SET = 255


def read(cmd: list, size: int):
    """What an ffmpeg command writes to its standard output, `size` bytes at a time (one
    picture, or several stacked), without ever holding more than one. No timeout: such a
    command covers a whole recording and ends with the file. An ffmpeg that fails before
    its first picture raises; a recording with a torn end gives what could be read."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    n = 0
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            n += 1
            yield buf
    finally:
        p.stdout.close()
        err = p.stderr.read()
        p.stderr.close()
        rc = p.wait()
    if rc != 0 and n == 0:
        raise subprocess.CalledProcessError(rc, cmd, stderr=err)


# ------------------------------------------------------------------ masks together
def either(a: bytes, b: bytes) -> bytes:
    """The pixels set in `a` or in `b`."""
    return (int.from_bytes(a, "little") | int.from_bytes(b, "little")).to_bytes(len(a), "little")


def without(a: bytes, b: bytes) -> bytes:
    """The pixels set in `a` and not in `b`."""
    return (int.from_bytes(a, "little") & ~int.from_bytes(b, "little")).to_bytes(len(a), "little")


def count(mask: bytes, w: int, x: int, y: int, bw: int, bh: int) -> int:
    """How many pixels of the rectangle (x, y, bw, bh) are set."""
    return sum(mask.count(SET, r * w + x, r * w + x + bw) for r in range(y, y + bh))


# ------------------------------------------------------------------ two pictures, pixel by pixel
def pairs(a: bytes, b: bytes):
    """For every pixel the number a + 256 * b: two pictures as one sequence, so that
    anything that depends on both values of a pixel is a table of 65536 entries
    (look_up) or a tally of the numbers that occur (collections.Counter)."""
    buf = bytearray(2 * len(a))
    lo = 0 if sys.byteorder == "little" else 1
    buf[lo::2], buf[1 - lo::2] = a, b
    return memoryview(buf).cast("H")


def look_up(table, both) -> bytes:
    """The picture whose pixels are table[a + 256 * b] (`both` comes from pairs())."""
    return bytes(map(table.__getitem__, both))


@lru_cache(maxsize=4)
def _apart(level: int) -> bytes:
    return bytes(SET if abs((i & 255) - (i >> 8)) > level else 0 for i in range(65536))


def differ(a: bytes, b: bytes, level: int) -> bytes:
    """Mask of the pixels where two pictures are more than `level` apart. For a whole
    recording ffmpeg does this (tblend and lut); this is for the odd pair of pictures."""
    return look_up(_apart(int(level)), pairs(a, b))


def luma(bgr0) -> bytes:
    """The grey of a colour picture given as ffmpeg's bgr0 (four bytes a pixel: what
    gfx.data() hands out): 0.299 red + 0.587 green + 0.114 blue, rounded. Read as one
    number the picture has a pixel in every 32 bits, so each colour is weighed for all
    pixels at once; the weights are scaled by 2 ** 24 and the grey is the top byte."""
    n = len(bgr0) // 4
    one = int.from_bytes(b"\xff\0\0\0" * n, "little")           # the lowest byte of every pixel
    v = int.from_bytes(bgr0, "little")
    s = (v & one) * 1912603 + (v >> 8 & one) * 9848226 + (v >> 16 & one) * 5016388 + (one // 255 << 23)
    return s.to_bytes(4 * n, "little")[3::4]


# ------------------------------------------------------------------ growing, joining, regions
def _spread(x: int, w: int, h: int, reach: int) -> int:
    """A mask (as one number) with its set pixels grown by `reach` in all four directions."""
    out = x
    for k in range(1, reach + 1):
        # all but the last k pixels of every row: nothing may cross from one row into the next
        inner = int.from_bytes((b"\xff" * (w - k) + bytes(k)) * h, "little")
        out |= ((x & inner) << 8 * k) | ((x >> 8 * k) & inner)
    x, row, whole = out, 8 * w, (1 << 8 * w * h) - 1
    for k in range(1, reach + 1):
        out |= ((x << row * k) & whole) | (x >> row * k)
    return out


def close_gaps(mask: bytes, w: int, h: int, reach: int) -> bytes:
    """Join set pixels that are no more than 2 * reach apart: grow by `reach`, then shrink
    back by as much (the edge of the picture does not eat into what touches it)."""
    whole = (1 << 8 * w * h) - 1
    x = _spread(int.from_bytes(mask, "little"), w, h, reach)
    return (whole ^ _spread(whole ^ x, w, h, reach)).to_bytes(w * h, "little")


@lru_cache(maxsize=8)
def _runs(gap: int):
    return re.compile(b"\xff+(?:\\x00{1,%d}\xff+)*" % gap if gap else b"\xff+")


def regions(mask: bytes, w: int, h: int, reach: int = 0) -> list:
    """The connected regions of a mask (pixels touching at a side or a corner), as
    [(x, y, width, height, area)] in the order in which reading row by row meets them.
    With `reach`, every set pixel first counts `reach` pixels wider to the left and to the
    right, so the letters of a word, and the words of a line, come out as one region.

    The set pixels are found as runs per row (a regular expression that also steps over
    gaps of up to 2 * reach); runs of neighbouring rows that touch are joined."""
    find = _runs(2 * reach).finditer
    row, x0, x1 = [], [], []            # of every run: its row, its first pixel, one past its last
    lines = []                          # (row, its first run, one past its last run)
    at = mask.find(SET)
    while at >= 0:
        y = at // w
        base, n = y * w, len(row)
        for m in find(mask, base, base + w):
            row.append(y)
            x0.append(max(0, m.start() - base - reach))
            x1.append(min(w, m.end() - base + reach))
        lines.append((y, n, len(row)))
        at = mask.find(SET, base + w)
    parent = list(range(len(row)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for (ya, a, a1), (yb, b, b1) in zip(lines, lines[1:]):
        if yb != ya + 1:
            continue
        while a < a1 and b < b1:
            if x0[a] <= x1[b] and x0[b] <= x1[a]:          # they overlap, or meet at a corner
                ra, rb = root(a), root(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
            if x1[a] < x1[b]:
                a += 1
            else:
                b += 1
    found, out = {}, []
    for i, y in enumerate(row):
        r = root(i)
        c = found.get(r)
        if c is None:
            c = found[r] = [x0[i], y, x1[i], y, 0]
            out.append(c)
        c[0], c[2], c[3] = min(c[0], x0[i]), max(c[2], x1[i]), y + 1
        c[4] += x1[i] - x0[i]
    return [(c[0], c[1], c[2] - c[0], c[3] - c[1], c[4]) for c in out]

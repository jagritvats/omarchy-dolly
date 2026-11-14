"""dolly/grid.py against the plain way of doing the same thing, pixel by pixel, on pictures
small enough for that."""
import random
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import grid  # noqa: E402


def mask(w, h, seed, share=0.3):
    rnd = random.Random(seed)
    return bytes(255 if rnd.random() < share else 0 for _ in range(w * h))


def test_masks_together_and_counting():
    a, b = mask(31, 17, 1), mask(31, 17, 2)
    assert grid.either(a, b) == bytes(x | y for x, y in zip(a, b))
    assert grid.without(a, b) == bytes(x & ~y & 255 for x, y in zip(a, b))
    assert grid.either(bytes(8), bytes(8)) == bytes(8) and grid.without(a, a) == bytes(len(a))    # leading zeros kept
    for x, y, bw, bh in ((0, 0, 31, 17), (5, 3, 9, 4), (30, 16, 1, 1), (0, 9, 31, 1)):
        plain = sum(a[r * 31 + c] == 255 for r in range(y, y + bh) for c in range(x, x + bw))
        assert grid.count(a, 31, x, y, bw, bh) == plain


def test_two_pictures_pixel_by_pixel():
    rnd = random.Random(3)
    a, b = rnd.randbytes(5000), rnd.randbytes(5000)
    both = grid.pairs(a, b)
    assert list(both) == [x + 256 * y for x, y in zip(a, b)]
    table = bytes((i * 7 + (i >> 8)) & 255 for i in range(65536))
    assert grid.look_up(table, both) == bytes(table[x + 256 * y] for x, y in zip(a, b))
    for level in (0, 10, 14, 200, 255):
        assert grid.differ(a, b, level) == bytes(255 if abs(x - y) > level else 0 for x, y in zip(a, b))
    edge = bytes([0, 14, 15, 255, 100]), bytes([14, 0, 0, 0, 115])
    assert grid.differ(*edge, 14) == bytes([0, 0, 255, 255, 255])


def test_luma_is_the_rounded_weighted_sum():
    rnd = random.Random(4)
    px = bytearray(rnd.randbytes(4 * 20000))
    px[:16] = bytes([0, 0, 0, 0, 255, 255, 255, 255, 255, 0, 0, 9, 0, 0, 255, 200])     # black, white, blue, red
    for v in range(256):                                                                 # every grey
        px[16 + 4 * v:20 + 4 * v] = bytes([v, v, v, rnd.randrange(256)])
    got = grid.luma(bytes(px))
    assert len(got) == 20000 and got[:4] == bytes([0, 255, 29, 76])
    assert got[4:260] == bytes(range(256))
    for i in range(20000):
        b, g, r = px[4 * i], px[4 * i + 1], px[4 * i + 2]            # the fourth byte never counts
        exact = 114 * b + 587 * g + 299 * r                          # thousandths of a level
        if exact % 1000 != 500:                                      # (a tie may round either way)
            assert got[i] == (exact + 500) // 1000, (b, g, r)
        assert abs(got[i] - exact / 1000) <= 0.5 + 1e-4
    assert grid.luma(memoryview(px)) == got                          # what gfx.data() hands out


def grown(m, w, h, rx, ry, outside=0):
    """Plain dilation: a pixel is set when any pixel within (rx, ry) of it is."""
    def at(x, y):
        return m[y * w + x] if 0 <= x < w and 0 <= y < h else outside
    return bytes(255 if any(at(x + dx, y + dy) for dy in range(-ry, ry + 1) for dx in range(-rx, rx + 1)) else 0
                 for y in range(h) for x in range(w))


def negative(m):
    return bytes(255 - v for v in m)


@pytest.mark.parametrize("w,h,reach,share", [(23, 11, 1, 0.2), (40, 30, 2, 0.1), (40, 30, 2, 0.5), (9, 9, 3, 0.15), (64, 3, 2, 0.3)])
def test_close_gaps_is_grow_then_shrink(w, h, reach, share):
    m = mask(w, h, w + reach, share)
    big = grown(m, w, h, reach, reach)
    plain = negative(grown(negative(big), w, h, reach, reach))       # shrinking: the negative grown; outside counts as set
    assert grid.close_gaps(m, w, h, reach) == plain
    assert grid.close_gaps(bytes(w * h), w, h, reach) == bytes(w * h)
    assert grid.close_gaps(b"\xff" * (w * h), w, h, reach) == b"\xff" * (w * h)      # the edge does not eat into it


def flood(m, w, h):
    """Plain region labelling: (x, y, width, height, area) in reading order of each region's first pixel."""
    seen, out = set(), []
    for start in range(w * h):
        if not m[start] or start in seen:
            continue
        todo, cells = [start], []
        seen.add(start)
        while todo:
            p = todo.pop()
            cells.append(p)
            x, y = p % w, p // w
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    q = (y + dy) * w + x + dx
                    if 0 <= x + dx < w and 0 <= y + dy < h and m[q] and q not in seen:
                        seen.add(q)
                        todo.append(q)
        xs, ys = [p % w for p in cells], [p // w for p in cells]
        out.append((min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, len(cells)))
    return out


@pytest.mark.parametrize("w,h,reach,share", [(30, 20, 0, 0.3), (30, 20, 0, 0.6), (48, 30, 4, 0.02), (48, 30, 4, 0.1),
                                             (17, 40, 1, 0.2), (60, 4, 2, 0.15), (5, 5, 0, 0.5)])
def test_regions_are_what_a_flood_fill_finds(w, h, reach, share):
    m = mask(w, h, 7 * w + h + reach, share)
    assert grid.regions(m, w, h, reach) == flood(grown(m, w, h, reach, 0), w, h)
    assert grid.regions(bytes(w * h), w, h, reach) == []


def test_regions_by_hand():
    def pic(*rows):
        return bytes(255 if c == "#" else 0 for r in rows for c in r)
    # corners touch; the two arms of a U are one region although they meet only in the last row
    assert grid.regions(pic("#..#",
                            ".#.#",
                            "...#"), 4, 3) == [(0, 0, 2, 2, 2), (3, 0, 1, 3, 3)]
    assert grid.regions(pic("#.#",
                            "#.#",
                            "###"), 3, 3) == [(0, 0, 3, 3, 7)]
    # the end of one row and the start of the next are not neighbours
    assert grid.regions(pic("..##",
                            "....",
                            "##.."), 4, 3) == [(2, 0, 2, 1, 2), (0, 2, 2, 1, 2)]
    assert grid.regions(pic("...#",
                            "#..."), 4, 2) == [(3, 0, 1, 1, 1), (0, 1, 1, 1, 1)]
    assert grid.regions(pic("...#",
                            "#..."), 4, 2, reach=1) == [(0, 0, 4, 2, 4)]         # widened by one, their corners meet
    # with a reach of 2, letters 4 apart are one word; the reach stops at the edge of the picture
    assert grid.regions(pic("#....#......#"), 13, 1, reach=2) == [(0, 0, 8, 1, 8), (10, 0, 3, 1, 3)]


def test_read_hands_out_whole_pictures_and_says_when_nothing_came():
    write = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes(range(10)))"]
    assert list(grid.read(write, 4)) == [bytes([0, 1, 2, 3]), bytes([4, 5, 6, 7])]          # the torn end is dropped
    with pytest.raises(subprocess.CalledProcessError) as e:
        list(grid.read([sys.executable, "-c", "import sys; sys.exit('no such file')"], 4))
    assert b"no such file" in e.value.stderr
    got = grid.read(write, 2)
    assert next(got) == bytes([0, 1])
    got.close()                                                      # stopped early: the pipe is closed, no error

"""dolly/gfx.py: the drawing layer on cairo (text through Pango).

No NumPy and no Pillow here: pictures are made with gfx itself or from plain bytes, and
read back with gfx.pixel / gfx.plane. The bounds are what was measured on Omarchy (cairo
1.18.6, Pango 1.58) when the module was written, with a margin; how its results compare
with the NumPy and Pillow renderer it replaces is in the module's docstrings.
"""
import ast
import io
import math
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from dolly import gfx

ROOT = Path(__file__).resolve().parents[1]
needs_cairo = pytest.mark.skipif(not gfx.available(), reason="python-cairo is not installed")
WHITE, BLACK = (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)


def flat(w, h, level, alpha=False):
    s = gfx.new(w, h, alpha)
    gfx.fill_rect(s, 0, 0, w, h, (level / 255, level / 255, level / 255))
    return s


def row(surface, y, channel="g"):
    w = surface.get_width()
    return list(gfx.plane(surface, channel)[y * w:(y + 1) * w])


def columns(w, h, levels):
    """A frame whose column x has grey level levels[x]."""
    buf = bytearray(4 * w * h)
    line = bytearray()
    for v in levels:
        line += bytes((v, v, v, 0))
    for y in range(h):
        buf[4 * w * y:4 * w * (y + 1)] = line
    return gfx.frame_from(buf, w, h)


def ffmpeg():
    from dolly.ff import ffmpeg_bin
    return ffmpeg_bin()


# ------------------------------------------------------------------ frames
@needs_cairo
def test_a_new_frame_is_black_and_a_new_sprite_is_clear():
    f, s = gfx.new(7, 5), gfx.new(7, 5, alpha=True)
    assert (f.get_width(), f.get_height()) == (7, 5) and not gfx.has_alpha(f) and gfx.has_alpha(s)
    assert gfx.pixel(f, 3, 2) == (0, 0, 0) and gfx.pixel(s, 3, 2) == (0, 0, 0, 0)
    assert len(gfx.data(f)) == 4 * 7 * 5


@needs_cairo
def test_a_frame_wraps_a_buffer_without_copying():
    buf = bytearray(4 * 6 * 4)
    f = gfx.frame_from(buf, 6, 4)
    gfx.fill_rect(f, 2, 1, 1, 1, gfx.rgb("#ff8040"))
    f.flush()
    assert bytes(buf[4 * (6 * 1 + 2):4 * (6 * 1 + 2) + 3]) == bytes((0x40, 0x80, 0xff))     # blue, green, red: bgr0
    buf[0:3] = bytes((10, 20, 30))                    # written from outside, as a decoder would
    gfx.changed(f)
    assert gfx.pixel(f, 0, 0) == (30, 20, 10)
    assert bytes(gfx.data(f)) == bytes(buf)
    with pytest.raises(ValueError):
        gfx.frame_from(bytearray(10), 6, 4)


@needs_cairo
def test_frames_come_from_a_pipe_and_go_to_one(tmp_path):
    two = bytes((1, 2, 3, 0)) * 12 + bytes((9, 8, 7, 0)) * 12 + b"\x05" * 7       # two 4x3 frames and a torn third
    stream = io.BytesIO(two)
    a, b = gfx.read_frame(stream, 4, 3), gfx.read_frame(stream, 4, 3)
    assert gfx.pixel(a, 0, 0) == (3, 2, 1) and gfx.pixel(b, 3, 2) == (7, 8, 9)
    assert gfx.read_frame(stream, 4, 3) is None and gfx.read_frame(stream, 4, 3) is None
    # a real decoder and a real encoder
    dec = subprocess.Popen([ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=5", "-frames:v", "3",
                            "-f", "rawvideo", "-pix_fmt", "bgr0", "-"], stdout=subprocess.PIPE)
    frames = []
    while True:
        f = gfx.read_frame(dec.stdout, 64, 48)
        if f is None:
            break
        frames.append(f)
    dec.wait()
    assert len(frames) == 3 and frames[0] is not frames[1]
    assert not gfx.same(frames[0], frames[2])                       # the test picture moves
    out = tmp_path / "frame.png"
    enc = subprocess.Popen([ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr0", "-s", "64x48", "-i", "-",
                            "-frames:v", "1", str(out)], stdin=subprocess.PIPE)
    enc.stdin.write(gfx.data(frames[1]))
    enc.stdin.close()
    assert enc.wait() == 0
    assert gfx.same(gfx.load(out), frames[1])


@needs_cairo
def test_copy_and_crop_are_pictures_of_their_own():
    f = columns(8, 4, [0, 10, 20, 30, 40, 50, 60, 70])
    c = gfx.copy(f)
    gfx.fill_rect(c, 0, 0, 8, 4, WHITE)
    assert gfx.pixel(f, 3, 1) == (30, 30, 30) and gfx.pixel(c, 3, 1) == (255, 255, 255)
    part = gfx.crop(f, 2, 1, 3, 2)
    assert (part.get_width(), part.get_height()) == (3, 2)
    assert [gfx.pixel(part, x, 0)[0] for x in range(3)] == [20, 30, 40]
    over = gfx.crop(f, 6, 0, 4, 2)                                   # past the right edge: black there
    assert [gfx.pixel(over, x, 0)[0] for x in range(4)] == [60, 70, 0, 0]


@needs_cairo
def test_reading_values_back():
    f = gfx.new(4, 2)
    gfx.fill_rect(f, 0, 0, 2, 2, gfx.rgb((200, 100, 50)))
    assert gfx.pixel(f, 1, 1) == (200, 100, 50) and gfx.pixel(f, 2, 1) == (0, 0, 0)
    assert gfx.plane(f, "r") == bytes((200, 200, 0, 0)) * 2 and gfx.plane(f, "b") == bytes((50, 50, 0, 0)) * 2
    assert gfx.mean(f) == (100.0, 50.0, 25.0)
    s = gfx.new(2, 2, alpha=True)
    gfx.fill_rect(s, 0, 0, 2, 2, WHITE, 0.5)
    assert gfx.pixel(s, 0, 0) == (128, 128, 128, 128)                # colours are stored multiplied by alpha
    assert gfx.mean(s) == (128.0, 128.0, 128.0, 128.0)
    assert gfx.rgb("#ff0080") == (1.0, 0.0, 128 / 255) and gfx.rgb((255, 0, 51)) == (1.0, 0.0, 0.2)


@needs_cairo
def test_same_looks_at_the_picture_only():
    a, b = bytearray(4 * 3 * 2), bytearray(4 * 3 * 2)
    for i in range(0, len(a), 4):
        a[i:i + 4] = bytes((1, 2, 3, 0))
        b[i:i + 4] = bytes((1, 2, 3, 255))                          # the unused byte differs
    fa, fb = gfx.frame_from(a, 3, 2), gfx.frame_from(b, 3, 2)
    assert gfx.same(fa, fb)
    b[4] = 9
    gfx.changed(fb)
    assert not gfx.same(fa, fb)
    assert not gfx.same(gfx.new(3, 2), gfx.new(2, 3)) and not gfx.same(gfx.new(3, 2), gfx.new(3, 2, alpha=True))


# ------------------------------------------------------------------ files
@needs_cairo
def test_png_goes_out_and_comes_back_the_same(tmp_path):
    f = columns(37, 11, [(x * 7) % 256 for x in range(37)])
    gfx.fill_rect(f, 5, 2, 9, 4, gfx.rgb("#3070f0"))
    gfx.save(f, tmp_path / "f.png")
    assert gfx.image_size(tmp_path / "f.png") == (37, 11)
    back = gfx.load(tmp_path / "f.png")
    assert not gfx.has_alpha(back) and gfx.same(back, f)
    # another reader agrees with what was written
    raw = subprocess.run([ffmpeg(), "-v", "error", "-i", str(tmp_path / "f.png"), "-f", "rawvideo", "-pix_fmt", "bgr0", "-"],
                         capture_output=True).stdout
    assert gfx.same(gfx.frame_from(bytearray(raw), 37, 11), f)
    s = gfx.new(16, 16, alpha=True)
    gfx.disc(s, 8, 8, 6, gfx.rgb("#ff8000"), 0.5)
    gfx.save(s, tmp_path / "s.png")
    back = gfx.load(tmp_path / "s.png")
    assert gfx.has_alpha(back) and gfx.same(back, s) and gfx.pixel(back, 8, 8) == (128, 64, 0, 128)


@needs_cairo
def test_jpeg_and_other_files_through_ffmpeg(tmp_path):
    f = gfx.new(96, 64)
    gfx.fill_rect(f, 0, 0, 48, 64, gfx.rgb("#c02020"))
    gfx.fill_rect(f, 48, 0, 48, 64, gfx.rgb("#2040c0"))
    gfx.save(f, tmp_path / "f.jpg", quality=92)
    assert gfx.image_size(tmp_path / "f.jpg") == (96, 64)
    back = gfx.load(tmp_path / "f.jpg")
    assert (back.get_width(), back.get_height()) == (96, 64) and not gfx.has_alpha(back)
    for x, want in ((20, (0xc0, 0x20, 0x20)), (76, (0x20, 0x40, 0xc0))):
        got = gfx.pixel(back, x, 32)
        assert all(abs(g - w) <= 6 for g, w in zip(got, want)), (got, want)      # measured: 2
    wall = ROOT / "harness" / "wall.jpg"
    assert gfx.image_size(wall) == (2560, 1440)
    w = gfx.load(wall)
    assert (w.get_width(), w.get_height()) == (2560, 1440) and 20 < gfx.mean(w)[0] < 235
    # as a wallpaper: scaled to fill a size, its middle kept, by ffmpeg while it decodes
    fill = gfx.load(wall, size=(300, 300))
    assert (fill.get_width(), fill.get_height()) == (300, 300)
    ref = gfx.cover(w, 300, 300)
    assert all(abs(a - b) < 1.5 for a, b in zip(gfx.mean(fill), gfx.mean(ref)))
    for x, y in ((40, 40), (150, 150), (260, 220)):
        assert all(abs(a - b) <= 12 for a, b in zip(gfx.mean(gfx.crop(fill, x - 8, y - 8, 16, 16)),
                                                     gfx.mean(gfx.crop(ref, x - 8, y - 8, 16, 16))))
    gfx.save(ref, tmp_path / "ref.png")
    from_png = gfx.load(tmp_path / "ref.png", size=(150, 100))
    assert (from_png.get_width(), from_png.get_height()) == (150, 100)


@needs_cairo
def test_a_png_with_16_bits_a_colour_loads_as_an_ordinary_frame(tmp_path):
    src = gfx.new(48, 32)
    gfx.fill_rect(src, 0, 0, 24, 32, gfx.rgb("#c02020"))
    gfx.fill_rect(src, 24, 0, 24, 32, gfx.rgb("#2040c0"))
    for fmt, alpha in (("rgb48be", False), ("rgba64be", True)):
        out = tmp_path / f"{fmt}.png"
        enc = subprocess.run([ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr0", "-s", "48x32", "-i", "-",
                              "-frames:v", "1", "-pix_fmt", fmt, str(out)], input=bytes(gfx.data(src)), capture_output=True)
        assert enc.returncode == 0, enc.stderr[-300:]
        back = gfx.load(out)
        assert gfx.image_size(out) == (48, 32) and gfx.has_alpha(back) == alpha
        # cairo holds such a file as floats; coming back to 8 bits may cost a level
        for x, want in ((10, (0xc0, 0x20, 0x20)), (40, (0x20, 0x40, 0xc0))):
            assert all(abs(g - w) <= 1 for g, w in zip(gfx.pixel(back, x, 16), want)), gfx.pixel(back, x, 16)
        if alpha:
            assert gfx.pixel(back, 10, 16)[3] == 255


def test_sizes_are_read_from_the_header(tmp_path):
    def riff(chunk, payload):
        body = b"WEBP" + chunk + struct.pack("<I", len(payload)) + payload
        return b"RIFF" + struct.pack("<I", len(body)) + body
    files = {
        "lossy.webp": (riff(b"VP8 ", b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", 640, 360) + bytes(8)), (640, 360)),
        "lossless.webp": (riff(b"VP8L", b"\x2f" + struct.pack("<I", 1279 | 719 << 14) + bytes(8)), (1280, 720)),
        "extended.webp": (riff(b"VP8X", bytes(4) + (3839).to_bytes(3, "little") + (2159).to_bytes(3, "little") + bytes(8)),
                          (3840, 2160)),
        # a JPEG whose frame header comes after a large block (a camera's thumbnail) and a table
        "photo.jpg": (b"\xff\xd8\xff\xe1" + struct.pack(">H", 40002) + bytes(40000) + b"\xff\xdb\x00\x04\x00\x00"
                      + b"\xff\xc2" + struct.pack(">HBHH", 11, 8, 900, 1600) + bytes(6), (1600, 900)),
        "plain.png": (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 320, 200) + bytes(9), (320, 200)),
    }
    for name, (content, want) in files.items():
        (tmp_path / name).write_bytes(content)
        assert gfx.image_size(tmp_path / name) == want, name
    for name, content in (("cut.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF"), ("scan.jpg", b"\xff\xd8\xff\xda\x00\x08" + bytes(20)),
                          ("odd.webp", riff(b"ALPH", bytes(20))), ("zero.png", files["plain.png"][0][:16] + bytes(17))):
        (tmp_path / name).write_bytes(content)
        with pytest.raises(gfx.ImageError):
            gfx.image_size(tmp_path / name)


@needs_cairo
def test_a_webp_file_made_by_ffmpeg(tmp_path):
    src = gfx.new(96, 64)
    gfx.fill_rect(src, 0, 0, 48, 64, gfx.rgb("#c02020"))
    gfx.fill_rect(src, 48, 0, 48, 64, gfx.rgb("#2040c0"))
    enc = subprocess.run([ffmpeg(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr0", "-s", "96x64", "-i", "-",
                          "-frames:v", "1", "-c:v", "libwebp", "-quality", "95", str(tmp_path / "f.webp")],
                         input=bytes(gfx.data(src)), capture_output=True)
    if enc.returncode != 0:
        pytest.skip("this ffmpeg has no WebP encoder")
    assert gfx.image_size(tmp_path / "f.webp") == (96, 64)
    back = gfx.load(tmp_path / "f.webp")
    assert (back.get_width(), back.get_height()) == (96, 64)
    assert all(abs(g - w) <= 8 for g, w in zip(gfx.pixel(back, 20, 32), (0xc0, 0x20, 0x20)))
    assert all(abs(g - w) <= 8 for g, w in zip(gfx.pixel(back, 76, 32), (0x20, 0x40, 0xc0)))


@needs_cairo
def test_files_that_are_not_pictures_are_refused(tmp_path):
    for name in ("a.png", "a.jpg", "a.webp"):
        p = tmp_path / name
        p.write_text("not a picture at all\n" * 20)
        with pytest.raises(gfx.ImageError):
            gfx.image_size(p)
        with pytest.raises(gfx.ImageError):
            gfx.load(p)
    torn = tmp_path / "torn.png"
    good = tmp_path / "good.png"
    gfx.save(flat(40, 40, 90), good)
    torn.write_bytes(good.read_bytes()[:60])
    with pytest.raises(gfx.ImageError):
        gfx.load(torn)
    with pytest.raises(gfx.ImageError):
        gfx.image_size(tmp_path / "missing.png")


# ------------------------------------------------------------------ placing
@needs_cairo
def test_place_by_whole_pixels_is_an_exact_copy():
    src = columns(4, 2, [10, 20, 30, 40])
    out = flat(12, 6, 99)
    gfx.place(out, src, 1, 1, 5, 3)
    assert row(out, 3) == [99] * 5 + [10, 20, 30, 40] + [99] * 3
    assert row(out, 4) == row(out, 3) and row(out, 2) == [99] * 12 and row(out, 5) == [99] * 12


@needs_cairo
def test_place_takes_the_old_renderers_numbers():
    # output pixel x shows the source point (x - tx) / kx, pixel centres at whole numbers
    src = columns(8, 2, [0, 40, 80, 120, 160, 200, 240, 255])
    out = gfx.new(20, 2)
    gfx.place(out, src, 2.0, 1.0, 3.0, 0.0)
    got = row(out, 0)
    for x, want in ((3, 0), (4, 20), (5, 40), (7, 80), (8, 100), (13, 200)):
        assert abs(got[x] - want) <= 1, (x, got[x], want)
    half = gfx.new(12, 2)
    gfx.place(half, src, 1.0, 1.0, 0.5, 0.0)                 # half a pixel to the right: neighbours mix evenly
    got = row(half, 0)
    assert [abs(got[x] - w) <= 1 for x, w in ((1, 20), (2, 60), (3, 100), (6, 220))] == [True] * 4
    quarter = gfx.new(12, 2)
    gfx.place(quarter, src, 1.0, 1.0, 0.25, 0.0)
    assert abs(row(quarter, 0)[2] - 70) <= 1                 # 3/4 of 80 and 1/4 of 40
    assert gfx.placed_box(8, 2, 2.0, 1.0, 3.0, 0.0) == (2.5, 0.0, 16.0, 2.0)
    with pytest.raises(ValueError):
        gfx.place(out, src, 0.0, 1.0, 0, 0)


@needs_cairo
def test_what_lies_outside_a_placed_picture():
    src = columns(4, 1, [10, 20, 30, 40])
    for extend, left, right in ((gfx.NONE, [99] * 4, [99] * 4), (gfx.PAD, [10] * 4, [40] * 4),
                                (gfx.REFLECT, [40, 30, 20, 10], [40, 30, 20, 10])):
        out = flat(12, 1, 99)
        gfx.place(out, src, 1, 1, 4, 0, extend=extend)
        assert row(out, 0) == left + [10, 20, 30, 40] + right, extend
    # with a clip, only the clip is drawn on
    out = flat(12, 1, 99)
    c = gfx.context(out)
    c.rectangle(2, 0, 8, 1)
    c.clip()
    gfx.place(c, src, 1, 1, 4, 0, extend=gfx.PAD)
    assert row(out, 0) == [99, 99, 10, 10, 10, 20, 30, 40, 40, 40, 99, 99]


@needs_cairo
@pytest.mark.parametrize("k", [0.3, 0.45, 0.52, 0.6, 0.7, 0.74])
def test_shrinking_counts_every_source_pixel(k):
    w = 144
    out_w = int(w * k)
    # one bright column in a dark picture always leaves about the same trace, wherever it is
    totals = []
    for x in range(0, w, 5):
        src = columns(w, 4, [255 if i == x else 0 for i in range(w)])
        out = gfx.resize(src, out_w, 4)
        totals.append(sum(row(out, 1)))
    assert min(totals) > 0
    inner = totals[2:-2]
    assert max(inner) - min(inner) <= 0.2 * 255 * k + 6, (min(inner), max(inner))   # plain bilinear: 0 to 255
    assert abs(sum(inner) / len(inner) - 255 * k) <= 0.06 * 255 * k + 2
    # stripes one pixel wide do not turn into a moire of black and white
    stripes = gfx.resize(columns(w, 4, [255 * (i % 2) for i in range(w)]), out_w, 4)
    mid = row(stripes, 1)[3:-3]
    assert 80 <= min(mid) and max(mid) <= 175, (min(mid), max(mid))
    # and a flat picture stays flat
    assert set(row(gfx.resize(flat(w, 4, 77), out_w, 4), 2)) == {77}


@needs_cairo
def test_place_shrinks_inside_a_clip_and_at_a_fraction_of_a_pixel():
    src = flat(200, 100, 200)
    out = flat(160, 90, 20)
    c = gfx.context(out)
    x, y, w, h = gfx.placed_box(200, 100, 0.6, 0.6, 10.3, 8.7)
    assert (round(x, 2), round(y, 2), round(w, 2), round(h, 2)) == (10.5, 8.9, 120.0, 60.0)
    gfx.rounded_rect(c, x, y, w, h, 12)
    c.clip()
    gfx.place(c, src, 0.6, 0.6, 10.3, 8.7, extend=gfx.PAD)
    assert gfx.pixel(out, 70, 40) == (200, 200, 200)
    assert gfx.pixel(out, 5, 40) == (20, 20, 20) and gfx.pixel(out, 135, 40) == (20, 20, 20)
    assert gfx.pixel(out, 11, 9) == (20, 20, 20)                       # the rounded corner
    edge = gfx.pixel(out, 10, 40)[0]                                   # the card starts half way through this pixel
    assert 100 <= edge <= 120, edge
    # without a clip the picture ends at its own edge, as softly
    plain = flat(160, 90, 20)
    gfx.place(plain, src, 0.6, 0.6, 10.3, 8.7)
    assert gfx.pixel(plain, 70, 40) == (200, 200, 200) and gfx.pixel(plain, 135, 40) == (20, 20, 20)
    assert 100 <= gfx.pixel(plain, 10, 40)[0] <= 120
    assert gfx.pixel(plain, 131, 40)[0] in range(20, 201) and gfx.pixel(plain, 132, 40) == (20, 20, 20)


@needs_cairo
def test_filters_nearest_and_sharp():
    src = columns(2, 2, [50, 250])
    blocks = gfx.resize(src, 8, 8, "nearest")
    assert row(blocks, 0) == [50] * 4 + [250] * 4 and row(blocks, 7) == [50] * 4 + [250] * 4
    soft = row(gfx.resize(src, 8, 8), 3)
    assert soft[0] == 50 and soft[-1] == 250 and soft == sorted(soft) and 50 < soft[3] < soft[4] < 250
    edge = columns(16, 4, [40] * 8 + [220] * 8)
    sharp, plain = row(gfx.resize(edge, 48, 12, "sharp"), 6), row(gfx.resize(edge, 48, 12), 6)
    assert sharp[2] == 40 and sharp[-3] == 220 and plain[2] == 40 and plain[-3] == 220
    # the sharp filter (Catmull-Rom) overshoots a little beside an edge and is steeper
    # across it; the bilinear one stays between the two levels
    assert (min(sharp) < 40 or max(sharp) > 220) and min(plain) == 40 and max(plain) == 220
    steepest = lambda r: max(b - a for a, b in zip(r, r[1:]))             # noqa: E731
    assert steepest(sharp) > steepest(plain)


@needs_cairo
def test_resize_and_cover():
    src = columns(8, 4, [0, 0, 0, 0, 255, 255, 255, 255])
    same = gfx.resize(src, 8, 4)
    assert same is not src and gfx.same(same, src)
    # shrinking by far more than cairo's own filter reaches still averages everything
    big = gfx.new(640, 640)
    gfx.fill_rect(big, 300, 180, 64, 64, WHITE)
    small = gfx.resize(big, 10, 10)
    assert abs(sum(gfx.plane(small, "g")) / 100 - 255 * 64 * 64 / (640 * 640)) < 0.6     # the mean is kept
    assert gfx.pixel(small, 0, 9) == (0, 0, 0)
    tall = gfx.resize(gfx.new(1000, 10, alpha=True), 10, 10)
    assert gfx.has_alpha(tall) and (tall.get_width(), tall.get_height()) == (10, 10)
    wide = gfx.new(200, 100)
    gfx.fill_rect(wide, 0, 0, 100, 100, gfx.rgb("#ff0000"))
    gfx.fill_rect(wide, 100, 0, 100, 100, gfx.rgb("#0000ff"))
    sq = gfx.cover(wide, 100, 100)                                     # the middle square of it
    assert gfx.pixel(sq, 25, 50) == (255, 0, 0) and gfx.pixel(sq, 75, 50) == (0, 0, 255)
    half = gfx.cover(wide, 50, 50)
    assert gfx.pixel(half, 10, 25) == (255, 0, 0) and gfx.pixel(half, 40, 25) == (0, 0, 255)
    up = gfx.cover(wide, 300, 300)
    assert gfx.pixel(up, 40, 150) == (255, 0, 0) and gfx.pixel(up, 260, 150) == (0, 0, 255)


# ------------------------------------------------------------------ blur
def edge_sigma(values):
    """Standard deviation of a blurred step edge, from its slope."""
    d = [b - a for a, b in zip(values, values[1:])]
    tot = sum(d)
    m = sum(v * (i + 0.5) for i, v in enumerate(d)) / tot
    return math.sqrt(sum(v * (i + 0.5 - m) ** 2 for i, v in enumerate(d)) / tot)


@needs_cairo
@pytest.mark.parametrize("sigma", [1.0, 2.0, 4.0, 11.5, 21.0, 57.6])
def test_blur_is_as_wide_as_asked(sigma):
    w = int(520 + 12 * sigma)
    got = []
    for off in (0, 3, 7, 12):                               # wherever the edge lies on the pixel grid
        f = gfx.new(w, 64)
        gfx.fill_rect(f, w // 2 + off, 0, w, 64, WHITE)
        r = row(gfx.blur(f, sigma), 32)
        assert r[0] == 0 and r[-1] == 255
        assert all(b >= a - 1 for a, b in zip(r, r[1:]))    # a smooth ramp, no bands that step back
        got.append(edge_sigma(r))
    assert all(abs(g - sigma) <= 0.08 * sigma + 0.1 for g in got), got       # measured: within 5 %


@needs_cairo
def test_blur_keeps_flat_pictures_flat_and_the_amount_of_light():
    for level in (0, 1, 77, 128, 200, 254, 255):
        for sigma in (0.8, 3.0, 9.0, 30.0):
            assert set(gfx.plane(gfx.blur(flat(70, 50, level), sigma), "g")) == {level}, (level, sigma)
    full = gfx.new(60, 40, alpha=True)
    gfx.fill_rect(full, 0, 0, 60, 40, BLACK)
    assert set(gfx.plane(gfx.blur(full, 12.0), "a")) == {255}
    f = gfx.new(400, 300)
    gfx.fill_rect(f, 170, 120, 60, 60, WHITE)
    before = sum(gfx.plane(f, "r"))
    for sigma in (2.0, 7.0, 20.0):
        b = gfx.blur(f, sigma)
        assert b is not f and gfx.pixel(f, 200, 150) == (255, 255, 255)        # the original is left alone
        # (the faintest part of a wide blur is a level of 255 or none at all)
        assert 0.96 * before <= sum(gfx.plane(b, "r")) <= 1.09 * before, sigma    # measured: 100, 101 and 106 %
        assert gfx.pixel(b, 199, 149) == gfx.pixel(b, 200, 150)                # symmetric about the square's centre
        # (to a few levels: the grid the picture is halved on is not centred on the square)
        assert abs(gfx.pixel(b, 150, 150)[0] - gfx.pixel(b, 249, 150)[0]) <= 5
        assert abs(gfx.pixel(b, 200, 100)[0] - gfx.pixel(b, 200, 199)[0]) <= 5
    assert gfx.same(gfx.blur(f, 0.0), f)


@needs_cairo
def test_blur_and_pixelate_a_rectangle_in_place():
    f = flat(200, 120, 255)
    gfx.fill_rect(f, 40, 30, 100, 60, BLACK)                   # a black box in a white frame
    gfx.fill_rect(f, 80, 50, 20, 20, gfx.rgb("#00ff00"))       # with a green secret in it
    before = gfx.copy(f)
    gfx.blur_rect(f, 40, 30, 100, 60, 8.0)
    assert gfx.pixel(f, 90, 60)[1] < 200 and gfx.pixel(f, 60, 40)[1] > 0     # the secret is smeared over the box
    assert gfx.pixel(f, 41, 31)[0] == 0                         # the white around the box did not come in
    for x, y in ((39, 60), (140, 60), (90, 29), (90, 90), (0, 0), (199, 119)):
        assert gfx.pixel(f, x, y) == (255, 255, 255)            # and outside nothing changed
    # the secret's light is still in the box, spread out (measured: 4 % more, the faint fringe)
    assert abs(sum(gfx.plane(f, "g")) - sum(gfx.plane(before, "g"))) <= 0.08 * 20 * 20 * 255
    gfx.blur_rect(f, -50, -50, 20, 20, 5.0)                     # off the picture: nothing to do
    gfx.blur_rect(f, 190, 110, 50, 50, 5.0)                     # partly off: the part that is there
    p = gfx.copy(before)
    gfx.pixelate_rect(p, 40, 30, 100, 60, 20)
    inside = {gfx.pixel(p, x, y) for x in range(40, 140) for y in range(30, 90)}
    assert 2 <= len(inside) <= 15                               # 5 x 3 blocks
    assert gfx.pixel(p, 45, 35) == gfx.pixel(p, 55, 45) and gfx.pixel(p, 39, 60) == (255, 255, 255)
    assert gfx.same(gfx.crop(p, 0, 0, 40, 120), gfx.crop(before, 0, 0, 40, 120))


@needs_cairo
def test_soft_rect_is_a_shadow():
    m = gfx.soft_rect(300, 200, 100, 60, 100, 80, 10.0, radius=12)
    a = lambda x, y: gfx.pixel(m, x, y)[3]                       # noqa: E731
    assert a(150, 100) == 255 and a(5, 5) == 0 and a(295, 195) == 0
    assert abs(a(100, 100) - 128) <= 8 and abs(a(199, 100) - 128) <= 8        # half way at the edges
    assert abs(a(150, 60) - 128) <= 8 and abs(a(150, 139) - 128) <= 8
    assert a(100, 60) < a(100, 100)                              # a corner is lighter than an edge
    assert a(85, 100) < a(95, 100) < a(105, 100) < a(115, 100)
    # as a shadow on a picture: darker under the card, untouched far away
    f = flat(300, 200, 200)
    gfx.paint_mask(f, m, BLACK, 0.5)
    assert gfx.pixel(f, 150, 100) == (100, 100, 100) and gfx.pixel(f, 5, 5) == (200, 200, 200)
    sharp = gfx.soft_rect(120, 80, 20, 20, 80, 40, 0.0)
    assert gfx.pixel(sharp, 19, 40)[3] == 0 and gfx.pixel(sharp, 20, 40)[3] == 255


@needs_cairo
def test_gradient_fill_glow_shadow_and_polygon():
    f = gfx.new(200, 100)
    gfx.fill_gradient(f, ["#000000", "#ffffff"], 90)                 # a CSS angle: 90deg runs to the right
    assert gfx.pixel(f, 2, 50)[0] < 10 and gfx.pixel(f, 197, 50)[0] > 245
    assert abs(gfx.pixel(f, 100, 10)[0] - gfx.pixel(f, 100, 90)[0]) <= 1
    gfx.fill_gradient(f, ["#000000", "#ffffff"], 180)                # 180deg: to the bottom
    assert gfx.pixel(f, 100, 2)[0] < 10 and gfx.pixel(f, 100, 97)[0] > 245
    g = flat(200, 100, 100)
    gfx.glow(g)                                                      # light from the top left, a little less far from it
    assert gfx.pixel(g, 40, 5)[0] > 100 > gfx.pixel(g, 195, 95)[0] >= 90
    s = flat(200, 100, 200)
    gfx.shadow(gfx.context(s), 60, 30, 80, 40, 4.0, 6.0, 0.5)
    assert abs(gfx.pixel(s, 100, 50)[0] - 100) <= 3 and gfx.pixel(s, 5, 5) == (200, 200, 200)
    assert 100 < gfx.pixel(s, 60, 50)[0] < 200                       # soft at the edge
    p = gfx.new(60, 60)
    c = gfx.context(p)
    square = [(10, 10), (40, 10), (40, 40), (10, 40)]
    gfx.polygon(c, square)
    gfx.polygon(c, [(x + 10, y + 10) for x, y in square[::-1]])      # the other way round, overlapping
    c.set_source_rgb(1, 1, 1)
    c.fill()
    assert gfx.pixel(p, 30, 30) == (255, 255, 255)                   # where they overlap: filled, not cut out
    assert gfx.pixel(p, 15, 15) == (255, 255, 255) and gfx.pixel(p, 45, 45) == (255, 255, 255)
    assert gfx.pixel(p, 5, 5) == (0, 0, 0) and gfx.pixel(p, 45, 15) == (0, 0, 0)


# ------------------------------------------------------------------ mixing
@needs_cairo
def test_mix_and_average():
    a, b = flat(20, 10, 40), flat(20, 10, 200)
    assert gfx.same(gfx.mix(a, b, 0.0), a) and gfx.same(gfx.mix(a, b, 1.0), b)
    assert abs(gfx.pixel(gfx.mix(a, b, 0.5), 3, 3)[0] - 120) <= 1
    assert abs(gfx.pixel(gfx.mix(a, b, 0.25), 3, 3)[0] - 80) <= 1
    assert gfx.pixel(gfx.mix(a, a, 0.37), 5, 5) == (40, 40, 40)                # flat stays flat
    assert gfx.pixel(a, 0, 0) == (40, 40, 40)
    three = gfx.average([flat(8, 8, 30), flat(8, 8, 60), flat(8, 8, 90)])
    assert abs(gfx.pixel(three, 4, 4)[0] - 60) <= 1
    weighted = gfx.average([flat(8, 8, 0), flat(8, 8, 100), flat(8, 8, 200)], [1, 1, 2])
    assert abs(gfx.pixel(weighted, 4, 4)[0] - 125) <= 1
    # sprites: half of a solid one and half of nothing is half there
    solid, clear = gfx.new(8, 8, alpha=True), gfx.new(8, 8, alpha=True)
    gfx.fill_rect(solid, 0, 0, 8, 8, WHITE)
    half = gfx.average([solid, clear])
    assert all(abs(v - 128) <= 1 for v in gfx.pixel(half, 4, 4))
    assert all(abs(v - 64) <= 1 for v in gfx.pixel(gfx.mix(clear, solid, 0.25), 4, 4))
    # a motion blur: the same sprite at several places, added up with place()
    streak = gfx.new(40, 8, alpha=True)
    for i in range(4):
        gfx.place(streak, solid, 1, 1, 4 + 6 * i, 0, operator=gfx.ADD, alpha=0.25)
    assert all(abs(v - 64) <= 1 for v in gfx.pixel(streak, 5, 4)) and all(abs(v - 128) <= 2 for v in gfx.pixel(streak, 11, 4))


# ------------------------------------------------------------------ shapes and gradients
@needs_cairo
def test_rounded_rectangle_has_soft_corners():
    s = gfx.new(120, 80, alpha=True)
    c = gfx.context(s)
    gfx.rounded_rect(c, 10, 10, 100, 60, 20)
    c.set_source_rgba(1, 1, 1, 1)
    c.fill()
    a = gfx.plane(s, "a")
    area = sum(a) / 255
    assert abs(area - (100 * 60 - (4 - math.pi) * 20 * 20)) < 3            # measured: under 0.5 pixel
    px = lambda x, y: a[y * 120 + x]                                       # noqa: E731
    assert px(60, 40) == 255 and px(10, 40) == 255 and px(60, 10) == 255 and px(109, 69 - 20) == 255
    assert px(10, 10) == 0 and px(109, 10) == 0 and px(10, 69) == 0 and px(109, 69) == 0
    assert any(0 < px(x, y) < 255 for x in range(10, 30) for y in range(10, 30))      # the arc is anti-aliased
    assert px(15, 15) == px(104, 15) == px(15, 64) == px(104, 64)                     # the four corners agree
    plain = gfx.new(30, 30, alpha=True)
    c = gfx.context(plain)
    gfx.rounded_rect(c, 5, 5, 20, 20, 0)
    c.fill()
    assert sum(gfx.plane(plain, "a")) == 255 * 400
    big_r = gfx.new(40, 40, alpha=True)
    c = gfx.context(big_r)
    gfx.rounded_rect(c, 0, 0, 40, 40, 500)                                 # a radius past half the side: a circle
    c.fill()
    assert abs(sum(gfx.plane(big_r, "a")) / 255 - math.pi * 400) < 3


@needs_cairo
def test_discs_rings_and_rectangles():
    s = gfx.new(100, 100, alpha=True)
    gfx.disc(s, 50.3, 49.6, 30.0, WHITE)
    assert abs(sum(gfx.plane(s, "a")) / 255 - math.pi * 900) < 6           # measured: 1.5
    assert gfx.pixel(s, 50, 50) == (255, 255, 255, 255) and gfx.pixel(s, 5, 5) == (0, 0, 0, 0)
    r = gfx.new(100, 100, alpha=True)
    gfx.ring(r, 50.3, 49.6, 30.0, 4.0, WHITE, 0.5)
    assert abs(sum(gfx.plane(r, "a")) / 255 - 0.5 * 2 * math.pi * 30 * 4) < 8
    assert gfx.pixel(r, 50, 50)[3] == 0 and abs(gfx.pixel(r, 80, 50)[3] - 128) <= 2 and gfx.pixel(r, 90, 50)[3] == 0
    f = flat(20, 20, 100)
    gfx.fill_rect(f, 5, 5, 10, 10, WHITE, 0.5)
    gfx.fill_rect(f, 0.5, 0, 1, 20, WHITE)                                 # half over two columns
    assert abs(gfx.pixel(f, 10, 10)[0] - 178) <= 1 and gfx.pixel(f, 17, 17) == (100, 100, 100)
    assert abs(gfx.pixel(f, 0, 2)[0] - 178) <= 1 and abs(gfx.pixel(f, 1, 2)[0] - 178) <= 1
    # shapes drawn through a context keep to its clip
    g = gfx.new(60, 60)
    c = gfx.context(g)
    c.rectangle(0, 0, 30, 60)
    c.clip()
    gfx.disc(c, 30, 30, 20, WHITE)
    assert gfx.pixel(g, 20, 30) == (255, 255, 255) and gfx.pixel(g, 40, 30) == (0, 0, 0)


@needs_cairo
def test_gradients():
    f = gfx.new(256, 40)
    c = gfx.context(f)
    c.set_source(gfx.linear_gradient(0, 0, 256, 0, [(0, gfx.rgb("#000000")), (1, gfx.rgb("#ffffff"))]))
    c.paint()
    cols = [sum(gfx.pixel(f, x, y)[0] for y in range(40)) / 40 for x in range(256)]
    assert cols[0] < 3 and cols[255] > 252 and abs(cols[128] - 128) < 3
    assert all(b >= a - 1.5 for a, b in zip(cols, cols[1:]))              # rising (a level of dither at most)
    three = gfx.new(90, 10)
    c = gfx.context(three)
    c.set_source(gfx.linear_gradient(0, 0, 90, 0, [(0, gfx.rgb("#ff0000")), (0.5, gfx.rgb("#00ff00")), (1, gfx.rgb("#0000ff"))]))
    c.paint()
    assert gfx.pixel(three, 1, 5)[0] > 240 and gfx.pixel(three, 45, 5)[1] > 240 and gfx.pixel(three, 88, 5)[2] > 240
    spot = flat(100, 100, 200)
    c = gfx.context(spot)                                                  # darken away from the centre
    c.set_source(gfx.radial_gradient(50, 50, 50, [(0, BLACK, 0.0), (0.5, BLACK, 0.0), (1, BLACK, 0.5)]))
    c.paint()
    assert gfx.pixel(spot, 50, 50) == (200, 200, 200) and gfx.pixel(spot, 60, 50) == (200, 200, 200)
    assert abs(gfx.pixel(spot, 50, 2)[0] - 104) <= 6 and abs(gfx.pixel(spot, 2, 2)[0] - 100) <= 3


@needs_cairo
def test_noise_is_grey_small_and_repeatable():
    a, b, other = flat(200, 150, 102), flat(200, 150, 102), flat(200, 150, 102)
    gfx.add_noise(a)
    gfx.add_noise(b)
    gfx.add_noise(other, seed=8)
    assert gfx.same(a, b) and not gfx.same(a, other)
    g = gfx.plane(a, "g")
    assert g == gfx.plane(a, "r") == gfx.plane(a, "b")
    m = sum(g) / len(g)
    sd = math.sqrt(sum((v - m) ** 2 for v in g) / len(g))
    assert abs(m - 102) < 0.05 and 0.8 < sd < 1.1 and min(g) >= 98 and max(g) <= 106     # measured: 0.95
    dark = gfx.new(50, 50)
    gfx.add_noise(dark)
    assert max(gfx.plane(dark, "g")) <= 4
    strong = flat(100, 100, 128)
    gfx.add_noise(strong, sigma=4.0)
    g = gfx.plane(strong, "g")
    m = sum(g) / len(g)
    assert 3.4 < math.sqrt(sum((v - m) ** 2 for v in g) / len(g)) < 4.6


# ------------------------------------------------------------------ small analysis pictures
@needs_cairo
def test_difference_threshold_and_invert():
    a = columns(6, 2, [0, 10, 20, 100, 200, 255])
    b = columns(6, 2, [5, 10, 0, 130, 100, 0])
    d = gfx.difference(a, b)
    assert row(d, 0) == [5, 0, 20, 30, 100, 255] and row(a, 0) == [0, 10, 20, 100, 200, 255]
    t = gfx.threshold(gfx.copy(d), 20)                                     # above 20, not 20 itself
    assert row(t, 1) == [0, 0, 0, 255, 255, 255]
    for level in (0, 1, 99, 100, 254, 255):
        got = row(gfx.threshold(columns(4, 1, [0, 100, 101, 255]), level), 0)
        assert got == [255 if v > level else 0 for v in (0, 100, 101, 255)], level
    inv = gfx.invert(columns(3, 1, [0, 100, 255]))
    assert row(inv, 0) == [255, 155, 0]
    # every colour on its own
    f = gfx.new(2, 1)
    gfx.fill_rect(f, 0, 0, 1, 1, gfx.rgb((200, 10, 90)))
    gfx.threshold(f, 50)
    assert gfx.pixel(f, 0, 0) == (255, 0, 255) and gfx.pixel(f, 1, 0) == (0, 0, 0)


# ------------------------------------------------------------------ text
def ink_box(surface, channel="a"):
    """(x0, y0, x1, y1) of what was drawn, or None."""
    w, h = surface.get_width(), surface.get_height()
    p = gfx.plane(surface, channel)
    ys = [y for y in range(h) if any(p[y * w:(y + 1) * w])]
    xs = [x for x in range(w) if any(p[x::w])]
    return (xs[0], ys[0], xs[-1] + 1, ys[-1] + 1) if xs and ys else None


MONO = "JetBrainsMono Nerd Font, JetBrains Mono, monospace"


@needs_cairo
def test_text_is_measured_and_drawn():
    assert gfx.text_engine() in ("pango", "cairo")
    f = gfx.font(MONO, 26, bold=True)
    w4, w2 = gfx.text_width("Ctrl", f), gfx.text_width("Ct", f)
    assert 40 < w4 < 90 and abs(w4 - 2 * w2) < 0.5                         # a monospace font
    assert abs(gfx.text_width("Ctrl", gfx.font(MONO, 52, bold=True)) - 2 * w4) < 0.02 * w4 + 0.5
    assert gfx.text_width("", f) == 0
    asc, desc = gfx.font_metrics(f)
    assert 18 < asc < 34 and 3 < desc < 12 and 26 <= asc + desc < 44
    s = gfx.new(200, 80, alpha=True)
    gfx.draw_text(s, "Ctrl", 100, 40, f, WHITE, anchor="mm")
    x0, y0, x1, y1 = ink_box(s)
    assert abs((x0 + x1) / 2 - 100) <= 3 and abs((x1 - x0) - w4) <= 8      # centred on x, as wide as measured
    assert 40 - asc <= y0 and y1 <= 40 + asc and abs((y0 + y1) / 2 - 40) <= 6
    assert gfx.pixel(s, 5, 5) == (0, 0, 0, 0) and max(gfx.plane(s, "a")) == 255
    assert any(0 < v < 255 for v in gfx.plane(s, "a"))                     # smooth edges
    assert gfx.plane(s, "r") == gfx.plane(s, "g") == gfx.plane(s, "b")     # grey smoothing, no coloured fringes


@needs_cairo
def test_text_anchors():
    f = gfx.font(MONO, 30)
    asc, desc = gfx.font_metrics(f)
    w = gfx.text_width("Hxg", f)

    def box(anchor):
        s = gfx.new(300, 140, alpha=True)
        gfx.draw_text(s, "Hxg", 150, 70, f, WHITE, anchor=anchor)
        return ink_box(s)
    la, ls, ld, lm = box("la"), box("ls"), box("ld"), box("lm")
    assert 150 <= la[0] <= 154 and abs(la[2] - (150 + w)) <= 4             # starts at x
    assert 70 <= la[1] and la[3] <= 70 + asc + desc + 1                    # hangs below y
    assert ls[1] - la[1] == pytest.approx(-asc, abs=1.01)                  # the baseline on y: up by the ascent
    assert ld[3] <= 70 + 1 and ld[1] - la[1] == pytest.approx(-(asc + desc), abs=1.01)
    assert lm[1] - la[1] == pytest.approx(-(asc + desc) / 2, abs=1.01)
    # "H" stands on the baseline, "g" reaches under it
    s = gfx.new(300, 140, alpha=True)
    gfx.draw_text(s, "H", 150, 70, f, WHITE, anchor="ls")
    assert abs(ink_box(s)[3] - 70) <= 1
    ra, ma = box("ra"), box("ma")
    assert abs(ra[2] - 150) <= 4 and abs((ma[0] + ma[2]) / 2 - 150) <= 3


@needs_cairo
def test_a_label_stands_on_a_whole_pixel_row_where_0_7_3_put_it():
    f = gfx.font(MONO, 53, bold=True)
    asc, desc = gfx.font_metrics(f)
    below = math.floor((math.ceil(asc) - math.ceil(desc)) / 2 + 0.5)      # the baseline, whole pixels under the middle

    def drawn(y, anchor="m"):
        s = gfx.new(300, 160, alpha=True)
        gfx.label(s, "H", 150, y, f, WHITE, anchor=anchor)
        return s
    box = ink_box(drawn(70))
    assert abs(box[3] - (70 + below)) <= 1                                 # "H" stands on that row
    assert gfx.same(drawn(70), drawn(70.5)) and gfx.same(drawn(70), drawn(70.9))   # y is cut to a whole pixel, not rounded
    assert ink_box(drawn(71))[1::2] == (box[1] + 1, box[3] + 1)
    assert abs((box[0] + box[2]) / 2 - 150) <= 2 and ink_box(drawn(70, "l"))[0] >= 150
    # the same letter by draw_text's own middle sits a fraction of a pixel off the row
    s = gfx.new(300, 160, alpha=True)
    gfx.draw_text(s, "H", 150, 70, f, WHITE, anchor="mm")
    assert not gfx.same(s, drawn(70))


@needs_cairo
def test_text_colour_alpha_and_clip():
    f = gfx.font(MONO, 40, bold=True)
    frame = flat(200, 80, 0)
    gfx.draw_text(frame, "MMM", 100, 40, f, gfx.rgb("#ff8000"), anchor="mm")
    r, g, b = (max(gfx.plane(frame, ch)) for ch in "rgb")
    assert r == 255 and abs(g - 128) <= 1 and b == 0
    faint = flat(200, 80, 0)
    gfx.draw_text(faint, "MMM", 100, 40, f, WHITE, alpha=0.5, anchor="mm")
    assert abs(max(gfx.plane(faint, "g")) - 128) <= 1
    clipped = gfx.new(200, 80, alpha=True)
    c = gfx.context(clipped)
    c.rectangle(0, 0, 100, 80)
    c.clip()
    gfx.draw_text(c, "MMM", 100, 40, f, WHITE, anchor="mm")
    assert ink_box(clipped)[2] <= 100
    arrows = gfx.new(200, 80, alpha=True)
    gfx.draw_text(arrows, "← ×3 →", 100, 40, f, WHITE, anchor="mm")       # what keycaps and badges show
    assert ink_box(arrows) is not None and ink_box(arrows)[2] - ink_box(arrows)[0] > 60


def fc(*args):
    try:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


@needs_cairo
def test_the_fonts_the_renderer_looked_for_are_found():
    """render.font_path asked fontconfig for "JetBrainsMono Nerd Font:bold" and gave Pillow the
    file. The same file, and the family list that replaces it, must draw the same letters."""
    path = fc("fc-match", "-f", "%{file}", "JetBrainsMono Nerd Font:bold")
    if not path or not Path(path).exists():
        pytest.skip("fontconfig has no font to offer")
    by_file = gfx.font(path, 26)
    assert by_file.family and by_file.size == 26
    assert by_file == gfx.font(path, 26) and hash(by_file) == hash(gfx.font(path, 26))
    family = fc("fc-scan", "--format", "%{family[0]}", path)
    if family not in [n.strip() for n in MONO.split(",")]:
        pytest.skip(f"JetBrains Mono is not installed here (fontconfig offers {family})")
    assert by_file.family == family and by_file.bold

    def draw(f):
        s = gfx.new(260, 60, alpha=True)
        gfx.draw_text(s, "Ctrl + Shift", 130, 30, f, WHITE, anchor="mm")
        return s
    assert gfx.same(draw(by_file), draw(gfx.font(MONO, 26, bold=True)))
    assert not gfx.same(draw(by_file), draw(gfx.font(MONO, 26)))           # bold is bold
    assert gfx.text_width("Ctrl", by_file) == pytest.approx(4 * 0.6 * 26, abs=0.2)    # 600/1000 em a letter


@needs_cairo
def test_a_font_file_that_is_not_installed(tmp_path):
    woff = ROOT / "harness" / "fonts" / "jetbrains-mono-latin-700-normal.woff2"
    if gfx.text_engine() != "pango" or not fc("fc-scan", "--format", "%{family[0]}", str(woff)):
        pytest.skip("needs Pango and a fontconfig that reads WOFF2")
    f = gfx.font(str(woff), 24)
    assert "JetBrains" in f.family and f.bold
    s = gfx.new(200, 60, alpha=True)
    gfx.draw_text(s, "demo", 100, 30, f, WHITE, anchor="mm")
    assert ink_box(s) is not None
    assert gfx.font("/nonexistent/font.ttf", 20).family == "monospace"     # falls back, does not fail
    assert gfx.font(None, 20).family == "monospace"


@needs_cairo
def test_text_without_pango(monkeypatch):
    f = gfx.font(MONO, 26, bold=True)
    with_engine = (gfx.text_engine(), gfx.text_width("Ctrl", f), gfx.font_metrics(f))
    monkeypatch.setattr(gfx, "_PANGO", False)
    gfx.font_metrics.cache_clear()
    try:
        assert gfx.text_engine() == "cairo"
        w = gfx.text_width("Ctrl", f)
        asc, desc = gfx.font_metrics(f)
        assert abs(w - with_engine[1]) <= 0.05 * with_engine[1] + 1
        assert abs(asc - with_engine[2][0]) <= 2 and abs(desc - with_engine[2][1]) <= 2
        s = gfx.new(200, 80, alpha=True)
        gfx.draw_text(s, "Ctrl", 100, 40, f, WHITE, anchor="mm")
        x0, y0, x1, y1 = ink_box(s)
        assert abs((x0 + x1) / 2 - 100) <= 3 and abs((y0 + y1) / 2 - 40) <= 6
    finally:
        gfx.font_metrics.cache_clear()


# ------------------------------------------------------------------ without cairo
def test_importing_never_fails_and_says_what_is_missing(tmp_path):
    (tmp_path / "cairo.py").write_text("raise ImportError('no cairo on this machine')\n")
    code = ("from dolly import gfx\n"
            "assert gfx.available() is False and gfx.text_engine() == ''\n"
            "f = gfx.font('monospace', 20)\n"
            "calls = [lambda: gfx.new(4, 4), lambda: gfx.frame_from(bytearray(64), 4, 4), lambda: gfx.context(None),\n"
            "         lambda: gfx.load('x.png'), lambda: gfx.text_width('a', f), lambda: gfx.font_metrics(f),\n"
            "         lambda: gfx.linear_gradient(0, 0, 1, 1, []), lambda: gfx.radial_gradient(0, 0, 1, []),\n"
            "         lambda: gfx.soft_rect(8, 8, 1, 1, 4, 4, 2.0)]\n"
            "for i, call in enumerate(calls):\n"
            "    try:\n"
            "        call()\n"
            "    except gfx.Unavailable as e:\n"
            "        assert 'python-cairo' in str(e), str(e)\n"
            "    else:\n"
            "        raise SystemExit('call %d drew without cairo' % i)\n"
            "assert gfx.rgb('#ff0000') == (1.0, 0.0, 0.0)\n"
            "assert gfx.placed_box(10, 10, 2, 2, 0, 0) == (-0.5, -0.5, 20, 20)\n"
            "print('ok')\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), str(ROOT)]))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stdout + r.stderr[-800:]


def imports_of(path: Path) -> set:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


def test_only_gfx_imports_cairo_and_gi():
    for p in sorted((ROOT / "dolly").rglob("*.py")):
        if p.name != "gfx.py":
            assert not ({"cairo", "gi"} & imports_of(p)), p.name
    for name in ("gfx.py", "num.py"):
        assert not ({"numpy", "PIL", "cv2"} & imports_of(ROOT / "dolly" / name)), name

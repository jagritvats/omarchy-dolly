"""Drawing for the renderer and the screen analysis, on cairo; text through Pango.

The only module that imports cairo or gi. Pictures are cairo image surfaces of two kinds:

- a *frame* is opaque (cairo's RGB24): four bytes a pixel, blue, green, red and one unused.
  ffmpeg calls that bgr0, so a frame read from an ffmpeg pipe is wrapped where it lies
  (read_frame, frame_from) and data() hands its bytes to an encoder pipe: no copy either way;
- a *sprite* has alpha (ARGB32), its colours already multiplied by it, as cairo wants them.
  A mask is a sprite of which only the alpha is used.

What is here is what Dolly needs:

- frames and files: new, frame_from, read_frame, data, copy, crop, load, save, image_size;
- placing a picture with a scale and a shift (place, placed_box, resize, cover);
- blur (blur, blur_rect, pixelate_rect, soft_rect), cross-fade and averaging (mix, average);
- shapes for a context (rounded_rect, fill_rect, disc, ring), gradients, grey noise;
- text (font, text_width, font_metrics, draw_text): Pango when gi is there, cairo's own
  simple text otherwise;
- per-pixel steps for small pictures (difference, threshold, invert) and reading values
  back (pixel, plane, mean, same).

For anything else, context(surface) is a plain cairo context: paths, fills, clips. Its
coordinates have the top left corner of pixel (0, 0) at (0, 0) and its centre at
(0.5, 0.5). The NumPy code this replaces counted pixel centres as whole numbers; place()
still takes its numbers that way, see there.

Importing this module never fails. Without cairo, available() is False and whatever makes
a picture raises Unavailable.
"""
from __future__ import annotations

import math
import random
import struct
import subprocess
import threading
import zlib
from functools import lru_cache
from statistics import NormalDist

try:
    import cairo
except Exception:                 # not installed, or a build that does not load
    cairo = None

NONE, PAD, REFLECT = "none", "pad", "reflect"          # what lies outside a placed picture

if cairo is not None:
    RGB, ARGB = cairo.FORMAT_RGB24, cairo.FORMAT_ARGB32
    # how a context combines what it draws with what is there (context.set_operator)
    OVER, SOURCE, ADD, CLEAR = (cairo.OPERATOR_OVER, cairo.OPERATOR_SOURCE, cairo.OPERATOR_ADD,
                                cairo.OPERATOR_CLEAR)
    DIFFERENCE, DEST_OUT = cairo.OPERATOR_DIFFERENCE, cairo.OPERATOR_DEST_OUT
    _EXTEND = {PAD: cairo.EXTEND_PAD, REFLECT: cairo.EXTEND_REFLECT}
else:
    RGB = ARGB = OVER = SOURCE = ADD = CLEAR = DIFFERENCE = DEST_OUT = None
    _EXTEND = {}


class Unavailable(RuntimeError):
    """cairo is not on this machine: nothing can be drawn (recording and the plain export
    do not need it)."""


class ImageError(ValueError):
    """A file that could not be read as a picture, or written as one."""


def available() -> bool:
    return cairo is not None


def _need():
    if cairo is None:
        raise Unavailable("Python's cairo module is missing (package python-cairo): "
                          "Dolly draws the editor preview and the styled export with it")


# ------------------------------------------------------------------ frames and sprites
def new(w: int, h: int, alpha: bool = False):
    """A black frame, or with alpha=True a clear sprite."""
    _need()
    return cairo.ImageSurface(ARGB if alpha else RGB, int(w), int(h))


def frame_from(buf, w: int, h: int, alpha: bool = False):
    """Wrap a bytearray of 4 * w * h bytes (ffmpeg's bgr0, or premultiplied bgra with
    alpha=True) as a picture, without copying: drawing on it changes `buf`. The surface
    keeps `buf` alive; after writing into `buf` yourself, call changed()."""
    _need()
    if len(buf) != 4 * w * h:
        raise ValueError(f"a {w}x{h} picture is {4 * w * h} bytes, not {len(buf)}")
    return cairo.ImageSurface.create_for_data(buf, ARGB if alpha else RGB, w, h, 4 * w)


def read_frame(stream, w: int, h: int):
    """The next bgr0 frame of a pipe as a frame of its own, or None at the end."""
    buf = bytearray(4 * w * h)
    view, got = memoryview(buf), 0
    while got < len(buf):
        n = stream.readinto(view[got:])
        if not n:
            return None
        got += n
    del view
    return frame_from(buf, w, h)


def data(surface):
    """The picture's bytes (a view, not a copy), rows of 4 bytes a pixel: what an ffmpeg
    pipe reading bgr0 takes. Everything drawn so far is in them."""
    surface.flush()
    return surface.get_data()


def changed(surface):
    """Tell cairo that the bytes of `surface` were written directly."""
    surface.mark_dirty()


def has_alpha(surface) -> bool:
    return surface.get_format() == ARGB


def copy(surface):
    w, h = surface.get_width(), surface.get_height()
    return cairo.ImageSurface.create_for_data(bytearray(data(surface)), surface.get_format(), w, h,
                                              surface.get_stride())


def crop(surface, x: int, y: int, w: int, h: int):
    """That rectangle as a picture of its own; what lies outside `surface` is black (clear
    in a sprite)."""
    out = cairo.ImageSurface(surface.get_format(), int(w), int(h))
    c = cairo.Context(out)
    c.set_operator(SOURCE)
    c.set_source_surface(surface, -int(x), -int(y))
    c.paint()
    return out


def context(surface):
    """A cairo context that draws on `surface`."""
    _need()
    return cairo.Context(surface)


def _ctx(target):
    return target if isinstance(target, cairo.Context) else cairo.Context(target)


def rgb(color) -> tuple:
    """"#rrggbb" or (r, g, b) in 0..255 -> (r, g, b) in 0..1, as the drawing calls take."""
    if isinstance(color, str):
        h = color.lstrip("#")[:6]
        return int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0
    return color[0] / 255.0, color[1] / 255.0, color[2] / 255.0


# ------------------------------------------------------------------ reading values back
def pixel(surface, x: int, y: int) -> tuple:
    """(r, g, b) of a frame's pixel, (r, g, b, a) of a sprite's (as stored: multiplied by a)."""
    d = data(surface)
    o = y * surface.get_stride() + 4 * x
    return (d[o + 2], d[o + 1], d[o]) + ((d[o + 3],) if has_alpha(surface) else ())


def plane(surface, channel: str) -> bytes:
    """One of "r", "g", "b", "a" as bytes, row after row, one byte a pixel."""
    return bytes(data(surface))["bgra".index(channel)::4]


def mean(surface) -> tuple:
    """Average (r, g, b) of a frame, (r, g, b, a) of a sprite, in 0..255."""
    n = max(1, surface.get_width() * surface.get_height())
    return tuple(sum(plane(surface, ch)) / n for ch in ("rgba" if has_alpha(surface) else "rgb"))


def same(a, b) -> bool:
    """Whether two pictures show the same thing (a frame's unused byte does not count)."""
    if (a.get_format(), a.get_width(), a.get_height()) != (b.get_format(), b.get_width(), b.get_height()):
        return False
    da, db = bytes(data(a)), bytes(data(b))
    return all(da[i::4] == db[i::4] for i in range(4 if has_alpha(a) else 3))


# ------------------------------------------------------------------ files
def _ffmpeg(args, stdin=None) -> bytes:
    from .ff import ffmpeg_bin
    try:
        r = subprocess.run([ffmpeg_bin(), "-v", "error", *args], input=stdin, capture_output=True, timeout=120)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        raise ImageError(f"ffmpeg could not run: {e}") from None
    if r.returncode != 0:
        raise ImageError(r.stderr.decode(errors="replace").strip()[-300:] or "ffmpeg failed")
    return r.stdout


def _kind(path):
    """("png", the file's first bytes), or the name of ffmpeg's reader for a JPEG or WebP
    file. What a file is is read from its first bytes, never from its name, and ffmpeg is
    told which reader to use: left to guess, it takes a text file for a picture."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError as e:
        raise ImageError(str(e)) from None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        return "png", head
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg_pipe", head
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp_pipe", head
    raise ImageError("not a PNG, JPEG or WebP picture")


def _jpeg_size(path) -> tuple:
    """(width, height) from a JPEG's frame header, stepping over the segments before it."""
    with open(path, "rb") as f:
        f.seek(2)
        while True:
            b = f.read(1)
            while b and b != b"\xff":                 # on to the next marker
                b = f.read(1)
            while b == b"\xff":
                b = f.read(1)
            if not b or b[0] in (0xd9, 0xda):         # the end, or picture data before any frame header
                raise ValueError("no frame header")
            if b[0] == 0x01 or 0xd0 <= b[0] <= 0xd8:  # markers that stand alone
                continue
            n, = struct.unpack(">H", f.read(2))
            if 0xc0 <= b[0] <= 0xcf and b[0] not in (0xc4, 0xc8, 0xcc):
                _depth, h, w = struct.unpack(">BHH", f.read(5))
                return w, h
            f.seek(n - 2, 1)


def image_size(path) -> tuple:
    """(width, height) of a PNG, JPEG or WebP file, read from its header: nothing is decoded
    and no program is started. ImageError when it is not such a file."""
    kind, head = _kind(path)
    try:
        if kind == "png":
            w, h = struct.unpack(">II", head[16:24])
        elif kind == "jpeg_pipe":
            w, h = _jpeg_size(path)
        elif head[12:16] == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":      # lossy WebP: 14 bits each
            w, h = (v & 0x3fff for v in struct.unpack("<HH", head[26:30]))
        elif head[12:16] == b"VP8L" and head[20] == 0x2f:                    # lossless: 14 bits each, less one
            bits, = struct.unpack("<I", head[21:25])
            w, h = (bits & 0x3fff) + 1, (bits >> 14 & 0x3fff) + 1
        elif head[12:16] == b"VP8X":                                         # extended: 24 bits each, less one
            w, h = int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
        else:
            raise ValueError("unknown WebP")
    except (struct.error, IndexError, ValueError, OSError):
        raise ImageError("not a readable picture") from None
    if w < 1 or h < 1:
        raise ImageError("not a readable picture")
    return w, h


def load(path, size: tuple | None = None):
    """A PNG, JPEG or WebP file as a frame, or as a sprite when it is a PNG with
    transparency. cairo reads PNG itself; the others are decoded by one ffmpeg call.
    size=(w, h) gives the picture scaled to fill that size, its middle kept (a wallpaper):
    ffmpeg then scales while it decodes, which halves the time for a 4K JPEG."""
    _need()
    p = str(path)
    kind, _head = _kind(p)
    if kind == "png":
        try:
            s = cairo.ImageSurface.create_from_png(p)
        except (cairo.Error, MemoryError, OSError) as e:
            raise ImageError(f"not a readable PNG: {e}") from None
        if s.get_format() not in (RGB, ARGB):          # a 16-bit PNG comes as a float picture
            deep = s
            s = new(deep.get_width(), deep.get_height(), deep.get_format() == getattr(cairo, "FORMAT_RGBA128F", None))
            c = cairo.Context(s)
            c.set_operator(SOURCE)
            c.set_source_surface(deep, 0, 0)
            c.paint()
        return cover(s, *size) if size else s
    # ffmpeg's scaler turns a JPEG's colours into ours exactly only when asked to (its
    # quick way is a level of green off). A BMP says its own size and its 32-bit pixels
    # are cairo's; it holds the bottom row first, so the picture is turned upside down
    wh = f"{int(size[0])}:{int(size[1])}" if size else ""
    vf = (f"scale={wh}:force_original_aspect_ratio=increase" if size else "scale=iw:ih") + \
        ":flags=lanczos+accurate_rnd+full_chroma_int" + (f",crop={wh}" if size else "") + ",format=bgra,vflip"
    bmp = _ffmpeg(["-f", kind, "-i", p, "-frames:v", "1", "-vf", vf, "-c:v", "bmp", "-f", "image2pipe", "-"])
    if bmp[:2] != b"BM" or len(bmp) < 54:
        raise ImageError("not a readable picture")
    start, = struct.unpack_from("<I", bmp, 10)
    w, h, _planes, bits = struct.unpack_from("<iiHH", bmp, 18)
    if bits != 32 or w <= 0 or h <= 0 or len(bmp) < start + 4 * w * h:
        raise ImageError("not a readable picture")
    return frame_from(bytearray(memoryview(bmp)[start:start + 4 * w * h]), w, h)


def save(surface, path, quality: int = 90):
    """Write a picture; the file type follows the name. PNG is written here (fast, lightly
    compressed: these are previews and checks), anything else by one ffmpeg call."""
    p = str(path)
    if not p.lower().endswith(".png"):
        w, h = surface.get_width(), surface.get_height()
        q = max(2, min(31, round(2 + (100 - quality) * 0.29)))        # ffmpeg's scale: 2 best, 31 worst
        _ffmpeg(["-y", "-f", "rawvideo", "-pix_fmt", "bgr0", "-s", f"{w}x{h}", "-i", "-", "-frames:v", "1",
                 "-q:v", str(q), "-pix_fmt", "yuvj420p", p], stdin=bytes(data(surface)))
        return
    if has_alpha(surface):
        surface.write_to_png(p)                       # cairo divides the alpha back out
        return
    w, h = surface.get_width(), surface.get_height()
    d = bytes(data(surface))
    px = bytearray(3 * w * h)
    px[0::3], px[1::3], px[2::3] = d[2::4], d[1::4], d[0::4]
    rows = b"".join(b"\0" + px[i:i + 3 * w] for i in range(0, len(px), 3 * w))

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    with open(p, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------ place / resize
def _scaled(src, fx: float, fy: float):
    """`src` at half its width and/or height (fx, fy: 0.5 or 1). Bilinear sampling exactly
    between two pixels is their average, and it is the fast path of cairo's scaler."""
    w, h = src.get_width(), src.get_height()
    out = cairo.ImageSurface(src.get_format(), max(1, math.ceil(w * fx)), max(1, math.ceil(h * fy)))
    c = cairo.Context(out)
    c.scale(fx, fy)
    c.set_source_surface(src, 0, 0)
    p = c.get_source()
    p.set_filter(cairo.FILTER_BILINEAR)
    p.set_extend(cairo.EXTEND_PAD)
    c.set_operator(SOURCE)
    c.paint()
    return out


def placed_box(src_w: float, src_h: float, kx: float, ky: float, tx: float, ty: float) -> tuple:
    """(x, y, width, height), in a context's coordinates, of where place() puts a picture
    of that size: the rectangle to clip to, or to round the corners of."""
    return tx + 0.5 - 0.5 * kx, ty + 0.5 - 0.5 * ky, src_w * kx, src_h * ky


def place(target, src, kx: float, ky: float, tx: float, ty: float, filter: str | None = None,
          extend: str = NONE, alpha: float = 1.0, operator=None):
    """Draw `src` scaled by (kx, ky) and shifted by (tx, ty) on `target` (a surface, or a
    context with its clip). The numbers mean what they did in the NumPy renderer: output
    pixel (x, y) shows the source point ((x - tx) / kx, (y - ty) / ky), pixel centres at
    whole numbers; any fraction of a pixel is honoured.

    filter: None picks by scale. Enlarging is bilinear. When it shrinks, every source pixel
    has to count, and evenly, or small text looks ragged and shimmers as the camera moves:
    the picture is halved (an exact average) until one bilinear step at twice the size,
    averaged down, does it. (Plain bilinear below full size weighs neighbouring pixels up
    to 2:1; an export of a screen at rest came out 3 to 7 % larger for it.) Within 3 % of
    full size plain bilinear is the closer one.
    "sharp" is Catmull-Rom, as crisp as the old bicubic and some 25 times slower: for an
    enlarged recording in the better export qualities and in stills. "nearest" keeps blocks.
    extend: NONE draws the picture's own rectangle and nothing else, PAD continues its
    edge pixels over the whole clip, REFLECT mirrors it there (slow when the mirrored part
    is actually reached).
    alpha and operator are how it is laid on (operator=ADD and a weight: averaging)."""
    if kx <= 0 or ky <= 0:
        raise ValueError("gfx.place scales and shifts only")
    c = _ctx(target)
    ox, oy = tx + 0.5 - 0.5 * kx, ty + 0.5 - 0.5 * ky       # where the source's corner lands
    flt = {"sharp": cairo.FILTER_BEST, "nearest": cairo.FILTER_NEAREST}.get(filter, cairo.FILTER_BILINEAR)
    c.save()
    if operator is not None:
        c.set_operator(operator)
    if extend == NONE:
        # its rectangle as the clip and the edge continued under it: the same picture as
        # letting it end, and cairo's scaler then stays on its fast path
        c.rectangle(ox, oy, src.get_width() * kx, src.get_height() * ky)
        c.clip()
        extend = PAD
    if filter is None:
        while kx < 0.5 or ky < 0.5:
            fx, fy = (0.5 if kx < 0.5 else 1.0), (0.5 if ky < 0.5 else 1.0)
            src, kx, ky = _scaled(src, fx, fy), kx / fx, ky / fy
        sx, sy = (2 if kx < 0.97 else 1), (2 if ky < 0.97 else 1)
        if sx > 1 or sy > 1:
            x0, y0, x1, y1 = c.clip_extents()
            x0, y0, x1, y1 = math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1)
            if x1 <= x0 or y1 <= y0:
                c.restore()
                return
            big = cairo.ImageSurface(src.get_format(), sx * (x1 - x0), sy * (y1 - y0))
            b = cairo.Context(big)
            b.translate(sx * (ox - x0), sy * (oy - y0))
            b.scale(sx * kx, sy * ky)
            b.set_source_surface(src, 0, 0)
            b.get_source().set_filter(cairo.FILTER_BILINEAR)
            b.get_source().set_extend(_EXTEND[extend])
            b.set_operator(SOURCE)
            b.paint()
            src, kx, ky, ox, oy = big, 1.0 / sx, 1.0 / sy, x0, y0
    c.translate(ox, oy)
    c.scale(kx, ky)
    c.set_source_surface(src, 0, 0)
    p = c.get_source()
    p.set_filter(flt)
    p.set_extend(_EXTEND[extend])
    if alpha >= 1.0:
        c.paint()
    else:
        c.paint_with_alpha(max(0.0, alpha))
    c.restore()


def resize(src, w: int, h: int, filter: str | None = None):
    """`src` at w x h (the whole picture onto the whole picture)."""
    w, h = int(w), int(h)
    if (w, h) == (src.get_width(), src.get_height()):
        return copy(src)
    out = cairo.ImageSurface(src.get_format(), w, h)
    kx, ky = w / src.get_width(), h / src.get_height()
    place(out, src, kx, ky, 0.5 * kx - 0.5, 0.5 * ky - 0.5, filter, PAD, operator=SOURCE)
    return out


def cover(src, w: int, h: int):
    """`src` scaled to fill w x h, its middle kept (a wallpaper behind a recording)."""
    k = max(w / src.get_width(), h / src.get_height())
    nw, nh = math.ceil(src.get_width() * k), math.ceil(src.get_height() * k)
    out = cairo.ImageSurface(src.get_format(), int(w), int(h))
    place(out, src, k, k, 0.5 * k - 0.5 - (nw - w) // 2, 0.5 * k - 0.5 - (nh - h) // 2, None, PAD, operator=SOURCE)
    return out


# ------------------------------------------------------------------ blur
def _gauss(src, sigma: float):
    """A true Gaussian of a small picture: copies shifted by whole pixels, mixed in one
    after the other, the far ones first. Each step is a cross-fade, so a flat picture
    stays exactly flat."""
    w, h = src.get_width(), src.get_height()
    r = max(1, math.ceil(3 * sigma))
    g = {i: math.exp(-0.5 * (i / sigma) ** 2) for i in range(-r, r + 1)}
    order = sorted(g, key=abs, reverse=True)
    opaque = not has_alpha(src)
    for dx, dy, reach in ((1, 0, w), (0, 1, h)):
        # the picture with its edge pixels continued, so every shifted copy covers the result
        m = min(r, reach)
        wide = cairo.ImageSurface(src.get_format(), w + 2 * m * dx, h + 2 * m * dy)
        c = cairo.Context(wide)
        c.set_operator(SOURCE)
        c.set_source_surface(src, m * dx, m * dy)
        c.get_source().set_extend(cairo.EXTEND_PAD)
        c.paint()
        out = cairo.ImageSurface(src.get_format(), w, h)
        c = cairo.Context(out)
        total = 0.0
        for i in order:
            if abs(i) > m:
                continue
            total += g[i]
            t = g[i] / total
            if total == g[i]:
                c.set_operator(SOURCE)
                c.set_source_surface(wide, (i - m) * dx, (i - m) * dy)
                c.paint()
            elif opaque:
                c.set_operator(OVER)
                c.set_source_surface(wide, (i - m) * dx, (i - m) * dy)
                c.paint_with_alpha(t)
            else:                                   # a sprite: fade what is there, add the share
                c.set_operator(DEST_OUT)
                c.set_source_rgba(0, 0, 0, t)
                c.paint()
                c.set_operator(ADD)
                c.set_source_surface(wide, (i - m) * dx, (i - m) * dy)
                c.paint_with_alpha(t)
        src = out
    return src


def blur(src, sigma: float):
    """`src` blurred like a Gaussian of standard deviation `sigma` pixels; the edge pixels
    continue outwards. A new picture.

    The picture is halved (each halving and the doubling back blur a known amount) until
    what is left to blur is 1.25 to 2.5 pixels wide, blurred there with a true Gaussian,
    and doubled back up. Against the exact Gaussian on real stills: PSNR 43 to 55 dB, the
    width within 5 %, the brightness within half a level; a flat picture stays exactly
    flat. A 1920x1200 frame takes about 16 ms from sigma 5 up (30 ms at sigma 3, 40 to
    60 ms below 2.5, where nothing is halved), an 800x320 box 2 ms. A sprite takes a third
    of that and loses the faintest fringe (a shadow's last level or two of 255)."""
    sigma = float(sigma)
    if sigma < 0.3:
        return copy(src)
    sizes, cur, n = [], src, 0
    while sigma / 2 ** n >= 2.5 and min(cur.get_width(), cur.get_height()) > 8:
        sizes.append((cur.get_width(), cur.get_height()))
        cur, n = _scaled(cur, 0.5, 0.5), n + 1
    # n halvings and doublings spread by a variance of (4^n - 1) / 3; the rest is the Gaussian's
    rest = math.sqrt(max(0.0, sigma * sigma - (4 ** n - 1) / 3.0)) / 2 ** n
    if rest >= 0.3:
        cur = _gauss(cur, rest)
    # cairo's scaler rounds down, which darkens a little with every step. A frame is
    # doubled back up as its negative: that rounds up, and evens out the halvings
    negative = bool(sizes) and not has_alpha(src)
    if negative:
        invert(cur)
    for w, h in reversed(sizes):
        up = cairo.ImageSurface(src.get_format(), w, h)
        c = cairo.Context(up)
        c.scale(2, 2)
        c.set_source_surface(cur, 0, 0)
        c.get_source().set_filter(cairo.FILTER_BILINEAR)
        c.get_source().set_extend(cairo.EXTEND_PAD)
        c.set_operator(SOURCE)
        c.paint()
        cur = up
    if negative:
        invert(cur)
    return copy(cur) if cur is src else cur


def _inside(surface, x, y, w, h):
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(surface.get_width(), int(x) + int(w)), min(surface.get_height(), int(y) + int(h))
    return x0, y0, x1 - x0, y1 - y0


def blur_rect(surface, x, y, w, h, sigma: float):
    """Blur that rectangle of `surface` where it is; nothing from outside it bleeds in."""
    x, y, w, h = _inside(surface, x, y, w, h)
    if w < 1 or h < 1:
        return
    c = cairo.Context(surface)
    c.set_operator(SOURCE)
    c.set_source_surface(blur(crop(surface, x, y, w, h), sigma), x, y)
    c.rectangle(x, y, w, h)
    c.fill()


def pixelate_rect(surface, x, y, w, h, cell: int):
    """Turn that rectangle of `surface` into blocks of about `cell` pixels."""
    x, y, w, h = _inside(surface, x, y, w, h)
    if w < 1 or h < 1:
        return
    small = resize(crop(surface, x, y, w, h), max(1, w // max(1, int(cell))), max(1, h // max(1, int(cell))))
    c = cairo.Context(surface)
    c.rectangle(x, y, w, h)
    c.clip()
    c.translate(x, y)
    c.scale(w / small.get_width(), h / small.get_height())
    c.set_source_surface(small, 0, 0)
    c.get_source().set_filter(cairo.FILTER_NEAREST)
    c.get_source().set_extend(cairo.EXTEND_PAD)
    c.set_operator(SOURCE)
    c.paint()


def soft_rect(w: int, h: int, x: float, y: float, rw: float, rh: float, sigma: float, radius: float = 0.0):
    """A w x h mask of the rectangle (x, y, rw, rh), corners rounded by `radius`, blurred
    by `sigma`: a drop shadow. Lay it on with paint_mask()."""
    m = new(w, h, alpha=True)
    c = cairo.Context(m)
    rounded_rect(c, x, y, rw, rh, radius)
    c.set_source_rgba(0, 0, 0, 1)
    c.fill()
    return blur(m, sigma)


def shadow(c, x: float, y: float, w: float, h: float, sigma: float, radius: float, alpha: float):
    """A soft black shadow of the rectangle (x, y, w, h), corners rounded by `radius`, on the
    context `c`. It is made at a quarter of the size and enlarged: nothing in a shadow is
    sharp, blurring is the costly part, and a followed window's shadow is new with every
    frame of a zoom."""
    q = 4
    target = c.get_target()
    x0, y0 = max(0, math.floor(x - 3 * sigma)), max(0, math.floor(y - 3 * sigma))
    x1 = min(target.get_width(), math.ceil(x + w + 3 * sigma))
    y1 = min(target.get_height(), math.ceil(y + h + 3 * sigma))
    if x1 <= x0 or y1 <= y0:
        return
    m = soft_rect(math.ceil((x1 - x0) / q), math.ceil((y1 - y0) / q), (x - x0) / q, (y - y0) / q, w / q, h / q,
                      sigma / q, radius / q)
    place(c, m, q, q, x0 + 0.5 * q - 0.5, y0 + 0.5 * q - 0.5, alpha=alpha)


# ------------------------------------------------------------------ mixing
def mix(a, b, t: float):
    """Cross-fade: `a` at t = 0, `b` at t = 1. A new picture."""
    return average([a, b], [1.0 - float(t), float(t)])


def average(pictures, weights=None):
    """The weighted average of pictures of one size (sub-frames of a motion blur). Frames
    are cross-faded in one after the other, which keeps their brightness exactly; sprites
    are added up share by share."""
    weights = [1.0] * len(pictures) if weights is None else [float(v) for v in weights]
    first = pictures[0]
    if not has_alpha(first):
        out, total = copy(first), weights[0]
        c = cairo.Context(out)
        for p, wt in zip(pictures[1:], weights[1:]):
            total += wt
            if wt > 0 and total > 0:
                c.set_source_surface(p, 0, 0)
                c.paint_with_alpha(min(1.0, wt / total))
        return out
    out, total = new(first.get_width(), first.get_height(), alpha=True), sum(weights) or 1.0
    c = cairo.Context(out)
    c.set_operator(ADD)
    for p, wt in zip(pictures, weights):
        c.set_source_surface(p, 0, 0)
        c.paint_with_alpha(max(0.0, min(1.0, wt / total)))
    return out


def paint_mask(target, mask, color, alpha: float = 1.0, x: float = 0, y: float = 0):
    """Lay a colour on `target` through `mask` (a shadow: a dark colour through soft_rect)."""
    c = _ctx(target)
    c.save()
    c.set_source_rgba(color[0], color[1], color[2], alpha)
    c.mask_surface(mask, x, y)
    c.restore()


# ------------------------------------------------------------------ shapes and gradients
def rounded_rect(c, x: float, y: float, w: float, h: float, r: float):
    """Add a rectangle with round corners to the path of context `c` (then fill or clip)."""
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    if r <= 0:
        c.rectangle(x, y, w, h)
        return
    c.new_sub_path()
    c.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    c.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    c.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    c.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    c.close_path()


def polygon(c, pts, dx: float = 0.0, dy: float = 0.0):
    """Add the closed polygon to the path, always running the same way round: several
    in one path then fill as their union."""
    area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))
    pts = pts if area >= 0 else pts[::-1]
    c.move_to(pts[0][0] + dx, pts[0][1] + dy)
    for x, y in pts[1:]:
        c.line_to(x + dx, y + dy)
    c.close_path()


def fill_rect(target, x: float, y: float, w: float, h: float, color, alpha: float = 1.0):
    c = _ctx(target)
    c.save()
    c.set_source_rgba(color[0], color[1], color[2], alpha)
    c.rectangle(x, y, w, h)
    c.fill()
    c.restore()


def disc(target, cx: float, cy: float, r: float, color, alpha: float = 1.0):
    """A filled circle with a smooth edge, centred anywhere."""
    c = _ctx(target)
    c.save()
    c.set_source_rgba(color[0], color[1], color[2], alpha)
    c.arc(cx, cy, max(0.0, r), 0, 2 * math.pi)
    c.fill()
    c.restore()


def ring(target, cx: float, cy: float, r: float, width: float, color, alpha: float = 1.0):
    """A circle line `width` wide, half of it on each side of radius `r`."""
    c = _ctx(target)
    c.save()
    c.set_source_rgba(color[0], color[1], color[2], alpha)
    c.set_line_width(width)
    c.arc(cx, cy, max(0.0, r), 0, 2 * math.pi)
    c.stroke()
    c.restore()


def _stops(g, stops):
    for st in stops:
        col = st[1]
        g.add_color_stop_rgba(st[0], col[0], col[1], col[2], st[2] if len(st) > 2 else 1.0)
    if hasattr(g, "set_dither"):          # cairo 1.18: no bands in a slow gradient
        g.set_dither(cairo.Dither.BEST)
    return g


def linear_gradient(x0: float, y0: float, x1: float, y1: float, stops):
    """A colour run from (x0, y0) to (x1, y1) to set as a context's source (or to mask with).
    stops: [(position 0..1, (r, g, b), alpha = 1), ...]."""
    _need()
    return _stops(cairo.LinearGradient(x0, y0, x1, y1), stops)


def radial_gradient(cx: float, cy: float, r: float, stops):
    """The same from the centre (cx, cy) out to radius `r`."""
    _need()
    return _stops(cairo.RadialGradient(cx, cy, 0, cx, cy, r), stops)


def fill_gradient(img, stops, angle_deg):
    """Fill the frame `img` with a colour run at a CSS angle (0deg = to top, 90 = to right)."""
    w, h = img.get_width(), img.get_height()
    a = math.radians(angle_deg)
    dx, dy = math.sin(a), -math.cos(a)
    half = abs(w / 2 * dx) + abs(h / 2 * dy)
    mx, my = w / 2 + 0.5, h / 2 + 0.5
    c = _ctx(img)
    c.save()
    c.set_source(linear_gradient(mx - dx * half, my - dy * half, mx + dx * half, my + dy * half,
                                 [(i / max(1, len(stops) - 1), rgb(s)) for i, s in enumerate(stops)]))
    c.paint()
    c.restore()


def glow(img):
    """A soft light from the top left, for depth: every pixel times 0.92 + 0.12 * glow, the
    glow a Gaussian hill 0.6 of the width and 0.8 of the height wide."""
    w, h = img.get_width(), img.get_height()
    light = copy(img)
    c = _ctx(img)
    fill_rect(c, 0, 0, w, h, (0, 0, 0), 0.08)               # times 0.92 everywhere,
    c.save()
    c.set_operator(ADD)                                     # plus 0.12 * glow of the picture itself
    c.set_source_surface(light, 0, 0)                       # (set before the stretch, so it stays put)
    c.translate(0.2 * w + 0.5, -0.1 * h + 0.5)
    c.scale(0.6 * w, 0.8 * h)                               # the hill is round in stretched coordinates
    c.mask(radial_gradient(0, 0, 3.0, [(i / 24, (0, 0, 0), 0.12 * math.exp(-(3.0 * i / 24) ** 2))
                                       for i in range(25)]))
    c.restore()


def add_noise(surface, sigma: float = 0.9, seed: int = 7):
    """Add grey noise of standard deviation `sigma` levels to a frame, the same for a given
    seed and size: it keeps a smooth background from banding once H.264 has had it."""
    w, h = surface.get_width(), surface.get_height()
    nd = NormalDist(0.0, max(1e-6, sigma))
    levels = [round(nd.inv_cdf((i + 0.5) / 256)) for i in range(256)]      # a byte -> a Gaussian step
    raw = random.Random(seed).randbytes(w * h)
    c = cairo.Context(surface)
    for table, op in ((bytes(max(0, v) for v in levels), ADD), (bytes(max(0, -v) for v in levels), DIFFERENCE)):
        buf = bytearray(4 * w * h)
        buf[0::4] = buf[1::4] = buf[2::4] = raw.translate(table)
        c.set_operator(op)                          # DIFFERENCE with a smaller value subtracts it
        c.set_source_surface(frame_from(buf, w, h), 0, 0)
        c.paint()


# ------------------------------------------------------------------ small analysis pictures
def difference(a, b):
    """|a - b| for every colour of every pixel, as a new frame."""
    out = copy(a)
    c = cairo.Context(out)
    c.set_operator(DIFFERENCE)
    c.set_source_surface(b, 0, 0)
    c.paint()
    return out


def invert(surface):
    """255 - value for every colour of a frame, in place."""
    c = cairo.Context(surface)
    c.set_operator(DIFFERENCE)
    c.set_source_rgb(1, 1, 1)
    c.paint()
    return surface


def threshold(surface, level: int):
    """In place: every colour of a frame becomes 255 where it was above `level`, else 0."""
    c = cairo.Context(surface)
    v = (254 - int(level)) / 255.0
    if 0 < v:
        c.set_operator(ADD)                          # values above the level reach 255, no other does
        c.set_source_rgb(v, v, v)
        c.paint()
    # with black, COLOR_BURN keeps 255 and drops everything else to 0 (nothing is above 255)
    c.set_operator(cairo.OPERATOR_COLOR_BURN if v >= 0 else SOURCE)
    c.set_source_rgb(0, 0, 0)
    c.paint()
    return surface


# ------------------------------------------------------------------ text
_PANGO = None           # (Pango, PangoCairo) once looked for; False when gi or Pango is missing


def _pango():
    global _PANGO
    if _PANGO is None:
        try:
            import gi
            gi.require_version("Pango", "1.0")
            gi.require_version("PangoCairo", "1.0")
            gi.require_foreign("cairo")
            from gi.repository import Pango, PangoCairo
            # a machine on which fontconfig finds no font at all: Pango draws little boxes
            # for every letter, cairo alone falls back to the plain font it carries inside
            probe = PangoCairo.create_layout(_scratch())
            probe.set_text("Ab", -1)
            _PANGO = False if probe.get_unknown_glyphs_count() else (Pango, PangoCairo)
        except Exception:
            _PANGO = False
    return _PANGO or None


def text_engine() -> str:
    """"pango" (shaping, fallback fonts, colour emoji), "cairo" (one font, plain letters)
    or "" when nothing can be drawn."""
    if cairo is None:
        return ""
    return "pango" if _pango() else "cairo"


class Font:
    """A font family at a size in pixels (the height of its em square). Make one with font()."""
    __slots__ = ("family", "size", "bold", "file")

    def __init__(self, family: str, size: float, bold: bool = False, file: str | None = None):
        self.family, self.size, self.bold, self.file = family, float(size), bool(bold), file

    def _key(self):
        return self.family, self.size, self.bold, self.file

    def __eq__(self, other):
        return isinstance(other, Font) and self._key() == other._key()

    def __hash__(self):
        return hash(self._key())

    def __repr__(self):
        return f"Font({self.family!r}, {self.size:g}, bold={self.bold})"


@lru_cache(maxsize=32)
def _font_file(path: str):
    """(family, bold) of a font file, as fontconfig reads it."""
    family, bold = "monospace", False
    try:
        out = subprocess.run(["fc-scan", "--format", "%{family[0]}\n%{weight}", path], capture_output=True,
                             text=True, timeout=5).stdout.splitlines()
        if out and out[0].strip():
            family = out[0].strip()
            bold = len(out) > 1 and float(out[1] or 80) >= 180       # fontconfig: 80 regular, 200 bold
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return family, bold


def font(name: str | None, size: float, bold: bool = False) -> Font:
    """`name`: a family, several separated by commas (the first one installed is used, as
    "JetBrainsMono Nerd Font, JetBrains Mono, monospace"), or the path of a font file (what
    fc-match gives; it need not be installed when Pango draws). None is the default
    monospace."""
    if name and "/" in name:
        family, file_bold = _font_file(name)
        return Font(family, size, bold or file_bold, name)
    return Font(name or "monospace", size, bold)


@lru_cache(maxsize=1)
def _font_options():
    fo = cairo.FontOptions()
    fo.set_antialias(cairo.ANTIALIAS_GRAY)            # never the desktop's coloured sub-pixels
    fo.set_hint_style(cairo.HINT_STYLE_SLIGHT)
    fo.set_hint_metrics(cairo.HINT_METRICS_OFF)       # the same layout at every size
    return fo


def _scratch():
    return cairo.Context(cairo.ImageSurface(ARGB, 8, 8))


@lru_cache(maxsize=64)
def _describe(f: Font):
    Pango = _pango()[0]
    d = Pango.FontDescription()
    d.set_family(f.family)
    d.set_weight(Pango.Weight.BOLD if f.bold else Pango.Weight.NORMAL)
    d.set_absolute_size(f.size * Pango.SCALE)
    return d


_KNOWN = threading.local()          # font files handed to Pango (it keeps its fonts per thread)


def _layout(c, text: str, f: Font):
    Pango, PangoCairo = _pango()
    known = _KNOWN.__dict__.setdefault("files", set())
    if f.file and f.file not in known:
        known.add(f.file)
        fonts = PangoCairo.FontMap.get_default()
        if hasattr(fonts, "add_font_file"):           # Pango 1.56: a font that is not installed
            try:
                fonts.add_font_file(f.file)
            except Exception:
                pass                                  # an installed one is found by its family anyway
    lay = PangoCairo.create_layout(c)
    pc = lay.get_context()
    PangoCairo.context_set_font_options(pc, _font_options())
    pc.set_round_glyph_positions(False)
    lay.context_changed()
    lay.set_font_description(_describe(f))
    lay.set_text(text, -1)
    return lay


def _toy(c, f: Font):
    c.select_font_face(f.family.split(",")[0].strip(), cairo.FONT_SLANT_NORMAL,
                       cairo.FONT_WEIGHT_BOLD if f.bold else cairo.FONT_WEIGHT_NORMAL)
    c.set_font_size(f.size)
    c.set_font_options(_font_options())


def _measure(c, text: str, f: Font):
    """(advance in pixels, the Pango layout to draw or None) of `text` on context `c`."""
    if _pango():
        lay = _layout(c, text, f)
        return lay.get_size()[0] / _pango()[0].SCALE, lay
    _toy(c, f)
    return c.text_extents(text).x_advance, None


def text_width(text: str, f: Font) -> float:
    """How far `text` advances in font `f`, in pixels (a fraction is kept)."""
    _need()
    return _measure(_scratch(), text, f)[0]


@lru_cache(maxsize=64)
def font_metrics(f: Font) -> tuple:
    """(ascent, descent) of the font in pixels: how far its letters reach above and below
    the baseline."""
    _need()
    c = _scratch()
    if _pango():
        Pango = _pango()[0]
        m = _layout(c, "", f).get_context().get_metrics(_describe(f), None)
        return m.get_ascent() / Pango.SCALE, m.get_descent() / Pango.SCALE
    _toy(c, f)
    return tuple(c.font_extents()[:2])


def draw_text(target, text: str, x: float, y: float, f: Font, color, alpha: float = 1.0, anchor: str = "la"):
    """Draw one line of text. `anchor` says what (x, y) is, as in Pillow: first letter
    l(eft), m(iddle) or r(ight); second a(scender: the top of the line), m(iddle of
    ascender and descender), s (the baseline) or d(escender)."""
    c = _ctx(target)
    asc, desc = font_metrics(f)
    base = y + {"a": asc, "m": (asc - desc) / 2.0, "s": 0.0, "d": -desc}[anchor[1]]
    c.save()
    width, lay = _measure(c, text, f)
    left = x - {"l": 0.0, "m": 0.5, "r": 1.0}[anchor[0]] * width
    c.set_source_rgba(color[0], color[1], color[2], alpha)
    if lay is not None:
        Pango, PangoCairo = _pango()
        c.move_to(left, base - lay.get_baseline() / Pango.SCALE)
        PangoCairo.show_layout(c, lay)
    else:
        c.move_to(left, base)
        c.show_text(text)
    c.restore()


def label(target, text: str, x: float, y: float, f: Font, color, alpha: float = 1.0, anchor: str = "m"):
    """One line of text with the middle of its line at y (anchor: l, m or r, as draw_text).
    Its baseline goes on a whole pixel row, where the flat strokes of letters are crisp, by
    the rounding 0.7.3 placed it with: the font's ascent and descent in whole pixels, and y
    cut to a whole pixel. Keycaps, captions, chapter chips and title cards are set this way."""
    asc, desc = font_metrics(f)
    base = math.floor(y) + math.floor((math.ceil(asc) - math.ceil(desc)) / 2 + 0.5)
    draw_text(target, text, x, base, f, color, alpha, anchor + "s")

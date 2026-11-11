"""Image operations for the renderer and the screen analysis, on NumPy and Pillow.

Everything works on NumPy arrays: frames are uint8 BGR (height, width, 3); masks and sprites
are float32, one plane or several. Only what Dolly needs is here:

- resizing, and placing a scaled picture (a scale and a shift, never a rotation);
- Gaussian blur, compositing through a mask, cross-fading;
- filled rectangles and circles, drawn into masks;
- growing and shrinking a mask, and labelling its connected regions;
- grey conversion and writing an image file.

Pillow does the resampling and the 8-bit compositing; NumPy does the rest.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image

# How a picture is resampled. LINEAR and CUBIC interpolate when they enlarge; when they shrink,
# Pillow widens their kernel so that every source pixel counts (no shimmer on small text, and
# no separate "area" mode to choose). Measured against the OpenCV build Dolly used until 0.7.0:
# with LINEAR for shrinking and CUBIC for enlarging, export frames score SSIM 0.994 to 0.999 on average
# and files are 0 to 4 % larger (harness/bench has the scripts).
NEAREST, LINEAR, CUBIC = 0, 1, 2
CONSTANT, REFLECT = 0, 1                      # what lies outside the picture when it is placed

_FILTER = {LINEAR: Image.BILINEAR, CUBIC: Image.BICUBIC, NEAREST: Image.NEAREST}


# ------------------------------------------------------------------ Pillow bridge
def _planes(a: np.ndarray):
    """-> (Pillow images, how to put them back together). Pillow has 8-bit grey and RGB
    images and 32-bit float grey ones; anything else goes plane by plane."""
    if a.dtype == np.uint8:
        if a.ndim == 2 or a.shape[2] == 3:
            return [Image.fromarray(np.ascontiguousarray(a))], False
        return [Image.fromarray(np.ascontiguousarray(a[..., c])) for c in range(a.shape[2])], True
    f = a.astype(np.float32, copy=False)
    if f.ndim == 2:
        return [Image.fromarray(np.ascontiguousarray(f))], False
    return [Image.fromarray(np.ascontiguousarray(f[..., c])) for c in range(f.shape[2])], True


def _resample(a: np.ndarray, size: tuple[int, int], flt, box=None) -> np.ndarray:
    ims, stacked = _planes(a)
    out = [i.resize(size, flt, box=box) for i in ims]
    if not stacked:
        return np.array(out[0]).astype(a.dtype, copy=False)          # np.array: a writable copy
    return np.stack([np.asarray(i) for i in out], axis=2).astype(a.dtype, copy=False)


# ------------------------------------------------------------------ resize / place
def resize(src: np.ndarray, size, interpolation=LINEAR) -> np.ndarray:
    """`src` at `size` = (width, height)."""
    w, h = int(size[0]), int(size[1])
    sh, sw = src.shape[:2]
    if (w, h) == (sw, sh):
        return src.copy()
    if interpolation == NEAREST:                    # blocks start at the top-left corner
        xs = np.minimum((np.arange(w) * (sw / w)).astype(np.intp), sw - 1)
        ys = np.minimum((np.arange(h) * (sh / h)).astype(np.intp), sh - 1)
        return src[ys][:, xs]
    return _resample(src, (w, h), _FILTER[interpolation])


def _reflect_pad(src: np.ndarray, left: int, top: int, right: int, bottom: int) -> np.ndarray:
    pad = [(top, bottom), (left, right)] + [(0, 0)] * (src.ndim - 2)
    return np.pad(src, pad, mode="symmetric")         # fedcba|abcdef|fedcba


def warp(src: np.ndarray, M, size, interpolation=LINEAR, border=CONSTANT, fill=0) -> np.ndarray:
    """Place `src` scaled and shifted in a (width, height) picture: the output pixel (x, y)
    shows the source point ((x - tx) / kx, (y - ty) / ky), for M = [[kx, 0, tx], [0, ky, ty]]
    with pixel centres at whole numbers. Outside the source the picture is `fill`
    (CONSTANT) or the source mirrored at its edges (REFLECT).

    It is a crop and a resize with a sub-pixel source box. With CONSTANT, the one output row
    or column that straddles the edge of the source stays at `fill` instead of being a
    blend; in Dolly that line lies under the rounded-corner mask."""
    w, h = int(size[0]), int(size[1])
    kx, ky, tx, ty = float(M[0][0]), float(M[1][1]), float(M[0][2]), float(M[1][2])
    if abs(float(M[0][1])) > 1e-9 or abs(float(M[1][0])) > 1e-9 or kx <= 0 or ky <= 0:
        raise ValueError("imgops.warp places with a scale and a shift only")
    sh, sw = src.shape[:2]
    flt = _FILTER[interpolation]

    def box_of(j0, j1, k, t):        # source span (pixel i covers [i, i + 1]) seen by output [j0, j1)
        lo = (j0 - t) / k + 0.5 - 0.5 / k
        return lo, lo + (j1 - j0) / k

    if border == REFLECT:
        le, ri = box_of(0, w, kx, tx)
        to, bo = box_of(0, h, ky, ty)
        pl, pt = max(0, int(math.ceil(-le))), max(0, int(math.ceil(-to)))
        pr, pb = max(0, int(math.ceil(ri - sw))), max(0, int(math.ceil(bo - sh)))
        if pl or pt or pr or pb:
            pl, pr, pt, pb = min(pl, sw), min(pr, sw), min(pt, sh), min(pb, sh)
            src = _reflect_pad(src, pl, pt, pr, pb)
            le, ri, to, bo = le + pl, ri + pl, to + pt, bo + pt
            sh, sw = src.shape[:2]
        le, to = max(0.0, le), max(0.0, to)
        ri, bo = min(float(sw), ri), min(float(sh), bo)
        return _resample(src, (w, h), flt, box=(le, to, ri, bo))
    # CONSTANT: only the output columns and rows whose footprint lies inside the source
    eps = 1e-6
    j0 = max(0, int(math.ceil(tx + 0.5 - 0.5 * kx - eps)))
    j1 = min(w, int(math.floor(tx + 0.5 + (sw - 0.5) * kx + eps)))
    i0 = max(0, int(math.ceil(ty + 0.5 - 0.5 * ky - eps)))
    i1 = min(h, int(math.floor(ty + 0.5 + (sh - 0.5) * ky + eps)))
    out = np.full((h, w) + src.shape[2:], fill, src.dtype)
    if j1 <= j0 or i1 <= i0:
        return out
    le, ri = box_of(j0, j1, kx, tx)
    to, bo = box_of(i0, i1, ky, ty)
    le, to = min(max(0.0, le), sw - 1e-3), min(max(0.0, to), sh - 1e-3)
    ri, bo = max(min(float(sw), ri), le + 1e-3), max(min(float(sh), bo), to + 1e-3)
    out[i0:i1, j0:j1] = _resample(src, (j1 - j0, i1 - i0), flt, box=(le, to, ri, bo))
    return out


# ------------------------------------------------------------------ blur / composite
def _gauss(sigma: float, eight_bit: bool) -> np.ndarray:
    k = int(round(sigma * (3 if eight_bit else 4) * 2 + 1)) | 1      # as wide as the picture's depth needs
    x = np.arange(k, dtype=np.float64) - k // 2
    g = np.exp(-(x * x) / (2.0 * sigma * sigma))
    return (g / g.sum()).astype(np.float32)


def _blur_axis(a: np.ndarray, g: np.ndarray, axis: int) -> np.ndarray:
    r = len(g) // 2
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r, r)
    n = a.shape[axis]
    p = np.pad(a, pad, mode="reflect" if n > r else "edge")
    out = np.zeros_like(a)
    sl = [slice(None)] * a.ndim
    for i, gi in enumerate(g):
        sl[axis] = slice(i, i + n)
        out += gi * p[tuple(sl)]
    return out


def _blur_fft(a: np.ndarray, sigma: float) -> np.ndarray:
    """A large Gaussian of one float plane, exact, in the frequency domain (mirrored edges)."""
    h, w = a.shape
    r = int(math.ceil(3.5 * sigma))
    y0, x0 = min(r, h - 1), min(r, w - 1)
    p = np.pad(a, ((y0, y0), (x0, x0)), mode="reflect")
    fy = np.fft.fftfreq(p.shape[0])[:, None]
    fx = np.fft.rfftfreq(p.shape[1])[None, :]
    resp = np.exp(-2.0 * (math.pi * sigma) ** 2 * (fx * fx + fy * fy))
    out = np.fft.irfft2(np.fft.rfft2(p) * resp, s=p.shape)
    return out[y0:y0 + h, x0:x0 + w].astype(np.float32)


def gaussian_blur(src: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur with standard deviation `sigma` pixels; edges are mirrored."""
    sigma = float(sigma)
    eight = src.dtype == np.uint8
    a = src.astype(np.float32)
    if a.ndim == 2 and sigma >= 6 and min(a.shape) > 8:
        out = _blur_fft(a, sigma)
    else:
        g = _gauss(sigma, eight)
        out = _blur_axis(_blur_axis(a, g, 1), g, 0)
    if eight:
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)
    return out


def soft_rect(shape, p0, p1, sigma: float) -> np.ndarray:
    """A filled rectangle (corners p0 and p1, both included, clipped to the picture), blurred
    with `sigma`: float32, 1 inside. The same picture as gaussian_blur of the drawn
    rectangle, at the cost of two short one-dimensional blurs."""
    h, w = int(shape[0]), int(shape[1])
    g = _gauss(float(sigma), False)

    def profile(n, a, b):
        line = np.zeros(n, np.float32)
        lo, hi = max(0, min(a, b)), min(n - 1, max(a, b))
        if hi >= lo:
            line[lo:hi + 1] = 1.0
        return _blur_axis(line, g, 0)

    return np.outer(profile(h, int(p0[1]), int(p1[1])), profile(w, int(p0[0]), int(p1[0]))).astype(np.float32)


def blend(top: np.ndarray, under: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """`top` where the mask is 1, `under` where it is 0, a mix in between (mask: float plane)."""
    if top.dtype == np.uint8 and top.ndim == 3 and top.shape[2] == 3 and top.shape == under.shape:
        m = Image.fromarray(np.clip(mask * 255.0 + 0.5, 0, 255).astype(np.uint8))       # to 1/255
        return np.array(Image.composite(Image.fromarray(np.ascontiguousarray(top)),
                                        Image.fromarray(np.ascontiguousarray(under)), m))
    m = mask[..., None] if top.ndim == 3 else mask
    out = top * m + under * (1.0 - m)
    if top.dtype == np.uint8:
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)
    return out.astype(top.dtype, copy=False)


def mix(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """Cross-fade: `a` at t = 0, `b` at t = 1."""
    t = float(t)
    if a.dtype == np.uint8 and a.ndim == 3 and a.shape[2] == 3 and a.shape == b.shape:
        return np.array(Image.blend(Image.fromarray(np.ascontiguousarray(a)),
                                    Image.fromarray(np.ascontiguousarray(b)), t))
    out = a.astype(np.float32) * (1.0 - t) + b.astype(np.float32) * t
    if a.dtype == np.uint8:
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)
    return out.astype(a.dtype, copy=False)


# ------------------------------------------------------------------ shapes (drawn in place)
def fill_rect(img: np.ndarray, p0, p1, value) -> np.ndarray:
    """Fill the rectangle with corners p0 and p1 (x, y; both included)."""
    h, w = img.shape[:2]
    x0, x1 = sorted((int(p0[0]), int(p1[0])))
    y0, y1 = sorted((int(p0[1]), int(p1[1])))
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w - 1, x1), min(h - 1, y1)
    if x1 >= x0 and y1 >= y0:
        img[y0:y1 + 1, x0:x1 + 1] = value
    return img


def circle(img: np.ndarray, center, radius: float, value, thickness: float | None = None,
           smooth: bool = False) -> np.ndarray:
    """A disc (thickness None) or a ring `thickness` wide, centred anywhere (x, y need not be
    whole pixels). `smooth` gives it a one-pixel soft edge."""
    cx, cy, r = float(center[0]), float(center[1]), float(radius)
    h, w = img.shape[:2]
    reach = r + (thickness or 0) / 2.0 + 2
    x0, x1 = max(0, int(math.floor(cx - reach))), min(w, int(math.ceil(cx + reach)) + 1)
    y0, y1 = max(0, int(math.floor(cy - reach))), min(h, int(math.ceil(cy + reach)) + 1)
    if x1 <= x0 or y1 <= y0:
        return img
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    if smooth:
        cov = np.clip(r + 0.5 - d, 0, 1) if thickness is None else np.clip(thickness / 2.0 + 0.5 - np.abs(d - r), 0, 1)
    else:
        cov = ((d <= r + 1e-6) if thickness is None else (np.abs(d - r) <= thickness / 2.0)).astype(np.float32)
    roi = img[y0:y1, x0:x1]
    if roi.ndim == 3:
        cov = cov[..., None]
    roi[...] = (roi * (1 - cov) + np.asarray(value, np.float32) * cov).astype(img.dtype)
    return img


# ------------------------------------------------------------------ masks
def _spread(mask: np.ndarray, size, grow: bool) -> np.ndarray:
    kh, kw = (size, size) if isinstance(size, int) else (int(size[0]), int(size[1]))
    fill = 0 if grow else 255
    op = np.maximum if grow else np.minimum
    a = mask
    for axis, k in ((1, kw), (0, kh)):
        if k <= 1:
            continue
        pad = [(0, 0), (0, 0)]
        pad[axis] = (k // 2, k - 1 - k // 2)
        p = np.pad(a, pad, mode="constant", constant_values=fill)
        n = a.shape[axis]
        out = None
        for i in range(k):
            s = p[:, i:i + n] if axis == 1 else p[i:i + n, :]
            out = s.copy() if out is None else op(out, s)
        a = out
    return a


def dilate(mask: np.ndarray, size=3, iterations: int = 1) -> np.ndarray:
    """Grow the set pixels of a uint8 mask by a `size` x `size` square, `iterations` times."""
    for _ in range(iterations):
        mask = _spread(mask, size, True)
    return mask


def erode(mask: np.ndarray, size=3, iterations: int = 1) -> np.ndarray:
    """Shrink the set pixels of a uint8 mask by a `size` x `size` square, `iterations` times."""
    for _ in range(iterations):
        mask = _spread(mask, size, False)
    return mask


def close_gaps(mask: np.ndarray, size=3) -> np.ndarray:
    """Join set pixels that are less than `size` apart (grow, then shrink back)."""
    return erode(dilate(mask, size), size)


def regions(mask: np.ndarray):
    """Connected regions of the set pixels (touching sides or corners).
    -> (count, labels int32, stats int32 rows [x, y, width, height, area]); label 0 and
    row 0 are the background. Runs of set pixels per row, joined between neighbouring rows."""
    mb = mask != 0
    H, W = mb.shape
    z = np.zeros((H, 1), np.int8)
    d = np.diff(np.concatenate([z, mb.astype(np.int8), z], axis=1), axis=1)
    rr, cs = np.nonzero(d == 1)
    _, ce = np.nonzero(d == -1)                       # ends (not included); same order as the starts
    n = len(rr)
    labels = np.zeros((H, W), np.int32)
    if n == 0:
        return 1, labels, np.array([[0, 0, W, H, H * W]], np.int32)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    rows = rr.tolist()
    starts, ends = cs.tolist(), ce.tolist()
    first = np.searchsorted(rr, np.arange(H + 1)).tolist()     # runs of row y: first[y] .. first[y + 1]
    for y in range(1, H):
        a, a1 = first[y - 1], first[y]
        b, b1 = first[y], first[y + 1]
        while a < a1 and b < b1:
            if starts[a] < ends[b] + 1 and starts[b] < ends[a] + 1:      # + 1: corners touch too
                ra, rb = find(a), find(b)
                if ra != rb:
                    if ra < rb:
                        parent[rb] = ra
                    else:
                        parent[ra] = rb
            if ends[a] < ends[b]:
                a += 1
            else:
                b += 1
    lab_of, comp = {}, []
    for i in range(n):
        r = find(i)
        j = lab_of.get(r)
        if j is None:
            j = lab_of[r] = len(comp) + 1
            comp.append([starts[i], rows[i], ends[i], rows[i] + 1, 0])
        c = comp[j - 1]
        c[0], c[2], c[3] = min(c[0], starts[i]), max(c[2], ends[i]), max(c[3], rows[i] + 1)
        c[4] += ends[i] - starts[i]
        labels[rows[i], starts[i]:ends[i]] = j
    fg = sum(c[4] for c in comp)
    stats = np.array([[0, 0, W, H, H * W - fg]] + [[c[0], c[1], c[2] - c[0], c[3] - c[1], c[4]] for c in comp], np.int32)
    return len(comp) + 1, labels, stats


# ------------------------------------------------------------------ colour / files
def gray(bgr: np.ndarray) -> np.ndarray:
    """uint8 BGR -> uint8 grey (luma weights)."""
    g = bgr[..., 0] * 0.114 + bgr[..., 1] * 0.587 + bgr[..., 2] * 0.299
    return np.clip(g + 0.5, 0, 255).astype(np.uint8)


def write_image(path, img: np.ndarray) -> None:
    """Write a grey, BGR or BGRA uint8 picture; the file type follows the name."""
    a = np.asarray(img)
    if a.ndim == 3 and a.shape[2] == 3:
        a = a[..., ::-1]
    elif a.ndim == 3 and a.shape[2] == 4:
        a = a[..., [2, 1, 0, 3]]
    im = Image.fromarray(np.ascontiguousarray(a))
    if str(path).lower().endswith(".png"):
        im.save(str(path), compress_level=1)          # fast; these are previews and checks
    else:
        im.save(str(path))

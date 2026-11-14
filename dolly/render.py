"""Frame-accurate CPU compositor.

Pipeline per output frame (all CPU, drawn with cairo through dolly/gfx.py, no GPU required):

    ffmpeg (decode raw.mp4 -> CFR bgr0 frames) --pipe--> compose() --pipe--> ffmpeg (libx264)

A frame is a cairo picture from the decoder's pipe to the encoder's: nothing is converted
on the way (gfx.read_frame wraps the bytes ffmpeg wrote, gfx.data hands them on).

compose():
    1. background canvas (gradient / wallpaper + soft drop shadow), pre-rendered
       once, placed with the camera transform
    2. the screen frame, placed with the same transform (every source pixel counted
       when shrinking) inside a rounded clip
    3. click ripples, then the re-drawn cursor (sub-pixel placed, optional
       motion blur, press "squish")
    4. screen-space overlays: keystroke keycaps, webcam bubble

Why not ffmpeg zoompan/crop expressions: they snap to integer pixels (visible
wobble during slow pans), have no rounded corners or real compositing, and a
spring-driven per-frame camera would need thousands of chained expressions.
Here the camera path is plain lists computed once by zoom.camera_path(), so
the preview, the export and the tests share the same numbers.

Points computed here (the click at X, the cursor at Xc) count pixel centres as whole
numbers, as they always did; gfx.place() takes them that way, and what is drawn by hand
at such a point (a ripple) adds the half pixel that cairo's coordinates want.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import gfx
from . import num
from . import project as projmod
from .events import Timeline
from .privacy import apply_boxes
from .zoom import CameraGeometry, CameraPath, Segment, ZoomConfig, build_segments, camera_path
from . import log as _dlog

STALL_S = float(os.environ.get("DOLLY_STALL_S", "90"))  # worker watchdog, see render()


from .ff import QUALITY, ffmpeg_bin, probe  # noqa: E402,F401  (they live in ff.py; kept importable from here)

# The font of everything written on the picture: the first of these families that is
# installed, and for a letter it lacks (an emoji in a caption) whichever font has it.
FONT = "JetBrainsMono Nerd Font, JetBrains Mono, monospace"
TITLE_FONT = "Inter, Inter Display, " + FONT

# An enlarged recording (zoomed in past ENLARGED output pixels per recorded pixel) is
# resampled with a sharp filter (Catmull-Rom, the look of the bicubic used until 0.7.3) in
# these qualities and in single stills. It costs some 70 ms for a frame that is enlarged,
# which makes such a frame about SHARP_COST times the work of another. `fast` and `draft`
# enlarge bilinear: a little softer on small text, a few milliseconds a frame.
SHARP_QUALITIES = ("high", "best", "master")
ENLARGED = 1.05
SHARP_COST = 2.5


# ------------------------------------------------------------------ layout
@dataclass
class Layout:
    W: int
    H: int
    OW: int
    OH: int
    geom: CameraGeometry
    radius_v: float   # corner radius in video px
    cursor_base: float  # cursor height in video px at size 1.0


def make_layout(W: int, H: int, style: dict, capture: dict | None = None) -> Layout:
    out = style["output"]
    OW, OH = int(out.get("width", 1920)), int(out.get("height", 1080))
    if out.get("width") in (None, 0, "source"):
        OW = W
        OH = H if out.get("height") in (None, 0, "source") else OH
    pad = float(style.get("padding", 0.075))
    aspect = OW / OH
    m = pad * W
    CW = max(W + 2 * m, (H + 2 * m) * aspect)
    CH = CW / aspect
    geom = CameraGeometry(W, H, (CW - W) / 2, (CH - H) / 2)
    radius_v = float(style.get("radius", 14)) * CW / OW
    scale = float((capture or {}).get("scale", 1.0)) or 1.0
    # cursor theme size (HYPRCURSOR_SIZE / XCURSOR_SIZE, logical px; Omarchy: 24) x monitor scale
    cursor_base = float((capture or {}).get("cursor_size", 24) or 24) * 1.04 * scale
    OW -= OW % 2
    OH -= OH % 2
    return Layout(W, H, OW, OH, geom, radius_v, cursor_base)


# -------------------------------------------------------------- background
@lru_cache(maxsize=2)
def _wallpaper(path: str, stamp: int, OW: int, OH: int, blur: float):
    """The picture file scaled to fill the output, blurred: kept, because an export asks
    for it twice (the canvas and the title card) and decoding a large JPEG takes a moment.
    `stamp` is the file's change time. Callers copy it before they draw on it."""
    pic = gfx.load(path, size=(OW, OH))
    if gfx.has_alpha(pic):                                  # a PNG with transparency: on black
        flat = gfx.new(OW, OH)
        gfx.place(flat, pic, 1, 1, 0, 0)
        pic = flat
    return gfx.blur(pic, blur * OW / 1920) if blur > 0 else pic


def _clip_box(r, b):
    """Corner boxes (x0, y0, x1, y1), as Timeline snapshots hold window rects."""
    x0, y0 = max(r[0], b[0]), max(r[1], b[1])
    return (x0, y0, max(x0, min(r[2], b[2])), max(y0, min(r[3], b[3])))


def _box_area(r):
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def build_background(lay: Layout, style: dict, colors: dict | None = None,
                     shadow_rect: tuple | None = None, shadow_radius: float | None = None,
                     shadow: bool = True):
    """Canvas (OW x OH) with background + drop shadow: a gfx frame.

    shadow_rect (x0, y0, x1, y1 in output px) overrides the landscape screen
    rectangle (used by the vertical/square reframe card)."""
    OW, OH, g = lay.OW, lay.OH, lay.geom
    bg = style.get("background", {})
    colors = colors or projmod.style_colors(style)
    kind = bg.get("type", "gradient")
    img = None
    path = None
    if kind == "wallpaper" and bg.get("path"):
        try:                                # project.json may come from someone else: the same rules
            from . import images            # as when a picture is picked (place, type, size)
            path = images.check(bg["path"])
        except (images.ImageError, OSError):
            path = None
    if path:
        try:
            img = gfx.copy(_wallpaper(str(path), path.stat().st_mtime_ns, OW, OH, float(bg.get("blur", 0.0))))
        except (gfx.ImageError, OSError):   # a picture by its header only: the gradient instead
            img = None
    elif kind == "solid":
        img = gfx.new(OW, OH)
        gfx.fill_rect(img, 0, 0, OW, OH, gfx.rgb(bg.get("color", colors["background"])))
    if img is None:
        presets = projmod.gradient_presets(colors)
        if bg.get("stops"):
            stops, angle = bg["stops"], bg.get("angle", 135)
        else:
            p = presets.get(bg.get("preset", "theme-glow"), presets["theme-glow"])
            stops, angle = p["stops"], p["angle"]
        img = gfx.new(OW, OH)
        gfx.fill_gradient(img, stops, angle)
        gfx.glow(img)                          # soft vignette-ish light from top-left for depth

    # drop shadow of the screen rectangle, canvas space
    sh = float(style.get("shadow", 0.55)) if shadow else 0.0
    if sh > 0:
        k = OW / g.CW
        if shadow_rect is not None:
            x0, y0, x1, y1 = shadow_rect
            r = int(shadow_radius or 0)
        else:
            x0, y0 = g.px * k, g.py * k
            x1, y1 = x0 + g.W * k, y0 + g.H * k
            r = int(lay.radius_v * k)
        off = 0.012 * OH
        rect = (int(x0), int(y0 + off), int(x1) - int(x0) + 1, int(y1 + off) - int(y0 + off) + 1)
        both = gfx.new(OW, OH, alpha=True)  # a wide and a tight one, added up before they are laid on
        bc = gfx.context(both)
        bc.set_operator(gfx.ADD)
        gfx.paint_mask(bc, gfx.soft_rect(OW, OH, *rect, 0.03 * OW, r), (0, 0, 0), min(1.0, sh * 0.55))
        gfx.paint_mask(bc, gfx.soft_rect(OW, OH, *rect, 0.006 * OW, r), (0, 0, 0), min(1.0, sh * 0.35))
        gfx.paint_mask(img, both, [v * 0.5 for v in gfx.rgb(colors.get("darker_background", "#0e0e14"))])

    gfx.add_noise(img, 0.9, seed=7)         # dither to kill gradient banding after H.264
    return img


# ------------------------------------------------------------------ cursor
_ARROW = [(2, 1.5), (2, 21), (7, 16.5), (10.4, 24.6), (13.6, 23.2), (10.2, 15.4), (17, 15.2)]


def _sprite_frame(height: int):
    """(unit, padding, width, height) of a cursor sprite: the cursor is 27 units tall."""
    s = height / 27.0
    pad = 6 * s
    return s, pad, int(20 * s + 2 * pad), int(height + 2 * pad)


def _grid(d: float, n: int):
    """Shifts within d to every side, n steps each way: a square brush d wide."""
    return [(i * d / n, j * d / n) for i in range(-n, n + 1) for j in range(-n, n + 1)]


@lru_cache(maxsize=4)
def cursor_sprite(height: int = 192):
    """Arrow cursor (a sprite) + hotspot. Drawn once at this height; placing it scales it."""
    s, pad, Wd, Hd = _sprite_frame(height)
    poly = [(pad + x * s, pad + y * s) for x, y in _ARROW]
    img = gfx.new(Wd, Hd, alpha=True)
    c = gfx.context(img)
    gfx.polygon(c, poly, 0.9 * s, 1.6 * s)
    c.set_source_rgba(0, 0, 0, 110 / 255)
    c.fill()
    img = gfx.blur(img, 1.6 * s)            # the soft shadow; the arrow is drawn over it
    c = gfx.context(img)
    gfx.polygon(c, poly)
    c.set_source_rgb(1, 1, 1)
    c.fill()
    # inner black arrow: where the arrow still covers itself when moved by the outline's
    # width to any side (clips add up)
    for dx, dy in _grid(0.74 * s, 2):
        gfx.polygon(c, poly, dx, dy)
        c.clip()
    c.set_source_rgb(18 / 255, 18 / 255, 22 / 255)
    c.paint()
    return img, (pad + 2 * s - 0.5, pad + 1.5 * s - 0.5), height


@lru_cache(maxsize=4)
def shape_sprite(shape: str, height: int = 192):
    """I-beam / grab cursors, same contract as cursor_sprite: (sprite, hotspot, height)."""
    s, pad, Wd, Hd = _sprite_frame(height)

    def box(x0, y0, x1, y1):
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if shape == "ibeam":
        cx = Wd / 2
        top, bot = pad + 3 * s, pad + 23 * s
        w = 1.1 * s
        parts = [box(cx - w, top, cx + w, bot), box(cx - 4 * s, top - w, cx + 4 * s, top + w),
                 box(cx - 4 * s, bot - w, cx + 4 * s, bot + w)]
        hot = (cx - 0.5, (top + bot) / 2 - 0.5)
    else:  # grab: four-way move arrows
        cx, cy = Wd / 2, pad + 12 * s
        L, w, hd = 9 * s, 1.1 * s, 3.2 * s
        parts = [box(cx - L + hd, cy - w, cx + L - hd, cy + w), box(cx - w, cy - L + hd, cx + w, cy + L - hd)]
        for (dx, dy) in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            tx, ty = cx + dx * L, cy + dy * L
            if dx:
                parts.append([(tx, ty), (tx - dx * hd, ty - hd), (tx - dx * hd, ty + hd)])
            else:
                parts.append([(tx, ty), (tx - hd, ty - dy * hd), (tx + hd, ty - dy * hd)])
        hot = (cx - 0.5, cy - 0.5)

    def body(c, dx=0.0, dy=0.0, outline=False):
        # the outline is the shape under a square brush: the shape at every shift within it
        for ox, oy in (_grid(0.77 * s, 3) if outline else [(0.0, 0.0)]):
            for p in parts:
                gfx.polygon(c, p, dx + ox, dy + oy)
    img = gfx.new(Wd, Hd, alpha=True)
    c = gfx.context(img)
    body(c, 0.9 * s, 1.6 * s, outline=True)
    c.set_source_rgba(0, 0, 0, 110 / 255)
    c.fill()
    img = gfx.blur(img, 1.6 * s)
    c = gfx.context(img)
    body(c, outline=True)
    c.set_source_rgb(1, 1, 1)
    c.fill()
    body(c)
    c.set_source_rgb(18 / 255, 18 / 255, 22 / 255)
    c.fill()
    return img, hot, height


# ------------------------------------------------------------------ keys
KEY_LABELS = {
    "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "super": "Super",
    "Return": "Enter", "Escape": "Esc", "BackSpace": "Bksp", "Tab": "Tab", "space": "Space",
    "Left": "←", "Right": "→", "Up": "↑", "Down": "↓", "Delete": "Del",
}
SHOW_ALONE = {"Return", "Escape", "Tab"}


def key_overlay_events(tl: Timeline) -> list[dict]:
    out: list[dict] = []
    for k in tl.keys:
        mods = [m for m in ("super", "ctrl", "alt", "shift") if m in k.get("mods", [])]
        real_mods = [m for m in mods if m != "shift"]
        if not real_mods and k["key"] not in SHOW_ALONE:
            continue
        key = k["key"]
        label = KEY_LABELS.get(key, key.upper() if len(key) == 1 else key)
        caps = [KEY_LABELS[m] for m in mods] + [label]
        if out and out[-1]["caps"] == caps and k["t"] - out[-1]["t_last"] < 0.9:
            out[-1]["count"] += 1
            out[-1]["t_last"] = k["t"]
            continue
        out.append({"t": k["t"], "t_last": k["t"], "caps": caps, "count": 1})
    return schedule_keycaps(out)


KEY_HOLD = 1.1          # s a keycap stays after its last press
KEY_MIN_SHOW = 0.6      # s a keycap is given before the next one may take its place
KEY_MAX_LAG = 0.35      # s the next keycap may be held back for that
KEY_FADE_IN = 0.12
KEY_FADE_OUT = 0.2
KEY_CUT_FADE = 0.08     # s fade when a keycap makes way for the next one


def schedule_keycaps(out: list[dict]) -> list[dict]:
    """When each keycap is on screen (`show`, `hide`, `fade_out`). One shows at a time, so a
    chord pressed right after another key (Enter, then Ctrl+L 0.25 s later) used to appear
    only while it was already fading out. Now the earlier keycap makes way: it keeps
    KEY_MIN_SHOW unless that would hold the next one back by more than KEY_MAX_LAG, and a
    keycap that was held back stays as long as it would have."""
    prev = None
    for ev in out:
        show = ev["t"]
        if prev is not None and prev["hide"] > show:
            show = min(max(ev["t"], prev["show"] + KEY_MIN_SHOW), ev["t"] + KEY_MAX_LAG)
            prev["hide"], prev["fade_out"] = show, KEY_CUT_FADE
        ev["show"], ev["hide"], ev["fade_out"] = show, max(ev["t_last"], show) + KEY_HOLD, KEY_FADE_OUT
        prev = ev
    return out


def keycap_alpha(ev: dict, t: float) -> float:
    """0..1: how visible this keycap is at time t."""
    t0, t1 = ev.get("show", ev["t"]), ev.get("hide", ev["t_last"] + KEY_HOLD)
    if not (t0 - 0.05 <= t <= t1):
        return 0.0
    fin = min(1.0, max(0.0, (t - t0) / KEY_FADE_IN))
    fout = min(1.0, max(0.0, (t1 - t) / ev.get("fade_out", KEY_FADE_OUT)))
    a = min(fin, fout)
    return float(a * a * (3 - 2 * a))


@lru_cache(maxsize=64)
def keycaps_image(caps: tuple, count: int, scale: float):
    """The keycaps of one shortcut as a sprite (never drawn on: it is kept)."""
    size = int(26 * scale)
    font = gfx.font(FONT, size, bold=True)
    padx, pady, gap = int(16 * scale), int(11 * scale), int(8 * scale)
    labels = list(caps) + ([f"×{count}"] if count > 1 else [])
    widths = [max(int(gfx.text_width(c, font)), size // 2) + 2 * padx for c in labels]
    h = size + 2 * pady
    total = sum(widths) + gap * (len(labels) - 1)
    m = int(14 * scale)
    r = int(10 * scale)
    img = gfx.new(total + 2 * m, h + 2 * m, alpha=True)
    c = gfx.context(img)
    x = m
    for w in widths:                                    # the shadows first, blurred together
        gfx.rounded_rect(c, x, m + 3 * scale, w + 1, h + 1, r)
        x += w + gap
    c.set_source_rgba(0, 0, 0, 120 / 255)
    c.fill()
    img = gfx.blur(img, 6 * scale)
    c = gfx.context(img)
    x = m
    t = max(1, int(scale))
    for lab, w in zip(labels, widths):
        if not lab.startswith("×"):
            # the cap takes the place of the shadow under it, and its thin light rim the
            # place of the cap (SOURCE): both let the picture show through as they are
            c.set_operator(gfx.SOURCE)
            gfx.rounded_rect(c, x, m, w + 1, h + 1, r)
            c.set_source_rgba(18 / 255, 19 / 255, 28 / 255, 228 / 255)
            c.fill()
            gfx.rounded_rect(c, x + t / 2, m + t / 2, w + 1 - t, h + 1 - t, r - t / 2)
            c.set_line_width(t)
            c.set_source_rgba(1, 1, 1, 40 / 255)
            c.stroke()
            c.set_operator(gfx.OVER)
        gfx.label(c, lab, x + w / 2, m + h / 2, font, gfx.rgb((240, 242, 250)))
        x += w + gap
    return img


@lru_cache(maxsize=16)
def badge_image(label: str, sc: float):
    """The fast-forward badge of a sped-up stretch: two triangles and the speed, as a sprite."""
    font = gfx.font(FONT, int(26 * sc), bold=True)
    tw = int(gfx.text_width(label, font))
    h = int(46 * sc)
    tri = int(14 * sc)
    w = tw + 2 * tri + int(52 * sc)
    img = gfx.new(w, h, alpha=True)
    c = gfx.context(img)
    gfx.rounded_rect(c, 0, 0, w, h, h // 2)
    c.set_source_rgba(14 / 255, 14 / 255, 20 / 255, 200 / 255)
    c.fill()
    cx, cy = int(20 * sc), h // 2
    for k in range(2):  # fast-forward glyph drawn, not a font char
        x = cx + k * tri + 0.5
        gfx.polygon(c, [(x, cy + 0.5 - tri * 0.7), (x + tri, cy + 0.5), (x, cy + 0.5 + tri * 0.7)])
    c.set_source_rgb(245 / 255, 246 / 255, 252 / 255)
    c.fill()
    gfx.label(c, label, cx + 2 * tri + int(10 * sc), cy, font, gfx.rgb((245, 246, 252)), anchor="l")
    return img



# --------------------------------------------------------------- compositor
ASPECTS = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0, "4:5": 4 / 5}


def output_size(style: dict, aspect: str | None) -> tuple[int, int]:
    out = style["output"]
    OW, OH = int(out.get("width", 1920)), int(out.get("height", 1080))
    if aspect and aspect != "16:9":
        a = ASPECTS[aspect]
        if abs(OW / OH - a) > 0.02:  # derive from the long side, e.g. 1920x1080 -> 1080x1920
            long = max(OW, OH)
            OW, OH = (int(round(long * a)), long) if a < 1 else (long, int(round(long / a)))
    return OW - OW % 2, OH - OH % 2


class Compositor:
    """Composites one output frame from one source frame at source time ts.

    landscape ("16:9"): the zooming camera over a padded, rounded, shadowed screen
    reframe ("9:16", "1:1", "4:5"): a rounded card on the themed background
                         showing a crop that follows the action (reframe.py)
    """

    def __init__(self, tl: Timeline, style: dict, segments: list[Segment], zcfg: ZoomConfig,
                 t_start: float, t_end: float, colors: dict | None = None, aspect: str | None = None,
                 privacy: list | None = None, captions: list | None = None, timemap=None, aoi=None,
                 quality: str | None = None, planned: dict | None = None):
        """`planned`: the finished path as a worker was handed it (the fields of a CameraPath,
        or of a CropPath for the upright shapes); the path is then not worked out again."""
        self.tl, self.style, self.zcfg = tl, style, zcfg
        self.filter = "sharp" if quality in SHARP_QUALITIES else None    # how an enlarged recording is resampled
        self.aoi = aoi if zcfg.use_aoi else None
        self.colors = colors or projmod.style_colors(style)
        self.aspect = aspect or style["output"].get("aspect", "16:9")
        self.reframe = self.aspect != "16:9"
        # window recording mode: the monitor is recorded, the followed window is the "screen"
        self.window_target = tl.capture.get("target") if tl.capture.get("mode") == "window" else None
        self._win_track = self._window_track(t_start, t_end) if self.window_target else None
        OW, OH = output_size(style, self.aspect)
        st = projmod.deep_merge(style, {"output": {"width": OW, "height": OH}})
        self.lay = make_layout(tl.w, tl.h, st, tl.capture)
        self.fps = float(style["output"].get("fps", 60))
        cs = style.get("cursor", {})
        sm = float(cs.get("smoothing", 0.55))
        sigma = 0.012 + 0.07 * sm
        self.t_start = t_start
        if self.reframe:
            from .reframe import CropPath, reframe_path
            pad = float(style.get("padding", 0.06)) * OW
            top = pad + (0.06 * OH if style.get("title", {}).get("banner") else 0)
            self.card = (int(pad), int(top), int(OW - pad), int(OH - pad))
            cw, ch = self.card[2] - self.card[0], self.card[3] - self.card[1]
            self.card_r = float(style.get("radius", 14)) * OW / 1080
            self._bg_how = (st, {"shadow_rect": self.card, "shadow_radius": self.card_r})
            self.crop = CropPath(**planned) if planned else reframe_path(
                tl, segments, cw / ch, self.fps, t_start, t_end, zcfg, cursor_sigma=sigma, aoi=self.aoi)
            self.vmask_r = float(style.get("radius", 14)) * 0.6
            self.n = len(self.crop.cx)
            self.base_w = min(tl.w, tl.h * cw / ch)
        else:
            self._bg_how = (st, {"shadow": self.window_target is None})
            self.path = CameraPath(**planned) if planned else camera_path(
                tl, segments, self.lay.geom, self.fps, t_start, t_end, zcfg, cursor_sigma=sigma, aoi=self.aoi,
                base=(lambda t: self.window_rect(t + 0.4)) if self.window_target else None)
            self.n = len(self.path.z)
        self._bg = None                      # the canvas, drawn when the first frame asks for it:
        self.bg_plain = None                 # the editor makes a Compositor for its camera path alone
        baked = bool(tl.capture.get("cursor_baked", False))
        self.draw_cursor = bool(cs.get("enabled", True)) and not baked
        self.ripple = bool(cs.get("ripple", True))
        # motion blur streaks the re-drawn cursor only; with the real cursor baked in it would
        # do nothing visible but still turn off frame reuse (slower exports)
        self.motion_blur = bool(cs.get("motion_blur", False)) and self.draw_cursor
        self.cursor_size = float(cs.get("size", 1.5))
        self.hide_idle = float(cs.get("hide_idle", 0) or 0)
        self.keys = key_overlay_events(tl) if style.get("keys", {}).get("enabled", True) else []
        self.keys_top = style.get("keys", {}).get("position", "bottom") == "top"
        self.hide_typing = bool(cs.get("hide_when_typing", True)) and zcfg.use_aoi
        self.shapes = bool(cs.get("shapes", True)) and zcfg.use_aoi
        self._vis = self._visibility_track(t_start, t_end)
        self.click_times = [c["t"] for c in tl.clicks]
        self._click_order = sorted(self.click_times)
        self.privacy = [b for b in (privacy or []) if b.get("enabled", True)]
        cap = style.get("captions", {})
        self.captions = captions if (captions and cap.get("enabled", True)) else []
        self.timemap = timemap
        sp = style.get("spotlight", {})
        self.spotlight = float(sp.get("strength", 0.5)) if sp.get("enabled") else 0.0
        self._prev_cursor = None
        self._last_src, self._last_key, self._last_out = None, None, None
        self._webcam_on = False
        self.reused = 0
        self._bg_cache = (None, None)        # (camera, the background moved with it)
        self._priv_cache = (None, None, None)
        self._cam_pos = None
        self._cam_last_t = None
        self._spot_cache = (None, None)      # (view size, the spotlight's darkness around a clear middle)

    @property
    def bg(self):
        """The canvas behind the recording (gradient or picture, with the drop shadow)."""
        if self._bg is None:
            self._bg = build_background(self.lay, self._bg_how[0], self.colors, **self._bg_how[1])
        return self._bg

    # ------------------------------------------------------------ window mode
    def _window_track(self, t0, t1):
        """Smoothed rect of the followed window (holds the last rect after it closes)."""
        tl = self.tl
        dt = 1 / 60
        ts = num.arange(max(0.0, t0 - 1.0), t1 + 1.0, dt)
        rs = []
        last = None
        for t in ts:
            r = tl.window_rect_at(t, self.window_target)
            if r is not None:
                vis = _clip_box(r, (0, 0, tl.w, tl.h))
                if _box_area(vis) >= 0.5 * _box_area(r):
                    last = vis           # (partly) dragged off the monitor: the part still on it
            rs.append(last)
        first = next((r for r in rs if r is not None), (0, 0, tl.w, tl.h))
        rs = [r if r is not None else first for r in rs]
        return ts, [num.gauss_smooth([float(r[c]) for r in rs], 0.05 / dt) for c in range(4)]

    def window_rect(self, t):
        if self._win_track is None:
            return None
        ts, sides = self._win_track
        return tuple(num.interp(t, ts, side) for side in sides)

    # ------------------------------------------------------------ cursor state
    def _visibility_track(self, t0, t1):
        """Cursor opacity over time (120 Hz): hidden while typing (like Omarchy's
        cursor.hide_on_key_press) until the mouse moves, and while it is outside
        the recorded area (another monitor); fades 0.12 s out / 0.15 s in."""
        tl = self.tl
        dt = 1 / 120
        ts = num.arange(t0 - 1.0, t1 + 1.0, dt)
        vis = [1.0] * len(ts)
        if self.zcfg.use_aoi:
            m = 3.0 * float(tl.capture.get("scale", 1.0) or 1.0)
            for i, t in enumerate(ts):
                if not tl.cursor_inside(t, m):
                    vis[i] = 0.0
        if self.hide_typing and tl.keys:
            typing_keys = sorted(k["t"] for k in tl.keys if not (set(k.get("mods", [])) & {"ctrl", "alt", "super"}))
            mx, my = tl.moves_x, tl.moves_y
            moving_t = [tl.moves_t[i] for i in range(1, len(tl.moves_t))
                        if abs(mx[i] - mx[i - 1]) + abs(my[i] - my[i - 1]) > 2.0]
            for k in typing_keys:
                j = bisect.bisect_right(moving_t, k)
                nxt = moving_t[j] if j < len(moving_t) else 1e9
                a, b = bisect.bisect_left(ts, k), bisect.bisect_left(ts, nxt)     # hidden for k <= t < nxt
                vis[a:b] = [0.0] * (b - a)
        # ramps: fade-in after the cursor reappears (0.15 s); fade-out *finishes* when it
        # leaves / typing starts (0.12 s, offline so we can start early) -> never drawn outside
        a_out, a_in = dt / 0.12, dt / 0.15
        up = list(vis)
        for i in range(1, len(up)):
            if vis[i] > up[i - 1]:
                up[i] = min(vis[i], up[i - 1] + a_in)
        down = list(vis)
        for i in range(len(down) - 2, -1, -1):
            if vis[i] > down[i + 1]:
                down[i] = min(vis[i], down[i + 1] + a_out)
        return ts, [min(u, d) for u, d in zip(up, down)]

    def cursor_alpha(self, ts: float) -> float:
        a = num.interp(ts, self._vis[0], self._vis[1])
        if self.hide_idle > 0:
            idle = ts - self.tl.last_activity_before(ts)
            a *= num.clip(1 - (idle - self.hide_idle) / 0.4, 0, 1)
        return a

    def cursor_shape(self, ts: float) -> str:
        """arrow | ibeam (over a terminal) | grab (dragging a floating window)."""
        if not self.shapes:
            return "arrow"
        tl = self.tl
        x, y = tl.cursor_at(ts)
        w = tl.window_under(ts, x, y)
        if w is None:
            return "arrow"
        if w.get("floating"):
            r0 = tl.window_rect_at(ts - 0.12, w["address"])
            if r0 is not None and abs(r0[0] - w["rect"][0]) + abs(r0[1] - w["rect"][1]) > 2:
                return "grab"
        from .aoi import TERMINAL_CLASSES
        terms = {c.lower() for c in (tl.capture.get("terminals") or [])} | TERMINAL_CLASSES
        return "ibeam" if str(w.get("class", "")).lower() in terms else "arrow"

    # ------------------------------------------------------------ mapping
    def _s(self, arr, ts):
        x = (ts - self.t_start) * self.fps
        i0 = int(math.floor(x))
        if i0 <= 0:
            return float(arr[0])
        if i0 >= len(arr) - 1:
            return float(arr[-1])
        u = x - i0
        return float(arr[i0] + (arr[i0 + 1] - arr[i0]) * u)

    def affine_at(self, ts: float):
        """(z, k, vx0, vy0): output = (video - v0) * k."""
        if self.reframe:
            c = self.crop
            cw, ch = self._s(c.cw, ts), self._s(c.ch, ts)
            cx, cy = self._s(c.cx, ts), self._s(c.cy, ts)
            k = (self.card[2] - self.card[0]) / cw
            vx0 = cx - cw / 2 - self.card[0] / k
            vy0 = cy - ch / 2 - self.card[1] / k
            return self.base_w / cw, k, vx0, vy0
        g, OW = self.lay.geom, self.lay.OW
        z, cx, cy = self._s(self.path.z, ts), self._s(self.path.cx, ts), self._s(self.path.cy, ts)
        k = OW * z / g.CW
        return z, k, cx - g.CW / (2 * z), cy - g.CH / (2 * z)

    def affine(self, i: int):
        return self.affine_at(self.t_start + i / self.fps)

    def cursor_at(self, ts):
        p = self.crop if self.reframe else self.path
        return self._s(p.cur_x, ts), self._s(p.cur_y, ts)

    # ------------------------------------------------------------ compose
    def compose(self, i: int, frame, webcam=None):
        """Output frame i from the source frame `frame` (gfx frames, as everywhere below)."""
        return self.compose_at(self.t_start + i / self.fps, frame, webcam)

    def _state_key(self, ts, z, k, vx0, vy0):
        """Everything that makes this output frame differ from the previous one with
        the same source frame, or None while an overlay is animating."""
        if self._webcam_on or self.motion_blur:
            return None
        j = bisect.bisect_right(self._click_order, ts - 0.55)
        if j < len(self._click_order) and self._click_order[j] < ts + 0.12:
            return None                             # press squish / ripple
        cx, cy = self.cursor_at(ts)
        a = round(self.cursor_alpha(ts), 2) if self.draw_cursor else 0.0
        keys = None
        for j, ev in enumerate(self.keys):
            t0_, t1_ = ev["t"], ev["t_last"] + 1.1
            if t0_ - 0.05 <= ts <= t1_:
                if ts < t0_ + 0.12 or ts > t1_ - 0.2:
                    return None                     # fading
                keys = j
                break
        cap = next((j for j, c in enumerate(self.captions) if c["start"] <= ts <= c["end"]), None)
        if cap is not None and self.captions:
            c = self.captions[cap]
            if ts < c["start"] + 0.12 or ts > c["end"] - 0.12:
                return None
        sp = self.timemap.speed_at(ts) if self.timemap else 1.0
        if sp > 1.01:
            return None
        chap = None
        cj, ce = self._chapter_at(ts)
        if cj is not None:
            if ce < 0.3 or ce > 2.4:
                return None                         # fading
            chap = cj
        priv = tuple(j for j, b in enumerate(self.privacy) if b["start"] <= ts <= b["end"])
        win = tuple(round(v, 1) for v in self.window_rect(ts)) if self.window_target else None
        return (round(z, 5), round(k, 6), round(vx0, 2), round(vy0, 2), round(cx, 1), round(cy, 1), a,
                self.cursor_shape(ts) if a > 0 else None, keys, cap, priv, win, chap)

    def compose_at(self, ts: float, frame, webcam=None):
        self._webcam_on = webcam is not None
        z_, k_, vx_, vy_ = self.affine_at(ts)
        key = self._state_key(ts, z_, k_, vx_, vy_)
        if key is not None and frame is self._last_src and key == self._last_key:
            self.reused += 1
            return self._last_out           # nothing changed: reuse (skip recomposition)
        out = self._compose(ts, frame, webcam)
        self._last_src, self._last_key, self._last_out = frame, key, out
        return out

    def _box(self, r, k, vx0, vy0):
        """(x, y, width, height) on the output of the video rectangle r = (x0, y0, x1, y1)."""
        return (r[0] - vx0) * k + 0.5 - 0.5 * k, (r[1] - vy0) * k + 0.5 - 0.5 * k, (r[2] - r[0]) * k, (r[3] - r[1]) * k

    def _place(self, c, frame, k, vx0, vy0):
        """The recording under the camera, onto whatever context `c` is clipped to."""
        gfx.place(c, frame, k, k, -vx0 * k, -vy0 * k, filter=self.filter if k > ENLARGED else None, extend=gfx.PAD)

    def work_at(self, ts: float) -> float:
        """How much work the frame at source time ts is, next to one that is not enlarged
        with the sharp filter (1.0): what an export shares out between its workers."""
        return SHARP_COST if self.filter == "sharp" and self.affine_at(ts)[1] > ENLARGED else 1.0

    def _compose(self, ts: float, frame, webcam=None):
        lay = self.lay
        if self.privacy:
            act = tuple(j for j, b in enumerate(self.privacy) if b["start"] <= ts <= b["end"])
            pc = self._priv_cache
            if pc[0] is frame and pc[1] == act:
                frame = pc[2]            # same source frame, same boxes: blur once
            elif act:
                # a copy: the decoded frame is held for the next output frames and other boxes
                blurred = apply_boxes(gfx.copy(frame), self.privacy, ts)
                self._priv_cache = (frame, act, blurred)
                frame = blurred
        z, k, vx0, vy0 = self.affine_at(ts)
        if self.reframe:
            out = gfx.copy(self.bg)
            c = gfx.context(out)
            x0, y0, x1, y1 = self.card
            cwid, chgt = x1 - x0, y1 - y0
            gfx.rounded_rect(c, x0, y0, cwid, chgt, self.card_r)
            c.clip()
            # crop wider/taller than the screen (letterbox): the card shows a dimmed
            # background around a rounded screen instead of black bars
            # window mode: only the followed window, like the 16:9 card
            scr = self.window_rect(ts) if self.window_target else (0, 0, self.tl.w, self.tl.h)
            VX0, VY0 = (scr[0] - vx0) * k - x0, (scr[1] - vy0) * k - y0
            VX1, VY1 = (scr[2] - vx0) * k - x0, (scr[3] - vy0) * k - y0
            if VX0 > 0.5 or VY0 > 0.5 or VX1 < cwid - 0.5 or VY1 < chgt - 0.5:
                gfx.fill_rect(c, x0, y0, cwid, chgt, (0, 0, 0), 0.45)
                c.save()
                gfx.rounded_rect(c, *self._box(scr, k, vx0, vy0), self.vmask_r * k * 2)
                c.clip()                 # clips add up: the card and the screen in it
                self._place(c, frame, k, vx0, vy0)
                c.restore()
            else:
                self._place(c, frame, k, vx0, vy0)
            view = (x0, y0, cwid, chgt)  # ripples, cursor and spotlight stay inside the card: its clip is still on
        else:
            out = self._landscape(frame, z, k, vx0, vy0, ts)
            c = gfx.context(out)
            view = (0, 0, lay.OW, lay.OH)
        # ripples + cursor (clipped to the card in reframe mode)
        cxv, cyv = self.cursor_at(ts)
        Xc, Yc = (cxv - vx0) * k, (cyv - vy0) * k
        unit = lay.cursor_base * self.cursor_size * k
        if self.reframe:
            unit = unit * 0.9
        if self.spotlight > 0:
            self._spotlight(c, view, Xc, Yc, z)
        press = 1.0
        for click in self.tl.clicks:
            dt = ts - click["t"]
            if -0.12 < dt < 0.22:
                press = min(press, 1 - 0.2 * math.exp(-((dt - 0.02) / 0.07) ** 2))
            if self.ripple and 0 <= dt < 0.55:
                self._ripple(c, (click["x"] - vx0) * k, (click["y"] - vy0) * k, dt / 0.55, unit)
        if self.draw_cursor:
            alpha = self.cursor_alpha(ts)
            if alpha > 0.01:
                self._cursor(c, ts, Xc, Yc, unit * press, alpha, self.cursor_shape(ts))
        c.reset_clip()
        # screen-space overlays
        speed = self.timemap.speed_at(ts) if self.timemap else 1.0
        cap_on = self._captions(c, ts) if self.captions and speed <= 1.01 else False
        self._keys(c, ts, raised=cap_on)
        self._chapter(c, ts)
        if speed > 1.01:
            a = 1.0
            for (pa, pb, sp) in self.timemap.pieces():
                if pa <= ts <= pb:
                    a = num.clip(min(ts - pa, pb - ts) / sp / 0.15, 0, 1)
                    break
            self._badge(c, speed, a)
        if webcam is not None:
            self._webcam(c, webcam, ts, (Xc, Yc))
        return out

    def _moved_bg(self, z, tx, ty):
        out = gfx.new(self.lay.OW, self.lay.OH)
        # the camera never looks more than a fraction of a pixel past the canvas: its edge continued
        gfx.place(out, self.bg, z, z, tx, ty, extend=gfx.PAD, operator=gfx.SOURCE)
        return out

    def _landscape(self, frame, z, k, vx0, vy0, ts=None):
        lay, g = self.lay, self.lay.geom
        tx, ty = (-g.px - vx0) * k, (-g.py - vy0) * k
        if abs(z - 1) < 1e-4 and abs(tx) < 1e-3 and abs(ty) < 1e-3:
            out = gfx.copy(self.bg)
        else:
            cam = (z, tx, ty)
            if self._bg_cache[0] != cam:         # a moving camera: straight onto the new frame
                self._bg_cache = (cam, None)
                out = self._moved_bg(z, tx, ty)
            else:                                # a still camera: the background is moved once, not per frame
                if self._bg_cache[1] is None:
                    self._bg_cache = (cam, self._moved_bg(z, tx, ty))
                out = gfx.copy(self._bg_cache[1])
        c = gfx.context(out)
        if self.window_target and ts is not None:
            return self._window_card(c, out, frame, k, vx0, vy0, ts)
        # one resample straight from the recording, shrinking or not: every source pixel
        # counts when it shrinks (gfx.place), so small text does not shimmer. The corners are
        # a clip, exact to a fraction of a pixel wherever the camera is.
        gfx.rounded_rect(c, *self._box((0, 0, lay.W, lay.H), k, vx0, vy0), lay.radius_v * k)
        c.clip()
        self._place(c, frame, k, vx0, vy0)
        return out

    def _window_card(self, c, out, frame, k, vx0, vy0, ts):
        """Window mode: only the followed window, as a rounded card with a soft shadow."""
        OW, OH = self.lay.OW, self.lay.OH
        x, y, w, h = self._box(self.window_rect(ts), k, vx0, vy0)
        if w <= 0 or h <= 0 or x >= OW or y >= OH or x + w <= 0 or y + h <= 0:
            return out
        # shadow: blurred dark rect a little below the card
        pad = int(0.03 * OH)
        gfx.shadow(c, x, y + pad // 3, w, h, max(1.0, pad / 2.2), 0.0, 0.55)
        gfx.rounded_rect(c, x, y, w, h, self.lay.radius_v * k)
        c.clip()
        self._place(c, frame, k, vx0, vy0)
        return out

    def _spotlight(self, c, view, X, Y, z):
        """Dim everything but a soft-edged spot around the cursor: black laid on, as strong
        as 1 - exp(-(d / r) ** 4) at distance d (flat-topped, soft edge)."""
        s = self.spotlight * num.clip((z - 1.05) / 0.45, 0, 1)
        if s < 0.01:
            return
        _, _, w, h = view
        q = 4
        if self._spot_cache[0] != (w, h):
            # that darkness once, at a quarter of the size (nothing in it is sharp), the spot in
            # its middle and wide enough to cover the view wherever the spot is
            r = 0.2 * min(h, w) + 0.12 * max(h, w)
            sw, sh = 2 * (w // q) + 2, 2 * (h // q) + 2
            dark = gfx.new(sw, sh, alpha=True)
            dc = gfx.context(dark)
            dc.set_source(gfx.radial_gradient(sw / 2, sh / 2, 2.5 * r / q,
                                              [(i / 40, (0, 0, 0), 1 - math.exp(-(2.5 * i / 40) ** 4)) for i in range(41)]))
            dc.paint()
            self._spot_cache = ((w, h), dark)
        dark = self._spot_cache[1]
        gfx.place(c, dark, q, q, X - q * (dark.get_width() / 2 - 0.5), Y - q * (dark.get_height() / 2 - 0.5),
                  extend=gfx.PAD, alpha=s)

    def _ripple(self, c, X, Y, u, unit):
        e = 1 - (1 - u) ** 3
        r = unit * (0.25 + 1.05 * e)
        a = 0.55 * (1 - u) ** 1.6
        if a < 0.01:
            return
        th = max(1, int(round(unit * 0.09)))
        gfx.disc(c, X + 0.5, Y + 0.5, r, (1, 1, 1), a * 0.22)
        gfx.ring(c, X + 0.5, Y + 0.5, r, th, (1, 1, 1), a)

    def _cursor(self, c, ts, X, Y, height, alpha, shape="arrow"):
        sprite, (hx, hy), sh = cursor_sprite() if shape == "arrow" else shape_sprite(shape)
        s = height / sh
        samples = [(X, Y, 1.0)]
        prev = self._prev_cursor
        self._prev_cursor = (ts, X, Y)
        if self.motion_blur and prev is not None and 0 < ts - prev[0] < 0.5:
            Xp, Yp = prev[1], prev[2]
            dist = math.hypot(X - Xp, Y - Yp)
            if dist > height * 0.25:
                # 180-degree shutter capped at ~1 cursor height, weighted to the
                # current position so the arrow stays readable while it streaks
                span = min(0.5, height * 1.0 / dist)
                n = int(min(9, 3 + dist * span / (height * 0.15)))
                w = [0.35 + 0.65 * (j / (n - 1)) ** 2 for j in range(n)]
                tot = sum(w)
                samples = [(X - (X - Xp) * span * (1 - j / (n - 1)), Y - (Y - Yp) * span * (1 - j / (n - 1)), w[j] / tot)
                           for j in range(n)]
        if len(samples) == 1:
            gfx.place(c, sprite, s, s, X - hx * s, Y - hy * s, alpha=alpha)
            return
        # the streak: every sample's share added up on a clear patch, which is then laid on
        xs = [p[0] for p in samples]
        ys = [p[1] for p in samples]
        w_s, h_s = sprite.get_width() * s, sprite.get_height() * s
        x0 = int(math.floor(min(xs) - hx * s)) - 1
        y0 = int(math.floor(min(ys) - hy * s)) - 1
        x1 = int(math.ceil(max(xs) - hx * s + w_s)) + 1
        y1 = int(math.ceil(max(ys) - hy * s + h_s)) + 1
        patch = gfx.new(x1 - x0, y1 - y0, alpha=True)
        pc = gfx.context(patch)
        for (px, py, wgt) in samples:
            gfx.place(pc, sprite, s, s, px - hx * s - x0, py - hy * s - y0, operator=gfx.ADD, alpha=wgt)
        gfx.place(c, patch, 1, 1, x0, y0, alpha=alpha)

    def _keys(self, c, t, raised=False):
        if not self.keys:
            return
        OW, OH = self.lay.OW, self.lay.OH
        for ev in self.keys:
            t0, t1 = ev.get("show", ev["t"]), ev.get("hide", ev["t_last"] + KEY_HOLD)
            if not (t0 - 0.05 <= t <= t1):
                continue
            a = keycap_alpha(ev, t)
            if a <= 0.01:
                continue
            sc = min(OW, OH) / 1080 if self.reframe else OH / 1080
            img = keycaps_image(tuple(ev["caps"]), ev["count"], sc)
            w, h = img.get_width(), img.get_height()
            x0 = (OW - w) // 2
            if self.keys_top:
                top = 0.07 * OH if not self.reframe else self.card[1] + 0.03 * OH
                y0 = int(top - (1 - a) * 10 * sc)
            else:
                base = (0.17 if raised else 0.07) * OH if not self.reframe else (OH - self.card[3]) + 0.06 * OH * (2 if raised else 1)
                y0 = int(OH - base - h + (1 - a) * 10 * sc)
            gfx.place(c, img, 1, 1, x0, y0, alpha=a)
            break

    CHAPTER_S = 2.8

    def _chapter_at(self, ts):
        """(index, seconds shown) of the chapter chip at source time ts. The chip stays
        CHAPTER_S seconds of *output* time, so a sped-up stretch does not flash it."""
        tm = self.timemap
        o = tm.to_output(ts) if tm else ts
        cur = None
        for j, c in enumerate(self.style.get("chapters") or []):
            oc = tm.to_output(c["t"]) if tm else c["t"]
            if oc - 0.05 <= o <= oc + self.CHAPTER_S:
                cur = (j, o - oc)
        return cur if cur else (None, 0.0)

    def _chapter(self, c, ts):
        """Chapter titles (style.chapters: [{"t": source s, "text"}]): a chip at the top
        left for CHAPTER_S seconds of output time, with short fades."""
        j, el = self._chapter_at(ts)
        if j is None:
            return
        cur = self.style["chapters"][j]
        a = num.clip(min(el / 0.25, (self.CHAPTER_S - el) / 0.35), 0, 1)
        if a <= 0.01:
            return
        OW, OH = self.lay.OW, self.lay.OH
        sc = min(OW, OH) / 1080
        img = captions_mod().caption_image(cur["text"], int(OW * 0.6), sc, FONT)
        m = int(0.035 * min(OW, OH))
        top = m + (self.card[1] if self.reframe else 0)
        left = m + (self.card[0] if self.reframe else 0)
        gfx.place(c, img, 1, 1, left, top, alpha=a)

    def _captions(self, c, ts) -> bool:
        cue = next((q for q in self.captions if q["start"] <= ts <= q["end"]), None)
        if not cue:
            return False
        OW, OH = self.lay.OW, self.lay.OH
        sc = min(OW, OH) / 1080
        img = captions_mod().caption_image(cue["text"], int(OW * 0.86), sc, FONT)
        a = num.clip(min((ts - cue["start"]) / 0.12, (cue["end"] - ts) / 0.12), 0, 1)
        w, h = img.get_width(), img.get_height()
        bottom = (OH - self.card[3]) + 0.03 * OH if self.reframe else 0.05 * OH
        gfx.place(c, img, 1, 1, (OW - w) // 2, int(OH - bottom - h), alpha=a)
        return True

    def _badge(self, c, speed, alpha=1.0):
        OW, OH = self.lay.OW, self.lay.OH
        img = badge_image(f"{speed:g}×", min(OW, OH) / 1080)
        m = int(0.035 * min(OW, OH))
        y = m + (self.card[1] if self.reframe else 0)
        x = OW - img.get_width() - m - (OW - self.card[2] if self.reframe else 0)
        gfx.place(c, img, 1, 1, x, y, alpha=alpha)

    def cam_geometry(self, ts, cursor_xy=None):
        """(x0, y0, w, h, radius, full_u) of the camera on the output at ts.

        Bubble: size (fraction of the short side; S/M/L or free), shape circle /
        rounded / rect (4:3), a corner or a free position ("pos": centre as fractions
        of the output, set by dragging in the editor), moving away from the cursor.
        "Camera only" ranges (style.webcam.full) grow it to the whole frame with a
        smooth spring (intro/outro style)."""
        OW, OH = self.lay.OW, self.lay.OH
        wc = self.style.get("webcam", {})
        d = int(min(OW, OH) * float(wc.get("size", 0.22)))
        shape = wc.get("shape", "circle")
        bw, bh = (int(d * 4 / 3), d) if shape == "rect" else (d, d)
        margin = int(0.035 * min(OW, OH))
        corner = wc.get("corner", "bottom-left")

        def pos(c):
            return (margin if "left" in c else OW - bw - margin, OH - bh - margin if "bottom" in c else margin)
        if wc.get("pos"):
            fx, fy = wc["pos"]
            home = (int(fx * OW - bw / 2), int(fy * OH - bh / 2))
        else:
            home = pos(corner)
        target = home
        if wc.get("auto_move", True) and cursor_xy is not None:
            cx, cy = cursor_xy
            near = home[0] - bw * 0.4 < cx < home[0] + bw * 1.4 and home[1] - bh * 0.4 < cy < home[1] + bh * 1.4
            if near:
                if wc.get("pos"):
                    target = (OW - home[0] - bw, home[1])
                else:
                    flip = corner.replace("left", "R").replace("right", "left").replace("R", "right")
                    target = pos(flip)
        if self._cam_pos is None:
            self._cam_pos = list(map(float, target))
        else:
            dt = max(0.0, min(0.5, ts - (self._cam_last_t or ts)))
            k = 1 - math.exp(-dt * 7.0)
            self._cam_pos[0] += (target[0] - self._cam_pos[0]) * k
            self._cam_pos[1] += (target[1] - self._cam_pos[1]) * k
        self._cam_last_t = ts
        x0 = max(0.0, min(OW - bw, self._cam_pos[0]))
        y0 = max(0.0, min(OH - bh, self._cam_pos[1]))
        r = bh / 2 if shape == "circle" else (bh * 0.18 if shape == "rounded" else bh * 0.04)
        u = self.cam_full_u(ts)
        if u > 0:
            e = u * u * (3 - 2 * u)
            x0, y0 = x0 * (1 - e), y0 * (1 - e)
            bw, bh = bw + (OW - bw) * e, bh + (OH - bh) * e
            r = r * (1 - e)
        return x0, y0, int(round(bw)), int(round(bh)), r, u

    def cam_full_u(self, ts) -> float:
        """0 = bubble, 1 = camera fills the frame; 0.4 s eased transitions at range edges."""
        tr = 0.4
        best = 0.0
        for a, b in (self.style.get("webcam", {}).get("full") or []):
            if a - tr < ts < b + tr:
                best = max(best, num.clip(min((ts - a + tr) / tr, (b + tr - ts) / tr), 0, 1))
        return best

    def _webcam(self, c, cam, ts, cursor_xy):
        OW, OH = self.lay.OW, self.lay.OH
        wc = self.style.get("webcam", {})
        x0, y0, w, h, r, u = self.cam_geometry(ts, cursor_xy)
        x0, y0 = int(round(x0)), int(round(y0))
        w, h = max(8, min(w, OW - x0)), max(8, min(h, OH - y0))
        if wc.get("shadow", True) and u < 1:
            gfx.shadow(c, x0, y0 + int(0.012 * OH), w, h, max(1.0, 0.012 * min(OW, OH)), r, 0.45 * (1 - u))
        cw, ch = cam.get_width(), cam.get_height()
        k = max(w / cw, h / ch)                     # the camera fills the bubble, its middle kept
        c.save()
        gfx.rounded_rect(c, x0, y0, w, h, r)
        c.clip()
        gfx.place(c, cam, k, k, x0 - (cw * k - w) / 2 + 0.5 * k - 0.5, y0 - (ch * k - h) / 2 + 0.5 * k - 0.5,
                  extend=gfx.PAD)
        c.restore()
        bwid = int(wc.get("border", 3) or 0)
        if bwid > 0 and u < 1:
            col = gfx.rgb(self.colors.get("accent", "#ffffff")) if wc.get("border_color", "accent") == "accent" \
                else (1.0, 1.0, 1.0)
            t = max(1, int(round(bwid * min(OW, OH) / 1080)))
            gfx.rounded_rect(c, x0 - t / 2, y0 - t / 2, w + t, h + t, r + t / 2 if r > 0 else 0)    # a line t wide
            c.set_line_width(t)                                                                  # around the bubble
            c.set_source_rgba(*col, 1 - u)
            c.stroke()

    # ------------------------------------------------------------ title cards
    def title_card(self, text: str, subtitle: str = ""):
        if self.bg_plain is None:
            self.bg_plain = build_background(self.lay, self.style, self.colors, shadow=False)
        OW, OH = self.lay.OW, self.lay.OH
        img = gfx.copy(self.bg_plain)
        c = gfx.context(img)
        sc = min(OW, OH) / 1080
        tall = 1.35 if OH > OW * 1.2 else 1.0   # vertical canvases get a larger title
        big = gfx.font(TITLE_FONT, int(64 * sc * tall), bold=True)
        small = gfx.font(TITLE_FONT, int(30 * sc * tall), bold=True)
        # wrap title to the canvas width
        words, lines, cur = text.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if gfx.text_width(t, big) <= OW * 0.82 or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        lh = int(78 * sc * tall)
        total = lh * len(lines) + (int(56 * sc) if subtitle else 0)
        y = OH / 2 - total / 2 + lh / 2
        for l in lines:
            gfx.label(c, l, OW / 2 + 2 * sc, y + 3 * sc, big, gfx.rgb((10, 10, 16)))
            gfx.label(c, l, OW / 2, y, big, gfx.rgb((250, 250, 255)))
            y += lh
        if subtitle:
            gfx.label(c, subtitle, OW / 2, y + int(8 * sc), small, gfx.rgb((235, 236, 248)))
        return img


def captions_mod():
    from . import captions
    return captions


# ------------------------------------------------------------------- export
def load_aoi(project_dir, tl: Timeline, zcfg: ZoomConfig):
    """Area-of-interest track for a project (activity analysis is cached on disk)."""
    if not zcfg.use_aoi:
        return None
    try:
        from . import aoi as aoimod
        return aoimod.build_aoi(tl, aoimod.cached_activity(Path(project_dir), tl))
    except Exception as e:  # never block an export on analysis
        sys.stderr.write(f"dolly: area-of-interest analysis skipped ({e})\n")
        return None


def resolve_segments(tl: Timeline, data: dict, zcfg: ZoomConfig, aoi=None) -> list[Segment]:
    if data.get("segments") is None:
        return build_segments(tl, zcfg, aoi)
    return [Segment.from_dict(s) for s in data["segments"]]


def shift_timeline(tl: Timeline, dt: float) -> Timeline:
    return tl.shift(dt)


def plan_timing(project_dir, style_over=None, t_range=None) -> dict:
    from .timemap import TimeMap
    proj = projmod.Project(project_dir)
    data = proj.load()
    style = projmod.deep_merge(data["style"], style_over)
    tl = Timeline.load(proj.events)
    trim = t_range or data.get("trim") or [0.0, tl.duration]
    t0, t1 = max(0.0, float(trim[0])), min(tl.duration, float(trim[1]))
    if not (math.isfinite(t0) and math.isfinite(t1) and t1 > t0):
        raise projmod.EmptyTrim(f"no video remains in the selected trim ({t0:g}–{t1:g} s); "
                                "adjust the trim in the editor or record another take")
    speed = (data.get("speed") or []) if style.get("speed", {}).get("enabled", True) else []
    tm = TimeMap(t0, t1, speed)
    title = style.get("title", {})
    intro = float(title.get("intro", 2.2)) if title.get("enabled") and title.get("text") else 0.0
    outro = float(title.get("outro", 0) or 0) if title.get("enabled") and title.get("outro_text") else 0.0
    return {"t0": t0, "t1": t1, "timemap": tm, "intro": intro, "outro": outro,
            "out_duration": intro + tm.duration + outro, "style": style, "data": data, "tl": tl}


class FrameReader:
    """Sequential decoder returning the frame for non-decreasing source times.

    A constant-frame-rate source is decoded at its own rate (no duplicated
    frames through the pipe: a 30 fps capture exported at 60 fps decodes half
    the frames) and the *same frame object* is returned until the next source
    frame is due, which lets the compositor reuse unchanged output frames.
    Frames are gfx frames (ffmpeg's bgr0), each in memory of its own: the one
    handed out stays untouched while the next is read."""

    def __init__(self, path: Path, fps: float, t0: float, w: int, h: int, vf_extra: str = ""):
        self.fps, self.t0, self.w, self.h = fps, t0, w, h
        self.rate, self.first = fps, max(0.0, t0)
        native = False
        if not vf_extra:
            try:
                v = next(s for s in probe(path).get("streams", []) if s.get("codec_type") == "video")
                r, a = v.get("r_frame_rate", "0/1"), v.get("avg_frame_rate", "0/1")
                rn, rd = (float(x) for x in r.split("/"))
                an, ad = (float(x) for x in a.split("/"))
                rf, af = rn / max(rd, 1), an / max(ad, 1)
                native = rf > 0 and abs(rf - af) / rf < 0.002
                if native:
                    self.rate = rf
                    st = float(v.get("start_time") or 0.0)
                    k = math.ceil((max(0.0, t0) - 1e-6) * rf)
                    self.first = k / rf + 0 * st
            except (StopIteration, ValueError, KeyError):
                native = False
        vf = [] if native else ["-vf", f"fps={fps}" + (f",{vf_extra}" if vf_extra else "")]
        self.p = subprocess.Popen([ffmpeg_bin(), "-v", "error", "-ss", f"{max(0.0, t0):.4f}", "-i", str(path), *vf,
                                   "-f", "rawvideo", "-pix_fmt", "bgr0", "-"],
                                  stdout=subprocess.PIPE, bufsize=w * h * 4 * 2)
        self.idx = -1
        self.frame = None

    def at(self, ts: float):
        want = max(0, int(round((ts - self.first) * self.rate)))
        while self.idx < want:
            frame = gfx.read_frame(self.p.stdout, self.w, self.h)
            if frame is None:
                break  # hold the last frame
            self.frame = frame
            self.idx += 1
        return self.frame

    def close(self):
        try:
            self.p.stdout.close()
            self.p.kill()
            self.p.wait()
        except Exception:
            _dlog.swallowed()
            pass


def audio_filter(pieces, intro: float, total: float, denoise: bool, normalize: bool, rnn_model: str | None = None,
                 inp: str = "1:a") -> str:
    """Trimmed + speed-ramped + cleaned audio, padded to the video length -> [aout]."""
    import re
    from .audio import model_arg
    rnn_model = model_arg(rnn_model)
    if not re.fullmatch(r"[0-9]+:a(:[0-9]+)?", inp):
        raise ValueError(f"not an input stream: {inp!r}")
    parts, labels = [], []
    for k, (a, b, sp) in enumerate(pieces):
        ch = f"[{inp}]atrim={a:.4f}:{b:.4f},asetpts=PTS-STARTPTS"
        if sp > 1.001:
            r = sp
            while r > 2.0 + 1e-9:
                ch += ",atempo=2.0"
                r /= 2.0
            ch += f",atempo={r:.4f},volume=0.35"
        parts.append(ch + f"[p{k}]")
        labels.append(f"[p{k}]")
    f = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[cat]"
    post = []
    if denoise:
        post.append("highpass=f=70")
        post.append(f"arnndn=m='{rnn_model}'" if rnn_model else "afftdn=nf=-25:tn=1")
    if normalize:
        post.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    post.append("aresample=48000")
    if intro > 0:
        post.append(f"adelay=delays={int(intro * 1000)}:all=1")
    post.append(f"apad=whole_dur={total:.4f},atrim=0:{total:.4f}")
    return f + ";[cat]" + ",".join(post) + "[aout]"




def _encoder_cmd(ff, OW, OH, fps, quality, out, gop):
    preset, crf = QUALITY.get(quality, QUALITY["high"])
    return [ff, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr0", "-s", f"{OW}x{OH}", "-r", str(fps),
            "-i", "-", "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-profile:v", "high", "-r", str(fps), "-fps_mode", "cfr", "-g", str(gop), str(out)]


from .workers import default_workers  # noqa: E402,F401  (kept importable from here)


def chunk_bounds(work: list, nw: int) -> list:
    """Where the nw chunks of an export begin and end (nw + 1 frame numbers), given the
    work each frame is. The chunks get equal shares of the work, not of the frames: a take
    is often zoomed in for one long stretch, and with the sharp filter that stretch kept
    one worker busy long after the others were done. Frames of equal work are split as
    they always were."""
    n, total = len(work), sum(work)
    if n == 0 or total <= 0 or min(work) == max(work):
        return [round(n * k / nw) for k in range(nw + 1)]
    bounds, done, i = [0], 0.0, 0
    for k in range(1, nw):
        while i < n and done + work[i] / 2 < total * k / nw:
            done += work[i]
            i += 1
        bounds.append(i)
    return bounds + [n]


def export(project_dir: str | Path, out_path: str | Path | None = None, fmt: str | None = None,
           quality: str | None = None, progress=None, t_range=None, style_over: dict | None = None,
           frames_dir: str | Path | None = None, frame_every: int = 0, aspect: str | None = None,
           notify: bool = False, workers: int | None = None, stats: dict | None = None,
           settings: bool = True, _chunk: dict | None = None) -> Path:
    """Render a project. Main frames are split into contiguous chunks rendered by
    forked worker processes (each with its own decoder and x264 encoder), then
    joined with the concat demuxer (no re-encode) and muxed with the audio.
    quality="draft" renders a fast half-size 30 fps preview.

    settings=True applies Settings > Export/Privacy/Storage around the render:
    secret blur (OCR once per project), size cap, an extra vertical copy, and
    "keep raw files" off (raw video to the trash after a successful export)."""
    proj = projmod.Project(project_dir)
    if settings and quality != "draft" and _chunk is None:
        _auto_scrub(proj)
    job_spec = {"project_dir": str(project_dir), "fmt": fmt, "quality": quality, "t_range": t_range,
                "style_over": style_over, "aspect": aspect,
                "frames_dir": str(frames_dir) if frames_dir else None, "frame_every": frame_every}
    quality = quality or (style_over or {}).get("output", {}).get("quality") or \
        proj.load()["style"].get("output", {}).get("quality") or "high"
    if quality == "draft":
        base_style = projmod.deep_merge(proj.load()["style"], style_over)
        dw, dh = output_size(base_style, aspect or base_style["output"].get("aspect", "16:9"))
        sc = min(1.0, 960 / max(dw, dh))
        style_over = projmod.deep_merge(style_over or {}, {"output": {
            "width": int(dw * sc) - int(dw * sc) % 2, "height": int(dh * sc) - int(dh * sc) % 2,
            "fps": min(30, float(base_style["output"].get("fps", 60)))}})
    plan = plan_timing(project_dir, style_over, t_range)
    style, data, tl = plan["style"], plan["data"], plan["tl"]
    shift_timeline(tl, float(style.get("sync_offset", 0.0)))
    zcfg = ZoomConfig.from_dict(data.get("zoom_config"))
    aoi = load_aoi(proj.dir, tl, zcfg)
    segs = resolve_segments(tl, data, zcfg, aoi)
    fps = float(style["output"].get("fps", 60))
    fmt = fmt or style["output"].get("format", "mp4")
    t0, t1, tm = plan["t0"], plan["t1"], plan["timemap"]
    cues = []
    if style.get("captions", {}).get("enabled", False):
        cues = captions_mod().load(proj.dir)
    priv = data.get("privacy") if style.get("privacy", {}).get("enabled", True) else []
    planned = json.loads(Path(_chunk["plan"]).read_text()) if _chunk and _chunk.get("plan") else None
    comp = Compositor(tl, style, segs, zcfg, t0, t1, aspect=aspect, privacy=priv, captions=cues, timemap=tm, aoi=aoi,
                      quality=quality, planned=planned)
    lay = comp.lay
    proj.exports.mkdir(exist_ok=True)
    if out_path is None:
        out_path = proj.exports / f"{proj.name}-{time.strftime('%H%M%S')}{'' if comp.aspect == '16:9' else '-' + comp.aspect.replace(':', 'x')}.{fmt}"
    out_path = Path(out_path)
    mp4_path = out_path if fmt == "mp4" else out_path.with_suffix(".tmp.mp4")
    ff = ffmpeg_bin()
    n_main = int(round(tm.duration * fps))
    n_intro, n_outro = int(round(plan["intro"] * fps)), int(round(plan["outro"] * fps))
    n_all = n_intro + n_main + n_outro
    if n_all <= 0:
        raise projmod.EmptyTrim("the selected trim is shorter than one output frame; extend the trim")
    total = n_all / fps
    gop = int(fps * 2)

    wc = style.get("webcam", {})
    webcam_path = next((p for p in (proj.dir / "webcam.mkv", proj.dir / "webcam.mp4") if p.exists()),
                       proj.dir / "webcam.mkv")
    cam_off = float(tl.capture.get("webcam_offset", 0.0))
    cam_scale = float(tl.capture.get("webcam_scale", 1.0) or 1.0)
    # a live bubble is already in the screen video: never add a second one
    use_cam = bool(wc.get("enabled")) and webcam_path.exists() and tl.capture.get("camera_mode") != "live"
    cam_h = 720 if wc.get("full") else 480
    cam_w = int(round(cam_h * 16 / 9 / 2)) * 2
    cam_vf = f"scale={cam_w}:{cam_h}:force_original_aspect_ratio=increase,crop={cam_w}:{cam_h}" + \
        (",hflip" if wc.get("mirror", True) else "")
    tracks = [t for t in (tl.capture.get("audio_tracks") or []) if (proj.dir / t["file"]).exists()]
    has_audio = fmt == "mp4" and (bool(tracks) or
                                  any(s.get("codec_type") == "audio" for s in probe(proj.raw).get("streams", [])))
    frames_dir = Path(frames_dir) if frames_dir else None
    if frames_dir:
        frames_dir.mkdir(parents=True, exist_ok=True)

    if _chunk is not None:
        # a worker process (dolly/render_chunk.py): same plan, one chunk, progress on stdout
        return _run_chunk(_chunk, comp, tm, fps, proj, lay, ff, quality, gop, n_intro, frames_dir, frame_every,
                          use_cam, webcam_path, cam_off, cam_scale, cam_w, cam_h, cam_vf)
    work = Path(tempfile.mkdtemp(prefix=".render-", dir=proj.exports))
    from .health import Job, check_disk
    msg = check_disk(proj.exports, need=256 << 20)
    if msg:
        raise RuntimeError(msg)
    job = Job("exporting" if fmt == "gif" or notify else "rendering", proj.name, str(out_path))
    user_progress = progress

    def progress(i, n, el):          # noqa: F811 - also feeds the bar's "rendering NN %"
        job.update(i / max(n, 1))
        if user_progress:
            user_progress(i, n, el)
    import multiprocessing as mp
    counter = mp.Value("i", 0, lock=False)
    reused = mp.Value("i", 0, lock=False)
    t_begin = time.time()

    def render_main(a: int, b: int, out_file: Path, warm: int = 8):
        """Main frames [a, b) -> out_file (video only). Runs in a worker or inline."""
        ts0 = tm.to_source(max(0, a - warm) / fps)
        reader = FrameReader(proj.raw, fps, ts0, lay.W, lay.H)
        cam = FrameReader(webcam_path, fps, max(0.0, (ts0 - cam_off) / cam_scale), cam_w, cam_h, cam_vf) \
            if use_cam else None
        enc = subprocess.Popen(_encoder_cmd(ff, lay.OW, lay.OH, fps, quality, out_file, gop), stdin=subprocess.PIPE)
        comp._prev_cursor = None
        try:
            for i in range(max(0, a - warm), b):
                ts = tm.to_source(i / fps)
                f = reader.at(ts)
                if f is None:
                    raise RuntimeError("could not decode the recording (dolly doctor; check raw video)")
                c = cam.at((ts - cam_off) / cam_scale) if cam is not None and ts - cam_off >= 0 else None
                img = comp.compose_at(ts, f, c)
                if i < a:
                    continue          # warm-up: settles motion blur / webcam springs, not written
                enc.stdin.write(gfx.data(img))
                if progress and inline and counter.value % 10 == 0:
                    progress(counter.value, n_all, time.time() - t_begin)
                g = n_intro + i
                if frames_dir and frame_every and g % frame_every == 0:
                    gfx.save(img, frames_dir / f"frame_{g:05d}.png")
                counter.value += 1
            reused.value += comp.reused
        finally:
            enc.stdin.close()
            enc.wait()
            reader.close()
            if cam is not None:
                cam.close()
        if enc.returncode != 0:
            raise RuntimeError("video encoder failed (ffmpeg/libx264): see `dolly doctor`")

    def still(ts):
        r = FrameReader(proj.raw, fps, max(0.0, ts - 1.0), lay.W, lay.H)   # a little early: the end may be past the last frame
        try:
            f = r.at(ts)
        finally:
            r.close()
        return comp.compose_at(ts, f) if f is not None else None

    title = style.get("title", {})
    parts: list[Path] = []
    inline = True
    nw = workers or default_workers(n_main, fps)
    nw = max(1, min(nw, max(1, n_main // max(1, int(fps)))))
    try:
        if n_intro:
            p_in = work / "c-intro.mp4"
            enc = subprocess.Popen(_encoder_cmd(ff, lay.OW, lay.OH, fps, quality, p_in, gop), stdin=subprocess.PIPE)
            card = comp.title_card(title.get("text", ""), title.get("subtitle", ""))
            first = still(tm.to_source(0.0))
            fade = max(1, int(0.45 * fps))
            for i in range(n_intro):
                u = (i - (n_intro - fade)) / fade
                img = card if u <= 0 or first is None else gfx.mix(card, first, u * u * (3 - 2 * u))
                enc.stdin.write(gfx.data(img))
                counter.value += 1
            enc.stdin.close()
            enc.wait()
            parts.append(p_in)
        bounds = chunk_bounds([comp.work_at(tm.to_source(i / fps)) for i in range(n_main)], nw)
        chunks = [(bounds[k], bounds[k + 1], work / f"c-{k:03d}.mp4") for k in range(nw) if bounds[k + 1] > bounds[k]]
        inline = len(chunks) <= 1
        if inline:
            for (a, b, f) in chunks:
                render_main(a, b, f)
        else:
            # Workers are FRESH processes (`python -m dolly.render_chunk job.json`), never a
            # fork: forking a process with live threads (the editor's HTTP threads, a
            # logging lock, a library's thread pool) can copy a held lock into the child, which
            # then waits on it forever (seen in the Omarchy shell harness: futex_do_wait,
            # "rendering 0/1789"). Each worker reads the same project, is handed the camera
            # path worked out here and renders its chunk; progress comes back as lines on stdout.
            start_count = counter.value
            procs = _spawn_workers(job_spec, chunks, work, comp)
            last_seen, last_move = counter.value, time.time()
            stalled = False
            done = {id(p): 0 for p, _ in procs}
            while True:
                alive = False
                for p, rd in procs:
                    for line in rd.read_lines():
                        if line.startswith("frames "):
                            done[id(p)] = int(line.split()[1])
                        elif line.startswith("reused "):
                            reused.value += int(line.split()[1])
                    alive = alive or p.poll() is None
                counter.value = start_count + sum(done.values())
                if progress:
                    progress(min(counter.value, n_all - 1), n_all, time.time() - t_begin)
                if not alive:
                    break
                if counter.value != last_seen:
                    last_seen, last_move = counter.value, time.time()
                elif time.time() - last_move > STALL_S:          # backstop only
                    stalled = True
                    for p, _ in procs:
                        p.kill()
                    for p, _ in procs:
                        p.wait(5)
                    break
                time.sleep(0.1)
            if stalled:
                sys.stderr.write(f"dolly: render workers made no progress for {STALL_S:.0f} s; "
                                 "finishing in one process (set DOLLY_WORKERS=1 to skip workers)\n")
                counter.value = start_count
                for (a, b, f) in chunks:
                    render_main(a, b, f)
            else:
                bad = [p for p, _ in procs if p.returncode != 0]
                if bad:
                    err = (work / f"worker-{procs.index(next(x for x in procs if x[0] is bad[0]))}.log")
                    tail = err.read_text()[-400:] if err.exists() else ""
                    raise RuntimeError(f"render worker failed (exit {bad[0].returncode}): {tail} "
                                       "(try DOLLY_WORKERS=1, see `dolly doctor`)")
        parts += [f for (_, _, f) in chunks]
        if n_outro:
            p_out = work / "c-outro.mp4"
            enc = subprocess.Popen(_encoder_cmd(ff, lay.OW, lay.OH, fps, quality, p_out, gop), stdin=subprocess.PIPE)
            card = comp.title_card(title.get("outro_text", ""), title.get("outro_subtitle", ""))
            last = still(tm.to_source(max(0.0, (n_main - 1) / fps))) if n_main else None
            fade = max(1, int(0.45 * fps))
            for i in range(n_outro):
                u = min(1.0, (i + 1) / fade)
                e = u * u * (3 - 2 * u)
                img = card if last is None else gfx.mix(last, card, e)
                enc.stdin.write(gfx.data(img))
                counter.value += 1
            enc.stdin.close()
            enc.wait()
            parts.append(p_out)
        # join (no re-encode) + audio
        lst = work / "parts.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
        cmd = [ff, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst)]
        if has_audio and tracks:
            from .audio import mix_filter
            au = style.get("audio", {})
            settings = {"duck": au.get("duck", True), "normalize": au.get("normalize", True),
                        "mic": {"denoise": au.get("denoise", True), **(au.get("mic") or {})},
                        "system": dict(au.get("system") or {})}
            for t in tracks:
                cmd += ["-f", "matroska", "-i", str(proj.dir / t["file"])]
            af = mix_filter(tracks, 1, tm.pieces(), plan["intro"], total, settings,
                            tl.capture.get("mic_mutes") or [], au.get("rnnoise_model"))
            cmd += ["-filter_complex", af, "-map", "0:v", "-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]
        elif has_audio:
            au = style.get("audio", {})
            mic = bool(tl.capture.get("mic", False))  # only clean up voice, never desktop audio
            af = audio_filter(tm.pieces(), plan["intro"], total, bool(au.get("denoise", False)) and mic,
                              bool(au.get("normalize", True)), au.get("rnnoise_model"))
            cmd += ["-i", str(proj.raw), "-filter_complex", af, "-map", "0:v", "-map", "[aout]",
                    "-c:a", "aac", "-b:a", "160k"]
        cmd += ["-c:v", "copy", "-movflags", "+faststart", str(mp4_path)]
        r = subprocess.run(cmd, capture_output=True, text=True)    # no timeout: muxes the whole export
        if r.returncode != 0:
            raise RuntimeError(f"could not write {mp4_path.name}: {r.stderr.strip()[-300:]} "
                               "(disk full? see `dolly doctor`)")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        job.done()
    el = time.time() - t_begin
    if progress:
        progress(n_all, n_all, el)
    if stats is not None:
        stats.update({"frames": n_all, "seconds": round(el, 2), "fps": round(n_all / max(el, 1e-6), 1),
                      "workers": len(chunks) if n_main else 0, "reused": int(reused.value), "quality": quality})
    if cues:
        out_cues = captions_mod().remap(cues, tm, plan["intro"])
        captions_mod().write_srt(out_cues, out_path.with_suffix(".srt"))
    if fmt == "gif":
        gw = min(lay.OW, int(style.get("gif", {}).get("width", 800)))
        gfps = int(style.get("gif", {}).get("fps", 15))
        vf = f"fps={gfps},scale={gw}:-2:flags=lanczos"
        # global palette + bayer dither + rectangle diffs: smooth gradients without
        # error-diffusion "crawl", and small files because static areas stay static
        # no timeout: a GIF encode of the whole export (time grows with its length)
        subprocess.run([ff, "-v", "error", "-y", "-i", str(mp4_path), "-vf",
                        f"{vf},split[a][b];[a]palettegen=stats_mode=full:max_colors=192[p];"
                        f"[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
                        str(out_path)], check=True)
        mp4_path.unlink(missing_ok=True)
    if settings and quality != "draft" and t_range is None and frames_dir is None:
        out_path = _post_export(proj, out_path, fmt, comp.aspect, quality, progress, style_over, notify)
    if notify:
        try:
            from .share import notify_export
            notify_export(proj.dir, out_path)
        except Exception:
            _dlog.swallowed()
            pass
    return out_path


def _auto_scrub(proj):
    """Settings > Privacy > Blur secrets: OCR the recording once and add blur boxes."""
    data = proj.load()
    if not data["style"].get("privacy", {}).get("auto_scrub") or data.get("scrubbed"):
        return
    try:
        from .events import Timeline
        from .privacy import scan_secrets
        tl = Timeline.load(proj.events)
        boxes = scan_secrets(proj.raw, tl.duration, every=1.0)
        data["privacy"] = [b for b in data.get("privacy", []) if b.get("source") != "secret"] + boxes
        data["scrubbed"] = time.time()
        proj.save(data)
    except Exception as e:      # tesseract missing: say so, never block the export
        print(f"secret blur skipped: {e}", file=sys.stderr)


def _post_export(proj, out_path: Path, fmt, aspect, quality, progress, style_over, notify) -> Path:
    from . import config as cfgmod
    try:
        ex = cfgmod.export_defaults()
        keep_raw = cfgmod.load()["storage"]["keep_raw"]
    except Exception:
        _dlog.swallowed()
        return out_path
    cap = int(ex.get("size_cap_mb") or 0)
    if cap and out_path.exists() and out_path.stat().st_size > cap * (1 << 20):
        from .share import encode as enc
        capped = out_path.with_name(out_path.stem + f"-{cap}MB" + out_path.suffix)
        info = enc.media_info(out_path)
        if fmt == "gif":
            enc.gif_under_cap(out_path, capped, cap * (1 << 20), int(info.get("w") or 800), 15, log=lambda *a: None)
        else:
            enc.target_size_mp4(out_path, capped, cap * (1 << 20), int(info["w"]), int(info["h"]),
                                float(info.get("fps") or 30), log=lambda *a: None)
        out_path.unlink()
        out_path = capped
    if ex.get("also_vertical") and aspect == "16:9" and fmt != "gif":
        export(proj.dir, None, fmt, quality, progress=progress, style_over=style_over, aspect="9:16",
               notify=False, settings=False)
    if not keep_raw:
        from .library import trash_raw
        trash_raw(proj.dir)
    return out_path


class _LineReader:
    def __init__(self, f):
        self.f, self.buf = f, b""
        os.set_blocking(f.fileno(), False)

    def read_lines(self):
        try:
            data = self.f.read()
        except (BlockingIOError, ValueError):
            data = None
        if data:
            self.buf += data
        out = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            out.append(line.decode(errors="replace").strip())
        return out


def _spawn_workers(job_spec: dict, chunks, work: Path, comp):
    procs = []
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]) +
               (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    # The path goes to the workers finished. Working it out again in each cost seconds on a
    # long take and most of a worker's memory: its tracks, one value per simulation step,
    # are lists of float objects (a 10 minute take: about 170 MB a process).
    plan = work / "plan.json"
    plan.write_text(json.dumps(vars(comp.crop if comp.reframe else comp.path)))
    for k, (a, b, f) in enumerate(chunks):
        jf = work / f"job-{k}.json"
        jf.write_text(json.dumps({**job_spec, "chunk": {"a": a, "b": b, "out": str(f), "plan": str(plan)}}))
        p = subprocess.Popen([sys.executable, "-m", "dolly.render_chunk", str(jf)], stdout=subprocess.PIPE,
                             stderr=open(work / f"worker-{k}.log", "w"), stdin=subprocess.DEVNULL, env=env)
        procs.append((p, _LineReader(p.stdout)))
    return procs


def _run_chunk(chunk, comp, tm, fps, proj, lay, ff, quality, gop, n_intro, frames_dir, frame_every,
               use_cam, webcam_path, cam_off, cam_scale, cam_w, cam_h, cam_vf):
    """Worker side: render frames [a, b) of the main part into chunk["out"]."""
    a, b, out_file = int(chunk["a"]), int(chunk["b"]), Path(chunk["out"])
    warm = 8
    ts0 = tm.to_source(max(0, a - warm) / fps)
    reader = FrameReader(proj.raw, fps, ts0, lay.W, lay.H)
    cam = FrameReader(webcam_path, fps, max(0.0, (ts0 - cam_off) / cam_scale), cam_w, cam_h, cam_vf) \
        if use_cam else None
    enc = subprocess.Popen(_encoder_cmd(ff, lay.OW, lay.OH, fps, quality, out_file, gop), stdin=subprocess.PIPE)
    n = 0
    print("frames 0", flush=True)
    try:
        for i in range(max(0, a - warm), b):
            ts = tm.to_source(i / fps)
            f = reader.at(ts)
            if f is None:
                raise RuntimeError("could not decode the recording")
            c = cam.at((ts - cam_off) / cam_scale) if cam is not None and ts - cam_off >= 0 else None
            img = comp.compose_at(ts, f, c)
            if i < a:
                continue
            enc.stdin.write(gfx.data(img))
            g = n_intro + i
            if frames_dir and frame_every and g % frame_every == 0:
                gfx.save(img, Path(frames_dir) / f"frame_{g:05d}.png")
            n += 1
            if n % 5 == 0:
                print(f"frames {n}", flush=True)
    finally:
        enc.stdin.close()
        enc.wait()
        reader.close()
        if cam is not None:
            cam.close()
    if enc.returncode != 0:
        raise RuntimeError("video encoder failed (ffmpeg/libx264)")
    print(f"frames {n}", flush=True)
    print(f"reused {comp.reused}", flush=True)
    return out_file


def _chunk_entry(fn, a, b, f):
    try:
        fn(a, b, f)
    except BaseException as e:  # report and fail the worker
        sys.stderr.write(f"dolly render worker: {e}\n")
        os._exit(3)
    os._exit(0)


def render_still(project_dir, t: float, out_png, style_over=None, aspect=None, quality: str = "best") -> Path:
    """Render one composited frame at source time t (thumbnails / checks). A still is
    looked at closely and one frame costs nothing: it is drawn as the `best` export draws
    (`quality` names another export quality to see what that one gives)."""
    proj = projmod.Project(project_dir)
    data = proj.load()
    style = projmod.deep_merge(data["style"], style_over)
    tl = Timeline.load(proj.events)
    zcfg = ZoomConfig.from_dict(data.get("zoom_config"))
    aoi = load_aoi(proj.dir, tl, zcfg)
    segs = resolve_segments(tl, data, zcfg, aoi)
    fps = float(style["output"].get("fps", 60))
    cues = captions_mod().load(proj.dir) if style.get("captions", {}).get("enabled") else []
    comp = Compositor(tl, style, segs, zcfg, max(0.0, t - 3.0), t + 2.0 / fps, aspect=aspect,
                      privacy=data.get("privacy"), captions=cues, aoi=aoi, quality=quality)
    raw = subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{t:.4f}", "-i", str(proj.raw), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "bgr0", "-"], capture_output=True, timeout=60).stdout
    if len(raw) != 4 * tl.w * tl.h:
        raise RuntimeError("could not decode the recording (dolly doctor; check raw video)")
    img = comp.compose_at(t, gfx.frame_from(bytearray(raw), tl.w, tl.h))
    gfx.save(img, out_png)
    return Path(out_png)


def cli_progress(i, n, el):
    rate = i / el if el > 0 else 0
    if sys.stderr.isatty():
        sys.stderr.write(f"\r  rendering {i}/{n} frames  {rate:5.1f} fps")
        if i == n:
            sys.stderr.write("\n")
    else:                                       # logs / pipes: one line per 10 %
        bucket = i * 10 // max(1, n)
        if bucket != getattr(cli_progress, "_bucket", -1):
            sys.stderr.write(f"  rendering {i}/{n} frames  {rate:5.1f} fps\n")
            cli_progress._bucket = bucket
        if i == n:
            cli_progress._bucket = -1
    sys.stderr.flush()

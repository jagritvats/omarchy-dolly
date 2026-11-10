"""A tiny deterministic Omarchy/Hyprland desktop simulator for scenario tests.

It renders what gpu-screen-recorder would capture from one monitor (no
hardware cursor), writes the event log the Dolly logger would write
(logical global coordinates, its own clock, optional drift) and a ground-truth
file saying, frame by frame, what the viewer must be able to see.

Model (logical px, like Hyprland):
  * a top bar with workspace numbers, a ticking clock and a spinner (noise)
  * tiled / floating windows on workspaces, animated like Omarchy's
    looknfeel (windows 0.30 s easeOutQuint, popin 87 %; workspaces switch
    instantly by default, optional slide)
  * terminals (prompt, typed text, output, scrolling, blinking caret),
    apps (header, list, dialog with a text field), browsers (spinner)
  * notifications (layer, no keyboard), the Omarchy menu (layer, keyboard)
  * the Dolly sync marker (magenta/green square, two frames)

Everything is a pure function of t, so frames dedupe by signature.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
MONO = str(HERE / "fonts" / "jetbrains-mono-latin-400-normal.woff2")
MONO_B = str(HERE / "fonts" / "jetbrains-mono-latin-700-normal.woff2")

BG = (0x26, 0x1b, 0x1a)          # Tokyo Night background (BGR)
FG = (0xf5, 0xca, 0xc0)
DIM = (0x89, 0x71, 0x56)
GREEN = (0x6a, 0xce, 0x9e)
BLUE = (0xf7, 0xa2, 0x7a)
YELLOW = (0x68, 0xaf, 0xe0)
RED = (0x8e, 0x76, 0xf7)
PANEL = (0x3b, 0x26, 0x24)
ACTIVE = (0xff, 0xcc, 0x33)       # Omarchy active border (33ccff)
INACTIVE = (0x59, 0x59, 0x59)
COLORS = {"fg": FG, "dim": DIM, "green": GREEN, "blue": BLUE, "yellow": YELLOW, "red": RED}
BAR_H = 26
GAP_OUT, GAP_IN, BORDER = 10, 5, 2
SYNC_RGB = (255, 0, 255)
SYNC_INNER = (0, 255, 0)


def ease_out_quint(u):
    u = min(max(u, 0.0), 1.0)
    return 1 - (1 - u) ** 5


@lru_cache(maxsize=16)
def font(path, px):
    return ImageFont.truetype(path, int(px))


@lru_cache(maxsize=4096)
def text_img(text: str, px: int, color: tuple, bold: bool = False):
    """Rendered text as (premultiplied float BGR, alpha) arrays."""
    f = font(MONO_B if bold else MONO, px)
    w = max(1, int(math.ceil(f.getlength(text))) + 2)
    h = int(px * 1.35)
    im = Image.new("L", (w, h), 0)
    ImageDraw.Draw(im).text((1, int(px * 0.12)), text, font=f, fill=255)
    a = np.asarray(im, np.float32)[..., None] / 255.0
    col = np.array(color, np.float32)[None, None, :]
    return col * a, a


def blit_text(img, x, y, text, px, color, bold=False, clip=None):
    if not text:
        return
    prem, a = text_img(text, int(px), tuple(color), bold)
    h, w = a.shape[:2]
    x, y = int(round(x)), int(round(y))
    H, W = img.shape[:2]
    cx0, cy0, cx1, cy1 = clip if clip else (0, 0, W, H)
    x0, y0, x1, y1 = max(x, cx0, 0), max(y, cy0, 0), min(x + w, cx1, W), min(y + h, cy1, H)
    if x1 <= x0 or y1 <= y0:
        return
    roi = img[y0:y1, x0:x1].astype(np.float32)
    pa = a[y0 - y:y1 - y, x0 - x:x1 - x]
    pp = prem[y0 - y:y1 - y, x0 - x:x1 - x]
    img[y0:y1, x0:x1] = (pp + roi * (1 - pa)).astype(np.uint8)


def fill(img, r, color, clip=None):
    H, W = img.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in r]
    if clip:
        x0, y0, x1, y1 = max(x0, clip[0]), max(y0, clip[1]), min(x1, clip[2]), min(y1, clip[3])
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if x1 > x0 and y1 > y0:
        img[y0:y1, x0:x1] = color


# ------------------------------------------------------------------ content
@dataclass
class TermEntry:
    t: float                       # when it appears
    text: str = ""
    color: str = "fg"
    prompt: str | None = None      # prompt entries: typed keys follow
    keys: list = field(default_factory=list)   # [(t, ch)]
    enter: float | None = None


class Terminal:
    """A shell: prompt + typed keys + output lines, scrolled to the bottom."""
    kind = "term"
    cls = "Alacritty"

    def __init__(self, cwd="~/app"):
        self.entries: list[TermEntry] = []
        self.cwd = cwd

    def prompt(self, t, cwd=None):
        e = TermEntry(t, prompt=f"{cwd or self.cwd} $ ")
        self.entries.append(e)
        return e

    def out(self, t, text, color="fg"):
        self.entries.append(TermEntry(t, text, color))

    def lines_at(self, t):
        """[(segments[(text, color)]), ...], caret (row, col) or None."""
        rows = []
        caret = None
        for e in self.entries:
            if e.t > t:
                continue
            if e.prompt is not None:
                typed = "".join(ch for (kt, ch) in e.keys if kt <= t)
                rows.append([(e.prompt, "blue"), (typed, "fg")])
                if e.enter is None or e.enter > t:
                    last_key = max([kt for kt, _ in e.keys if kt <= t], default=e.t)
                    caret = (len(rows) - 1, len(e.prompt) + len(typed), last_key)
                else:
                    caret = None
            else:
                rows.append([(e.text, e.color)])
                caret = None
        return rows, caret

    def recent_output(self, t, span=0.5):
        return sum(1 for e in self.entries if e.prompt is None and t - span < e.t <= t)


class App:
    """A generic GUI app: header, sidebar, list rows, optional dialog with a field."""
    kind = "app"
    cls = "chromium"

    def __init__(self, title="Tasks", rows=None, spinner=False):
        self.title = title
        self.rows = rows or ["Write release notes", "Review PR #482", "Fix login redirect", "Ship v2.3",
                             "Update screenshots", "Plan Q3 roadmap", "Reply to Priya"]
        self.dialogs: list[dict] = []   # {t0, t1, rect (rel, logical), title, keys[(t,ch)]}
        self.highlights: list[tuple] = []  # (t0, t1, row)
        self.spinner = spinner

    def dialog_at(self, t):
        for d in self.dialogs:
            if d["t0"] <= t < d["t1"]:
                return d
        return None


# ------------------------------------------------------------------ windows
@dataclass
class Win:
    id: str
    content: object
    ws: str = "1"
    floating: bool = False
    keys: list = field(default_factory=list)   # [(t, (x, y, w, h))] target rects (logical, monitor-local)
    open_t: float = -1.0
    close_t: float = 1e9
    anim: float = 0.30
    title: str = ""
    fullscreen: list = field(default_factory=list)  # [(t0, t1)]

    @property
    def cls(self):
        return self.content.cls

    def rect_at(self, t):
        """Animated rect: each new target eases from wherever the window was."""
        ks = self.keys
        if not ks or t < ks[0][0]:
            return ks[0][1] if ks else None
        cur = ks[0][1]
        for i in range(1, len(ks)):
            tk, r = ks[i]
            if t < tk:
                break
            dur = self.anim if not self.floating else 0.0
            u = ease_out_quint((t - tk) / dur) if dur > 0 else 1.0
            start = cur
            cur = tuple(a + (b - a) * u for a, b in zip(start, r))
        return cur

    def settled_rect(self, t):
        r = None
        for tk, rr in self.keys:
            if tk <= t:
                r = rr
        return r or (self.keys[0][1] if self.keys else None)

    def visible(self, t):
        return self.open_t <= t < self.close_t + 0.12

    def is_fullscreen(self, t):
        return any(a <= t < b for a, b in self.fullscreen)


def tiles(n, lw, lh):
    """Dwindle layout for n windows (logical, monitor-local)."""
    x0, y0 = GAP_OUT, BAR_H + GAP_OUT
    x1, y1 = lw - GAP_OUT, lh - GAP_OUT
    if n <= 1:
        return [(x0, y0, x1 - x0, y1 - y0)]
    mid = (x0 + x1) / 2
    left = (x0, y0, mid - GAP_IN - x0, y1 - y0)
    if n == 2:
        return [left, (mid + GAP_IN, y0, x1 - mid - GAP_IN, y1 - y0)]
    my = (y0 + y1) / 2
    return [left, (mid + GAP_IN, y0, x1 - mid - GAP_IN, my - GAP_IN - y0),
            (mid + GAP_IN, my + GAP_IN, x1 - mid - GAP_IN, y1 - my - GAP_IN)]


# ------------------------------------------------------------------ scene
class Scene:
    def __init__(self, lw=1280, lh=800, scale=1.5, origin=(0, 0), seed=1):
        self.lw, self.lh, self.s = lw, lh, scale
        self.W, self.H = int(round(lw * scale)), int(round(lh * scale))
        self.W -= self.W % 2
        self.H -= self.H % 2
        self.origin = origin
        self.rng = random.Random(seed)
        self.wins: list[Win] = []
        self.focus: list[tuple] = []                 # (t, win id)
        self.ws_switch: list[tuple] = []            # (t, ws, style)  style: "instant" | "slide"
        self.special: list[tuple] = []              # (t0, t1) special workspace shown
        self.notifs: list[dict] = []                # {t0, t1, title, body}
        self.menus: list[dict] = []                 # {t0, t1, keys[(t,ch)], items}
        self.popups: list[dict] = []                # {t0, t1, x, y, items}
        self.sync_marks: list[tuple] = []           # (t, kind)
        self.truth_extra: list = []                 # scenario-provided (t0, t1, fn(t) -> dict)
        self.wall = self._wallpaper()

    # ---------------------------------------------------------- building
    def add(self, win: Win, focus=True):
        self.wins.append(win)
        if focus:
            self.set_focus(win.open_t, win.id)
        return win

    def set_focus(self, t, wid):
        self.focus.append((t, wid))
        self.focus.sort(key=lambda f: f[0])

    def focus_at(self, t):
        f = None
        for tt, wid in self.focus:
            if tt <= t:
                f = wid
        w = self.win(f)
        if w is not None and not (w.open_t <= t < w.close_t):
            return None
        return f

    def win(self, wid):
        return next((w for w in self.wins if w.id == wid), None)

    def ws_at(self, t):
        ws = "1"
        for tt, w, _ in self.ws_switch:
            if tt <= t:
                ws = w
        return ws

    def special_on(self, t):
        return any(a <= t < b for a, b in self.special)

    def transition(self, t):
        """(from_ws, to_ws, u) during a slide, else None."""
        prev = "1"
        for tt, w, style in self.ws_switch:
            if style == "slide" and tt <= t < tt + 0.3:
                return prev, w, ease_out_quint((t - tt) / 0.3)
            if tt <= t:
                prev = w
        return None

    # ---------------------------------------------------------- render
    def _wallpaper(self):
        p = HERE / "wall.jpg"
        img = cv2.imread(str(p)) if p.exists() else None
        if img is None:
            img = np.zeros((self.H, self.W, 3), np.uint8)
            img[:] = (60, 40, 30)
        return cv2.resize(img, (self.W, self.H), interpolation=cv2.INTER_AREA)

    def L(self, v):
        return v * self.s

    def R(self, r):
        """Logical (x, y, w, h) -> video (x0, y0, x1, y1)."""
        x, y, w, h = r
        return (x * self.s, y * self.s, (x + w) * self.s, (y + h) * self.s)

    def signature(self, t):
        parts = [round(t, 3)] if self._animating(t) else [self._tick(t)]
        return parts

    def _animating(self, t):
        return True  # cheap enough; frames still dedupe by content hash in the writer

    def _tick(self, t):
        return int(t)

    def render(self, t):
        img = self.wall.copy()
        tr = self.transition(t)
        if tr is not None:
            a, b, u = tr
            self._draw_ws(img, t, a, dx=-u * self.lw)
            self._draw_ws(img, t, b, dx=(1 - u) * self.lw)
        else:
            self._draw_ws(img, t, self.ws_at(t))
        if self.special_on(t) or any(a - 0.3 < t < a for a, b in self.special) or any(b <= t < b + 0.2 for a, b in self.special):
            self._draw_special(img, t)
        self._draw_bar(img, t)
        self._draw_popups(img, t)
        self._draw_notifs(img, t)
        self._draw_menus(img, t)
        self._draw_sync(img, t)
        return img

    def _draw_ws(self, img, t, ws, dx=0.0):
        focus = self.focus_at(t)
        wins = [w for w in self.wins if w.ws == ws and w.visible(t)]
        wins.sort(key=lambda w: (w.floating, w.id == focus))
        for w in wins:
            self._draw_win(img, t, w, focus == w.id, dx)

    def _draw_special(self, img, t):
        on = self.special_on(t)
        # Hyprland "specialWorkspace" slidevert: slides down from the top
        u = 1.0
        for a, b in self.special:
            if a <= t < a + 0.3:
                u = ease_out_quint((t - a) / 0.3)
            elif b <= t < b + 0.2:
                u = 1 - (t - b) / 0.2
        dim = (img.astype(np.float32) * (1 - 0.35 * u)).astype(np.uint8)
        img[:] = dim
        focus = self.focus_at(t)
        for w in self.wins:
            if w.ws == "special" and (w.visible(t) or not on):
                self._draw_win(img, t, w, focus == w.id, 0, dy=-(1 - u) * self.lh)

    def _draw_win(self, img, t, w, focused, dx=0.0, dy=0.0):
        r = w.rect_at(t)
        if r is None:
            return
        if w.is_fullscreen(t):
            r = (0, 0, self.lw, self.lh)
        x, y, ww, hh = r
        a = 1.0
        if t < w.open_t + 0.33:          # popin 87 %
            u = ease_out_quint((t - w.open_t) / 0.33)
            sc = 0.87 + 0.13 * u
            cx, cy = x + ww / 2, y + hh / 2
            ww, hh = ww * sc, hh * sc
            x, y = cx - ww / 2, cy - hh / 2
            a = u
        if t >= w.close_t:
            u = min(1.0, (t - w.close_t) / 0.12)
            sc = 1 - 0.13 * u
            cx, cy = x + ww / 2, y + hh / 2
            ww, hh = ww * sc, hh * sc
            x, y = cx - ww / 2, cy - hh / 2
            a = 1 - u
        if a <= 0.01:
            return
        X0, Y0, X1, Y1 = self.R((x + dx, y + dy, ww, hh))
        layer = img if a >= 0.999 else img.copy()
        b = max(1, round(BORDER * self.s))
        fill(layer, (X0, Y0, X1, Y1), ACTIVE if focused else INACTIVE)
        inner = (X0 + b, Y0 + b, X1 - b, Y1 - b)
        fill(layer, inner, BG)
        clip = tuple(int(round(v)) for v in inner)
        if isinstance(w.content, Terminal):
            self._draw_term(layer, t, w, inner, clip, focused)
        elif isinstance(w.content, App):
            self._draw_app(layer, t, w, inner, clip)
        if layer is not img:
            cv2.addWeighted(layer, a, img, 1 - a, 0, dst=img)

    # terminal metrics (video px)
    def term_metrics(self):
        px = int(round(13 * self.s))
        lh = int(round(17 * self.s))
        cw = font(MONO, px).getlength("M")
        pad = 8 * self.s
        return px, lh, cw, pad

    def term_layout(self, t, w, inner=None):
        """Visible rows, first row index, rows that fit, caret, text origin (video px)."""
        if inner is None:
            r = w.rect_at(t)
            if w.is_fullscreen(t):
                r = (0, 0, self.lw, self.lh)
            X0, Y0, X1, Y1 = self.R(r)
            b = max(1, round(BORDER * self.s))
            inner = (X0 + b, Y0 + b, X1 - b, Y1 - b)
        px, lh, cw, pad = self.term_metrics()
        rows, caret = w.content.lines_at(t)
        fit = max(1, int((inner[3] - inner[1] - 2 * pad) // lh))
        first = max(0, len(rows) - fit)
        return rows, first, fit, caret, (inner[0] + pad, inner[1] + pad), (px, lh, cw)

    def _draw_term(self, img, t, w, inner, clip, focused):
        rows, first, fit, caret, (ox, oy), (px, lh, cw) = self.term_layout(t, w, inner)
        for i, segs in enumerate(rows[first:]):
            x = ox
            for text, col in segs:
                blit_text(img, x, oy + i * lh, text, px, COLORS.get(col, FG), clip=clip)
                x += cw * len(text)
        if caret is not None and focused:
            row, col, last_key = caret
            on = (t - last_key) < 0.5 or int((t - last_key - 0.5) / 0.53) % 2 == 0
            if on and row >= first:
                cx = ox + col * cw
                cy = oy + (row - first) * lh
                fill(img, (cx, cy + 2, cx + cw, cy + lh - 1), FG, clip)

    def term_line_rect(self, t, w):
        """Truth for typing: the prompt line from its start to the caret (+1 char), video px."""
        rows, first, fit, caret, (ox, oy), (px, lh, cw) = self.term_layout(t, w)
        if caret is None:
            return None, lh
        row, col, _ = caret
        y = oy + (row - first) * lh
        return (ox, y, ox + (col + 1) * cw, y + lh), lh

    def _draw_app(self, img, t, w, inner, clip):
        s = self.s
        X0, Y0, X1, Y1 = inner
        app: App = w.content
        fill(img, (X0, Y0, X1, Y0 + 34 * s), PANEL, clip)
        blit_text(img, X0 + 14 * s, Y0 + 8 * s, app.title, int(14 * s), FG, bold=True, clip=clip)
        side = X0 + min(180 * s, (X1 - X0) * 0.28)
        fill(img, (X0, Y0 + 34 * s, side, Y1), (0x30, 0x22, 0x20), clip)
        for i, name in enumerate(["Inbox", "Today", "Upcoming", "Projects", "Archive"]):
            blit_text(img, X0 + 16 * s, Y0 + (52 + 26 * i) * s, name, int(12 * s), DIM if i else FG, clip=clip)
        for i, row in enumerate(app.rows):
            ry = Y0 + (50 + 38 * i) * s
            if ry > Y1 - 30 * s:
                break
            hl = any(a <= t < b and k == i for a, b, k in app.highlights)
            fill(img, (side + 14 * s, ry, X1 - 14 * s, ry + 30 * s), (0x52, 0x3a, 0x33) if hl else (0x36, 0x27, 0x24), clip)
            fill(img, (side + 24 * s, ry + 9 * s, side + 36 * s, ry + 21 * s), DIM, clip)
            blit_text(img, side + 46 * s, ry + 6 * s, row, int(12 * s), FG, clip=clip)
        if app.spinner:
            cx, cy = X1 - 30 * s, Y0 + 17 * s
            ang = (t * 360 * 1.2) % 360
            cv2.ellipse(img, (int(cx), int(cy)), (int(8 * s), int(8 * s)), ang, 0, 270, FG, max(1, int(2 * s)), cv2.LINE_AA)
        d = app.dialog_at(t)
        if d is not None:
            u = ease_out_quint((t - d["t0"]) / 0.2)
            rx, ry, rw, rh = d["rect"]
            D = (X0 + rx * s, Y0 + ry * s, X0 + (rx + rw) * s, Y0 + (ry + rh) * s)
            sh = img.copy()
            fill(sh, (X0, Y0, X1, Y1), (0, 0, 0), clip)
            cv2.addWeighted(sh, 0.35 * u, img, 1 - 0.35 * u, 0, dst=img)
            fill(img, D, (0x44, 0x30, 0x2c), clip)
            fill(img, (D[0], D[1], D[2], D[1] + 3 * s), ACTIVE, clip)
            blit_text(img, D[0] + 18 * s, D[1] + 16 * s, d["title"], int(15 * s), FG, bold=True, clip=clip)
            fy = D[1] + 52 * s
            fill(img, (D[0] + 18 * s, fy, D[2] - 18 * s, fy + 32 * s), (0x26, 0x1b, 0x1a), clip)
            typed = "".join(ch for kt, ch in d.get("keys", []) if kt <= t)
            blit_text(img, D[0] + 26 * s, fy + 7 * s, typed, int(13 * s), FG, clip=clip)
            fill(img, (D[2] - 120 * s, D[3] - 46 * s, D[2] - 18 * s, D[3] - 16 * s), (0xf7, 0xa2, 0x7a), clip)
            blit_text(img, D[2] - 98 * s, D[3] - 40 * s, "Create", int(12 * s), (0x26, 0x1b, 0x1a), bold=True, clip=clip)

    def dialog_rect(self, t, w):
        d = w.content.dialog_at(t) if isinstance(w.content, App) else None
        if d is None:
            return None
        r = w.rect_at(t)
        b = BORDER
        rx, ry, rw, rh = d["rect"]
        return self.R((r[0] + b + rx, r[1] + b + ry, rw, rh))

    def _draw_bar(self, img, t):
        s = self.s
        fill(img, (0, 0, self.W, BAR_H * s), (0x1e, 0x14, 0x14))
        ws = self.ws_at(t)
        for i, name in enumerate("12345"):
            x = (10 + 22 * i) * s
            blit_text(img, x, 5 * s, name, int(12 * s), FG if name == ws else DIM, bold=name == ws)
        sec = int(t) + 27
        clock = f"Mon 14:{3 + sec // 60:02d}:{sec % 60:02d}"
        blit_text(img, self.W / 2 - 60 * s, 5 * s, clock, int(12 * s), FG)
        ang = (t * 300) % 360
        cv2.ellipse(img, (int(self.W - 24 * s), int(13 * s)), (int(6 * s), int(6 * s)), ang, 0, 250, FG,
                    max(1, int(1.5 * s)), cv2.LINE_AA)

    def notif_rect(self, n, t):
        s = self.s
        u = ease_out_quint((t - n["t0"]) / 0.25)
        x = self.lw - GAP_OUT - 340 + (1 - u) * 60
        return (x, BAR_H + GAP_OUT, 340, 74)

    def _draw_notifs(self, img, t):
        for n in self.notifs:
            if not (n["t0"] <= t < n["t1"]):
                continue
            r = self.R(self.notif_rect(n, t))
            fill(img, r, (0x38, 0x28, 0x24))
            fill(img, (r[0], r[1], r[0] + 3 * self.s, r[3]), ACTIVE)
            blit_text(img, r[0] + 14 * self.s, r[1] + 10 * self.s, n["title"], int(13 * self.s), FG, bold=True)
            blit_text(img, r[0] + 14 * self.s, r[1] + 36 * self.s, n["body"], int(12 * self.s), DIM)

    def menu_rect(self):
        return (self.lw / 2 - 210, self.lh / 2 - 170, 420, 340)

    def _draw_menus(self, img, t):
        for m in self.menus:
            if not (m["t0"] <= t < m["t1"]):
                continue
            s = self.s
            r = self.R(self.menu_rect())
            fill(img, (r[0] - 2 * s, r[1] - 2 * s, r[2] + 2 * s, r[3] + 2 * s), ACTIVE)
            fill(img, r, (0x26, 0x1b, 0x1a))
            typed = "".join(ch for kt, ch in m["keys"] if kt <= t)
            blit_text(img, r[0] + 16 * s, r[1] + 14 * s, "> " + typed, int(14 * s), FG)
            items = [i for i in m["items"] if typed.lower() in i.lower()] or m["items"][:1]
            for k, it in enumerate(items[:8]):
                y = r[1] + (52 + 34 * k) * s
                if k == 0:
                    fill(img, (r[0] + 8 * s, y - 4 * s, r[2] - 8 * s, y + 26 * s), (0x52, 0x3a, 0x33))
                blit_text(img, r[0] + 22 * s, y, it, int(13 * s), FG if k == 0 else DIM)

    def popup_rect(self, p):
        return (p["x"], p["y"], 190, 30 * len(p["items"]) + 12)

    def _draw_popups(self, img, t):
        for p in self.popups:
            if not (p["t0"] <= t < p["t1"]):
                continue
            r = self.R(self.popup_rect(p))
            fill(img, r, (0x3a, 0x29, 0x26))
            for k, it in enumerate(p["items"]):
                blit_text(img, r[0] + 14 * self.s, r[1] + (8 + 30 * k) * self.s, it, int(12 * self.s), FG)

    def sync_rect(self):
        return (0, 0, 40, 40)

    def _draw_sync(self, img, t):
        # visible for exactly two frames starting at the first frame at/after the marker
        for tm, _kind in self.sync_marks:
            if tm <= t < tm + self._sync_len:
                r = self.R(self.sync_rect())
                fill(img, r, SYNC_RGB[::-1])
                c = ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
                q = (r[2] - r[0]) / 4
                fill(img, (c[0] - q, c[1] - q, c[0] + q, c[1] + q), SYNC_INNER[::-1])

    _sync_len = 2 / 30

    # ---------------------------------------------------------- logger output
    def clients_at(self, t):
        """What `hyprctl clients` would report: visible windows (global logical) + focus."""
        ws = self.ws_at(t)
        out = []
        for w in self.wins:
            if not (w.open_t <= t < w.close_t):
                continue
            if w.ws != ws and not (w.ws == "special" and self.special_on(t)):
                continue
            r = w.rect_at(t)
            if w.is_fullscreen(t):
                r = (0, 0, self.lw, self.lh)
            out.append({"address": "0x" + w.id, "class": w.cls, "title": w.title or w.cls,
                        "rect": [round(r[0] + self.origin[0]), round(r[1] + self.origin[1]), round(r[2]), round(r[3])],
                        "floating": w.floating, "fullscreen": w.is_fullscreen(t), "workspace": w.ws})
        f = self.focus_at(t)
        return out, ("0x" + f) if f else None


def to_global(scene, x, y):
    return x + scene.origin[0], y + scene.origin[1]

"""9:16 fills the frame: no whole window letterboxed with large bands, and a dialog stays whole.

Ground truth, 10 Nov 2026: for 17.6 of 28 seconds the vertical export showed a floating
window (or more) letterboxed at 1.77 times the full-height strip, the picture 56 % of the
card's height; and the dialog a click opened was cut at its right edge, button included.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import aoi as aoimod  # noqa: E402
from dolly.aoi import AOITrack  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.reframe import CUT_HOP, LETTERBOX_MAX, POINTER_PAN, reframe_path  # noqa: E402
from dolly.zoom import ZoomConfig  # noqa: E402

W, H = 1920, 1200
ASPECT = 0.53                                   # the 9:16 card inside its padding
STRIP = H * ASPECT                              # the widest crop that fills the card: 636 px
WIN = (386.0, 196.0, 1486.0, 956.0)             # a floating 1100x760 window
FPS = 30


def track(spans, duration=12.0, transitions=()):
    t = np.round(np.arange(0, duration, 0.1), 3)
    rect, kind, foc = [None] * len(t), [None] * len(t), [None] * len(t)
    for i, ti in enumerate(t):
        for a, b, k, r, f in spans:
            if a <= ti < b:
                rect[i], kind[i], foc[i] = r, k, f
    return AOITrack(t, rect, kind, foc, list(transitions), 16.0)


def window(addr="0x1", rect=WIN, cls="chromium"):
    return {"address": addr, "class": cls, "title": "t", "rect": [rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]],
            "floating": True, "fullscreen": False, "xwayland": False, "workspace": "1"}


def timeline(events=(), duration=12.0, windows=None, start=(1200.0, 800.0)):
    wins = [window()] if windows is None else windows
    ev = [{"t": 0.0, "type": "move", "x": start[0], "y": start[1]},
          {"t": 0.02, "type": "clients", "focus": wins[0]["address"] if wins else None, "windows": wins}] + list(events)
    return Timeline.from_dict({"video": {"w": W, "h": H, "fps": 60, "duration": duration},
                               "capture": {"cursor_baked": True, "scale": 1.0}, "events": ev})


def crop(tl, aoi, segs=()):
    return reframe_path(tl, list(segs), ASPECT, FPS, 0.0, tl.duration, ZoomConfig(), aoi=aoi)


def span(p, t):
    i = int(t * FPS)
    return float(p.cx[i] - p.cw[i] / 2), float(p.cx[i] + p.cw[i] / 2)


def glide(p0, p1, t0, t1, n=30):
    return [{"t": round(t0 + (t1 - t0) * i / n, 3), "type": "move",
             "x": p0[0] + (p1[0] - p0[0]) * i / n, "y": p0[1] + (p1[1] - p0[1]) * i / n} for i in range(n + 1)]


def click(t, x, y):
    return {"t": t, "type": "click", "x": float(x), "y": float(y), "button": "left"}


# ------------------------------------------------------------------- no large bands
def test_a_whole_window_is_not_letterboxed():
    """A window that just opened (1.7 strips wide): the strip shows a part of it, full height."""
    p = crop(timeline(), track([(2.0, 3.5, "layout", WIN, None)]))
    assert p.cw.max() <= STRIP * 1.02
    x0, x1 = span(p, 3.4)
    assert WIN[0] - 8 <= x0 and x1 <= WIN[2] + 8          # the crop lies on the window


def test_output_wider_than_the_strip_is_shown_from_the_start_of_its_lines():
    out = (362.0, 172.0, 1462.0, 932.0)
    p = crop(timeline(windows=[window(rect=out, cls="foot")]), track([(2.0, 4.0, "output", out, None)]))
    assert p.cw.max() <= STRIP * 1.02
    x0, _ = span(p, 3.8)
    assert abs(x0 - out[0]) <= 0.06 * STRIP               # the left edge of the text, with its margin


def test_a_dialog_a_little_wider_than_the_strip_is_shown_whole():
    dialog = (600.0, 400.0, 1300.0, 800.0)                # 700 px: 1.1 strips
    p = crop(timeline(), track([(2.0, 5.0, "output", dialog, None)]))
    x0, x1 = span(p, 4.0)
    assert x0 <= dialog[0] and dialog[2] <= x1
    assert STRIP < p.cw.max() <= STRIP * LETTERBOX_MAX + 1


def test_what_a_click_changed_never_widens_the_crop():
    """The row a click lit up (700 px, then gone half a second later) used to widen the crop
    to its limit for every click of a sequence."""
    ev = glide((1200, 800), (577, 326), 1.0, 1.9) + [click(2.1, 577, 326)]
    aoi = track([(1.0, 2.1, "pointer", (467.0, 236.0, 687.0, 416.0), (577.0, 326.0)),
                 (2.1, 2.8, "pointer", (427.0, 236.0, 1128.0, 416.0), (577.0, 326.0)),
                 (2.8, 3.3, "pointer", (467.0, 236.0, 687.0, 416.0), (577.0, 326.0))])
    p = crop(timeline(ev), aoi)
    assert p.cw.max() <= STRIP * 1.02
    x0, x1 = span(p, 2.6)
    assert x0 <= 577 - 150 and 577 + 150 <= x1            # the click and what is around it


# --------------------------------------------------------------------- calm motion
def test_a_pointer_flying_across_the_screen_is_followed_calmly():
    ev = glide((1700, 300), (300, 300), 1.0, 2.6, 80)     # 1400 px in 1.6 s: 1.4 strips a second
    spans = [(1.0 + 0.1 * i, 1.1 + 0.1 * i, "pointer",
              (1700 - 87.5 * i - 110.0, 210.0, 1700 - 87.5 * i + 110.0, 390.0), None) for i in range(17)]
    p = crop(timeline(ev, start=(1700.0, 300.0)), track(spans))
    assert not p.cuts
    speed = np.abs(np.diff(p.cx)) * FPS / p.cw[1:]        # crop widths a second
    assert speed.max() <= POINTER_PAN * 1.05


def test_a_far_jump_is_a_cut_that_lands_settled():
    left, right = (100.0, 300.0, 500.0, 600.0), (1300.0, 300.0, 1700.0, 600.0)
    aoi = track([(1.0, 3.0, "output", left, None), (3.0, 6.0, "output", right, None)])
    p = crop(timeline(windows=[window(rect=(0.0, 0.0, 1920.0, 1200.0))]), aoi)
    assert (right[0] + right[2] - left[0] - left[2]) / 2 > CUT_HOP * STRIP
    cuts = [c / FPS for c in p.cuts]
    assert any(2.4 <= c <= 3.4 for c in cuts), cuts
    c = next(c for c in p.cuts if 2.4 <= c / FPS <= 3.4)
    after = p.cx[c + 1:c + 1 + FPS]                       # the second after the cut: no drift
    assert np.ptp(after) <= 0.03 * STRIP
    x0, x1 = span(p, 4.0)
    assert x0 <= right[0] and right[2] <= x1


# ----------------------------------------------------------------- workspace switch
def _switch(windows_after):
    ev = [{"t": 5.0, "type": "workspace", "name": "2", "special": False},
          {"t": 5.1, "type": "clients", "focus": windows_after[0]["address"] if windows_after else None,
           "windows": windows_after}]
    tl = timeline(ev, windows=[window(rect=(12.0, 38.0, 1908.0, 1188.0))], duration=9.0)
    return crop(tl, track([], 9.0, transitions=[(4.95, 5.15)]))


def test_after_a_workspace_switch_the_focused_window_fills_the_frame():
    p = _switch([window("0x2")])
    x0, x1 = span(p, 6.0)
    assert x1 - x0 <= STRIP * 1.02 and WIN[0] - 8 <= x0 and x1 <= WIN[2] + 8


def test_an_empty_workspace_is_still_shown_whole():
    p = _switch([])
    x0, x1 = span(p, 5.2)
    assert x1 - x0 >= 0.9 * W


# ------------------------------------------------- a dialog drawn dark on a dark page
def test_a_low_contrast_dialog_is_what_the_click_opened():
    """Only its text and border change, so it is not a solid changed region; and it opened
    420 px from the pointer, too far to count as "next to the click". It was not framed at
    all: the 9:16 crop stayed on the pointer and cut the dialog's right edge and its button."""
    dialog = (684.0, 616.0, 1188.0, 868.0)
    ev = glide((1200, 800), (577, 524), 1.0, 1.9) + [click(2.0, 577, 524)]
    ev += glide((577, 524), (1085, 843), 4.0, 4.4) + [click(4.6, 1085, 843)]
    tl = timeline(ev, duration=8.0)
    n = 80
    blobs = [[] for _ in range(n)]
    blobs[23] = [(*dialog, 0.0082)]                       # appears 0.3 s after the click
    blobs[48] = [(*dialog, 0.0082)]                       # ... and goes 0.2 s after the click on OK
    act = aoimod.Activity(t=np.round(np.arange(n) * 0.1, 3), frac=np.array([sum(b[4] for b in bl) for bl in blobs]),
                          blobs=blobs, w=W, h=H, reactions=[(2.0, None), (4.6, None)])
    tr = aoimod.build_aoi(tl, act)
    r, kind, _ = tr.at(2.6)
    assert kind == "output" and r is not None and r[0] <= dialog[0] + 1 and r[2] >= dialog[2] - 1, (kind, r)
    r, kind, _ = tr.at(4.5)                               # the pointer is inside it: still all of it
    assert r is not None and r[0] <= dialog[0] + 1 and r[2] >= dialog[2] - 1, (kind, r)
    # and the crop keeps the whole dialog, button included, until it is closed
    p = crop(tl, tr)
    for t in (2.9, 3.4, 3.9, 4.4):
        x0, x1 = span(p, t)
        assert x0 <= dialog[0] and dialog[2] <= x1, (t, x0, x1)

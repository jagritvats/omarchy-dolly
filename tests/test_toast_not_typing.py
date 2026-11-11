"""A notification card over the window being typed in is not what was typed.

11 Nov 2026, a take on a 2560x1440 monitor: a card left over from the take before faded out
of the top right corner 0.3 s after the last key. Omarchy's notification layer stays mapped,
so no layer event announced it; the fading card was read as fresh glyphs, the subject grew
from the typed line (420 px) to the full 2560 px, and the start of the line left the frame.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import aoi as aoimod  # noqa: E402
from dolly.events import Timeline  # noqa: E402

W, H = 2560, 1440
WIN = [12.0, 38.0, 2536.0, 1390.0]                       # x, y, w, h: a tiled terminal
AREA = [2161.0, 0.0, 2560.0, 1440.0]                     # the notification column of this monitor
CARD = (2155.0, 27.0, 2560.0, 133.0)                     # the card that fades out
LINE = (300.0, 69.0, 395.0, 91.0)                        # glyphs of the command being typed


def take(toasts, areas=(AREA,), other_area=None):
    win = {"address": "0x1", "class": "foot", "title": "t", "rect": WIN, "floating": False, "fullscreen": False,
           "xwayland": False, "workspace": "1"}
    ev = [{"t": 0.0, "type": "move", "x": 1280.0, "y": 720.0},
          {"t": 0.02, "type": "clients", "focus": "0x1", "windows": [win]}]
    ev += [{"t": round(1.2 + 0.05 * i, 3), "type": "key", "key": "a", "mods": []} for i in range(20)]   # 1.2 .. 2.15
    ev += [{"t": t, "type": "toasts", "count": c} for t, c in toasts]
    cap = {"cursor_baked": True, "scale": 1.0, "notification_areas": [list(a) for a in areas] + ([other_area] if other_area else [])}
    tl = Timeline.from_dict({"video": {"w": W, "h": H, "fps": 60, "duration": 8.0}, "capture": cap, "events": ev})
    n = 80
    blobs = [[] for _ in range(n)]
    for i in range(12, 22):
        blobs[i] = [(LINE[0] + 8 * (i - 12), LINE[1], LINE[2] + 8 * (i - 12), LINE[3], 0.0002)]
    blobs[26] = [(*CARD, 0.003)]                         # the card goes at 2.6 s, typing still "recent"
    blobs[27] = [(CARD[0] + 5, CARD[1] + 5, CARD[2], 75.0, 0.0008)]
    act = aoimod.Activity(t=np.round(np.arange(n) * 0.1, 3), frac=np.array([sum(b[4] for b in bl) for bl in blobs]),
                          blobs=blobs, w=W, h=H)
    return tl, aoimod.build_aoi(tl, act)


def widest(tr, t0=2.3, t1=3.4):
    return max((r[2] - r[0]) for r, k, t in zip(tr.rect, tr.kind, tr.t) if r is not None and t0 <= t <= t1 and k == "typing")


def test_a_card_fading_out_over_the_window_is_not_typed_text():
    tl, tr = take([(0.36, 1), (2.92, 0)])
    assert widest(tr) < 700                              # the typed line; it was 2548 px wide
    r, kind, foc = tr.at(2.7)
    assert kind == "typing" and r[2] < 800 and (foc is None or foc[0] < 800)


def test_without_a_card_count_the_corner_still_counts():
    """No shell count (another desktop, an old recording): nothing is known, nothing is hidden."""
    tl, tr = take([])
    assert widest(tr) > 2000


def test_a_change_there_while_no_card_is_up_is_real():
    tl, tr = take([(0.36, 0)])
    assert widest(tr) > 2000


def test_the_card_area_of_another_monitor_is_not_on_this_recording():
    tl, tr = take([(0.36, 1), (2.92, 0)], areas=(), other_area=[4081.0, 0.0, 4480.0, 1200.0])
    assert aoimod._toast_rects(tl, 2.6) == []
    assert widest(tr) > 2000                             # so nothing here is taken for a card


def test_card_boxes_follow_the_count():
    tl, _ = take([(0.36, 1), (2.92, 0), (5.0, 2), (6.0, 0)])
    assert aoimod._toast_rects(tl, 1.0) == [(2161.0, 0.0, 2560.0, 210.0)]
    assert aoimod._toast_rects(tl, 3.5) == [(2161.0, 0.0, 2560.0, 210.0)]      # still fading 0.6 s after the count fell
    assert aoimod._toast_rects(tl, 4.0) == []
    assert aoimod._toast_rects(tl, 4.5) == [(2161.0, 0.0, 2560.0, 360.0)]      # two cards about to slide in
    assert aoimod._toast_rects(tl, 7.5) == []

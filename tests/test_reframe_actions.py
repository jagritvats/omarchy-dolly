"""Pure portrait regressions from real fractional-scale typing/control captures.

No compositor, video decoder, input injection, or desktop access is needed.
The control rectangle is ground truth, not an assertion that the pointer fits.
"""
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import reframe  # noqa: E402
from dolly.aoi import AOITrack  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.zoom import Segment, ZoomConfig  # noqa: E402

W, H, FPS, ASPECT = 1920, 1080, 30, 950 / 1790
STRIP = H * ASPECT


def scene(events, duration=12, terminal=False):
    win = {"address": "0x1", "class": "foot" if terminal else "dummy-form",
           "rect": [0, 32.5, W, H - 32.5], "title": "dummy", "floating": False}
    return Timeline.from_dict({
        "video": {"w": W, "h": H, "fps": 60, "duration": duration},
        "capture": {"scale": 1.25},
        "events": [{"type": "clients", "t": 0, "focus": "0x1", "windows": [win]},
                   {"type": "move", "t": 0, "x": 1805, "y": 108}, *events],
    })


def control_scene():
    times = [i / 10 for i in range(120)]
    rects, kinds, foci, events, segs, controls = [None] * 120, [None] * 120, [None] * 120, [], [], []
    for t, x, y in [(2.0, 422.5, 326.25), (6.0, 422.5, 702.5), (10.0, 1497.5, 702.5)]:
        control = (x - 288, y - 52.375, x + 288, y + 52.375)
        reaction = (x - 302, y - 90, x + 302, y + 90)
        controls.append((t, control))
        events += [{"type": "move", "t": t - .4, "x": x, "y": y},
                   {"type": "click", "t": t, "x": x, "y": y}]
        segs.append(Segment(t - .75, t + 1.7, 2, reason="click"))
        for i, ti in enumerate(times):
            if t - .5 <= ti <= t + 1.7:
                rects[i] = reaction if t <= ti <= t + .6 else (x - 110, y - 90, x + 110, y + 90)
                kinds[i], foci[i] = "pointer", (x, y)
    return scene(events), AOITrack(times, rects, kinds, foci, [], 14), segs, controls


def typing_scene():
    times = [i / 10 for i in range(100)]
    rects, kinds, foci = [], [], []
    for t in times:
        # Initial window repaint looks wide, then activity narrows to the fresh
        # glyphs and grows again. A sliding max used to zoom in and back out.
        width = 960 if t < 2.5 else 180 + min(1, (t - 2.5) / 3.5) * 420
        on = 2 <= t <= 6.5
        rects.append((960, 651, 960 + width, 721) if on else None)
        kinds.append("typing" if on else None)
        foci.append((980 + min(width - 40, (t - 2) * 120), 685) if on else None)
    events = [{"type": "key", "key": "char", "t": 2 + i / 10} for i in range(41)]
    return scene(events, duration=10, terminal=True), AOITrack(times, rects, kinds, foci, [], 14), [
        Segment(1.25, 7.3, 2, reason="typing")]


def path(tl, aoi, segs, cfg=None, start=0, end=None):
    return reframe.reframe_path(tl, segs, ASPECT, FPS, start, end or tl.duration, cfg, aoi=aoi)


def view(p, t):
    i = min(len(p.cx) - 1, max(0, round((t - p.t0) * FPS)))
    return (p.cx[i] - p.cw[i] / 2, p.cy[i] - p.ch[i] / 2,
            p.cx[i] + p.cw[i] / 2, p.cy[i] + p.ch[i] / 2)


class TestReframeActions(unittest.TestCase):
    # stdlib unittest also runs under pytest, but needs no extra package on a
    # stock desktop. Only pure Timeline/AOI inputs are used: no desktop calls.
    def test_clicked_control_is_whole_at_click_and_through_hold_without_large_bands(self):
        tl, aoi, segs, controls = control_scene()
        for shots in [segs, [Segment(segs[0].start, segs[-1].end, 2, reason="click x3")]]:
            with self.subTest(segments=len(shots)):
                p = path(tl, aoi, shots)
                for t, rect in controls:
                    for i in range(52):
                        v = view(p, t + i / FPS)
                        self.assertTrue(v[0] <= rect[0] and v[1] <= rect[1] and v[2] >= rect[2] and v[3] >= rect[3],
                                        (t, i, v, rect))
                self.assertLessEqual(max(p.cw), STRIP * reframe.LETTERBOX_MAX + 1)

    def test_typing_shot_does_not_breathe_when_activity_loses_and_regains_glyphs(self):
        tl, aoi, segs = typing_scene()
        p = path(tl, aoi, segs)
        widths = p.cw[3 * FPS:6 * FPS]
        self.assertLess(max(widths) - min(widths), .005 * STRIP)
        # Same legibility cap as the crop planner, not a full-screen thumbnail.
        self.assertLessEqual(max(widths), aoi.line_h * .88 / (ZoomConfig().min_line_frac * 1.12) + 1)
        self.assertTrue(all(math.isfinite(x) for x in p.cx + p.cw))

    def test_trimmed_typing_uses_the_whole_shots_width_not_only_the_trim(self):
        tl, aoi, segs = typing_scene()
        full = path(tl, aoi, segs)
        short = path(tl, aoi, segs, start=3, end=5)
        for t in [3.5, 4, 4.5]:
            self.assertAlmostEqual(short.cw[round((t - 3) * FPS)], full.cw[round(t * FPS)], delta=.05)

    def test_manual_shots_keep_the_previous_path(self):
        for kind in ["pointer", "typing"]:
            with self.subTest(kind=kind):
                tl, aoi, segs = control_scene()[:3] if kind == "pointer" else typing_scene()
                segs = [Segment(s.start, s.end, s.zoom, [1100, 600], False, "manual") for s in segs]
                actual = path(tl, aoi, segs)
                with patch.object(reframe, "_action_framing", lambda *args: [0] * len(args[4])):
                    self.assertEqual(actual, path(tl, aoi, segs))

    def test_disabled_setting_does_not_add_action_framing(self):
        for setting in ["enabled", "zoom_clicks", "follow_typing", "use_aoi"]:
            with self.subTest(setting=setting):
                tl, aoi, segs = typing_scene() if setting == "follow_typing" else control_scene()[:3]
                cfg = ZoomConfig(**{setting: False})
                actual = path(tl, aoi, segs, cfg)
                with patch.object(reframe, "_action_framing", lambda *args: [0] * len(args[4])):
                    self.assertEqual(actual, path(tl, aoi, segs, cfg))

    def test_a_later_auto_click_does_not_widen_the_manual_shot(self):
        tl, aoi, segs, _ = control_scene()
        shots = [Segment(0, 5, 2, [1000, 540], False, "manual"), *segs[1:]]
        actual = path(tl, aoi, shots)
        with patch.object(reframe, "_action_framing", lambda *args: [0] * len(args[4])):
            previous = path(tl, aoi, shots)
        # Away from the handover's edge smoothing, the explicit width is exact.
        for t in [3.5, 4, 4.25]:
            self.assertAlmostEqual(actual.cw[round(t * FPS)], previous.cw[round(t * FPS)], delta=.01)

    def test_pointer_motion_without_an_automatic_click_does_not_widen_the_strip(self):
        tl, aoi, _, _ = control_scene()
        p = path(tl, aoi, [])
        self.assertLessEqual(max(p.cw), STRIP * 1.02)

    def test_click_on_a_whole_wide_page_does_not_turn_it_into_a_thumbnail(self):
        tl, aoi, segs, _ = control_scene()
        aoi.rect = [(0, 150, W, 1000) if r else None for r in aoi.rect]
        p = path(tl, aoi, segs)
        self.assertLessEqual(max(p.cw), STRIP * reframe.LETTERBOX_MAX + 1)


if __name__ == "__main__":
    unittest.main()

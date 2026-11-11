"""While the camera stands still the compositor moves the background and places the
rounded-corner masks once, and reuses them for the following frames. A frame composed with
that reused work must equal one composed from scratch, and a moved camera must not be
served the old work."""
import numpy as np
import pytest

from dolly import project as projmod
from dolly.events import Timeline
from dolly.render import Compositor
from dolly.zoom import Segment, ZoomConfig

W, H = 640, 400


def timeline():
    return Timeline.from_dict({"video": {"w": W, "h": H, "fps": 30, "duration": 8.0},
                               "capture": {"cursor_baked": True},
                               "events": [{"t": 0.0, "type": "move", "x": 320, "y": 200}]})


def compositor(aspect, zoomed):
    style = projmod.deep_merge(projmod.DEFAULT_STYLE, {"output": {"width": 640, "height": 360, "fps": 30},
                                                       "keys": {"enabled": False}})
    segs = [Segment(start=1.0, end=7.0, zoom=1.8, focus=[200, 150], follow=False, source="manual")] if zoomed else []
    return Compositor(timeline(), style, segs, ZoomConfig(), 0.0, 8.0, aspect=aspect)


@pytest.mark.parametrize("aspect", [None, "9:16"])
@pytest.mark.parametrize("zoomed", [False, True])
def test_reused_camera_work_equals_a_fresh_composition(aspect, zoomed):
    rng = np.random.default_rng(5)
    a, b, c = (rng.integers(0, 255, (H, W, 3), dtype=np.uint8) for _ in range(3))
    comp = compositor(aspect, zoomed)
    comp.compose_at(4.0, a)                               # fills the per-camera work
    again = comp.compose_at(4.0, b).copy()                # same camera, another source frame: reuses it
    assert np.array_equal(again, compositor(aspect, zoomed).compose_at(4.0, b))
    moved = comp.compose_at(1.15, c).copy()               # mid-glide: another camera
    assert np.array_equal(moved, compositor(aspect, zoomed).compose_at(1.15, c))
    back = comp.compose_at(4.0, b)                        # and back again
    assert np.array_equal(back, again)


def test_the_zoomed_camera_really_reuses_its_work():
    comp = compositor(None, True)
    frame = np.full((H, W, 3), 90, np.uint8)
    comp.compose_at(4.0, frame)
    bg, bands = comp._bg_cache, comp._band_cache
    assert bg[0] is not None and bands[0] is not None
    comp.compose_at(4.0, frame.copy())                    # a new frame object, the same camera
    assert comp._bg_cache[1] is bg[1] and comp._band_cache[1] is bands[1]
    comp.compose_at(1.15, frame)
    assert comp._bg_cache[1] is not bg[1]

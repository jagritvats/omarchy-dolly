"""While the camera stands still the compositor moves the background once and reuses it for
the following frames. A frame composed with that reused work must equal one composed from
scratch, and a moved camera must not be served the old work."""
import random

import pytest

from dolly import gfx
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


def noise(seed):
    """A source frame of random pixels."""
    return gfx.frame_from(bytearray(random.Random(seed).randbytes(4 * W * H)), W, H)


@pytest.mark.parametrize("aspect", [None, "9:16"])
@pytest.mark.parametrize("zoomed", [False, True])
def test_reused_camera_work_equals_a_fresh_composition(aspect, zoomed):
    a, b, c = (noise(s) for s in (5, 6, 7))
    comp = compositor(aspect, zoomed)
    comp.compose_at(4.0, a)                               # sees the camera
    comp.compose_at(4.0, gfx.copy(a))                     # the same camera again: its work is kept
    again = gfx.copy(comp.compose_at(4.0, b))             # another source frame: reuses it
    assert gfx.same(again, compositor(aspect, zoomed).compose_at(4.0, b))
    moved = gfx.copy(comp.compose_at(1.15, c))            # mid-glide: another camera
    assert gfx.same(moved, compositor(aspect, zoomed).compose_at(1.15, c))
    back = comp.compose_at(4.0, b)                        # and back again
    assert gfx.same(back, again)


def test_the_zoomed_camera_really_reuses_its_work():
    comp = compositor(None, True)
    frame = gfx.new(W, H)
    gfx.fill_rect(frame, 0, 0, W, H, gfx.rgb((90, 90, 90)))
    comp.compose_at(4.0, frame)
    assert comp._bg_cache[0] is not None and comp._bg_cache[1] is None      # seen once: nothing kept yet
    comp.compose_at(4.0, gfx.copy(frame))                 # a new frame object, the same camera
    bg = comp._bg_cache
    assert bg[1] is not None
    comp.compose_at(4.0, gfx.copy(frame))
    assert comp._bg_cache[1] is bg[1]                     # the moved background is the same picture
    comp.compose_at(1.15, frame)
    assert comp._bg_cache[1] is not bg[1]


def test_an_unchanged_frame_is_not_composed_again():
    comp = compositor(None, True)
    frame = noise(1)
    first = comp.compose_at(5.0, frame)
    assert comp.compose_at(5.0 + 1 / 30, frame) is first and comp.reused == 1     # same source, still camera
    assert comp.compose_at(5.0 + 2 / 30, noise(2)) is not first                   # a new source frame

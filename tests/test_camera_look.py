"""0.8.0 camera: a zoom-in never leaves its subject behind, and camera motion blur."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from dolly import gfx  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.zoom import CameraGeometry, Segment, ZoomConfig, camera_path  # noqa: E402
from test_pipeline import compositor  # noqa: E402

W, H = 1920, 1200


def _path(keep: bool):
    tl = Timeline.from_dict({"video": {"w": W, "h": H, "fps": 30, "duration": 8.0},
                             "capture": {}, "events": [{"t": 0.0, "type": "move", "x": 300, "y": 900}]})
    segs = [Segment(1.0, 3.0, 1.3, focus=[300, 900], follow=False, source="manual"),
            Segment(3.0, 7.0, 2.5, focus=[1700, 150], follow=False, source="manual")]
    cfg = ZoomConfig(zoom_keeps_subject=keep, glide=False)
    return camera_path(tl, segs, CameraGeometry(W, H, 60, 60), 30, 0.0, 8.0, cfg)


def _subject_out_of_view(p, x=1700, y=150, frac=1.0):
    """Frames (from 3 s on) where the point is outside the view while zoomed past 1.6x."""
    out = 0
    for i in range(int(3.0 * 30), len(p.z)):
        z, cx, cy = p.z[i], p.cx[i], p.cy[i]
        hw, hh = (W + 120) / (2 * z) * frac, (H + 120) / (2 * z) * frac
        if z > 1.6 and (abs(x - cx) > hw or abs(y - cy) > hh):
            out += 1
    return out


def test_a_zoom_in_keeps_the_place_it_goes_to_in_view():
    assert _subject_out_of_view(_path(False)) > 0          # the race this prevents is real
    p = _path(True)
    assert _subject_out_of_view(p) == 0
    assert max(p.z) > 2.4                                  # and it still gets there
    assert p.z[int(6.5 * 30)] > 2.4


def _stripes():
    img = gfx.new(640, 400)
    gfx.fill_rect(img, 0, 0, 640, 400, gfx.rgb((30, 30, 40)))
    for x in range(0, 640, 16):
        gfx.fill_rect(img, x, 0, 8, 400, gfx.rgb((230, 230, 240)))
    return img


def _planned(cx):
    n = len(cx)
    return {"fps": 30.0, "t0": 0.0, "z": [1.6] * n, "cx": list(cx), "cy": [200.0] * n,
            "cur_x": [-100.0] * n, "cur_y": [-100.0] * n, "cuts": []}


def test_camera_motion_blur_softens_a_quick_pan_only():
    n = 6 * 30
    still = _planned([320.0] * n)
    quick = _planned([320.0 + 8.0 * (i - 90) for i in range(n)])   # 8 video px a frame, centred at 3 s
    frame = _stripes()
    on = compositor(MOVE_AWAY, {"camera": {"motion_blur": True}}, planned=quick)
    off = compositor(MOVE_AWAY, {"camera": {"motion_blur": False}}, planned=quick)
    a, b = on.compose_at(3.0, frame), off.compose_at(3.0, frame)
    assert not gfx.same(a, b) and on.blurred == 1
    # softer: the stripes' edges are smeared into in-between greys
    assert _midtones(a) > 1.5 * _midtones(b) + 10
    s_on = compositor(MOVE_AWAY, {"camera": {"motion_blur": True}}, planned=still)
    s_off = compositor(MOVE_AWAY, {"camera": {"motion_blur": False}}, planned=still)
    assert gfx.same(s_on.compose_at(3.0, frame), s_off.compose_at(3.0, frame)) and s_on.blurred == 0


MOVE_AWAY = [{"t": 0.0, "type": "move", "x": -50, "y": -50}]


def _midtones(img):
    row = gfx.plane(gfx.crop(img, 100, 180, 400, 1), "g")
    return sum(1 for v in row if 70 < v < 190)

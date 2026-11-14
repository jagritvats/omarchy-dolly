"""Activity analysis streams the proxy decode (flat memory for long recordings): ffmpeg marks
what changed, and that gives the same result as pictures compared here."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import aoi, grid  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _clip(path: Path):
    # 6 s at 10 fps: a box appears at 2 s (a "dialog" a click at 1.8 s opens), text-like changes later
    vf = ("drawbox=x=400:y=200:w=300:h=200:color=white:t=fill:enable='gte(t,2)',"
          "drawtext=text='%{n}':x=40:y=40:fontsize=24:fontcolor=white")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x202020:s=1280x800:r=10",
                    "-t", "6", "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", str(path)],
                   check=True)


@needs_ffmpeg
def test_streamed_matches_all_frames(tmp_path):
    raw = tmp_path / "raw.mkv"
    try:
        _clip(raw)
    except subprocess.CalledProcessError:
        pytest.skip("ffmpeg without drawtext")
    cfg = aoi.AOIConfig()
    streamed = aoi.compute_activity(raw, 1280, 800, cfg, clicks=[1.8, 5.95])
    stream = list(aoi._stream_proxy(raw, cfg.fps, cfg.proxy_w, 300, cfg.diff_thresh))
    frames = [grey for grey, _ in stream]
    assert all(len(grey) == len(changed) == 480 * 300 for grey, changed in stream)
    # what ffmpeg marked as changed is what comparing each picture with the one before gives
    assert stream[0][1] == bytes(480 * 300)
    assert [changed for _, changed in stream[1:]] == [grid.differ(b, a, cfg.diff_thresh) for a, b in zip(frames, frames[1:])]
    whole = aoi.compute_activity(raw, 1280, 800, cfg, frames=frames, clicks=[1.8, 5.95])
    assert len(streamed.t) == len(whole.t) == len(frames) >= 59
    assert streamed.t == [i / 10 for i in range(len(frames))]
    assert streamed.frac == whole.frac and streamed.blobs == whole.blobs
    # the box: 300 x 200 of 1280 x 800, as one blob of the sample it appears in
    assert streamed.frac[0] == 0.0 and streamed.blobs[0] == []
    (box,) = [b for b in streamed.blobs[20] if b[4] > 0.01]
    assert box[4] == pytest.approx(300 * 200 / (1280 * 800), rel=0.05)
    assert [round(v) for v in box[:4]] == pytest.approx([400 - 4 * 8 / 3, 200, 700 + 4 * 8 / 3, 400], abs=6)    # 4 proxy pixels wider
    # the dialog the click opened (whole.reactions has no colour pass: compare geometry presence)
    assert streamed.reactions[0][0] == 1.8
    assert streamed.reactions[0][1] == pytest.approx((400, 200, 700, 400), abs=6)
    assert whole.reactions[0][1] == pytest.approx((400, 200, 700, 400), abs=6)
    assert streamed.reactions[1] == (5.95, None)     # a click at the very end is clipped, not an error; a digit is no dialog


def test_sparse_frames_keep_only_click_frames():
    want = aoi._click_frame_indices([1.8, 30.0], 10.0)
    assert want == {17, 24, 299, 306}


def test_color_change_timeout_is_not_fatal(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 30)
    monkeypatch.setattr(subprocess, "run", slow)
    assert aoi._color_changed(Path("/nonexistent.mkv"), 1.0, 1.7, 480, 300, 8.4) is None


# ------------------------------------------------------------------ pictures made here: no ffmpeg
PW, PH = 480, 300                       # the proxy of a 1920 x 1200 recording


def _page():
    """A page with a spread of greys (40 .. 159)."""
    return bytearray(40 + (x * 7 + y * 3) % 120 for y in range(PH) for x in range(PW))


def _fill(pic, x0, y0, x1, y1, value):
    for y in range(y0, y1):
        pic[y * PW + x0:y * PW + x1] = bytes([value]) * (x1 - x0)
    return pic


def test_wrong_sized_pictures_are_refused():
    with pytest.raises(ValueError):
        aoi.compute_activity(None, 1920, 1200, frames=[bytes(100)])
    assert aoi.compute_activity(None, 1920, 1200, frames=[]).t == []


def test_words_merge_into_lines_and_lines_stay_apart():
    page = _page()
    typed = _page()
    for x in range(100, 200, 6):                         # letters 3 wide, 3 apart, on two lines 6 rows apart
        _fill(typed, x, 50, x + 3, 58, 255)
        _fill(typed, x, 64, x + 3, 72, 255)
    act = aoi.compute_activity(None, 1920, 1200, frames=[bytes(page), bytes(typed), bytes(typed)])
    assert act.t == [0.0, 0.1, 0.2] and act.frac[0] == act.frac[2] == 0.0 and act.blobs[2] == []
    letters = 17 * 3 * 8
    assert act.frac[1] == 2 * letters / (PW * PH)
    # each line is one blob, 4 proxy pixels wider on both sides, counting only the pixels that changed
    assert act.blobs[1] == [((100 - 4) * 4.0, 50 * 4.0, (199 + 4) * 4.0, 58 * 4.0, letters / (PW * PH)),
                            ((100 - 4) * 4.0, 64 * 4.0, (199 + 4) * 4.0, 72 * 4.0, letters / (PW * PH))]


def test_a_dimmed_page_is_not_activity_the_dialog_on_it_is():
    page = _page()
    modal = _fill(bytearray(v // 2 for v in page), 150, 90, 330, 210, 220)       # everything at half, a dialog on top
    frames = [bytes(page)] * 3 + [bytes(modal)] * 9
    act = aoi.compute_activity(None, 1920, 1200, frames=frames, clicks=[0.25, 0.3])
    dialog = 180 * 120 / (PW * PH)
    assert act.frac[3] == dialog                         # not the whole screen: the page only got darker
    assert act.blobs[3] == [(146 * 4.0, 90 * 4.0, 334 * 4.0, 210 * 4.0, dialog)]
    assert aoi.transitions(act, aoi.AOIConfig()) == []
    assert act.reactions == [(0.25, (600.0, 360.0, 1320.0, 840.0)), (0.3, (600.0, 360.0, 1320.0, 840.0))]
    # without the dialog the same dimming is nothing at all; a real change of the whole screen still is one
    dim = bytes(v // 2 for v in page)
    act = aoi.compute_activity(None, 1920, 1200, frames=[bytes(page), dim], clicks=[0.0])
    assert act.frac == [0.0, 0.0] and act.reactions == [(0.0, None)]
    other = bytes(255 - v for v in page)
    act = aoi.compute_activity(None, 1920, 1200, frames=[bytes(page), other])
    assert act.frac[1] > 0.8 and len(aoi.transitions(act, aoi.AOIConfig())) == 1


def test_a_click_reaction_is_the_solid_panel_with_its_rows():
    page = _page()
    after = _page()
    _fill(after, 100, 60, 260, 150, 230)                 # a panel ...
    _fill(after, 100, 156, 260, 172, 230)                # ... and a row of it, split off by a strip that did not change
    _fill(after, 400, 250, 404, 254, 230)                # something small far away
    for x in range(300, 440, 8):                         # thin strokes of text beside it: changed, but no panel
        _fill(after, x, 100, x + 1, 108, 230)
    frames = {0: bytes(page), 7: bytes(after)}
    (t, r), = aoi.click_reactions(frames, 8, PW, PH, 10.0, 1920, 1200, [0.1])
    assert t == 0.1 and r == (400.0, 240.0, 1040.0, 688.0)
    assert aoi.click_reactions(frames, 8, PW, PH, 10.0, 1920, 1200, [0.1, 0.1]) == [(0.1, r), (0.1, r)]
    assert aoi.click_reactions({}, 0, PW, PH, 10.0, 1920, 1200, [0.1]) == []
    assert aoi.click_reactions({0: bytes(page)}, 1, PW, PH, 10.0, 1920, 1200, [0.1]) == [(0.1, None)]

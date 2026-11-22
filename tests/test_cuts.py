"""0.9.0 cuts: a stretch left out of the export (a caption line cut out, a pause cut)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dolly import captions  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.ff import ffmpeg_bin, probe  # noqa: E402
from dolly.timemap import CUT, TimeMap, clean_cuts, detect_pauses  # noqa: E402


# ------------------------------------------------------------------ the time map
def test_a_cut_takes_no_time_and_is_skipped():
    tm = TimeMap(0, 20, [], [{"start": 5, "end": 8}])
    assert tm.duration == pytest.approx(17)
    assert tm.to_source(4.9) == pytest.approx(4.9)
    assert tm.to_source(5.0) == pytest.approx(8.0)            # the first frame after the cut
    assert tm.to_source(6.0) == pytest.approx(9.0)
    assert tm.to_output(6.5) == pytest.approx(5.0)            # inside the cut: where it was cut
    assert tm.in_cut(6.5) and not tm.in_cut(9) and tm.speed_at(6.5) == CUT
    assert tm.pieces() == [(0, 5, 1.0), (8, 20, 1.0)]         # the sound skips it too
    assert tm.cuts() == [(5, 8)]


def test_a_speed_up_across_a_cut_applies_to_what_is_left():
    tm = TimeMap(0, 20, [{"start": 2, "end": 10, "speed": 2}], [{"start": 4, "end": 6}])
    assert tm.pieces() == [(0, 2, 1.0), (2, 4, 2.0), (6, 10, 2.0), (10, 20, 1.0)]
    assert tm.duration == pytest.approx(2 + 1 + 2 + 10)


def test_cuts_at_the_ends_overlapping_and_outside_the_trim():
    tm = TimeMap(1, 11, [], [{"start": 0, "end": 2}, {"start": 9, "end": 12}, {"start": 4, "end": 5}, {"start": 4.5, "end": 6}])
    assert tm.cuts() == [(1, 2), (4, 6), (9, 11)]
    assert tm.duration == pytest.approx(10 - 1 - 2 - 2)
    assert tm.to_source(0) == pytest.approx(2)
    assert tm.to_source(tm.duration) == pytest.approx(11)    # a cut up to the end: no NaN


def test_no_cuts_is_the_old_time_map():
    a, b = TimeMap(0, 10, [{"start": 2, "end": 4, "speed": 4}]), TimeMap(0, 10, [{"start": 2, "end": 4, "speed": 4}], [])
    assert (a.src, a.speed, a.out) == (b.src, b.speed, b.out)


def test_a_caption_inside_a_cut_is_left_out_of_the_srt():
    tm = TimeMap(0, 20, [], [{"start": 5, "end": 8}])
    cues = [{"start": 1, "end": 3, "text": "kept"}, {"start": 5.5, "end": 7.5, "text": "cut"}, {"start": 9, "end": 10, "text": "after"}]
    out = captions.remap(cues, tm)
    assert [c["text"] for c in out] == ["kept", "after"] and out[1]["start"] == pytest.approx(6.0)


# ------------------------------------------------------------------ cleaning (data from a page or a folder)
def test_clean_cuts():
    got = clean_cuts([{"start": 3, "end": 5, "id": "x2", "source": "manual", "reason": "caption"},
                      {"start": 4, "end": 6, "source": "auto", "reason": "pause"},          # overlaps: merged
                      {"start": "a", "end": 2}, {"start": 9, "end": 9.01}, {"start": -1, "end": 2},
                      {"start": 7, "end": 8, "id": "x2", "reason": "<b>"}, "nope"], duration=7.5)
    assert [(c["start"], c["end"]) for c in got] == [(3.0, 6.0), (7.0, 7.5)]
    assert got[0]["source"] == "manual" and got[0]["reason"] == "caption" and got[1]["reason"] == "manual"
    assert len({c["id"] for c in got}) == 2


# ------------------------------------------------------------------ pauses
def _tl(events, dur=20.0):
    return Timeline.from_dict({"video": {"w": 640, "h": 400, "fps": 30, "duration": dur}, "capture": {}, "events": events})


def test_pauses_are_where_nothing_happens_and_nothing_is_said():
    ev = [{"t": 1.0, "type": "click", "x": 10, "y": 10}, {"t": 9.0, "type": "click", "x": 20, "y": 20}]
    act = ([i * 0.2 for i in range(100)], [0] * 100)               # a still screen
    cuts = detect_pauses(_tl(ev), act, speech=None)
    assert [(c["start"], c["end"]) for c in cuts] == [(1.35, 8.65), (9.35, 19.65)]
    talk = detect_pauses(_tl(ev), act, speech=[(5.0, 6.0)])           # something said in the first one
    assert [(c["start"], c["end"]) for c in talk] == [(1.35, 4.65), (6.35, 8.65), (9.35, 19.65)]
    short = detect_pauses(_tl(ev), act, speech=[(3.0, 5.0)])          # what is left before it is too short
    assert [(c["start"], c["end"]) for c in short][0] == (5.35, 8.65)
    busy = ([i * 0.2 for i in range(100)], [500] * 100)              # the screen changes all the time
    assert detect_pauses(_tl(ev), busy, speech=None) == []
    assert all(c["source"] == "auto" and c["reason"] == "pause" for c in cuts)


# ------------------------------------------------------------------ editor + export
@pytest.fixture
def take(tmp_path):
    d = tmp_path / "take"
    d.mkdir()
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x400:rate=30:duration=10",
                    "-f", "lavfi", "-i", "sine=f=330:duration=10", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(d / "raw.mp4")], check=True)
    doc = {"video": {"w": 640, "h": 400, "fps": 30, "duration": 10.0},
           "capture": {"backend": "gsr", "mic": True, "click_source": "lua"},
           "events": [{"t": 0.0, "type": "move", "x": 100, "y": 100}]}
    (d / "events.json").write_text(json.dumps(doc))
    return d


def test_the_editor_saves_cuts_cleaned(take):
    from dolly.editor.server import Editor
    ed = Editor(take)
    ed.save({"cuts": [{"start": 2, "end": 4, "reason": "caption"}, {"start": 3, "end": 1}]})
    data = json.loads((take / "project.json").read_text())
    assert [(c["start"], c["end"], c["reason"]) for c in data["cuts"]] == [(2.0, 4.0, "caption")]
    assert ed.project_payload()["cuts"] == data["cuts"]


def test_an_export_leaves_the_cut_out(take):
    from dolly.render import export
    (take / "project.json").write_text(json.dumps({"segments": [], "speed": [], "cuts": [{"start": 3, "end": 6}],
                                                   "style": {"title": {"enabled": False}}}))
    out = export(take, take / "exports" / "cut.mp4", "mp4", "fast",
                 style_over={"output": {"width": 640, "height": 360, "fps": 30}})
    p = probe(out)
    assert float(p["format"]["duration"]) == pytest.approx(7.0, abs=0.15)
    assert any(s["codec_type"] == "audio" for s in p["streams"])
    a = next(s for s in p["streams"] if s["codec_type"] == "audio")
    assert float(a.get("duration") or p["format"]["duration"]) == pytest.approx(7.0, abs=0.2)


def test_everything_cut_says_so(take):
    from dolly import project as projmod
    from dolly.render import plan_timing
    (take / "project.json").write_text(json.dumps({"cuts": [{"start": 0, "end": 10}]}))
    with pytest.raises(projmod.EmptyTrim, match="cut"):
        plan_timing(take)

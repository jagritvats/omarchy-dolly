"""The analysis caches in a recording folder are data, never code.

A recording folder gets passed around (a shared drive, an archive from someone else). The
frame-difference cache used to be a pickle, and loading a pickle runs what the file says;
the cache of the speed-up detection was a NumPy file, which can hold a pickle too.
"""
import json
import os
import pickle
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import aoi, timemap  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.project import Project  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
W, H, FPS, DUR = 640, 400, 30, 4.0


@pytest.fixture(scope="module")
def recording(tmp_path_factory):
    """A small recording folder: a moving test picture, the pointer on its way to one click."""
    d = tmp_path_factory.mktemp("recording")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={FPS}:duration={DUR}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "raw.mp4")], check=True)
    events = [{"t": round(0.1 + 0.025 * i, 3), "type": "move", "x": 100.0 + 6 * i, "y": 100.0 + 2 * i} for i in range(40)]
    events.append({"t": 1.2, "type": "click", "x": 300.0, "y": 180.0, "button": "left"})
    (d / "events.json").write_text(json.dumps({"video": {"w": W, "h": H, "fps": FPS, "duration": DUR},
                                               "capture": {"scale": 1.0}, "events": events}))
    return d


@pytest.fixture()
def take(recording, tmp_path):
    d = tmp_path / "take"
    shutil.copytree(recording, d)
    return d, Timeline.load(Project(d).events)


def same(a: aoi.Activity, b: aoi.Activity):
    assert a.t == b.t and a.frac == b.frac and (a.w, a.h) == (b.w, b.h)
    assert [[tuple(float(v) for v in x) for x in row] for row in a.blobs] == b.blobs
    assert [(float(t), None if r is None else tuple(float(v) for v in r)) for t, r in a.reactions] == b.reactions


def test_the_cache_is_json_and_gives_back_the_same_analysis(take, monkeypatch):
    d, tl = take
    fresh = aoi.cached_activity(d, tl)
    doc = json.loads((d / aoi.ACTIVITY_CACHE).read_text())
    assert set(doc) == {"w", "h", "t", "frac", "blobs", "reactions"} and len(doc["t"]) == len(fresh.t) > 10
    assert any(doc["blobs"]) and len(doc["reactions"]) == len(tl.clicks) == 1
    assert isinstance(fresh.t, list) and isinstance(fresh.frac, list) and (doc["w"], doc["h"]) == (W, H)
    monkeypatch.setattr(aoi, "compute_activity", lambda *a, **k: pytest.fail("the cache was not used"))
    cached = aoi.cached_activity(d, tl)
    same(fresh, cached)
    plan = lambda act: [(r, k, f) for r, k, f in zip(*(lambda tr: (tr.rect, tr.kind, tr.focus))(aoi.build_aoi(tl, act)))]
    assert plan(fresh) == plan(cached)                     # and the zoom plan built on it is the same


def test_a_cache_written_by_0_7_3_still_loads(take, monkeypatch):
    """The file keeps its name and its shape: what the NumPy version wrote is read as it is."""
    d, tl = take
    old = {"w": 640, "h": 400, "t": [0.0, 0.1, 0.2], "frac": [0.0, 0.0125, 0.0],
           "blobs": [[], [[10.666666666666666, 21.333333333333332, 96.0, 64.0, 0.0125]], []],
           "reactions": [[1.2, None], [2.0, [8.0, 8.0, 320.0, 240.0]]]}
    (d / aoi.ACTIVITY_CACHE).write_text(json.dumps(old, separators=(",", ":")))
    monkeypatch.setattr(aoi, "compute_activity", lambda *a, **k: pytest.fail("the cache was not used"))
    act = aoi.cached_activity(d, tl)
    assert act.t == [0.0, 0.1, 0.2] and act.frac == [0.0, 0.0125, 0.0] and (act.w, act.h) == (640, 400)
    assert act.blobs == [[], [(10.666666666666666, 21.333333333333332, 96.0, 64.0, 0.0125)], []]
    assert act.reactions == [(1.2, None), (2.0, (8.0, 8.0, 320.0, 240.0))]
    assert aoi.activity_to_doc(act) == old                 # and it is written back the same


class Boom:
    def __init__(self, marker):
        self.marker = marker

    def __reduce__(self):
        return (os.system, (f"touch '{self.marker}'",))


def test_a_pickle_in_the_folder_is_never_loaded(take):
    d, tl = take
    marker = d / "ran"
    payload = pickle.dumps(Boom(marker))
    pickle.loads(payload)                                  # what the old code did with such a file
    assert marker.exists()
    marker.unlink()
    (d / "aoi-activity-v8.pkl").write_bytes(payload)       # where the old version kept its cache
    (d / aoi.ACTIVITY_CACHE).write_bytes(payload)          # and under the new name
    act = aoi.cached_activity(d, tl)
    assert not marker.exists() and len(act.t) > 10
    assert not (d / "aoi-activity-v8.pkl").exists()        # deleted unread
    json.loads((d / aoi.ACTIVITY_CACHE).read_text())       # replaced by a real cache


@pytest.mark.parametrize("doc", [
    "not json", "[]", "{}", '{"w":1,"h":1,"t":[0],"frac":[0],"blobs":[],"reactions":[]}',
    '{"w":1,"h":1,"t":[0],"frac":[0],"blobs":[[[1,2,3]]],"reactions":[]}',
    '{"w":1,"h":1,"t":[0],"frac":["x"],"blobs":[[]],"reactions":[]}',
    '{"w":1,"h":1,"t":[0],"frac":[0],"blobs":[[]],"reactions":[[0,[1,2]]]}',
    '{"w":{"__class__":"os.system"},"h":1,"t":[0],"frac":[0],"blobs":[[]],"reactions":[]}',
])
def test_a_cache_of_the_wrong_shape_is_worked_out_again(take, doc):
    d, tl = take
    (d / aoi.ACTIVITY_CACHE).write_text(doc)
    act = aoi.cached_activity(d, tl)
    assert len(act.t) > 10 and len(json.loads((d / aoi.ACTIVITY_CACHE).read_text())["t"]) == len(act.t)


def test_the_speed_up_cache_holds_numbers_only(take, monkeypatch):
    d, _ = take
    marker = d / "ran2"
    (d / "activity-v2.npz").write_bytes(pickle.dumps(Boom(marker)))     # where 0.7.3 kept it: a NumPy file can carry a pickle
    t, a = timemap.cached_activity(d)
    assert len(t) == len(a) > 10 and t[:3] == [0.0, 0.2, 0.4] and a[0] == 0.0 and max(a) > 100
    assert not marker.exists() and not (d / "activity-v2.npz").exists()            # deleted unread
    doc = json.loads((d / timemap.ACTIVITY_CACHE).read_text())
    assert set(doc) == {"t", "a"} and doc["t"] == t and doc["a"] == a
    for bad in ("not json", "[]", '{"t":[0,1],"a":[0]}', '{"t":[0],"a":["x"]}', '{"t":[{"__class__":"os.system"}],"a":[1]}',
                '{"t":5,"a":5}'):
        (d / timemap.ACTIVITY_CACHE).write_text(bad)
        assert timemap.cached_activity(d) == (t, a)                                # worked out again, the same
        assert json.loads((d / timemap.ACTIVITY_CACHE).read_text()) == doc
    monkeypatch.setattr(timemap, "visual_activity", lambda *a, **k: pytest.fail("the cache was not used"))
    assert timemap.cached_activity(d) == (t, a)

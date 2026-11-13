"""The analysis caches in a recording folder are data, never code.

A recording folder gets passed around (a shared drive, an archive from someone else). The
frame-difference cache used to be a pickle, and loading a pickle runs what the file says.
"""
import json
import os
import pickle
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_pipeline import proj  # noqa: E402,F401
from dolly import aoi, timemap  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.project import Project  # noqa: E402


@pytest.fixture()
def take(proj, tmp_path):
    d = tmp_path / "take"
    shutil.copytree(proj, d)
    for f in d.glob("aoi-activity*"):
        f.unlink()
    return d, Timeline.load(Project(d).events)


def same(a: aoi.Activity, b: aoi.Activity):
    assert np.array_equal(a.t, b.t) and np.array_equal(a.frac, b.frac) and (a.w, a.h) == (b.w, b.h)
    assert [[tuple(float(v) for v in x) for x in row] for row in a.blobs] == b.blobs
    assert [(float(t), None if r is None else tuple(float(v) for v in r)) for t, r in a.reactions] == b.reactions


def test_the_cache_is_json_and_gives_back_the_same_analysis(take, monkeypatch):
    d, tl = take
    fresh = aoi.cached_activity(d, tl)
    doc = json.loads((d / aoi.ACTIVITY_CACHE).read_text())
    assert set(doc) == {"w", "h", "t", "frac", "blobs", "reactions"} and len(doc["t"]) == len(fresh.t) > 10
    assert any(doc["blobs"]) and len(doc["reactions"]) == len(tl.clicks) == 1
    monkeypatch.setattr(aoi, "compute_activity", lambda *a, **k: pytest.fail("the cache was not used"))
    cached = aoi.cached_activity(d, tl)
    same(fresh, cached)
    plan = lambda act: [(r, k, f) for r, k, f in zip(*(lambda tr: (tr.rect, tr.kind, tr.focus))(aoi.build_aoi(tl, act)))]
    assert plan(fresh) == plan(cached)                     # and the zoom plan built on it is the same


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


def test_the_speed_up_cache_holds_numbers_only(take):
    d, _ = take
    t, a = timemap.cached_activity(d)
    assert len(t) == len(a) > 10
    np.savez(d / "activity-v2.npz", t=np.array([Boom(d / "ran2")], dtype=object), a=np.array([1.0]))
    t2, a2 = timemap.cached_activity(d)                    # an object array would need pickle: refused
    assert not (d / "ran2").exists() and np.array_equal(t, t2) and np.array_equal(a, a2)
    (d / "activity-v2.npz").write_bytes(b"not a zip file")
    assert len(timemap.cached_activity(d)[0]) == len(t)

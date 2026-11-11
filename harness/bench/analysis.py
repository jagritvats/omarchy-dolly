"""Screen analysis (what auto-zoom reads): how long it takes, and the zoom plan it leads to.

    python3 harness/bench/analysis.py TAKE_DIR OUT.json

Times aoi.compute_activity on the take's raw video (best of 3; the cache file is not used),
then builds the zoom plan from it and writes the segments. Set DOLLY_BENCH_ROOT to another
checkout of Dolly to run that checkout's code; harness/bench/plan_diff.py compares two outputs.
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("DOLLY_BENCH_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT))
from dolly import aoi, project as projmod  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.zoom import ZoomConfig, build_segments  # noqa: E402

take, out = Path(sys.argv[1]), Path(sys.argv[2])
proj = projmod.Project(take)
tl = Timeline.load(proj.events)
clicks = [c["t"] for c in tl.clicks]
best_w = best_c = 1e9
act = None
for _ in range(3):
    w0, c0 = time.perf_counter(), time.process_time()
    act = aoi.compute_activity(proj.raw, tl.w, tl.h, None, clicks=clicks)
    best_w = min(best_w, time.perf_counter() - w0)
    best_c = min(best_c, time.process_time() - c0)
data = proj.load()
cfg = ZoomConfig.from_dict(data.get("zoom_config"))
track = aoi.build_aoi(tl, act)
segs = [s.to_dict() for s in build_segments(tl, cfg, track)]
blobs = sum(len(r) for r in act.blobs) if hasattr(act, "blobs") else None
res = {"root": str(ROOT), "take": take.name, "video_s": round(tl.duration, 2),
       "wall_s": round(best_w, 3), "cpu_s_this_process": round(best_c, 3), "samples": int(len(act.t)),
       "blobs": blobs, "changed_fraction_sum": round(float(act.frac.sum()), 6), "segments": segs}
out.write_text(json.dumps(res, indent=1))
print(json.dumps({k: v for k, v in res.items() if k != "segments"} | {"segments": len(segs)}))

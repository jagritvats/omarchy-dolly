"""Composited frames straight from the compositor (no video encoder in between), as PNGs.

    python3 harness/bench/stills.py TAKE_DIR OUT_DIR [--aspect 9:16] [--n 40]

Evenly spaced source times across the take's export range. Set DOLLY_BENCH_ROOT to another
checkout of Dolly to render with that checkout's code instead of this one; comparing the two
sets (harness/bench/compare.py NEW/still_%03d.png OLD/still_%03d.png) shows what a change to
the renderer does to the picture, without H.264 noise on top.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("DOLLY_BENCH_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT))
from dolly import project as projmod  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.render import render_still  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("take")
ap.add_argument("out")
ap.add_argument("--aspect")
ap.add_argument("--n", type=int, default=40)
ap.add_argument("--at", default="", help="extra source times, comma separated (e.g. just after a click)")
ap.add_argument("--quality", help="draw as an export of this quality does (default: as a still, which is `best`)")
a = ap.parse_args()
how = {"quality": a.quality} if a.quality else {}        # a checkout from before 0.7.4 has no such choice
proj = projmod.Project(a.take)
data = proj.load()
tl = Timeline.load(proj.events)
t0, t1 = (data.get("trim") or [0.0, tl.duration])[:2]
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
times = [t0 + (t1 - t0) * (i + 0.5) / a.n for i in range(a.n)]
times += [float(x) for x in a.at.split(",") if x.strip()]
w0, c0 = time.perf_counter(), time.process_time()
for i, t in enumerate(times):
    render_still(a.take, t, out / f"still_{i + 1:03d}.png", aspect=a.aspect, **how)
(out / "times.json").write_text(json.dumps(times))
print(json.dumps({"root": str(ROOT), "stills": len(times), "wall_s": round(time.perf_counter() - w0, 1),
                  "cpu_s_this_process": round(time.process_time() - c0, 1),
                  "first_t": round(times[0], 2), "last_t": round(times[-1], 2)}))

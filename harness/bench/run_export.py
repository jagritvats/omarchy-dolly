"""One timed export. Prints one JSON line.

    python3 harness/bench/run_export.py --take DIR --out FILE [--aspect 9:16] [--quality high]

Run it inside a memory-capped scope (harness/bench/README.md shows the command), so a
runaway render cannot take the machine down. Measured: elapsed time, user+system CPU seconds of every process the export started, the
scope's peak memory (all processes together, from the cgroup), the largest single process,
and the output's size, length and frame count. Set DOLLY_BENCH_ROOT to another checkout of
Dolly to time that checkout's code instead of this one.
"""
import argparse
import json
import os
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("DOLLY_BENCH_ROOT") or Path(__file__).resolve().parents[2])


def cgroup_peak():
    try:
        rel = Path("/proc/self/cgroup").read_text().strip().splitlines()[-1].split(":", 2)[2]
        return int(Path("/sys/fs/cgroup" + rel + "/memory.peak").read_text())
    except (OSError, ValueError, IndexError):
        return None


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                        "stream=nb_read_packets,width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
        s = d["streams"][0]
        return {"frames": int(s["nb_read_packets"]), "width": s["width"], "height": s["height"],
                "seconds": round(float(d["format"]["duration"]), 3)}
    except (ValueError, KeyError, IndexError):
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--take", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--aspect", default="16:9")
    ap.add_argument("--quality", default="high")
    ap.add_argument("--limit", type=float, default=900.0)
    ap.add_argument("--tag", default="")
    ap.add_argument("--front", default="", help="folders put before the checkout on the module path, separated by ':' "
                    "(tests/no_cv2:tests/no_numpy times it with OpenCV, NumPy and Pillow hidden)")
    a, extra = ap.parse_known_args()                 # anything else goes to `dolly export` (e.g. --gif)
    path = os.pathsep.join([p for p in a.front.split(os.pathsep) if p] + [str(ROOT)])
    env = dict(os.environ, PYTHONPATH=path, DOLLY_OPEN_EDITOR="0")
    env.setdefault("DOLLY_WORKERS", "2")
    cmd = [sys.executable, "-m", "dolly.cli", "export", a.take, "-o", a.out, "--quality", a.quality,
           "--aspect", a.aspect, "--quiet", *extra]
    out = Path(a.out)
    out.unlink(missing_ok=True)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.perf_counter()
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        so, se = p.communicate(timeout=a.limit)
        timed_out = False
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        so, se = p.communicate()
        timed_out = True
    wall = time.perf_counter() - t0
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    res = {"root": str(ROOT), "tag": a.tag, "take": Path(a.take).name, "aspect": a.aspect, "quality": a.quality,
           "extra": extra, "workers": env["DOLLY_WORKERS"], "exit": p.returncode, "timed_out": timed_out,
           "wall_s": round(wall, 2),
           "cpu_s": round((after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime), 2),
           "user_s": round(after.ru_utime - before.ru_utime, 2), "sys_s": round(after.ru_stime - before.ru_stime, 2),
           "peak_mem_mb": None, "largest_process_mb": round(after.ru_maxrss / 1024, 1)}
    peak = cgroup_peak()
    if peak is not None:
        res["peak_mem_mb"] = round(peak / 2 ** 20, 1)
    if out.exists():
        res["bytes"] = out.stat().st_size
        res.update(probe(out))
    if p.returncode != 0:
        res["error"] = (se.strip().splitlines() or so.strip().splitlines() or ["?"])[-1][:300]
    print(json.dumps(res), flush=True)


if __name__ == "__main__":
    main()

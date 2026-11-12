#!/usr/bin/env python3
"""How well a take's event times match its picture: for the first key typed after each
pause, the time until the picture next changes. A key shows on screen one or two frames
after it is pressed, so a good time base gives small positive numbers.

    python3 harness/stage_timing.py ~/Videos/Dolly/<take> [--json]

Made for takes on the hidden stage (their time base is the pointer mark, dolly/stage.py),
but it reads any take. Decodes the take once at 320 px wide; nothing is written.
"""
import json
import statistics
import subprocess
import sys
from pathlib import Path

import numpy as np

W, H = 320, 180


def change_times(raw: Path, thresh: float = 0.35) -> list[float]:
    """Video times (from the first frame) at which the picture differs from the frame before."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-v", "info", "-copyts", "-i", str(raw), "-map", "0:v:0",
           "-fps_mode", "passthrough", "-vf", f"scale={W}:{H}:flags=area,format=gray,showinfo",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    n = len(r.stdout) // (W * H)
    frames = np.frombuffer(r.stdout[: n * W * H], np.uint8).reshape(n, H, W).astype(np.int16)
    pts = []
    for line in r.stderr.decode("utf-8", "replace").splitlines():
        if "showinfo" in line and "pts_time:" in line:
            pts.append(float(line.split("pts_time:")[1].split()[0]))
    pts = pts[:n]
    if len(pts) != n or n < 2:
        return []
    out = []
    for i in range(1, n):
        d = np.abs(frames[i] - frames[i - 1])
        if (d > 24).mean() * 100 > thresh / 100 or d.max() > 80:
            out.append(pts[i] - pts[0])
    return out


def measure(take: Path) -> dict:
    ev = json.loads((take / "events.json").read_text())
    raw = next(p for p in (take / "raw.mkv", take / "raw.mp4") if p.exists())
    allk = sorted(e["t"] for e in ev["events"] if e.get("type") == "key" and len(str(e.get("key", ""))) == 1)
    # only the first key after a pause: while a line is being typed the picture changes every
    # 90 ms, and a change cannot be told from the one before it
    keys = [t for i, t in enumerate(allk) if i == 0 or t - allk[i - 1] >= 0.6]
    ch = np.array(change_times(raw))
    lags = []
    for t in keys:
        for c in ch[(ch >= t - 0.4) & (ch <= t + 0.6)]:
            if not ((ch > c - 0.2) & (ch < c)).any():       # the first change after the picture stood still
                lags.append(float(c - t))
                break
    res = {"take": str(take), "sync": (ev.get("capture") or {}).get("sync", {}).get("method"),
           "keys": len(keys), "matched": len(lags), "changes": int(len(ch)),
           "each_ms": [round(v * 1000) for v in lags]}
    if lags:
        res.update({"median_ms": round(statistics.median(lags) * 1000, 1), "min_ms": round(min(lags) * 1000, 1),
                    "max_ms": round(max(lags) * 1000, 1)})
    return res


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    r = measure(Path(args[0]).expanduser())
    if "--json" in sys.argv:
        print(json.dumps(r))
    else:
        print(f"{r['matched']} of {r['keys']} first-keys-after-a-pause matched to a change of the picture "
              f"(time base: {r['sync']}); each: {r.get('each_ms')} ms")
        if "median_ms" in r:
            print(f"key to picture: median {r['median_ms']} ms, range {r['min_ms']} to {r['max_ms']} ms")

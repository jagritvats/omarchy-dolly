"""Frame-by-frame likeness of two renders of the same take (the second is the reference).

    python3 harness/bench/compare.py TEST REF [--crops DIR --tag NAME]

TEST and REF are video files, or printf patterns of PNG stills (still_%03d.png). Uses ffmpeg's
ssim and psnr filters on the luma+chroma planes and prints one JSON line: mean and worst
frame for each, and which frame is worst. With --crops it writes the worst frame of both,
whole and as a 4x crop around the largest difference.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image


def inputs(p):
    return ["-framerate", "1", "-i", p] if "%" in p else ["-i", p]


def frame(p, n, fps):
    """Frame n of a video or still sequence as an RGB array."""
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        if "%" in p:
            src = p % (n + 1)
            return np.asarray(Image.open(src).convert("RGB"))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", p, "-vf", f"select=eq(n{chr(92)},{n})", "-frames:v", "1", f.name],
                       check=True)
        return np.asarray(Image.open(f.name).convert("RGB"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("test")
    ap.add_argument("ref")
    ap.add_argument("--crops")
    ap.add_argument("--tag", default="cmp")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as td:
        s, q = Path(td) / "ssim.log", Path(td) / "psnr.log"
        g = f"[0:v][1:v]ssim=stats_file={s};[0:v][1:v]psnr=stats_file={q}"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs(a.test), *inputs(a.ref), "-lavfi", g, "-f", "null", "-"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(json.dumps({"error": r.stderr.strip()[-300:]}))
            sys.exit(1)
        ssim = [float(m.group(1)) for m in re.finditer(r"All:([0-9.]+)", s.read_text())]
        psnr = [float("inf") if m.group(1) == "inf" else float(m.group(1))
                for m in re.finditer(r"psnr_avg:([0-9.a-z]+)", q.read_text())]
    fin = [p for p in psnr if p != float("inf")]
    worst = int(np.argmin(ssim))
    res = {"frames": len(ssim), "ssim_mean": round(float(np.mean(ssim)), 5), "ssim_worst": round(min(ssim), 5),
           "psnr_mean_db": round(float(np.mean(fin)), 2) if fin else None,
           "psnr_worst_db": round(min(fin), 2) if fin else None, "identical_frames": len(psnr) - len(fin),
           "worst_frame": worst}
    if a.crops:
        out = Path(a.crops)
        out.mkdir(parents=True, exist_ok=True)
        ta, tb = frame(a.test, worst, 0), frame(a.ref, worst, 0)
        h, w = min(ta.shape[0], tb.shape[0]), min(ta.shape[1], tb.shape[1])
        d = np.abs(ta[:h, :w].astype(np.int16) - tb[:h, :w].astype(np.int16)).sum(axis=2).astype(np.float32)
        # the 240x135 window with the most difference
        bh, bw = 135, 240
        ii = np.pad(d, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        win = ii[bh:, bw:] - ii[:-bh, bw:] - ii[bh:, :-bw] + ii[:-bh, :-bw]
        y, x = np.unravel_index(int(np.argmax(win)), win.shape)
        res["worst_window"] = [int(x), int(y), bw, bh]
        res["worst_window_mean_abs_diff"] = round(float(win[y, x]) / (bw * bh * 3), 2)
        pair = np.concatenate([tb[y:y + bh, x:x + bw], np.full((bh, 6, 3), 255, np.uint8), ta[y:y + bh, x:x + bw]], axis=1)
        Image.fromarray(pair).resize((pair.shape[1] * 4, pair.shape[0] * 4), Image.NEAREST).save(
            out / f"{a.tag}-worst-crop-ref-left-test-right.png")
        Image.fromarray(tb[:h, :w]).save(out / f"{a.tag}-worst-frame-ref.png")
        Image.fromarray(ta[:h, :w]).save(out / f"{a.tag}-worst-frame-test.png")
    print(json.dumps(res))


if __name__ == "__main__":
    main()

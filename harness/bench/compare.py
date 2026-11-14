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
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dolly import gfx  # noqa: E402

CELL = 15                  # the difference is summed on a grid of 15x15 pixel cells, not per pixel


def inputs(p):
    return ["-framerate", "1", "-i", p] if "%" in p else ["-i", p]


def frame(p, n, fps):
    """Frame n of a video or still sequence as a picture."""
    if "%" in p:
        return gfx.load(p % (n + 1))
    with tempfile.TemporaryDirectory() as td:
        f = str(Path(td) / "f.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", p, "-vf", f"select=eq(n{chr(92)},{n})", "-frames:v", "1", f],
                       check=True)
        return gfx.load(f)


def worst_window(d, bw, bh):
    """(x, y, sum) of the bw x bh window with the most difference in `d`, to a cell."""
    w = d.get_width()
    gw, gh = w // CELL, d.get_height() // CELL
    # every cell's sum, exactly (a shrunk picture would round a difference of a level or two away)
    cell = [0] * (gw * gh)
    for ch in "rgb":
        p = gfx.plane(d, ch)
        for y in range(gh * CELL):
            row, o = (y // CELL) * gw, y * w
            for x in range(gw):
                cell[row + x] += sum(p[o + x * CELL:o + (x + 1) * CELL])
    ww, wh = max(1, min(gw, bw // CELL)), max(1, min(gh, bh // CELL))
    ii = [[0] * (gw + 1) for _ in range(gh + 1)]                  # summed-area table of the grid
    for y in range(gh):
        row, acc = ii[y + 1], 0
        for x in range(gw):
            acc += cell[y * gw + x]
            row[x + 1] = ii[y][x + 1] + acc
    best = (0, 0, -1)
    for y in range(gh - wh + 1):
        for x in range(gw - ww + 1):
            s = ii[y + wh][x + ww] - ii[y][x + ww] - ii[y + wh][x] + ii[y][x]
            if s > best[2]:
                best = (x * CELL, y * CELL, s)
    return best


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
    worst = min(range(len(ssim)), key=ssim.__getitem__)
    res = {"frames": len(ssim), "ssim_mean": round(statistics.fmean(ssim), 5), "ssim_worst": round(min(ssim), 5),
           "psnr_mean_db": round(statistics.fmean(fin), 2) if fin else None,
           "psnr_worst_db": round(min(fin), 2) if fin else None, "identical_frames": len(psnr) - len(fin),
           "worst_frame": worst}
    if a.crops:
        out = Path(a.crops)
        out.mkdir(parents=True, exist_ok=True)
        ta, tb = frame(a.test, worst, 0), frame(a.ref, worst, 0)
        h, w = min(ta.get_height(), tb.get_height()), min(ta.get_width(), tb.get_width())
        ta, tb = gfx.crop(ta, 0, 0, w, h), gfx.crop(tb, 0, 0, w, h)
        # the 240x135 window with the most difference
        bh, bw = 135, 240
        x, y, total = worst_window(gfx.difference(ta, tb), bw, bh)
        res["worst_window"] = [int(x), int(y), bw, bh]
        res["worst_window_mean_abs_diff"] = round(total / (bw * bh * 3), 2)
        pair = gfx.new(2 * bw + 6, bh)
        gfx.fill_rect(pair, 0, 0, 2 * bw + 6, bh, (1, 1, 1))
        gfx.place(pair, gfx.crop(tb, x, y, min(bw, w - x), min(bh, h - y)), 1, 1, 0, 0)
        gfx.place(pair, gfx.crop(ta, x, y, min(bw, w - x), min(bh, h - y)), 1, 1, bw + 6, 0)
        gfx.save(gfx.resize(pair, pair.get_width() * 4, bh * 4, "nearest"),
                 out / f"{a.tag}-worst-crop-ref-left-test-right.png")
        gfx.save(tb, out / f"{a.tag}-worst-frame-ref.png")
        gfx.save(ta, out / f"{a.tag}-worst-frame-test.png")
    print(json.dumps(res))


if __name__ == "__main__":
    main()

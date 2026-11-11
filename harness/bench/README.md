# Comparing two versions of Dolly's renderer

Small scripts for answering "what does this change do to the picture, the zoom plan and the
export time?" on real recordings. They were used to take OpenCV out of Dolly (0.7.0).

Each script runs this checkout's code. Set `DOLLY_BENCH_ROOT=/path/to/another/checkout` to
run that checkout's code instead (for example a worktree of the last release), then compare.

| Script | What it gives |
|---|---|
| `stills.py TAKE OUT [--aspect 9:16] [--n 40]` | Composited frames as PNGs, straight from the compositor (no video encoder in between) |
| `compare.py NEW/still_%03d.png OLD/still_%03d.png [--crops DIR --tag NAME]` | SSIM and PSNR, mean and worst frame; with `--crops`, the worst frame of both and a 4x crop where they differ most. Also takes two video files |
| `analysis.py TAKE OUT.json` | Screen-analysis time (best of 3) and the zoom plan it leads to |
| `plan_diff.py A.json B.json` | Whether two analysis runs give the same zoom plan |
| `run_export.py --take TAKE --out FILE [--aspect 9:16]` | One timed export: elapsed time, CPU seconds of every process it started, peak memory, file size |
| `make_imgops_reference.py` | Remakes `tests/data/imgops_reference.npz`. The only script that needs OpenCV |

Rules that keep the numbers honest:

- Run exports inside a memory-capped scope, one at a time:
  `systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 python3 harness/bench/run_export.py ...`
- Compare CPU seconds rather than elapsed time when the machine is busy, and take the best
  of several runs.
- A take is a recording folder (`~/Videos/Dolly/<date>`); copy it first, exports write into it.

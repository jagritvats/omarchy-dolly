"""Export worker: `python -m dolly.render_chunk job.json` renders one chunk of an export.

A fresh interpreter (never a fork of the caller, which may hold threads and locks):
it re-plans the export from the same inputs (deterministic) and renders frames
[a, b) of the main part, printing "frames N" progress lines on stdout."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    job = json.loads(Path(argv[0]).read_text())
    import os
    if os.environ.get("DOLLY_TEST_WORKER_HANG") == "1":       # tests: a worker that never progresses
        import time
        time.sleep(3600)
    from .render import export
    try:
        export(job["project_dir"], None, job.get("fmt"), job.get("quality"), t_range=job.get("t_range"),
               style_over=job.get("style_over"), aspect=job.get("aspect"), frames_dir=job.get("frames_dir"),
               frame_every=job.get("frame_every") or 0, settings=False, _chunk=job["chunk"])
    except BaseException as e:  # noqa: BLE001 - report and fail the worker
        sys.stderr.write(f"dolly render worker: {e}\n")
        raise SystemExit(3) from e


if __name__ == "__main__":
    main()

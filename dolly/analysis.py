"""The slow half of finishing a take: screen-activity analysis, the zoom plan, idle speed-ups.

Stop does the fast half first (the file is safe, events.json and project.json exist, the
"Demo recorded" card is sent, the editor opens) and leaves a marker file,
`analysis.pending`, in the recording folder. This module turns that marker into a plan:

    spawn(pdir)     start it in the background, detached (what Stop does)
    ensure(pdir)    block until the take is fully analysed: waits for the background job,
                    or does the work here when nobody is (a crash or a logout killed it)
    analyse(pdir)   the work itself

Everything that needs the plan calls ensure() first (export, share, `dolly run`, the JSON
commands, re-planning in the editor), so a half-analysed take is never rendered or
reported. A take whose marker is still there has `segments: null` in project.json, which
the renderer has always read as "plan it now": even a caller that forgot ensure() gets
the same zoom plan, only later.

One lock file (`analysis.lock`, flock) says who is working on a folder; it is released by
the kernel if that process dies, so a dead job never blocks anything.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import fsutil
from . import project as projmod

MARK = "analysis.pending"
LOCK = "analysis.lock"
JOB_KIND = "planning zooms"          # shown by the bar and the editor: "planning zooms 40 %"


def mark_path(pdir) -> Path:
    return Path(pdir) / MARK


def pending(pdir) -> dict | None:
    """The marker's contents while the take still needs its plan, else None."""
    p = mark_path(pdir)
    try:
        return json.loads(p.read_text() or "{}")
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}


def set_pending(pdir, new_project: bool, aoi: bool = True) -> None:
    fsutil.atomic_write_json(mark_path(pdir), {"new_project": bool(new_project), "aoi": bool(aoi),
                                               "since": time.time()}, durable=False)


@contextlib.contextmanager
def _locked(pdir, wait: float | None = None):
    """Hold the folder's analysis lock. wait=None blocks; a number gives up after that many
    seconds (yields False). Released when the block ends or the process dies."""
    f = open(Path(pdir) / LOCK, "a+")
    got = False
    try:
        if wait is None:
            fcntl.flock(f, fcntl.LOCK_EX)
            got = True
        else:
            end = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                    break
                except OSError:
                    if time.monotonic() >= end:
                        break
                    time.sleep(0.1)
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def running(pdir) -> bool:
    """Somebody (the background job, an export waiting in ensure) is analysing this folder now."""
    if pending(pdir) is None:
        return False
    with _locked(pdir, wait=0.0) as got:
        return not got


def _log(pdir, msg: str):
    with contextlib.suppress(OSError):
        with open(Path(pdir) / "recorder.log", "a") as f:
            f.write(msg.rstrip() + "\n")


def plan(pdir, info: dict | None = None, sync: bool = False, progress=None) -> dict:
    """Compute the plan and store it. The caller holds the lock. `sync`: part of a
    synchronous finalize, where the plan always replaces what is there (as it always did);
    otherwise it only fills what is still unplanned, so an edit made meanwhile wins."""
    from .capture import _from_go, aoimod_activity
    from .events import Timeline
    from .zoom import ZoomConfig, build_segments
    pdir = Path(pdir)
    info = info if info is not None else (pending(pdir) or {})
    say = progress or (lambda _f: None)
    proj = projmod.Project(pdir)
    data = proj.load()
    tl = Timeline.load(proj.events)
    go_t = (tl.capture or {}).get("go")
    cfg = ZoomConfig.from_dict(data.get("zoom_config"))
    say(0.05)
    track = None
    if info.get("aoi", True) and cfg.use_aoi:
        try:
            from . import aoi as aoimod
            track = aoimod.build_aoi(tl, aoimod.cached_activity(pdir, tl))
        except Exception as ex:
            _log(pdir, f"aoi analysis failed: {ex}")
    say(0.8)
    segs = [s.to_dict() for s in _from_go(build_segments(tl, cfg, track), go_t)]
    speed = None
    if info.get("new_project") and data["style"].get("speed", {}).get("enabled") and data.get("speed") is None:
        try:                                    # Settings > Camera > Speed up idle time
            from .timemap import SpeedConfig, detect_speedups
            speed = detect_speedups(tl, aoimod_activity(pdir, tl), SpeedConfig.from_dict(data.get("speed_config")))
        except Exception as ex:
            _log(pdir, f"idle speed-up failed: {ex}")
    say(0.95)
    # the editor may have saved a trim, a look or a blur box in the meantime: read again and
    # change only what the analysis owns
    with project_lock(pdir):
        cur = proj.load()
        if sync or cur.get("segments") is None:
            cur["segments"] = segs
        if speed is not None and cur.get("speed") is None:
            cur["speed"] = speed
        proj.save(cur)
        mark_path(pdir).unlink(missing_ok=True)
    say(1.0)
    return {"segments": segs, "speed": speed}


@contextlib.contextmanager
def project_lock(pdir):
    """Serialises read-change-write of project.json between the analysis and the editor."""
    f = open(Path(pdir) / ".project.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def analyse(pdir, sync: bool = False, show: bool = False) -> bool:
    """Do the pending analysis of `pdir` now (waits if another process is on it).
    True when this call did the work, False when there was nothing left to do."""
    pdir = Path(pdir)
    if pending(pdir) is None:
        return False
    with _locked(pdir):
        info = pending(pdir)
        if info is None:                     # the other process finished while we waited
            return False
        job = None
        if show:
            with contextlib.suppress(Exception):
                from . import health
                job = health.Job(JOB_KIND, str(pdir))
        try:
            plan(pdir, info, sync=sync, progress=(job.update if job else None))
        finally:
            if job:
                with contextlib.suppress(Exception):
                    job.done()
    return True


def ensure(pdir, say=None) -> None:
    """Return only when `pdir` is fully analysed. Cheap when it already is."""
    pdir = Path(pdir)
    if pending(pdir) is None:
        return
    if say:
        say("finishing the analysis of this recording (zoom plan)")
    analyse(pdir)


def spawn(pdir) -> bool:
    """Start the analysis in the background, detached from this process and its terminal.
    It needs ffmpeg and plain Python only (no cairo), so it runs on any machine that records."""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    try:
        subprocess.Popen([sys.executable, "-m", "dolly.analysis", str(pdir)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        return True
    except OSError as ex:
        _log(pdir, f"background analysis could not start ({ex}); it runs when the recording is next used")
        return False


def unfinished(root: Path | None = None) -> list[Path]:
    """Recordings whose analysis was interrupted (marker there, nobody working on it)."""
    root = Path(root) if root else projmod.videos_root()
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and mark_path(d).exists() and (d / "events.json").exists() and not running(d):
                out.append(d)
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m dolly.analysis RECORDING_DIR", file=sys.stderr)
        return 2
    with contextlib.suppress(OSError):
        os.nice(10)                          # a new take, or the editor, comes first
    try:
        analyse(argv[0], show=True)
    except Exception as ex:                  # the marker stays: the next use of the take tries again
        _log(argv[0], f"background analysis failed: {ex!r}")
        return 1
    with contextlib.suppress(Exception):
        from .capture import shell_refresh
        shell_refresh()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

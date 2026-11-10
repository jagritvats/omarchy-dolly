"""Instant replay: keep the last N seconds in RAM, save them as a polished clip.

    dolly replay start [--seconds 60]   gpu-screen-recorder replay buffer + rolling event log
    dolly replay save  [--last 30]      save the buffer (or the last 10/30/60/300 s) -> new project
    dolly replay stop
    dolly replay status

gpu-screen-recorder flags (checked against its manual, Nov 2026):
  -r SECONDS    replay buffer length (2-86400); -o must be a directory
  -replay-storage ram (default)
  SIGUSR1       save the whole buffer
  SIGRTMIN+1..6 save the last 10 s / 30 s / 60 s / 5 min / 10 min / 30 min
  SIGINT        stop without saving
wf-recorder has no replay mode, so replay needs gpu-screen-recorder.

Event alignment: the logger keeps events (with CLOCK_MONOTONIC times) in a
ring buffer in memory. On save we note the monotonic time, ask the logger to
dump its buffer, wait for gsr's file, and place the clip's first frame at
save_time - clip_duration.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import project as projmod
from .capture import _alive, _have, notify, pick_target, shell_refresh
from .i18n import t
from .logger import runtime_dir
from . import fsutil

RT_OFFSETS = {10: 1, 30: 2, 60: 3, 300: 4}


def state_file() -> Path:
    return runtime_dir() / "replay.json"


def replay_state() -> dict | None:
    try:
        st = json.loads(state_file().read_text())
    except (OSError, ValueError):
        return None
    if _alive(st.get("recorder_pid", 0)):
        return st
    from .guards import set_replay
    set_replay(None)
    return None


def start(a):
    if replay_state():
        print("replay buffer already running", file=sys.stderr)
        return
    if not _have("gpu-screen-recorder"):
        notify(t("notify.replayNeedsGsr"), t("notify.replayNeedsGsrBody"), urgency="critical")
        raise SystemExit(2)
    secs = int(getattr(a, "seconds", 60) or 60)
    from .health import other_recorder
    other = other_recorder()
    if other:
        notify(t("notify.recordBusyTitle"), t("notify.recordBusyBody", other), timeout=6000)
        raise SystemExit(f"dolly: {other} is already recording")
    tgt = pick_target("screen")
    region, scale, monitor = tgt.region, tgt.scale, tgt.monitor
    work = runtime_dir() / "replay"
    work.mkdir(exist_ok=True)
    outdir = projmod.videos_root() / ".replay"
    outdir.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write_json(work / "capture.json", {"region": region, "scale": scale, "target_info": tgt.to_meta()})
    log = open(work / "recorder.log", "a")
    proc = None
    for backend in (["gsr", "gsr-cpu"] if os.environ.get("DOLLY_BACKEND") in (None, "", "auto")
                    else [os.environ["DOLLY_BACKEND"]]):
        cmd = ["gpu-screen-recorder", *tgt.gsr_args(), "-f", "60", "-cursor", "no", "-k", "h264", "-q", "very_high",
               "-c", "mp4", "-r", str(secs), "-replay-storage", "ram", "-o", str(outdir)]
        cmd += ["-encoder", "cpu"] if backend == "gsr-cpu" else ["-fallback-cpu-encoding", "yes"]
        log.write(f"\n=== {time.strftime('%F %T')} replay backend={backend}\n$ {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
        time.sleep(1.5)
        if proc.poll() is None:
            break
        proc = None
    if not proc:
        notify(t("notify.replayCannotStart"), t("notify.seeLog", f"{work}/recorder.log"), urgency="critical")
        raise SystemExit(1)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    logger = subprocess.Popen([sys.executable, "-m", "dolly.cli", "log-events", str(work), "--replay", str(secs)],
                              stdout=log, stderr=log, env=env, start_new_session=True)
    st = {"recorder_pid": proc.pid, "logger_pid": logger.pid, "seconds": secs, "outdir": str(outdir),
          "work": str(work), "region": region, "scale": scale, "monitor": monitor, "fps": 60,
          "started": time.time(), "backend": backend}
    fsutil.atomic_write_json(state_file(), st, durable=False)
    from .guards import set_replay
    set_replay(st.get("recorder_pid"))
    shell_refresh()
    notify(t("notify.replayOn"), t("notify.replayOnBody", secs, "Super+Alt+Shift+R"), timeout=3000)
    print(f"replay buffer running ({secs} s)")


def _wait_new_file(d: Path, since: float, timeout: float = 20.0) -> Path | None:
    end = time.time() + timeout
    seen = None
    while time.time() < end:
        cands = [p for p in d.glob("*.mp4") if p.stat().st_mtime >= since - 0.5]
        if cands:
            p = max(cands, key=lambda x: x.stat().st_mtime)
            size = p.stat().st_size
            if seen and seen[0] == p and seen[1] == size and size > 0:
                return p
            seen = (p, size)
        time.sleep(0.3)
    return None


def save(a):
    st = replay_state()
    if not st:
        print("replay buffer is not running (dolly replay start)", file=sys.stderr)
        raise SystemExit(1)
    last = getattr(a, "last", None)
    wall0 = time.time()
    save_mono = time.monotonic()
    sig = signal.SIGUSR1 if not last else signal.SIGRTMIN + RT_OFFSETS[last]
    os.kill(st["recorder_pid"], sig)
    os.kill(st["logger_pid"], signal.SIGUSR1)  # dump the event ring
    clip = _wait_new_file(Path(st["outdir"]), wall0)
    if not clip:
        notify(t("notify.replaySaveFailed"), "gpu-screen-recorder wrote no file", urgency="critical")
        raise SystemExit(1)
    pdir = projmod.videos_root() / time.strftime("%Y-%m-%d_%H-%M-%S-replay")
    pdir.mkdir(parents=True)
    shutil.move(str(clip), pdir / "raw.mp4")
    from .capture import finalize, video_info
    dur = video_info(pdir / "raw.mp4")["duration"]
    start_mono = save_mono - dur
    dump = Path(st["work"]) / "replay-events.jsonl"
    for _ in range(30):
        if dump.exists() and dump.stat().st_mtime >= wall0 - 0.5:
            break
        time.sleep(0.1)
    lines = []
    if dump.exists():
        for line in dump.read_text().splitlines():
            try:
                lines.append(json.loads(line))
            except ValueError:
                pass
    with open(pdir / "events.raw.jsonl", "w") as f:
        f.write(json.dumps({"type": "start", "ms": 0}) + "\n")
        for e in lines:
            if e.get("type") in ("info",):
                f.write(json.dumps({**e, "ms": 0}) + "\n")
                continue
            m = e.get("mono")
            if m is None or m < start_mono - 0.2 or m > save_mono + 0.2:
                continue
            e = {k: v for k, v in e.items() if k != "mono"}
            e["ms"] = round((m - start_mono) * 1000, 1)
            f.write(json.dumps(e) + "\n")
    meta = {"backend": st["backend"], "region": st["region"], "scale": st["scale"], "monitor": st["monitor"],
            "fps": st["fps"], "cursor_baked": False, "mode": "replay", "replay_save_mono": save_mono,
            "started": time.time() - dur}
    fsutil.atomic_write_json(pdir / "capture.json", meta, indent=2)
    finalize(pdir)
    dolly = str(Path(__file__).resolve().parents[1] / "bin" / "dolly")
    notify(t("notify.replaySaved"), t("notify.replaySavedBody", f"{dur:.0f}", pdir.name), (t("notify.edit"), dolly, "edit", str(pdir)),
           (t("notify.quickExport"), dolly, "export", str(pdir)), timeout=8000)
    print(pdir)
    return pdir


def stop(_a=None):
    st = replay_state()
    if not st:
        return
    for k in ("recorder_pid", "logger_pid"):
        if _alive(st.get(k, 0)):
            os.kill(st[k], signal.SIGINT if k == "recorder_pid" else signal.SIGTERM)
    state_file().unlink(missing_ok=True)
    from .guards import set_replay
    set_replay(None)
    shell_refresh()
    notify(t("notify.replayOff"), "", timeout=1500)


def status(_a=None):
    st = replay_state()
    print(json.dumps({"replay": bool(st), "seconds": st.get("seconds") if st else None}))
    raise SystemExit(0 if st else 1)

"""Tiny state files for Omarchy menu `when:` guards.

Omarchy runs every menu guard on each menu open (budget: <=100 ms per guard,
<=200 ms per batch), so guards must not start Python. Dolly keeps these files
accurate and the guards are pure shell file tests (see GUARDS):

    $XDG_RUNTIME_DIR/dolly/recording.pid  pid of the recorder while recording
    $XDG_RUNTIME_DIR/dolly/replay.pid     pid of the replay recorder while armed
    ~/.local/state/dolly/last          path of the newest finalized recording
    ~/.local/state/dolly/share-ready   exists when a share target is configured

All writes are atomic (temp file + rename). XDG_RUNTIME_DIR is a tmpfs, so the
pid files cannot survive a reboot, and a guard also checks the pid is alive
(`kill -0` is a shell builtin), so a crash never leaves a stale menu row.
"""
from __future__ import annotations

import os
from pathlib import Path


def run_dir() -> Path:
    from .logger import runtime_dir
    return runtime_dir()


def state_dir() -> Path:
    d = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "dolly"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(p: Path, text: str):
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    tmp.replace(p)


def set_recording(pid: int | None):
    p = run_dir() / "recording.pid"
    _write(p, f"{pid}\n") if pid else p.unlink(missing_ok=True)


def set_replay(pid: int | None):
    p = run_dir() / "replay.pid"
    _write(p, f"{pid}\n") if pid else p.unlink(missing_ok=True)


def set_last(project_dir: Path | None):
    if project_dir is None:
        (state_dir() / "last").unlink(missing_ok=True)
        return
    _write(state_dir() / "last", f"{Path(project_dir).resolve()}\n")


def set_share_ready(ready: bool):
    p = state_dir() / "share-ready"
    _write(p, "1\n") if ready else p.unlink(missing_ok=True)


# Shell expressions used by dolly-setup (sh-compatible: builtins only, no Python, no forks
# besides the test itself).
RUN = '"${XDG_RUNTIME_DIR:-$HOME/.local/state}/dolly'
STATE = '"${XDG_STATE_HOME:-$HOME/.local/state}/dolly'
GUARDS = {
    "recording": "{ read -r p < " + RUN + '/recording.pid" && kill -0 "$p"; } 2>/dev/null',
    "not_recording": "! { read -r p < " + RUN + '/recording.pid" && kill -0 "$p"; } 2>/dev/null',
    "replay": "{ read -r p < " + RUN + '/replay.pid" && kill -0 "$p"; } 2>/dev/null',
    "not_replay": "! { read -r p < " + RUN + '/replay.pid" && kill -0 "$p"; } 2>/dev/null',
    "last": "{ read -r d < " + STATE + '/last" && [ -d "$d" ]; } 2>/dev/null',
    "share_ready": "[ -e " + STATE + '/share-ready" ]',
}

if __name__ == "__main__":   # dolly-setup reads the guards from here
    import json
    print(json.dumps(GUARDS))

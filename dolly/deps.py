"""What Dolly still needs on this machine, and installing it the way Omarchy does.

Omarchy plugins have no install hooks, and `omarchy plugin add` only clones and
enables. A fresh Omarchy lacks python-numpy and python-pillow, and in a VM
gpu-screen-recorder cannot capture ("unknown gpu vendor"), so wf-recorder is
needed. `missing()` names the Arch packages; `install()` opens a floating terminal
running `omarchy-pkg-add` (Omarchy's own helper: installs only what is missing,
asks for the sudo password in that terminal), like omarchy-agent-local does.

No third-party imports here: this module must work exactly when they are missing.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from . import fsutil

PY_PACKAGES = (("numpy", "python-numpy"), ("PIL", "python-pillow"))
# gpu-screen-recorder messages that mean "this GPU cannot capture at all" (any encoder)
GSR_FATAL = ("unknown gpu vendor", "failed to load opengl", "failed to get egl display",
             "no gpu found", "failed to find a gpu")


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "dolly"


def _note_path() -> Path:
    return _state_dir() / "gsr-unusable.json"


def _write_note(reason: str | None):
    try:
        _state_dir().mkdir(parents=True, exist_ok=True)
        gsr = shutil.which("gpu-screen-recorder") or ""
        fsutil.atomic_write_json(_note_path(), {"reason": reason, "t": time.time(), "gsr": gsr,
                                            "mtime": os.path.getmtime(gsr) if gsr else 0})
    except OSError:
        pass


def note_gsr_failure(log_text: str) -> str | None:
    """Called after gsr failed to start: remember why, if it can never work here."""
    low = log_text.lower()
    reason = next((m for m in GSR_FATAL if m in low), None)
    if reason:
        _write_note(reason)
    return reason


def gsr_unusable() -> str | None:
    """Why gpu-screen-recorder cannot capture here, or None (usable or unknown).
    A note from a failed start counts until gsr itself is reinstalled/updated."""
    gsr = shutil.which("gpu-screen-recorder")
    if not gsr:
        return "not installed"
    try:
        n = json.loads(_note_path().read_text())
        if n.get("gsr") == gsr and abs(n.get("mtime", 0) - os.path.getmtime(gsr)) < 1:
            return n.get("reason")        # cached either way until gsr is reinstalled/updated
    except (OSError, ValueError):
        pass
    if not os.environ.get("WAYLAND_DISPLAY"):
        return None                       # can't probe outside a session; don't guess
    try:
        r = subprocess.run([gsr, "--info"], capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (r.stdout + r.stderr).lower()
    # gsr 6.1 prints "gsr error: unknown gpu vendor ..." for --info but exits 0 (VM, Nov 10)
    reason = next((m for m in GSR_FATAL if m in out), None)
    _write_note(reason)
    return reason


def missing(capture: bool = True) -> dict:
    """{"packages": [...], "why": {pkg: reason}} - Arch package names Dolly still needs."""
    pkgs, why = [], {}
    for mod, pkg in PY_PACKAGES:
        if importlib.util.find_spec(mod) is None:
            pkgs.append(pkg)
            why[pkg] = "editor, export and sync markers"
    if not shutil.which("ffmpeg"):
        pkgs.append("ffmpeg")
        why["ffmpeg"] = "export"
    if capture and not shutil.which("wf-recorder"):
        bad = gsr_unusable()
        if bad:
            pkgs.append("wf-recorder")
            why["wf-recorder"] = ("no screen recorder installed" if bad == "not installed"
                                  else f"gpu-screen-recorder cannot capture here ({bad})")
    return {"packages": pkgs, "why": why}


def install_command(pkgs: list[str]) -> str:
    q = " ".join(shlex.quote(p) for p in pkgs)
    if shutil.which("omarchy-pkg-add"):
        return f"omarchy-pkg-add {q}"
    return f"sudo pacman -S --needed {q}"


def install(pkgs: list[str] | None = None, then: str | None = None) -> dict:
    """Open a floating terminal that installs `pkgs` (default: what is missing) and then
    runs `dolly doctor`. Returns {"packages", "command", "launched"}; with nothing to do,
    launched is False and command empty."""
    pkgs = list(pkgs) if pkgs is not None else missing()["packages"]
    if not pkgs:
        return {"packages": [], "command": "", "launched": False}
    dolly = str(Path(__file__).resolve().parent.parent / "bin" / "dolly")
    after = then or f"{shlex.quote(dolly)} doctor"
    cmd = f"{install_command(pkgs)} && {after}"
    term = shutil.which("omarchy-launch-floating-terminal-with-presentation")
    launched = False
    if term:
        try:
            subprocess.Popen([term, cmd], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            launched = True
        except OSError:
            pass
    return {"packages": pkgs, "command": cmd, "launched": launched}

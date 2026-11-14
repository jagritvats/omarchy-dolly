"""What Dolly still needs on this machine. Dolly installs nothing itself.

A stock Omarchy has everything Dolly uses: ffmpeg, gpu-screen-recorder, Python's cairo
module (python-cairo) and Pango through python-gobject. Two things can still be missing:
- python-cairo, on a machine where it was removed: Dolly then records and gives the plain
  export (dolly/plain.py); the editor and the styled export say that it is missing;
- a screen recorder that works: in a VM gpu-screen-recorder cannot capture ("unknown gpu
  vendor"), so wf-recorder is needed.
`missing()` names the Arch packages and why. Dolly says so in plain words and leaves the
installing to the user, with Omarchy's package installer (`HOW`).

No third-party imports here: this module must work exactly when they are missing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from . import fsutil

DRAW_PACKAGE = "python-cairo"
# where Omarchy's own menu offers its package installer (omarchy-menu.jsonc: Install > Package)
HOW = "Omarchy menu > Install > Package"
# gpu-screen-recorder messages that mean "this GPU cannot capture at all" (any encoder)
GSR_FATAL = ("unknown gpu vendor", "failed to load opengl", "failed to get egl display",
             "no gpu found", "failed to find a gpu")


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "dolly"


def _note_path() -> Path:
    return _state_dir() / "gsr-unusable.json"


def _write_note(reason: str | None):
    try:
        fsutil.private_dir(_state_dir())
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


PY_WHY = "the editor and the styled export (zoom, background, cursor, captions)"


def python_missing() -> list[str]:
    """[DRAW_PACKAGE] when Python's cairo module cannot be imported, else []. Recording,
    stopping, the zoom plan and the plain export (dolly/plain.py) work without it; the
    editor and the styled export (everything that draws) do not."""
    from . import gfx
    return [] if gfx.available() else [DRAW_PACKAGE]


def text_layout_missing() -> bool:
    """True when Pango cannot be used (no python-gobject or pango): text is then drawn with
    cairo's own simple font, and nothing else changes."""
    from . import gfx
    return gfx.text_engine() == "cairo"


def how(pkgs) -> str:
    """'wf-recorder (install it with Omarchy menu > Install > Package)'."""
    pkgs = list(pkgs)
    return f"{', '.join(pkgs)} (install {'it' if len(pkgs) == 1 else 'them'} with {HOW})"


def missing(capture: bool = True) -> dict:
    """{"packages": [...], "why": {pkg: reason}, "blocking": [...]} - Arch package names
    Dolly still needs. "blocking" are the ones a recording cannot start without (a screen
    recorder that works here, ffmpeg); python-cairo only holds back what draws."""
    pkgs, why, blocking = [], {}, []
    for pkg in python_missing():
        pkgs.append(pkg)
        why[pkg] = PY_WHY
    if not shutil.which("ffmpeg"):
        pkgs.append("ffmpeg")
        blocking.append("ffmpeg")
        why["ffmpeg"] = "recording and export"
    if capture and not shutil.which("wf-recorder"):
        bad = gsr_unusable()
        if bad:
            pkgs.append("wf-recorder")
            blocking.append("wf-recorder")
            why["wf-recorder"] = ("no screen recorder installed" if bad == "not installed"
                                  else f"gpu-screen-recorder cannot capture here ({bad})")
    return {"packages": pkgs, "why": why, "blocking": blocking}


def passed_over_environment() -> Path | None:
    """The private Python environment bin/dolly does not use, or None. Set up made one up
    to 0.7.3 for NumPy and Pillow, without the system's packages: no cairo inside, so the
    launcher runs the system Python instead (the same rule as in bin/dolly)."""
    if os.environ.get("DOLLY_VENV"):
        return None
    venv = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "dolly" / "venv"
    if not os.access(venv / "bin" / "python", os.X_OK):
        return None
    try:
        cfg = (venv / "pyvenv.cfg").read_text(errors="replace")
    except OSError:
        cfg = ""
    sees_system = any(ln.split("=", 1)[0].strip().lower() == "include-system-site-packages"
                      and ln.split("=", 1)[-1].strip().lower() == "true" for ln in cfg.splitlines())
    return None if sees_system else venv


def ask_for_python(what: str) -> list[str]:
    """`what` (the editor, a styled export) was asked for and python-cairo is missing: a
    card that says so and where it can be installed. Returns what is missing ([] when
    nothing is, and then nothing is shown)."""
    lacking = python_missing()
    if lacking:
        try:
            from .capture import notify
            from .i18n import t
            notify(t("notify.needsPython", what), t("notify.needsPythonBody", ", ".join(lacking)),
                   urgency="normal", timeout=0)
        except Exception:
            from . import log
            log.swallowed("needs-python card")
    return lacking

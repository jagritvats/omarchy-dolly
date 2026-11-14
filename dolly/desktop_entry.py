"""Managed XDG application entry. Never adopt or remove a foreign file/symlink."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ID = "io.github.jagritvats.dolly.desktop"
MARKER = "X-Dolly-Managed=true"


def path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    if not base.is_absolute():               # XDG paths must be absolute
        base = Path.home() / ".local/share"
    return base / "applications" / ID


def _string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", r"\n").replace("\r", r"\r").replace("\t", r"\t")


def _exec_arg(value: str) -> str:
    # Desktop Entry Exec is NOT shell syntax: quote argv, escape its four special
    # characters, then apply key-file string escaping. Literal % is a field code.
    quoted = "".join("\\" + c if c in '\\"`$' else c for c in value)
    return _string('"' + quoted.replace("%", "%%") + '"')


def content(launcher: str) -> str:
    exe = _exec_arg(launcher)
    return ("[Desktop Entry]\nType=Application\nVersion=1.0\n"
            "Name=Dolly Screen Recorder\nGenericName=Screen Recorder\n"
            "Comment=Record your screen, edit and share screencasts with Dolly\n"
            "Keywords=Dolly;screen;recorder;recording;screencast;demo;\n"
            "Icon=camera-video\nTerminal=false\nCategories=AudioVideo;Recorder;\n"
            f"Exec={exe} launch\nTryExec={_string(launcher)}\n"
            f"{MARKER}\nX-Dolly-Launcher={_string(launcher)}\n"
            "Actions=Library;Settings;\n\n"
            f"[Desktop Action Library]\nName=Library\nExec={exe} library --open\n\n"
            f"[Desktop Action Settings]\nName=Settings\nExec={exe} settings\n")


def _read() -> str | None:
    p = path()
    if p.is_symlink():
        return None
    try:
        return p.read_text()
    except (OSError, UnicodeError):
        return None


def managed() -> bool:
    return MARKER in (_read() or "").splitlines()


def ours(launcher: str) -> bool:
    return managed() and f"X-Dolly-Launcher={_string(launcher)}" in (_read() or "").splitlines()


def status(launcher: str) -> dict:
    p = path()
    if not p.exists() and not p.is_symlink():
        state = "missing"
    elif not managed():
        state = "foreign"
    elif not ours(launcher):
        state = "other-copy"
    else:
        state = "current" if _read() == content(launcher) else "stale"
    return {"state": state, "path": str(p)}


def _refresh():
    exe = shutil.which("update-desktop-database")
    if exe:
        try:
            subprocess.run([exe, str(path().parent)], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass                            # launchers also watch the directory


def apply(launcher: str) -> dict:
    st = status(launcher)
    if st["state"] in ("foreign", "current"):
        return {**st, "state": "unchanged" if st["state"] == "current" else "foreign"}
    try:
        from .fsutil import atomic_write_text
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, content(launcher), durable=False)
        p.chmod(0o644)
        _refresh()
        return {**st, "state": "written"}
    except OSError as e:
        return {**st, "state": "failed", "error": str(e)}


def undo(launcher: str) -> bool:
    if not ours(launcher):
        return False
    path().unlink()
    _refresh()
    return True

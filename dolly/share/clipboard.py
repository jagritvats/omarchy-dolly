"""Wayland clipboard via wl-copy (wl-clipboard, ships with Omarchy).

copy_file() offers the file as text/uri-list, the MIME type Chromium/Electron
(Slack, Discord, GitHub in the browser) and Firefox read when you paste into
an upload field, so Ctrl+V attaches the video itself. File managers that want
x-special/gnome-copied-files are not covered (wl-copy serves one type).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote


class ClipboardUnavailable(RuntimeError):
    pass


def _wl_copy(data: bytes, mime: str | None):
    exe = shutil.which("wl-copy")
    if not exe:
        raise ClipboardUnavailable("wl-copy not found (sudo pacman -S wl-clipboard)")
    cmd = [exe] + (["--type", mime] if mime else [])
    # wl-copy forks to serve the selection; it returns once it owns the clipboard
    subprocess.run(cmd, input=data, check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def file_uri(path: str | Path) -> str:
    return "file://" + quote(str(Path(path).resolve()))


def copy_file(path: str | Path):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    _wl_copy((file_uri(p) + "\r\n").encode(), "text/uri-list")


def copy_text(text: str):
    _wl_copy(text.encode(), "text/plain;charset=utf-8")

"""Dolly's diagnostic log: ~/.local/state/dolly/dolly.log (rotated, 512 KB x 3).

Errors Dolly deliberately survives (a probe that failed, an optional tool missing, a
notification that could not be sent) used to vanish in `except Exception: pass`. They
still don't stop anything, but `swallowed()` records them here, so `dolly doctor` and a
bug report have something to go on.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path

_LOG: logging.Logger | None = None


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "dolly"


def get() -> logging.Logger:
    global _LOG
    if _LOG is not None:
        return _LOG
    lg = logging.getLogger("dolly")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    if not lg.handlers:
        try:
            from . import fsutil
            d = fsutil.private_dir(state_dir())
            h = logging.handlers.RotatingFileHandler(d / "dolly.log", maxBytes=512 * 1024, backupCount=3,
                                                     encoding="utf-8", delay=True)
            h.setFormatter(logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s"))
        except OSError:
            h = logging.NullHandler()            # a read-only home must not break recording
        lg.addHandler(h)
    _LOG = lg
    return lg


def swallowed(where: str = "") -> None:
    """Call inside an `except` block that intentionally carries on: logs the exception
    (type, message, traceback) at DEBUG level. Never raises."""
    try:
        et, ev, _ = sys.exc_info()
        if et is None:
            return
        caller = where or sys._getframe(1).f_code.co_name
        get().debug("ignored %s in %s: %s", et.__name__, caller, ev, exc_info=True)
    except Exception:  # logging must never be the thing that breaks
        pass


def last_error_path() -> Path:
    return state_dir() / "last-error.log"


def write_last_error(argv: list[str]) -> Path | None:
    """Full traceback of the exception being handled, for `dolly doctor` / bug reports."""
    try:
        p = last_error_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        import time
        p.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  dolly {' '.join(argv)}\n\n"
                     + traceback.format_exc(), encoding="utf-8")
        get().error("unexpected error in dolly %s", " ".join(argv), exc_info=True)
        return p
    except Exception:
        return None

"""Crash-safe file writes and damage-tolerant JSON reads.

A recording's files (capture.json, events.json, project.json, the state file) are
rewritten while Dolly runs; a crash, power loss or full disk in the middle of a plain
`write_text` leaves a truncated file, and the next `json.loads` dies with a traceback.

    atomic_write_text / atomic_write_json
        write a temp file in the same folder, fsync it, then os.replace() it over the
        target (readers see the old file or the new one, never half of one).
        backup=True first keeps the current version as <name>.bak, but only when that
        version is itself readable (a damaged file never overwrites a good backup).

    read_json
        parse a JSON file; when it is damaged, fall back to <name>.bak (and restore it),
        else raise DamagedFile with a message a user can act on.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

_UMASK = os.umask(0)
os.umask(_UMASK)


def private_dir(path: str | Path) -> Path:
    """Create `path` and keep it this user's alone (0700): Dolly's state, cache and runtime
    folders hold logs, the list of recordings and what the recorder is doing. A folder of
    ours that an older version made with the umask is tightened; one that is not ours is
    left as it is (the caller decides whether to use it)."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        st = os.lstat(p)
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and st.st_mode & 0o077:
            os.chmod(p, 0o700)
    except OSError:
        pass
    return p


class DamagedFile(ValueError):
    """A JSON file Dolly needs is unreadable and has no usable backup."""

    def __init__(self, path: Path, reason: str = ""):
        self.path = Path(path)
        what = {"project.json": "edits (project.json)", "events.json": "event log (events.json)",
                "capture.json": "capture details (capture.json)"}.get(self.path.name, self.path.name)
        msg = f"this recording's {what} is damaged and has no usable backup: {self.path}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


def backup_path(path: Path) -> Path:
    path = Path(path)
    return path.with_name(path.name + ".bak")


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _readable_json(path: Path) -> bool:
    try:
        json.loads(path.read_text())
        return True
    except (OSError, ValueError):
        return False


def atomic_write_text(path: str | Path, text: str, backup: bool = False, durable: bool = True,
                      private: bool = False) -> None:
    """`private`: the file is this user's alone (0600) whatever the umask says."""
    path = Path(path)
    d = path.parent
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            if durable:
                os.fsync(f.fileno())
        if not private:                         # mkstemp makes 0600: match a normal file, or the
            mode = 0o666 & ~_UMASK              # file being replaced (one made private stays so)
            try:
                mode = stat.S_IMODE(os.stat(path).st_mode)
            except OSError:
                pass
            os.chmod(tmp, mode)
        if backup and path.exists() and (path.suffix != ".json" or _readable_json(path)):
            try:
                _copy(path, backup_path(path))   # copy, not move: the target stays in place until replaced
            except OSError:
                pass                             # a backup is a bonus; never block the write
        os.replace(tmp, path)
        if durable:
            _fsync_dir(d)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _copy(src: Path, dst: Path) -> None:
    data = src.read_bytes()
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=dst.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, stat.S_IMODE(os.stat(src).st_mode))    # a backup is as private as its file
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def erase_range(fd: int, offset: int, length: int) -> bool:
    """Zero `length` bytes at `offset` of an open file and give their storage back, without
    changing the file's length (Linux fallocate, punch hole). For a file another process
    keeps appending to: its writes and a reader's position are undisturbed. False where the
    filesystem cannot do it; the caller then relies on deleting the file."""
    if length <= 0:
        return True
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        fn = getattr(libc, "fallocate64", None) or libc.fallocate
        fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
        fn.restype = ctypes.c_int
        return fn(fd, 0x01 | 0x02, offset, length) == 0     # FALLOC_FL_KEEP_SIZE | FALLOC_FL_PUNCH_HOLE
    except (OSError, AttributeError, ValueError):
        return False


def atomic_write_json(path: str | Path, obj, indent: int | None = None, backup: bool = False,
                      durable: bool = True, private: bool = False, **dumps_kw) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent, **dumps_kw), backup=backup, durable=durable,
                      private=private)


def read_json(path: str | Path, recover: bool = True):
    """Parsed JSON of `path`. Damaged (truncated, not JSON, not UTF-8): use <name>.bak if it
    parses (and put it back in place, with a note on stderr and in dolly.log), else raise
    DamagedFile. A missing file raises FileNotFoundError as before."""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (ValueError, UnicodeDecodeError) as e:
        reason = f"{type(e).__name__}: {e}"[:160]
    if recover:
        bak = backup_path(path)
        try:
            data = json.loads(bak.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if data is not None:
            try:
                damaged = path.with_name(path.name + ".damaged")
                _copy(path, damaged)             # keep the broken one for inspection
                _copy(bak, path)
            except OSError:
                pass
            note = f"dolly: {path} was damaged; restored the previous version from {bak.name}"
            print(note, file=sys.stderr)
            try:
                from . import log
                log.get().warning("%s (%s)", note, reason)
            except Exception:  # never fail a read over logging
                pass
            return data
    raise DamagedFile(path, reason)

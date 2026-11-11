"""Recordings library: "find previous ones".

* `index()` scans the recordings folder and writes a cached JSON index
  (`$XDG_CACHE_HOME/dolly/library.json`). The bar panel reads that file
  directly (instant, never blocks the shell) and asks for a background
  refresh; each entry is re-read only when its folder changed.
* `poster()` keeps a small poster frame (`thumb.jpg`) per recording.
* rename (title stored in project.json; the folder keeps its timestamp name),
  trash with undo (freedesktop trash spec, so Files can restore it too), and
  "clean up raw files of exported recordings".
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path

from . import project as projmod
from . import fsutil

INDEX_VERSION = 2
VIDEO_EXT = (".mp4", ".gif", ".webm", ".mkv")


def cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "dolly" / "library.json"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "dolly"


def _du(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _sig(d: Path) -> str:
    """Changes when anything the entry shows changes (files added/removed, project saved)."""
    parts = []
    for name in ("project.json", "events.json", "capture.json", "shares.json", "raw.mkv", "raw.mp4", "thumb.jpg"):
        try:
            st = (d / name).stat()
            parts.append(f"{name}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            pass
    try:
        ex = d / "exports"
        parts.append(f"ex:{int(ex.stat().st_mtime)}:{len(os.listdir(ex))}")
    except OSError:
        pass
    return "|".join(parts)


def poster(pdir: Path, refresh: bool = False, duration: float | None = None) -> Path | None:
    """thumb.jpg: a 320 px frame a third in (max 2 s), from the raw video or the newest export.
    `duration`: the length when the caller knows it before events.json is written."""
    pdir = Path(pdir)
    out = pdir / "thumb.jpg"
    if out.exists() and not refresh:
        return out
    src = projmod.Project(pdir).raw
    if not src.exists():
        exps = sorted((pdir / "exports").glob("*.mp4"), key=lambda p: p.stat().st_mtime) if (pdir / "exports").is_dir() else []
        src = exps[-1] if exps else None
    if not src:
        return None
    try:
        from .render import ffmpeg_bin
        dur = 3.0
        try:
            dur = float(duration) if duration else float(fsutil.read_json(pdir / "events.json")["video"]["duration"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        t = max(0.0, min(2.0, dur / 3))
        tmp = out.with_suffix(".tmp.jpg")
        subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=320:-2", "-q:v", "4", str(tmp)], capture_output=True, timeout=30)
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            return out
    except Exception:
        pass
    return None


def entry(d: Path, with_poster: bool = True) -> dict | None:
    d = Path(d)
    if not d.is_dir() or d.name.startswith("."):
        return None
    proj = projmod.Project(d)
    meta = {}
    try:
        meta = fsutil.read_json(d / "capture.json")
    except (OSError, ValueError):
        pass
    raw = proj.raw
    if not raw.exists() and not meta and not (d / "exports").is_dir():
        return None
    data = {}
    try:
        data = fsutil.read_json(proj.file)
    except (OSError, ValueError):            # fsutil.DamagedFile is a ValueError
        pass
    duration = 0.0
    finalized = (d / "events.json").exists()
    try:
        duration = float(fsutil.read_json(d / "events.json")["video"]["duration"])
    except (OSError, ValueError, KeyError):
        pass
    exports = []
    if (d / "exports").is_dir():
        for f in sorted((d / "exports").iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.suffix.lower() in VIDEO_EXT and ".tmp" not in f.name:
                exports.append({"name": f.name, "path": str(f), "bytes": f.stat().st_size,
                                "vertical": "-9x16" in f.name or "-shorts" in f.name or "vertical" in f.name,
                                "square": "-1x1" in f.name, "gif": f.suffix.lower() == ".gif"})
    shares = []
    try:
        shares = json.loads((d / "shares.json").read_text())
    except (OSError, ValueError):
        pass
    created = meta.get("started")
    if not created:
        try:
            created = time.mktime(time.strptime(d.name[:19], "%Y-%m-%d_%H-%M-%S"))
        except ValueError:
            created = d.stat().st_mtime
    reason = meta.get("stop_reason")
    title = (data.get("title") or "").strip()
    thumb = poster(d) if with_poster else (d / "thumb.jpg" if (d / "thumb.jpg").exists() else None)
    return {
        "dir": str(d), "id": d.name, "title": title or default_title(d.name, meta),
        "named": bool(title), "created": created, "duration": round(duration, 2),
        "bytes": _du(d), "raw_bytes": raw.stat().st_size if raw.exists() else 0, "raw": raw.exists(),
        "exports": exports, "exported": bool(exports),
        "shared": any(s.get("action") in ("upload", "copy") for s in shares if isinstance(s, dict)),
        "links": [s.get("url") for s in shares if isinstance(s, dict) and s.get("url") and not s.get("deleted")],
        "captions": (d / "captions.srt").exists(),
        "vertical": any(e["vertical"] for e in exports),
        "recovered": reason == "recovered" or (not finalized and raw.exists()),
        "partial": reason in ("recorder-died", "disk-full", "suspend", "lock", "monitor-removed", "hyprland-exit"),
        "stop_reason": reason, "replay": d.name.endswith("-replay"), "mode": meta.get("mode", "screen"),
        "monitor": meta.get("monitor"), "thumb": str(thumb) if thumb else None, "sig": _sig(d),
    }


def default_title(folder: str, meta: dict) -> str:
    try:
        t = time.strptime(folder[:19], "%Y-%m-%d_%H-%M-%S")
        label = time.strftime("%b %-d, %H:%M", t)
    except ValueError:
        label = folder
    return ("Replay · " if folder.endswith("-replay") else "") + label


def _load_cache() -> dict:
    try:
        c = json.loads(cache_path().read_text())
        if c.get("version") == INDEX_VERSION:
            return c
    except (OSError, ValueError):
        pass
    return {"version": INDEX_VERSION, "items": []}


def _write_atomic(p: Path, data: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write_json(p, data, durable=False)     # a cache: rebuilt if lost


def index(refresh: bool = True, posters: bool = True) -> dict:
    """Scan, reuse unchanged entries, write the cache. Newest first."""
    root = projmod.videos_root()
    cache = _load_cache()
    old = {e["dir"]: e for e in cache.get("items", [])}
    items = []
    live = _live_dir()
    if root.is_dir():
        for d in root.iterdir():
            if not d.is_dir() or d.name.startswith(".") or (live and d.resolve() == live):
                continue                    # the recording in progress is not a (recovered) recording
            prev = old.get(str(d))
            if prev and not refresh:
                items.append(prev)
                continue
            if prev and prev.get("sig") == _sig(d) and (prev.get("thumb") or not posters):
                items.append(prev)
                continue
            e = entry(d, with_poster=posters)
            if e:
                items.append(e)
    # newest first; same start time (copied/imported projects, test fixtures) falls back to the
    # folder name, which is the start timestamp - never the filesystem's directory order (VM, Nov 10)
    items.sort(key=lambda e: (e["created"] or 0, e["id"]), reverse=True)
    # display text for every size (one rule for the bar panel, the editor and the CLI:
    # dolly.units.human_size); added here so cached entries get it too
    from .units import human_size
    for e in items:
        e["size_text"] = human_size(e.get("bytes"))
        for x in e.get("exports") or []:
            x["size_text"] = human_size(x.get("bytes"))
    total = sum(e["bytes"] for e in items)
    cleanable = sum(e["raw_bytes"] for e in items if e["exported"] and e["raw"])
    free = _free(root)
    out = {"version": INDEX_VERSION, "root": str(root), "updated": time.time(), "items": items,
           "total_bytes": total, "raw_bytes": sum(e["raw_bytes"] for e in items),
           "cleanable_bytes": cleanable, "free_bytes": free,
           "total_text": human_size(total), "cleanable_text": human_size(cleanable),
           "free_text": human_size(free) if free is not None else None}
    try:
        _write_atomic(cache_path(), out)
    except OSError:
        pass
    return out


def _live_dir() -> Path | None:
    try:
        from .capture import read_state
        st = read_state()
        return Path(st["dir"]).resolve() if st else None
    except Exception:
        return None


def _free(root: Path) -> int | None:
    from .health import disk_free_bytes
    return disk_free_bytes(root)


def find(ident: str) -> Path:
    """A recording by folder name, path or title."""
    p = Path(ident).expanduser()
    if p.is_dir():
        return p
    root = projmod.videos_root()
    if (root / ident).is_dir():
        return root / ident
    for e in index(refresh=False, posters=False)["items"]:
        if e["title"] == ident:
            return Path(e["dir"])
    raise FileNotFoundError(f"no recording {ident!r} in {root}")


# ------------------------------------------------------------------ rename
def rename(pdir: Path, title: str) -> dict:
    title = " ".join(str(title).split())[:120]
    proj = projmod.Project(pdir)
    data = fsutil.read_json(proj.file) if proj.file.exists() else {}
    data["title"] = title
    fsutil.atomic_write_json(proj.file, data, indent=2, backup=True)
    return entry(Path(pdir), with_poster=False)


# ------------------------------------------------------------------ trash (freedesktop spec) + undo
def trash_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "Trash"


def _trash_one(p: Path) -> dict:
    p = Path(p).resolve()
    td = trash_dir()
    (td / "files").mkdir(parents=True, exist_ok=True)
    (td / "info").mkdir(parents=True, exist_ok=True)
    base, n = p.name, 1
    name = base
    while (td / "files" / name).exists() or (td / "info" / f"{name}.trashinfo").exists():
        n += 1
        name = f"{base}.{n}"
    info = td / "info" / f"{name}.trashinfo"
    info.write_text("[Trash Info]\nPath=" + urllib.parse.quote(str(p)) +
                    "\nDeletionDate=" + time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
    try:
        shutil.move(str(p), str(td / "files" / name))
    except Exception:
        info.unlink(missing_ok=True)
        raise
    return {"from": str(p), "trashed": str(td / "files" / name), "info": str(info)}


def _undo_file() -> Path:
    return state_dir() / "undo.json"


def trash(pdirs: list) -> dict:
    """Move recordings to the trash; remembers them for `undo()`."""
    moved = []
    root = projmod.videos_root().resolve()
    for d in pdirs:
        d = Path(d).resolve()
        if not d.is_dir() or root not in d.parents:
            raise ValueError(f"{d} is not a recording in {root}")
        moved.append(_trash_one(d))
    state_dir().mkdir(parents=True, exist_ok=True)
    _write_atomic(_undo_file(), {"time": time.time(), "items": moved})
    index(refresh=False, posters=False)
    _refresh_last()
    return {"trashed": len(moved), "items": moved}


def _refresh_last():
    from .guards import set_last
    try:
        set_last(projmod.latest_project())
    except OSError:
        pass


def undo() -> dict:
    try:
        u = json.loads(_undo_file().read_text())
    except (OSError, ValueError):
        return {"restored": 0}
    n = 0
    for it in u.get("items", []):
        src, dst = Path(it["trashed"]), Path(it["from"])
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            Path(it["info"]).unlink(missing_ok=True)
            n += 1
    _undo_file().unlink(missing_ok=True)
    index(refresh=True, posters=False)
    _refresh_last()
    return {"restored": n}


def trash_raw(pdir: Path) -> int:
    """Raw capture files of one recording to the trash (keeps exports, events, project)."""
    freed = 0
    pdir = Path(pdir)
    for name in ("raw.mkv", "raw.mp4", "webcam.mkv"):
        f = pdir / name
        if f.exists():
            freed += f.stat().st_size
            _trash_one(f)
    if freed:
        try:
            proj = projmod.Project(pdir)
            data = fsutil.read_json(proj.file) if proj.file.exists() else {}
            data["raw_removed"] = time.time()
            fsutil.atomic_write_json(proj.file, data, indent=2, backup=True)
        except (OSError, ValueError):
            pass
    return freed


def cleanup_candidates(older_than_days: float = 0) -> list[dict]:
    now = time.time()
    return [e for e in index(refresh=True, posters=False)["items"]
            if e["exported"] and e["raw"] and now - (e["created"] or now) >= older_than_days * 86400]


def cleanup(older_than_days: float = 0, dry_run: bool = False) -> dict:
    cands = cleanup_candidates(older_than_days)
    freed = 0
    if not dry_run:
        for e in cands:
            freed += trash_raw(Path(e["dir"]))
        index(refresh=True, posters=False)
    else:
        freed = sum(e["raw_bytes"] for e in cands)
    return {"recordings": len(cands), "bytes": freed, "dry_run": dry_run, "dirs": [e["dir"] for e in cands]}


def auto_cleanup() -> dict | None:
    """Settings > Storage > clean up after N days (run after each recording)."""
    try:
        from .config import load
        days = load()["storage"]["cleanup_days"]
    except Exception:
        return None
    if not days:
        return None
    return cleanup(days)


# ------------------------------------------------------------------ desktop actions
def reveal(path: Path) -> list[str]:
    """Files (nautilus, Omarchy's file manager) with the item selected; else xdg-open."""
    path = Path(path)
    if shutil.which("nautilus"):
        cmd = ["nautilus", "--select", str(path)]
        if shutil.which("uwsm-app"):
            cmd = ["uwsm-app", "--", *cmd]
        return cmd
    return ["xdg-open", str(path if path.is_dir() else path.parent)]


def best_file(pdir: Path) -> Path | None:
    """What "Copy video" / "Share" use: the newest 16:9 MP4 export, else the raw capture."""
    e = entry(Path(pdir), with_poster=False)
    if not e:
        return None
    mp4 = [x for x in e["exports"] if x["name"].endswith(".mp4") and not x["vertical"] and not x["square"]]
    if mp4:
        return Path(mp4[0]["path"])
    if e["exports"]:
        return Path(e["exports"][0]["path"])
    raw = projmod.Project(pdir).raw
    return raw if raw.exists() else None

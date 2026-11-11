"""Share flow: platform presets -> size-capped export -> clipboard / link -> history.

    share_preset(project, "github")  -> exports/<name>-github.mp4 (<= 10 MB)
    copy_to_clipboard(file)           -> file on the Wayland clipboard (Ctrl+V attaches it)
    upload(file, target)              -> URL on the user's own storage (copied too)
    history(project) / delete_remote(project, share_id)
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from .. import project as projmod
from . import clipboard, config, encode, presets, targets

MB = 1_000_000


# ------------------------------------------------------------------ history
def _global_log() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    p = Path(base) / "dolly" / "shares.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def history(project_dir) -> list[dict]:
    f = Path(project_dir) / "shares.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return []


def _save_history(project_dir, items):
    f = Path(project_dir) / "shares.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.replace(f)


def record(project_dir, entry: dict) -> dict:
    entry = {"id": secrets.token_hex(4), "time": time.time(), "project": Path(project_dir).name, **entry}
    items = history(project_dir)
    items.append(entry)
    _save_history(project_dir, items)
    with open(_global_log(), "a") as g:
        g.write(json.dumps({**entry, "project_dir": str(project_dir)}) + "\n")
    return entry


# ------------------------------------------------------------------ export for a preset
def output_duration(project_dir) -> float:
    from ..render import plan_timing
    return plan_timing(project_dir)["out_duration"]


def estimate_all(project_dir) -> list[dict]:
    dur = output_duration(project_dir)
    from ..render import probe
    audio = any(s.get("codec_type") == "audio" for s in probe(projmod.Project(project_dir).raw).get("streams", []))
    out = []
    for p in presets.all_presets():
        est = encode.estimate(p, dur, _cached_master(project_dir, p), audio)
        out.append({**p, "estimate": est, "duration": dur})
    return out


def _master_key(project_dir, p: dict) -> str:
    proj = projmod.Project(project_dir)
    h = hashlib.sha256()
    h.update(proj.file.read_bytes() if proj.file.exists() else b"{}")
    h.update(str(proj.raw.stat().st_mtime).encode())
    h.update(json.dumps([p["aspect"], p["width"], p["height"], p["fps"]]).encode())
    return h.hexdigest()[:16]


def _cached_master(project_dir, p) -> Path | None:
    f = projmod.Project(project_dir).exports / ".cache" / f"master-{_master_key(project_dir, p)}.mp4"
    return f if f.exists() else None


def render_master(project_dir, p: dict, progress=None) -> Path:
    """High-quality intermediate at the preset's aspect/size/fps (cached)."""
    from ..render import export
    from .. import analysis
    analysis.ensure(project_dir)             # never render a take whose zoom plan is still being worked out
    proj = projmod.Project(project_dir)
    cache = proj.exports / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"master-{_master_key(project_dir, p)}.mp4"
    if f.exists():
        return f
    for old in cache.glob("master-*.mp4"):  # keep the cache small
        if time.time() - old.stat().st_mtime > 86400:
            old.unlink(missing_ok=True)
    over = {"output": {"width": p["width"], "height": p["height"], "fps": p["fps"]}}
    export(project_dir, f.with_suffix(".part.mp4"), "mp4", "master", progress=progress, style_over=over,
           aspect=p["aspect"])
    f.with_suffix(".part.mp4").replace(f)
    return f


def share_preset(project_dir, preset_id: str, progress=None, log=print) -> dict:
    p = presets.get(preset_id)
    proj = projmod.Project(project_dir)
    master = render_master(project_dir, p, progress)
    out = proj.exports / f"{proj.name}-{preset_id}.{p['format']}"
    cap = int(p["cap_mb"] * MB) if p.get("cap_mb") else None
    if p["format"] == "gif":
        rep = encode.gif_under_cap(master, out, cap, p["width"], p["fps"], log=log)
    elif cap:
        rep = encode.target_size_mp4(master, out, cap, p["width"], p["height"], p["fps"], log=log)
    else:
        crf = 16 if p.get("quality") == "best" else 20
        encode.crf_encode(master, out, p["width"], p["height"], p["fps"], crf=crf,
                          preset="slow" if p.get("quality") == "best" else "medium", audio_kbps=192)
        rep = {"method": "crf", "crf": crf, "size": out.stat().st_size}
    entry = record(project_dir, {"action": "export", "preset": preset_id, "file": str(out),
                                 "size": out.stat().st_size, "target": "local", "report": rep})
    return {"file": out, "report": rep, "entry": entry, "preset": p}


# ------------------------------------------------------------------ clipboard / link
def copy_to_clipboard(project_dir, file: Path, as_text=False) -> dict:
    if as_text:
        clipboard.copy_text(str(file))
    else:
        clipboard.copy_file(file)
    return record(project_dir, {"action": "copy-path" if as_text else "copy-file", "file": str(file),
                                "target": "clipboard"})


def upload(project_dir, file: Path, target_name: str | None = None, copy_link: bool = True) -> dict:
    t = targets.get_target(target_name)
    res = t.upload(Path(file))
    if copy_link and res.get("url"):
        try:
            clipboard.copy_text(res["url"])
            res["copied"] = True
        except clipboard.ClipboardUnavailable:
            res["copied"] = False
    return record(project_dir, {"action": "upload", "file": str(file), "target": t.name, "target_type": t.kind,
                                **{k: v for k, v in res.items() if k != "copied"}, "copied": res.get("copied")})


def delete_remote(project_dir, share_id: str) -> dict:
    items = history(project_dir)
    e = next((x for x in items if x["id"] == share_id), None)
    if not e or e.get("action") != "upload":
        raise KeyError(f"no upload with id {share_id}")
    if e.get("deleted"):
        return e
    if not e.get("deletable", True):
        raise targets.UploadError("this host does not support deleting; the link expires on its own")
    cfg = config.load()
    if e["target"] not in cfg["targets"]:
        raise config.ConfigError(f"target '{e['target']}' is no longer configured")
    targets.make(e["target"], cfg["targets"][e["target"]]).delete(e.get("remote_ref") or {})
    e["deleted"] = time.time()
    _save_history(project_dir, items)
    return e


def notify_export(project_dir, file: Path, title=None):
    """Omarchy notification with Open / Copy file / Get link / Edit actions."""
    from ..capture import notify
    from ..i18n import t
    title = title or t("notify.exported")
    dolly = str(Path(__file__).resolve().parents[2] / "bin" / "dolly")
    size = Path(file).stat().st_size / MB
    acts = [(t("notify.open"), "xdg-open", str(file)), (t("notify.copyFile"), dolly, "share", "copy", str(file))]
    try:
        if config.load()["targets"]:
            acts.append((t("notify.getLink"), dolly, "share", "upload", str(file)))
    except config.ConfigError:
        pass
    acts.append((t("notify.edit"), dolly, "edit", str(project_dir)))
    notify(title, t("notify.exportedDetails", Path(file).name, f"{size:.1f}"), *acts, timeout=12000)

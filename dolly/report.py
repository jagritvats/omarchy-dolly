"""`dolly doctor --report`: one text file to attach to a bug report.

What goes in: doctor's checks, versions, Dolly's settings, hotkey/menu status, the last
error, the tail of Dolly's log, and the recorder log + capture facts of the latest
recording. What never goes in: video, screenshots, typed keys or clicks (the event log
is only counted), sharing credentials. The home folder is written as "~".
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

TAIL_LINES = 150


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(unavailable: {e.__class__.__name__})"


def _tail(p: Path, n: int = TAIL_LINES) -> str:
    try:
        return "\n".join(p.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(none)"


def redact(text: str) -> str:
    """Home folder -> "~"; anything that looks like a credential in a key=value or JSON pair."""
    home = str(Path.home())
    text = text.replace(home, "~")
    user = os.environ.get("USER") or Path.home().name
    if user and len(user) > 2:
        text = re.sub(rf"(?<![\w-]){re.escape(user)}(?![\w-])", "<user>", text)
    text = re.sub(r'(?i)("?(?:\w*(?:secret|token|password|passwd|access_key|api_key)\w*)"?\s*[:=]\s*)("[^"]*"|\S+)',
                  r"\1<redacted>", text)
    # a link's query is where a signed link keeps its signature
    return re.sub(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]+", r"\1?<redacted>", text)


def _versions() -> list[str]:
    out = [f"python {platform.python_version()}  kernel {platform.release()}"]
    for label, cmd in (("omarchy", ["omarchy-version"]), ("hyprland", ["hyprctl", "version"]),
                       ("ffmpeg", ["ffmpeg", "-version"]), ("wf-recorder", ["wf-recorder", "--version"]),
                       ("gpu-screen-recorder", ["gpu-screen-recorder", "--version"]),
                       ("quickshell", ["qs", "--version"])):
        if shutil.which(cmd[0]):
            out.append(f"{label}: {(_run(cmd).splitlines() or ['?'])[0][:120]}")
        else:
            out.append(f"{label}: not installed")
    for mod in ("numpy", "PIL"):
        try:
            m = __import__(mod)
            out.append(f"python {mod}: {getattr(m, '__version__', '?')}")
        except Exception:
            out.append(f"python {mod}: missing")
    out.append("env: " + " ".join(f"{k}={os.environ.get(k, '')}" for k in
                                  ("XDG_CURRENT_DESKTOP", "QT_QUICK_BACKEND", "LIBGL_ALWAYS_SOFTWARE", "DOLLY_BACKEND")))
    return out


def _last_recording() -> list[str]:
    from . import project as projmod
    try:
        root = projmod.videos_root()
        dirs = sorted((d for d in root.iterdir() if d.is_dir() and (d / "capture.json").exists()), reverse=True)
    except OSError:
        return ["(no recordings folder)"]
    if not dirs:
        return ["(no recordings yet)"]
    d = dirs[0]
    out = [f"folder: {d.name}", "files: " + ", ".join(f"{f.name} ({f.stat().st_size // 1024} KB)"
                                                      for f in sorted(d.iterdir()) if f.is_file())]
    try:
        cap = json.loads((d / "capture.json").read_text())
        keep = ("backend", "mode", "fps", "scale", "monitor", "container", "cursor_baked", "audio_mode", "camera_mode")
        out.append("capture: " + json.dumps({k: cap.get(k) for k in keep}))
    except (OSError, ValueError):
        out.append("capture.json: unreadable")
    try:
        ev = json.loads((d / "events.json").read_text())
        counts: dict = {}
        for e in ev.get("events", []):
            counts[e.get("type")] = counts.get(e.get("type"), 0) + 1
        out.append(f"video: {json.dumps(ev.get('video'))}")
        out.append(f"sync: {json.dumps((ev.get('capture') or {}).get('sync'))}")
        out.append("event counts (contents are never included): " + json.dumps(counts, sort_keys=True))
    except (OSError, ValueError):
        out.append("events.json: missing or unreadable (not finalized?)")
    out += ["-- recorder.log (tail) --", _tail(d / "recorder.log", 60)]
    return out


def _stage(selftest: bool = False) -> list[str]:
    """The hidden stage: its self test (`dolly doctor --stage`, step by step), what it is set
    to, what is running or left behind, and the end of its compositor's log. The self test
    starts a stage for a few seconds unless one is running: only `dolly doctor --report`
    asks for it, and DOLLY_REPORT_STAGE=0 leaves it out there too."""
    from . import stage
    out: list[str] = []
    if not selftest or os.environ.get("DOLLY_REPORT_STAGE", "1") == "0":
        out.append("self test: not run (dolly doctor --stage runs it)")
    else:
        res = stage.selftest()
        out += ["self test (dolly doctor --stage):"] + stage.selftest_lines(res)
    out.append("settings: " + json.dumps(stage.settings()))
    st = stage.status()
    keep = ("running", "size", "minutes_left", "memory_mb", "sound", "look", "background", "background_note",
            "recording", "leftovers")
    out.append("status: " + json.dumps({k: st.get(k) for k in keep if k in st}))
    src = stage.wallpaper_source()
    out.append("desktop picture: " + (src.name if src else "none found"))
    out.append("sessions: " + json.dumps([{"sig": i["sig"][-10:], "alive": i["alive"], "stage": i["stage"], "wl": i["wl"]}
                                          for i in stage.instances()]))
    out += ["-- stage.log (what the stage's compositor said; its starts, warnings and errors) --",
            _stage_log(stage.base_dir() / "stage.log")]
    return out


def _stage_log(p: Path, n: int = 40) -> str:
    """The stage compositor's log without its welcome picture: the lines Dolly wrote at each
    start, and what Hyprland marked as a warning or an error."""
    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError:
        return "(none)"
    keep = [re.sub(r"\x1b\[[0-9;]*m", "", l).rstrip() for l in lines
            if l.startswith("===") or re.search(r"\b(ERR|ERROR|CRIT|CRITICAL|WARN|WARNING)\b|rror:|failed|cannot|could not", l)
            and "start-hyprland" not in l]          # (said at every start of a second Hyprland: expected)
    return "\n".join(l[:300] for l in keep[-n:]) or "(nothing but start-up lines)"


def build(stage_test: bool = False) -> str:
    from . import doctor, log
    parts: list[str] = [f"Dolly report  {time.strftime('%Y-%m-%d %H:%M:%S %z')}"]
    try:
        man = json.loads((Path(__file__).resolve().parent.parent / "manifest.json").read_text())
        parts.append(f"plugin: {man.get('id')} {man.get('version')}")
    except (OSError, ValueError):
        pass

    def section(title: str, lines):
        parts.append(f"\n== {title} ==")
        parts.extend(lines if isinstance(lines, list) else [lines])

    try:
        rep = doctor.check()
        section("doctor", [f"[{c['level']:5s}] {c['name']}" + (f"  - {c['detail']}" if c["detail"] else "")
                           for c in rep["checks"]] + [f"ready: {rep['ok']}"]
                + [f"  {k}: {v}" for k, v in rep.get("paths", {}).items()])
    except Exception as e:                       # a broken doctor is itself the finding
        section("doctor", f"doctor failed: {e!r}")
    section("versions", _versions())
    try:
        from . import integration
        section("shortcuts and menu", json.dumps(integration.status(), indent=1))
    except Exception as e:
        section("shortcuts and menu", f"failed: {e!r}")
    try:
        from . import config
        section("settings", json.dumps(config.load(), indent=1, default=str))
    except Exception as e:
        section("settings", f"failed: {e!r}")
    try:
        section("latest recording", _last_recording())
    except Exception as e:
        section("latest recording", f"failed: {e!r}")
    try:
        section("hidden stage (beta)", _stage(stage_test))
    except Exception as e:
        section("hidden stage (beta)", f"failed: {e!r}")
    state = log.state_dir()
    section("last error", _tail(state / "last-error.log", 80))
    section("dolly.log (tail)", _tail(state / "dolly.log"))
    return redact("\n".join(parts)) + "\n"


def write(dest: Path | None = None, stage_test: bool = False) -> Path:
    text = build(stage_test)
    name = f"dolly-report-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    for folder in ([dest] if dest else []) + [Path.cwd(), Path.home()]:
        try:
            p = Path(folder) / name
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            return p
        except OSError:
            continue
    raise OSError("could not write the report to the current folder or your home folder")

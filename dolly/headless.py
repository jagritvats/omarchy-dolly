"""Recording without a person at the keyboard: for scripts and coding agents.

    dolly record --window kitty --for 20 --json     # no picker, no countdown, stops by itself
    dolly export --json                             # the clip, described as JSON

`--window`, `--monitor` and `--region` name the target instead of asking with a click;
`--for N` stops and finalizes after N seconds; `--wait` blocks until a recording stopped
some other way is finalized; `--json` prints exactly one JSON object on stdout (errors too,
with a non-zero exit) and never opens the editor.

An agent's shell (ssh, a service, a sandbox) often lacks the desktop session's variables,
so the session is looked up from $XDG_RUNTIME_DIR when they are missing.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sys
import time
from pathlib import Path

FLAGS = ("window", "monitor", "region", "duration", "wait", "json", "no_editor")


class HeadlessError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.extra = code, extra


def wanted(a) -> bool:
    return any(getattr(a, k, None) not in (None, False) for k in FLAGS)


def adopt_session_env(env=None) -> dict:
    """Fill in HYPRLAND_INSTANCE_SIGNATURE / WAYLAND_DISPLAY / the session bus from the
    runtime dir when the caller's environment does not have them. Returns what it set."""
    env = os.environ if env is None else env
    rt = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    got = {}
    if not rt.is_dir():
        return got
    if not env.get("XDG_RUNTIME_DIR"):
        got["XDG_RUNTIME_DIR"] = str(rt)
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        try:
            inst = sorted((p for p in (rt / "hypr").iterdir() if (p / ".socket.sock").exists()),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            inst = []
        if inst:
            got["HYPRLAND_INSTANCE_SIGNATURE"] = inst[0].name
    if not env.get("WAYLAND_DISPLAY"):
        socks = sorted(p.name for p in rt.glob("wayland-*") if not p.name.endswith(".lock"))
        if socks:
            got["WAYLAND_DISPLAY"] = socks[-1]
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and (rt / "bus").exists():
        got["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={rt / 'bus'}"
    env.update(got)
    return got


# ------------------------------------------------------------------ targets
def resolve_window(selector: str, clients: list[dict]) -> dict:
    """A Hyprland client for `selector`: "address:0x.." / "0x..", "class:NAME", "title:TEXT",
    "active", or bare text (class equal, then class contains, then title contains).
    Several matches: the most recently focused one."""
    sel = (selector or "").strip()
    low = sel.lower()
    live = [c for c in clients or [] if c.get("mapped", True) and not c.get("hidden")]

    def cls(c):
        return str(c.get("class") or c.get("initialClass") or "").lower()

    def title(c):
        return str(c.get("title") or "").lower()
    kind, sep, val = low.partition(":")
    if low.startswith("0x") or (sep and kind == "address"):
        addr = val if kind == "address" else low
        m = [c for c in live if str(c.get("address", "")).lower() == addr]
    elif sep and kind == "class":
        m = [c for c in live if val and val in cls(c)]
    elif sep and kind == "title":
        m = [c for c in live if val and val in title(c)]
    elif low in ("active", "focused"):
        m = sorted(live, key=lambda c: c.get("focusHistoryID", 1 << 30))[:1]
    else:
        m = ([c for c in live if cls(c) == low] or [c for c in live if low and low in cls(c)]
             or [c for c in live if low and low in title(c)])
    if not m:
        raise HeadlessError("window-not-found", f"no window matches {selector!r}",
                            windows=[{"class": c.get("class"), "title": str(c.get("title", ""))[:60],
                                      "address": c.get("address")} for c in live][:20])
    return sorted(m, key=lambda c: c.get("focusHistoryID", 1 << 30))[0]


def parse_region(text: str) -> str:
    """"X,Y,WxH" or "X,Y WxH" -> the picker's "X,Y WxH"."""
    import re
    m = re.fullmatch(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*[, ]\s*(\d+)\s*x\s*(\d+)\s*", text or "")
    if not m or int(m.group(3)) < 16 or int(m.group(4)) < 16:
        raise HeadlessError("bad-region", f"--region wants X,Y,WxH (at least 16x16), got {text!r}")
    return f"{m.group(1)},{m.group(2)} {m.group(3)}x{m.group(4)}"


def _target(a) -> dict:
    """Set a.mode / a.pick (and a.window_address) from the flags; -> description for the JSON."""
    from . import capture
    from .monitors import SpanError, resolve_pick
    given = [k for k in ("window", "monitor", "region") if getattr(a, k, None)]
    if len(given) > 1:
        raise HeadlessError("bad-arguments", "give one of --window, --monitor, --region")
    if not given:
        if a.mode in (None, "screen"):
            a.mode = "screen"                       # the focused monitor, no picker
            return {"kind": "screen"}
        raise HeadlessError("needs-target", f"--mode {a.mode} asks with a click; name the target with "
                            "--window or --region (or use --mode screen)")
    mons = capture.hypr_json("monitors")
    if not mons:
        raise HeadlessError("no-compositor", "cannot talk to Hyprland (is this an Omarchy session?)")
    if a.window:
        c = resolve_window(a.window, capture.hypr_json("clients") or [])
        from . import hypr
        hypr.focus(c["address"])                    # a window you cannot see is not worth recording
        time.sleep(0.35)
        c = next((x for x in capture.hypr_json("clients") or [] if x.get("address") == c["address"]), c)
        (x, y), (w, h) = c.get("at", (0, 0)), c.get("size", (0, 0))
        a.mode, a.pick, a.window_address = "window", f"{x},{y} {w}x{h}", c["address"]
        d = {"kind": "window", "address": c["address"], "class": c.get("class"),
             "title": str(c.get("title", ""))[:120]}
        note = off_monitor_note(c, mons)
        if note:
            d["note"] = note
        return d
    if a.monitor:
        if not any(m.get("name") == a.monitor and not m.get("disabled") for m in mons):
            raise HeadlessError("monitor-not-found", f"no monitor named {a.monitor!r}",
                                monitors=[m.get("name") for m in mons])
        a.mode, a.pick = "screen", f"monitor:{a.monitor}"
        return {"kind": "monitor", "name": a.monitor}
    a.mode, a.pick = "region", parse_region(a.region)
    try:
        tg = resolve_pick(a.pick, mons)
    except SpanError as e:
        raise HeadlessError("bad-region", str(e)) from None
    d = {"kind": "region", "rect": tg.region, "monitor": tg.monitor}
    if tg.note:
        d["note"] = tg.note
    return d


def off_monitor_note(client: dict, mons: list) -> str:
    """One recording is one monitor: say so when part of the window is somewhere else."""
    from .monitors import logical_rect
    m = next((m for m in mons if m.get("id") == client.get("monitor")), None)
    if not m:
        return ""
    mx, my, mw, mh = logical_rect(m)
    (x, y), (w, h) = client.get("at", (0, 0)), client.get("size", (0, 0))
    if w <= 0 or h <= 0:
        return ""
    ix = max(0.0, min(x + w, mx + mw) - max(x, mx)) * max(0.0, min(y + h, my + mh) - max(y, my))
    if ix >= 0.995 * w * h:
        return ""
    return f"part of the window is outside {m.get('name')}; only the part on that monitor is recorded"


# ------------------------------------------------------------------ results
def summary(pdir: Path, target: dict | None = None) -> dict:
    pdir = Path(pdir)
    doc, proj = {}, {}
    with contextlib.suppress(OSError, ValueError):
        doc = json.loads((pdir / "events.json").read_text())
    with contextlib.suppress(OSError, ValueError):
        proj = json.loads((pdir / "project.json").read_text())
    v, cap = doc.get("video", {}), doc.get("capture", {})
    raw = next((p for p in (pdir / "raw.mkv", pdir / "raw.mp4") if p.exists()), None)
    meta = {}
    with contextlib.suppress(OSError, ValueError):
        meta = json.loads((pdir / "capture.json").read_text())
    warnings = list(meta.get("audio_warnings") or [])
    sync = (cap.get("sync") or {}).get("method")
    if sync in (None, "none"):
        warnings.append("no sync marker or clock estimate: events are timed from the logger start")
    if meta.get("stop_reason"):
        warnings.append(f"stopped early: {meta['stop_reason']}")
    with contextlib.suppress(Exception):
        from . import deps
        lacking = deps.python_missing()
        if lacking:
            warnings.append(f"no styled export here: it needs {deps.how(lacking)}; "
                            "`dolly export` gives the plain recording until then")
    if (target or {}).get("note"):
        warnings.append(target["note"])
    return {"ok": True, "recording": False, "dir": str(pdir), "raw": str(raw) if raw else None,
            "events": str(pdir / "events.json"), "project": str(pdir / "project.json"),
            "duration": round(float(v.get("duration") or 0), 3), "trim": proj.get("trim"),
            "width": v.get("w"), "height": v.get("h"), "fps": v.get("fps"),
            "backend": cap.get("backend"), "sync": sync, "zooms": len(proj.get("segments") or []),
            "blurs": len(proj.get("privacy") or []), "target": target or {"kind": cap.get("mode")},
            "warnings": warnings}


def classify(message: str) -> str:
    m = (message or "").lower()
    for needle, code in ((" is already recording; stop it first", "other-recorder"),
                         ("already recording", "already-recording"),
                         ("missing packages", "missing-packages"),
                         ("cannot record into", "folder-not-writable"),
                         ("cannot talk to hyprland", "no-compositor"),
                         (" free in ", "disk-full"), ("no screen recorder", "recorder-failed")):
        if needle in m:
            return code
    return "failed"


def _emit(obj: dict):
    sys.__stdout__.write(json.dumps(obj) + "\n")
    sys.__stdout__.flush()


def fail(code: str, message: str, **extra) -> "SystemExit":
    _emit({"ok": False, "error": code, "message": message, **extra})
    return SystemExit(1)


# ------------------------------------------------------------------ record
def _record(a) -> dict:
    from . import capture
    from .cli import apply_record_defaults
    if a.countdown is None:
        a.countdown = 0                              # nobody is watching a countdown
    target = _target(a)
    capture.start(apply_record_defaults(a))
    st = capture.read_state()
    if not st:
        raise HeadlessError("not-started", "the recording did not start; run `dolly doctor`")
    pdir = Path(st["dir"])
    _wait_ready(pdir)                                # N seconds of clip, not N seconds minus start-up
    if a.duration:
        stop_now = {"flag": False}

        def on_signal(_s, _f):
            stop_now["flag"] = True
        old = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            t_end = time.monotonic() + float(a.duration)
            while time.monotonic() < t_end and not stop_now["flag"] and capture.read_state():
                time.sleep(min(0.1, max(0.0, t_end - time.monotonic())))
        finally:
            for s, h in old.items():
                signal.signal(s, h)
        if capture.read_state():
            capture.stop(None, defer=False)          # finalizes, plan included; the editor stays closed in --json
        else:
            _wait_finalized(pdir)                    # a watchdog or the hotkey stopped it first
        return summary(pdir, target)
    if a.wait:
        while capture.read_state():
            time.sleep(0.2)
        _wait_finalized(pdir)
        return summary(pdir, target)
    return {"ok": True, "recording": True, "dir": str(pdir), "backend": st.get("backend"),
            "target": target, "stop": "dolly stop"}


def _wait_ready(pdir: Path, timeout: float = 12.0):
    """Until the event logger is running and has marked the start (the recorder and the
    logger take a few seconds to come up; that lead-in is trimmed from the clip)."""
    log, t_end, seen_start = pdir / "events.raw.jsonl", time.monotonic() + timeout, None
    while time.monotonic() < t_end:
        try:
            text = log.read_text(errors="replace")
        except OSError:
            text = ""
        if '"type": "sync"' in text or '"type":"sync"' in text:
            return
        if seen_start is None and '"start"' in text:
            seen_start = time.monotonic()
        if seen_start is not None and time.monotonic() - seen_start > 2.0:
            return                                   # no sync marker on this system: the logger is up
        time.sleep(0.05)


def stop_json(a) -> None:
    """`dolly stop --json`: stop, finalize, describe the recording."""
    from . import capture
    adopt_session_env()
    os.environ["DOLLY_OPEN_EDITOR"] = "0"
    st = capture.read_state()
    if not st:
        raise fail("not-recording", "nothing is being recorded")
    counting = capture._counting_down(st)        # stopping a countdown cancels: there is no take to describe
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            capture.stop(a, defer=False)             # the answer reports the plan: wait for it
    except SystemExit as e:
        if e.code not in (0, None):
            msg = e.code if isinstance(e.code, str) else "nothing was recorded; see recorder.log in the folder"
            raise fail("stop-failed", msg, dir=st["dir"]) from None
    if counting:
        _emit({"ok": True, "recording": False, "cancelled": True,
               "message": "stopped during the countdown: nothing was recorded"})
        return
    _emit(summary(Path(st["dir"])))


def _wait_finalized(pdir: Path, timeout: float = 900.0):
    from . import health
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if (pdir / "events.json").exists() and health.get_phase().get("phase", "idle") == "idle":
            from . import analysis
            analysis.ensure(pdir)                    # whoever stopped it left the plan for later
            return
        if not pdir.exists():
            raise HeadlessError("discarded", "the recording was cancelled")
        time.sleep(0.25)
    raise HeadlessError("timeout", f"{pdir} was not finalized within {int(timeout)} s")


def record(a):
    """`dolly record` with any of the headless flags."""
    as_json = bool(getattr(a, "json", False))
    adopt_session_env()
    if as_json or getattr(a, "no_editor", False):
        os.environ["DOLLY_OPEN_EDITOR"] = "0"
    if not as_json:
        try:
            _record(a)                               # start/stop print the folder, as always
        except HeadlessError as e:
            raise SystemExit(f"dolly: {e}") from None
        return
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            res = _record(a)
    except HeadlessError as e:
        raise fail(e.code, str(e), **e.extra) from None
    except SystemExit as e:
        msg = e.code if isinstance(e.code, str) else "no screen recorder could start; run `dolly doctor`"
        raise fail(classify(msg), msg.removeprefix("dolly: ")) from None
    except Exception as e:                           # still exactly one JSON object on stdout
        with contextlib.suppress(Exception):
            import logging
            logging.getLogger("dolly").exception("dolly record --json")
        raise fail("internal", f"{type(e).__name__}: {e}") from None
    _emit(res)


# ------------------------------------------------------------------ export
def export_json(run, a) -> None:
    """Run an export (run() -> output Path, progress on stderr) and describe the file."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            out = Path(run())
        from . import deps
        from .ff import probe
        plain = bool(getattr(a, "plain", False) or deps.python_missing())
        info = probe(out)
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        w, h = v.get("width"), v.get("height")
        _emit({"ok": True, "output": str(out), "bytes": out.stat().st_size,
               "duration": round(float((info.get("format") or {}).get("duration") or 0), 3),
               "width": w, "height": h, "aspect": getattr(a, "aspect", None) or _aspect(w, h),
               "format": out.suffix.lstrip("."), "engine": "plain" if plain else (getattr(a, "engine", None) or "compositor")})
    except SystemExit as e:
        msg = e.code if isinstance(e.code, str) else "export failed"
        raise fail("export-failed", msg.removeprefix("dolly: ")) from None
    except Exception as e:
        from .project import EmptyTrim
        raise fail("export-failed", str(e) if isinstance(e, EmptyTrim) else f"{type(e).__name__}: {e}") from None


def _aspect(w, h) -> str | None:
    if not w or not h:
        return None
    r = w / h
    return min((("16:9", 16 / 9), ("9:16", 9 / 16), ("1:1", 1.0), ("4:5", 0.8)), key=lambda p: abs(p[1] - r))[0]

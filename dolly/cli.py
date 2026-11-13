"""dolly command line.

    dolly record [--mode screen|region|window] [--mic] [--desktop-audio] [--no-keys] [--fps 60]
    dolly stop | toggle | status [--json]
    dolly finalize DIR            raw JSONL -> events.json (+ auto segments)
    dolly auto DIR                (re)generate auto-zoom segments into project.json
    dolly export [DIR] [--gif] [--quality fast|high|best] [-o OUT]
    dolly edit [DIR]              open the editor (local web UI) on DIR or the last recording
    dolly last                    print the last recording dir
    dolly doctor [--json]         check prerequisites; report capture/encode paths
    dolly demo [OUT]              synthetic end-to-end demo (no Hyprland needed)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import project as projmod
from .i18n import t


def _dir_or_last(d):
    if d:
        return Path(d).expanduser().resolve()
    last = projmod.latest_project()
    if not last:
        sys.exit("dolly: no recordings yet (looked in %s)" % projmod.videos_root())
    return last


def cmd_auto(a):
    from .events import Timeline
    from .zoom import ZoomConfig, build_segments
    proj = projmod.Project(_dir_or_last(a.dir))
    from . import analysis
    analysis.ensure(proj.dir)
    data = proj.load()
    tl = Timeline.load(proj.events)
    zcfg = ZoomConfig.from_dict(data.get("zoom_config"))
    from .capture import _from_go
    segs = _from_go(build_segments(tl, zcfg), (tl.capture or {}).get("go"))   # a recorded countdown is not the take
    data["segments"] = [s.to_dict() for s in segs]
    proj.save(data)
    for s in segs:
        print(f"  {s.id}: {s.start:6.2f}-{s.end:6.2f}s  {s.zoom:.2f}x  {s.reason}")
    print(f"{len(segs)} zoom segments -> {proj.file}")


def _project_of(file: Path) -> Path:
    for p in [file.parent, *file.parents]:
        if (p / "events.json").exists():
            return p
    raise SystemExit(f"dolly: {file} is not inside a Dolly recording folder")


def cmd_share(a):
    from . import share
    from .share import config as scfg
    act = a.share_cmd
    if act == "list":
        d = _dir_or_last(a.dir)
        for p in share.estimate_all(d):
            e = p["estimate"]
            cap = f"cap {p['cap_mb']} MB" if p.get("cap_mb") else "no cap"
            flag = "fits" if e["fits"] else "squeezed to fit"
            if e["over_duration"]:
                flag += f", TOO LONG (max {p['max_duration']} s)"
            print(f"  {p['id']:11s} {p['format']:4s} {p['aspect']:5s} {p['width']}x{p['height']}@{p['fps']:<3} "
                  f"≈{e['bytes'] / 1e6:6.1f} MB  {cap:12s} {flag}")
    elif act == "export":
        from .render import cli_progress
        d = _dir_or_last(a.dir)
        r = share.share_preset(d, a.preset, progress=cli_progress, log=lambda m: print(m, file=sys.stderr))
        f = r["file"]
        print(f"{f}  ({f.stat().st_size / 1e6:.2f} MB, {r['report']['method']})")
        if a.copy:
            share.copy_to_clipboard(d, f)
            print("copied to clipboard as a file")
        if a.upload is not None:
            e = share.upload(d, f, a.upload or None)
            print(e.get("url") or e.get("note"))
        if not a.copy and a.upload is None:
            share.notify_export(d, f, t("notify.exportedFor", r["preset"]["label"]))
    elif act == "copy":
        f = Path(a.file).resolve()
        share.copy_to_clipboard(_project_of(f), f, as_text=a.path)
        print("copied path" if a.path else "copied file (paste with Ctrl+V)")
    elif act == "upload":
        f = Path(a.file).resolve()
        e = share.upload(_project_of(f), f, a.target, copy_link=not a.no_copy)
        print(e.get("url") or e.get("note"))
        if e.get("url") and e.get("copied"):
            from .capture import notify

            notify(t("notify.linkCopied"), e["url"], (t("notify.open"), "xdg-open", e["url"]), timeout=8000)
    elif act == "targets":
        cfg = scfg.load()
        if not cfg["targets"]:
            print(f"local-only (no targets). Template: dolly share init -> {scfg.config_path()}")
        for n, tgt in cfg["targets"].items():
            print(f"  {n:12s} {tgt['type']:9s} {'(default)' if n == cfg['default'] else ''} "
                  f"{'PUBLIC temporary' if tgt['type'] == 'litterbox' else ''}")
    elif act == "init":
        print(scfg.init_template())
    elif act == "history":
        for e in share.history(_dir_or_last(a.dir)):
            when = time.strftime("%F %H:%M", time.localtime(e["time"]))
            extra = e.get("url") or e.get("file", "")
            print(f"  {e['id']} {when} {e['action']:9s} {e.get('preset') or '':10s} {e.get('target', ''):10s} "
                  f"{'DELETED ' if e.get('deleted') else ''}{extra}")
    elif act == "delete":
        e = share.delete_remote(_dir_or_last(a.dir), a.id)
        print(f"deleted remote copy of {Path(e['file']).name} from {e['target']}")


def cmd_library(a):
    """Recordings library: list / --json (cached index for the bar panel) / --open (editor
    Library view) / rename / trash / undo / cleanup / reveal / copy."""
    from . import library
    act = a.lib_cmd or ("open" if a.open else "list")
    if act == "open":
        from .editor.server import serve
        return serve(None, port=0, open_browser=not a.no_browser, view="library")
    if act == "list":
        if a.cached:
            try:
                idx = json.loads(library.cache_path().read_text())
            except (OSError, ValueError):
                idx = library.index(refresh=True, posters=not a.no_posters)
        else:
            idx = library.index(refresh=True, posters=not a.no_posters)
        items = idx["items"][: a.recent] if a.recent else idx["items"]
        if a.json:
            print(json.dumps({**idx, "items": items}))
            return
        for e in items:
            flags = " ".join(f for f, on in (("exported", e["exported"]), ("shared", e["shared"]),
                                             ("captions", e["captions"]), ("vertical", e["vertical"]),
                                             ("recovered", e["recovered"]), ("partial", e["partial"]),
                                             ("raw-cleaned", not e["raw"])) if on)
            print(f"{e['id']:28s} {e['duration']:6.1f}s {e.get('size_text') or '':>8s}  {e['title'][:40]:40s} {flags}")
            for u in e["links"]:
                print(f"    link: {u}")
        print(f"{len(idx['items'])} recordings, {idx['total_bytes'] / 1e9:.2f} GB "
              f"({idx['cleanable_bytes'] / 1e9:.2f} GB of raw files belong to exported recordings)")
        return
    if act == "rename":
        e = library.rename(library.find(a.target), " ".join(a.title))
        print(e["title"])
    elif act == "trash":
        r = library.trash([library.find(x) for x in a.targets])
        print(f"moved {r['trashed']} to the trash (dolly library undo restores)")
    elif act == "undo":
        print(f"restored {library.undo()['restored']}")
    elif act == "cleanup":
        r = library.cleanup(a.days, dry_run=a.dry_run)
        print(("would free" if a.dry_run else "freed") + f" {r['bytes'] / 1e6:.0f} MB from {r['recordings']} exported recordings")
    elif act == "reveal":
        import subprocess as sp
        d = library.find(a.target)
        sp.Popen(library.reveal(library.best_file(d) or d), start_new_session=True)
    elif act == "copy":
        from . import share
        d = library.find(a.target)
        f = library.best_file(d)
        if not f:
            raise SystemExit("nothing to copy yet")
        share.copy_to_clipboard(d, f)
        print(f"copied {f.name}")
    elif act == "poster":
        print(library.poster(library.find(a.target), refresh=True) or "")
    elif act == "scrub-keys":
        if a.once:
            try:
                os.nice(10)                         # started by the session: stay out of the way
            except OSError:
                pass
        r = library.scrub_typed_keys(once=a.once, folder=a.folder)
        if r is not None:
            print(f"removed typed text from {r['recordings']} recordings in {r['root']} "
                  f"({r['keys']} key presses kept as times only)")
            if r["failed"]:
                print(f"{r['failed']} folders could not be rewritten (read-only, or the disk is full)")
                sys.exit(1)


def cmd_config(a):
    from . import config as cfgmod
    if a.cfg_cmd == "get":
        print(json.dumps(cfgmod.get(a.key)) if a.key else json.dumps(cfgmod.load(), indent=2))
    elif a.cfg_cmd == "set":
        ups = {}
        for kv in a.pairs:
            k, _, v = kv.partition("=")
            ups[k] = v
        try:
            if a.from_bar:              # bar widget keys -> config keys; do not echo back
                ups = {cfgmod.BAR_KEYS.get(k, k): v for k, v in ups.items()}
            cfgmod.set_many(ups, sync_bar=not a.from_bar)
        except cfgmod.ConfigError as e:
            raise SystemExit(f"dolly: {e}") from e
        print("saved " + ", ".join(ups))
    elif a.cfg_cmd == "bar":
        print(json.dumps(cfgmod.bar_view()))
    elif a.cfg_cmd == "path":
        print(cfgmod.path())
    elif a.cfg_cmd == "schema":
        print(json.dumps(cfgmod.describe(), indent=2))


def cmd_panel(a):
    """Everything the bar panel shows, as one JSON object (the panel used to start six
    commands every time it opened). A part that fails is null and named under "errors"."""
    from concurrent.futures import ThreadPoolExecutor

    def config():
        from . import config as cfgmod
        return cfgmod.load()

    def recent():
        from . import library
        return library.index(refresh=True, posters=True)["items"][:5]

    def audio():
        from . import audio as audiomod
        return audiomod.devices()

    def cameras():
        from . import camera
        return camera.devices()

    def integration():
        from . import integration as integ
        return integ.status()

    def deps():
        from . import deps as depsmod
        return depsmod.missing()

    parts = {"config": config, "recent": recent, "audio": audio, "cameras": cameras,
             "integration": integration, "deps": deps}
    out: dict = {}
    errors: dict = {}

    def run(name):
        try:
            return name, parts[name](), None
        except Exception as e:                      # one broken part must not empty the panel
            return name, None, f"{type(e).__name__}: {str(e)[:160]}"

    # the parts wait on other programs (pactl, v4l2, hyprctl): side by side, the panel
    # waits for the slowest one, not for the sum
    with ThreadPoolExecutor(max_workers=len(parts)) as pool:
        for name, value, err in pool.map(run, parts):
            out[name] = value
            if err:
                errors[name] = err
    if errors:
        out["errors"] = errors
    print(json.dumps(out))


def _say_refreshed(res: dict | None):
    """A short card when an update refreshed the menu entry, hotkeys or command link, or
    when the running shell still shows the old bar button (it offers to restart the shell)."""
    if not res or not (res.get("changed") or res.get("ui_stale")):
        return
    try:
        import shutil
        from . import integration
        from .capture import notify
        lines = [integration.summary(res).capitalize() + "."] if res.get("changed") else []
        actions = []
        if res.get("ui_stale"):
            lines.append(t("notify.updatedRestart"))
            restart = shutil.which("omarchy-restart-shell")
            if restart:
                actions.append((t("notify.restartShell"), restart))
        notify(t("notify.updated", res.get("version", "")), " ".join(lines), *actions,
               timeout=12000 if actions else 5000)
    except Exception:
        from . import log
        log.swallowed("update card")


def cmd_shell_start(a):
    """Run once by the bar widget when the shell loads the plugin (start, restart, or the
    reload after `omarchy plugin update`): the runtime folder exists for the widget's
    file watchers, and what Set up wrote is refreshed if this is a new version."""
    from . import config as cfgmod
    from .logger import runtime_dir
    rd = runtime_dir()
    (rd / "jobs").mkdir(exist_ok=True)
    try:                                    # the widget watches config.json: its folder must be there
        cfgmod.path().parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    res = None
    try:
        from . import integration
        res = integration.auto_apply()
    except Exception:
        from . import log
        log.swallowed("auto-apply at shell start")
    _say_refreshed(res)
    try:                                    # once: recordings made before 0.7.2 name typed characters
        from . import library
        library.scrub_typed_keys_in_background()
    except Exception:
        from . import log
        log.swallowed("typed-key scrub at shell start")
    print(json.dumps({"runtime": str(rd), "changed": (res or {}).get("changed", [])}))


def cmd_update(a):
    """Update through Omarchy's own command, then refresh what Set up wrote."""
    import shutil
    import subprocess as sp
    from . import integration
    before, t0 = integration.version(), time.time()
    exe = shutil.which("omarchy")
    if not exe:
        raise SystemExit(f"dolly: `omarchy` was not found; update with: omarchy plugin update {integration.PLUGIN_ID}")
    r = sp.run([exe, "plugin", "update", integration.PLUGIN_ID, "--yes"])
    if r.returncode != 0:
        raise SystemExit(f"dolly: Omarchy could not update the plugin (exit code {r.returncode}); nothing else was changed")
    # the files on disk are the new version now; this process still runs the old code
    sp.run([str(integration.launcher()), "integration", "auto"], stdout=sp.DEVNULL, stderr=sp.DEVNULL)
    after, st = integration.version(), integration.stamp()
    print(f"Dolly {before} -> {after}." if after != before else f"Dolly is up to date ({after}).")
    if st.get("at", 0) >= t0 and st.get("set_up") and st.get("summary"):
        print(str(st["summary"]).capitalize() + ".")
    elif not st.get("set_up"):
        print("Set up has not been run: open the Dolly panel and click Set up for the hotkeys and the menu entry.")
    # a running shell keeps the bar button it loaded; only a restart shows the new one
    if after != before and integration.ui_stale():
        restart = shutil.which("omarchy-restart-shell")
        if a.no_restart or not restart:
            print("The bar button and panel update the next time the shell starts (omarchy-restart-shell).")
        elif integration._recording():
            print("A recording is running: the bar button and panel update the next time the shell starts.")
        else:
            sp.run([restart], stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            print("Restarted the Omarchy shell to load the new bar button and panel.")


def _after_update(argv: list[str]):
    """`omarchy plugin update` runs nothing of the plugin. The bar widget refreshes what Set
    up wrote when the shell reloads it; this is the backstop for a machine without the
    button on the bar: the first Dolly command of a new version does it. Normally it costs
    two small file reads."""
    if (argv[0] if argv else "") in ("log-events", "integration", "setup", "shell-start", "update"):
        return
    try:
        from . import integration
        if integration.stamp().get("version") == integration.version():
            return
        _say_refreshed(integration.auto_apply())
    except Exception:
        from . import log
        log.swallowed("auto-apply")


def cmd_speed(a):
    from .events import Timeline
    from .timemap import SpeedConfig, cached_activity, detect_speedups
    proj = projmod.Project(_dir_or_last(a.dir))
    from . import analysis
    analysis.ensure(proj.dir)
    data = proj.load()
    if a.clear:
        data["speed"] = []
    else:
        tl = Timeline.load(proj.events)
        data["speed"] = detect_speedups(tl, cached_activity(proj.dir), SpeedConfig.from_dict(data.get("speed_config")))
    proj.save(data)
    for s in data["speed"]:
        print(f"  {s['id']}: {s['start']:6.2f}-{s['end']:6.2f}s  {s['speed']:g}x  {s['reason']}")
    print(f"{len(data['speed'])} speed-up segments")


def cmd_scrub(a):
    from .events import Timeline
    from .privacy import scan_secrets
    proj = projmod.Project(_dir_or_last(a.dir))
    from . import analysis
    analysis.ensure(proj.dir)
    data = proj.load()
    tl = Timeline.load(proj.events)
    boxes = scan_secrets(proj.raw, tl.duration, every=a.every,
                         progress=lambda i, n: sys.stderr.write(f"\r  OCR sample {i}"))
    sys.stderr.write("\n")
    keep = [b for b in data.get("privacy", []) if b.get("source") != "secret"]
    data["privacy"] = keep + boxes
    proj.save(data)
    for b in boxes:
        print(f"  {b['id']}: {b['start']:6.2f}-{b['end']:6.2f}s  {b['label']}")
    print(f"{len(boxes)} possible secrets flagged (blurred on export; review in the editor)")


def cmd_captions(a):
    from .captions import CaptionsUnavailable, transcribe
    d = _dir_or_last(a.dir)
    try:
        cues = transcribe(d, a.language)
    except CaptionsUnavailable as e:
        print(f"captions skipped: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    print(f"{len(cues)} caption cues -> {d / 'captions.srt'}")


def cmd_export(a):
    if getattr(a, "json", False) and not getattr(a, "_json_inner", False):
        from . import headless
        a._json_inner, a.quiet = True, True
        return headless.export_json(lambda: cmd_export(a), a)
    from .render import cli_progress, export
    d = _dir_or_last(a.dir)
    from . import analysis
    analysis.ensure(d, say=None if a.quiet else (lambda m: print(f"dolly: {m}", file=sys.stderr)))
    over = {}
    if a.width:
        over.setdefault("output", {})["width"] = a.width
        over["output"]["height"] = round(a.width * 9 / 16 / 2) * 2
    if a.fps:
        over.setdefault("output", {})["fps"] = a.fps
    out = export(d, a.output, "gif" if a.gif else None, a.quality, progress=cli_progress, style_over=over or None,
                 aspect=a.aspect, notify=not a.quiet)
    print(out)
    return out


def cmd_finalize(a):
    from .capture import finalize
    finalize(Path(a.dir))


def cmd_edit(a):
    from .editor.server import serve
    serve(_dir_or_last(a.dir), port=a.port, open_browser=not a.no_browser, view=a.view)


def cmd_demo(a):
    from .demo import run_demo
    run_demo(a.out, fast=a.fast)


def apply_record_defaults(a):
    """Flags win; anything not given comes from Settings (config.json)."""
    from .config import record_defaults
    d = record_defaults()
    a.mode = a.mode or d["mode"]
    a.countdown = d["countdown"] if a.countdown is None else a.countdown
    a.fps = a.fps or d["fps"]
    a.responsive = d.get("responsive", True)
    audio = getattr(a, "audio", None)
    if audio is None and (getattr(a, "mic", None) is not None or getattr(a, "desktop_audio", None) is not None):
        mic, sysa = bool(getattr(a, "mic", False)), bool(getattr(a, "desktop_audio", False))   # old flags
        audio = "both" if mic and sysa else "mic" if mic else "system" if sysa else "none"
    a.audio = audio or d["audio"]
    cam = getattr(a, "camera", None)
    if cam is None and getattr(a, "webcam", None) is not None:
        cam = "edit" if a.webcam else "off"
    a.camera = cam or d["camera"]
    a.camera_device = d["camera_device"] or None
    a.mic = a.audio in ("mic", "both")
    a.desktop_audio = a.audio in ("system", "both")
    a.webcam = a.camera == "edit"
    a.no_keys = (not d["keys"]) if a.no_keys is None else a.no_keys
    a.solid = a.solid or d["solid"]
    a.dnd = d["dnd"] if a.no_dnd is None else not a.no_dnd
    a.mic_device = d["mic_device"]
    a.system_device = d["system_device"]
    return a


# commands the bar / panel poll in the background: an error there goes to stderr and the
# log, never to a toast (it would repeat every few seconds)
QUIET_CMDS = {"log-events", "status", "config", "audio", "camera", "last", "deps", "doctor", "panel",
              "shell-start", "integration"}


def _known_error(e: BaseException) -> str | None:
    """One actionable line for errors a user can fix, else None."""
    import errno
    import subprocess
    from .fsutil import DamagedFile
    if isinstance(e, DamagedFile):
        return str(e)
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        where = f" ({e.filename})" if e.filename else ""
        return f"the disk is full{where}; free some space or choose another folder in Settings > Storage"
    if isinstance(e, PermissionError):
        return f"permission denied: {e.filename or e}; check the folder in Settings > Storage"
    if isinstance(e, FileNotFoundError) and e.filename and os.sep not in str(e.filename):
        return f"{e.filename} is not installed; run `dolly deps --install` or `dolly doctor`"
    if isinstance(e, RuntimeError) and " not found" in str(e):
        return f"{e}; run `dolly doctor`"
    if isinstance(e, subprocess.TimeoutExpired):
        cmd = e.cmd[0] if isinstance(e.cmd, (list, tuple)) and e.cmd else e.cmd
        return f"{Path(str(cmd)).name} did not respond within {e.timeout:.0f} s"
    return None


def _report_error(e: BaseException, argv: list[str]) -> None:
    from . import log
    msg = _known_error(e)
    if msg:
        log.get().warning("dolly %s: %s", " ".join(argv), msg)
        line = f"dolly: {msg}"
    else:
        p = log.write_last_error(argv)
        line = (f"dolly: unexpected error ({type(e).__name__}: {str(e)[:200]}); "
                f"details in {p or log.last_error_path()}; run `dolly doctor`")
    print(line, file=sys.stderr)
    cmd = argv[0] if argv else ""
    if not sys.stderr.isatty() and cmd not in QUIET_CMDS and "--json" not in argv:
        try:                                 # launched from the bar or a hotkey: nobody sees stderr
            from .capture import notify
            notify(t("notify.errorTitle"), line.removeprefix("dolly: "), urgency="critical", timeout=8000)
        except Exception:
            log.swallowed("error toast")


def main(argv=None):
    """Errors end as one clear line (and a toast when launched from the bar or a hotkey),
    exit code 1; unexpected ones also leave their traceback in last-error.log."""
    try:
        _after_update(list(sys.argv[1:] if argv is None else argv))
        return _main(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as e:
        _report_error(e, list(sys.argv[1:] if argv is None else argv))
        raise SystemExit(1) from None


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="dolly", description="Polished demo recordings for Omarchy")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")

    r = sub.add_parser("record", help="start a recording (defaults: Settings > Capture, ~/.config/dolly/config.json)")
    r.add_argument("--mode", choices=["screen", "region", "window"], default=None)
    r.add_argument("--audio", choices=["none", "mic", "system", "both"], default=None,
                   help="separate tracks: microphone, system audio (default sink's monitor) or both")
    r.add_argument("--camera", choices=["off", "edit", "live"], default=None,
                   help="edit: bubble added in the editor (preview during the countdown); live: on-screen bubble")
    r.add_argument("--mic", action=argparse.BooleanOptionalAction, default=None, help=argparse.SUPPRESS)
    r.add_argument("--desktop-audio", action=argparse.BooleanOptionalAction, default=None, help=argparse.SUPPRESS)
    r.add_argument("--webcam", action=argparse.BooleanOptionalAction, default=None, help=argparse.SUPPRESS)
    r.add_argument("--no-keys", action="store_true", default=None, help="do not log keystrokes")
    r.add_argument("--fps", type=int, default=None, choices=[30, 60],
                   help="30 or 60, the same two as Settings > Capture (default: the setting)")
    r.add_argument("--countdown", type=int, default=None)
    r.add_argument("--no-dnd", action="store_true", default=None, help="leave notifications on")
    r.add_argument("--backend", choices=["auto", "gsr", "gsr-cpu", "wf-recorder"], default="auto")
    r.add_argument("--ignore-disk", action="store_true",
                   help="record even with little disk space (stops cleanly when the disk is full)")
    r.add_argument("--solid", choices=["auto", "on", "off"], default=None,
                   help="solid windows while recording (auto: only when Omarchy Glassy is on)")
    # for scripts and coding agents (dolly/headless.py): no picker, no countdown, JSON out
    r.add_argument("--window", metavar="SELECTOR", help="record this window without a click: class, title text, "
                   "class:NAME, title:TEXT, address:0x.. or 'active'")
    r.add_argument("--monitor", metavar="NAME", help="record this monitor (e.g. DP-1) without a click")
    r.add_argument("--region", metavar="X,Y,WxH", help="record this area (logical pixels) without a click")
    r.add_argument("--for", dest="duration", type=float, metavar="SECONDS",
                   help="stop and finalize by itself after this long")
    r.add_argument("--wait", action="store_true", help="block until the recording is stopped and finalized")
    r.add_argument("--no-editor", action="store_true", help="do not open the editor afterwards")
    r.add_argument("--json", action="store_true",
                   help="exactly one JSON object on stdout, errors included; implies --no-editor")
    r.add_argument("--pick", help=argparse.SUPPRESS)   # internal (dolly run): "X,Y WxH" instead of the picker
    rn = sub.add_parser("run", help="perform a storyboard of steps and record it (docs/storyboard.md)")
    rn.add_argument("file", nargs="?", help="storyboard (.toml or .json); - reads stdin")
    rn.add_argument("--dry-run", action="store_true", help="print the timed plan; touch nothing")
    rn.add_argument("--check", action="store_true", help="validate, and say what this machine can perform")
    rn.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    rn.add_argument("--no-export", action="store_true", help="record and analyse only")
    rn.add_argument("--keep-open", action="store_true", help="skip [[cleanup]] and stay on the stage")
    for name in ("stop", "toggle", "cancel"):
        x = sub.add_parser(name)
        x.add_argument("--reason", help=argparse.SUPPRESS)   # watchdog: why the recording ended
        if name == "stop":
            x.add_argument("--json", action="store_true", help="describe the finished recording as JSON")
        if name == "toggle":
            for opt in ("--mode",):
                x.add_argument(opt, help=argparse.SUPPRESS)
    sub.add_parser("recover", help="finalize recordings interrupted by a crash, logout or power loss")
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")

    f = sub.add_parser("finalize")
    f.add_argument("dir")
    au = sub.add_parser("auto")
    au.add_argument("dir", nargs="?")
    e = sub.add_parser("export")
    e.add_argument("dir", nargs="?")
    e.add_argument("-o", "--output")
    e.add_argument("--gif", action="store_true")
    e.add_argument("--quality", choices=["draft", "fast", "high", "best"])
    e.add_argument("--width", type=int, help="output width (16:9 height derived)")
    e.add_argument("--fps", type=int)
    e.add_argument("--aspect", choices=["16:9", "9:16", "1:1", "4:5"])
    e.add_argument("-q", "--quiet", action="store_true", help="no desktop notification")
    e.add_argument("--json", action="store_true", help="describe the exported file as one JSON object on stdout")

    sh = sub.add_parser("share", help="presets, clipboard, links")
    ss = sh.add_subparsers(dest="share_cmd", required=True)
    x = ss.add_parser("list"); x.add_argument("dir", nargs="?")
    x = ss.add_parser("export"); x.add_argument("preset"); x.add_argument("dir", nargs="?")
    x.add_argument("--copy", action="store_true"); x.add_argument("--upload", nargs="?", const="", default=None)
    x = ss.add_parser("copy"); x.add_argument("file"); x.add_argument("--path", action="store_true")
    x = ss.add_parser("upload"); x.add_argument("file"); x.add_argument("--target"); x.add_argument("--no-copy", action="store_true")
    ss.add_parser("targets"); ss.add_parser("init")
    x = ss.add_parser("history"); x.add_argument("dir", nargs="?")
    x = ss.add_parser("delete"); x.add_argument("id"); x.add_argument("dir", nargs="?")
    sp = sub.add_parser("speed", help="detect idle / long typing and speed it up")
    sp.add_argument("dir", nargs="?"); sp.add_argument("--clear", action="store_true")
    sc = sub.add_parser("scrub", help="OCR the recording and blur things that look like secrets")
    sc.add_argument("dir", nargs="?"); sc.add_argument("--every", type=float, default=1.0)
    cp = sub.add_parser("captions", help="local transcription (whisper.cpp / faster-whisper)")
    cp.add_argument("dir", nargs="?"); cp.add_argument("--language")
    rp = sub.add_parser("replay", help="instant replay buffer")
    rp.add_argument("replay_cmd", choices=["start", "save", "stop", "status"])
    rp.add_argument("--seconds", type=int, default=60)
    rp.add_argument("--last", type=int, choices=[10, 30, 60, 300], default=None)
    sub.add_parser("pause", help="pause/resume the current recording")
    sub.add_parser("mute", help="mute/unmute the microphone track (a muted span, not a cut)")
    cm = sub.add_parser("camera", help="camera devices, live bubble snap/size, preview")
    cms = cm.add_subparsers(dest="cam_cmd", required=True)
    cms.add_parser("devices"); x = cms.add_parser("formats"); x.add_argument("device")
    cms.add_parser("snap"); cms.add_parser("size")
    x = cms.add_parser("preview"); x.add_argument("--seconds", type=float, default=8)
    au = sub.add_parser("audio", help="audio devices and a level meter")
    aus = au.add_subparsers(dest="audio_cmd", required=True)
    aus.add_parser("devices")
    x = aus.add_parser("meter"); x.add_argument("source"); x.add_argument("--seconds", type=float, default=0)
    sub.add_parser("mark", help="mark 'zoom here' at the cursor while recording")
    ed = sub.add_parser("edit")
    ed.add_argument("dir", nargs="?")
    ed.add_argument("--view", choices=["edit", "share", "library", "settings"], default="edit")
    ed.add_argument("--port", type=int, default=0)
    ed.add_argument("--no-browser", action="store_true")
    sub.add_parser("last")
    lb = sub.add_parser("library", help="recordings: list, open the Library, rename, trash, clean up")
    lb.add_argument("--json", action="store_true")
    lb.add_argument("--recent", type=int, default=0)
    lb.add_argument("--cached", action="store_true", help="the cached index if present (instant)")
    lb.add_argument("--no-posters", action="store_true")
    lb.add_argument("--open", action="store_true", help="open the editor's Library view")
    lb.add_argument("--no-browser", action="store_true")
    ls = lb.add_subparsers(dest="lib_cmd")
    x = ls.add_parser("rename"); x.add_argument("target"); x.add_argument("title", nargs="+")
    x = ls.add_parser("trash"); x.add_argument("targets", nargs="+")
    ls.add_parser("undo")
    x = ls.add_parser("cleanup", help="raw files of exported recordings to the trash")
    x.add_argument("--days", type=float, default=0); x.add_argument("--dry-run", action="store_true")
    x = ls.add_parser("reveal"); x.add_argument("target")
    x = ls.add_parser("copy"); x.add_argument("target")
    x = ls.add_parser("poster"); x.add_argument("target")
    x = ls.add_parser("scrub-keys", help="remove typed text from the event files of recordings made before 0.7.2")
    x.add_argument("folder", nargs="?", help="another folder of recordings (default: the library and the Trash)")
    x.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    st_ = sub.add_parser("settings", help="open the editor's Settings view")
    st_.add_argument("--no-browser", action="store_true")
    st_.add_argument("--section", help="scroll to this group, e.g. shortcuts")
    cf = sub.add_parser("config", help="get/set settings (~/.config/dolly/config.json)")
    cs = cf.add_subparsers(dest="cfg_cmd", required=True)
    x = cs.add_parser("get"); x.add_argument("key", nargs="?")
    x = cs.add_parser("set"); x.add_argument("pairs", nargs="+", help="group.key=value")
    x.add_argument("--from-bar", action="store_true", help=argparse.SUPPRESS)
    cs.add_parser("bar"); cs.add_parser("path"); cs.add_parser("schema")
    doc = sub.add_parser("doctor", help="check what Dolly needs on this machine")
    doc.add_argument("--json", action="store_true")
    doc.add_argument("--report", action="store_true",
                     help="write a text file to attach to a bug report (no video, no typed keys)")
    it = sub.add_parser("integration", help="the menu entry and hotkeys Dolly added; hotkey conflicts")
    it.add_argument("action", nargs="?", choices=["status", "apply", "undo", "auto"], default="status",
                    help="apply: write the menu entry, hotkeys and the dolly command again (no password); "
                         "undo: remove them; auto: apply only after an update")
    it.add_argument("--json", action="store_true")
    sub.add_parser("setup", help="add Dolly's menu entry and hotkeys, install missing packages (opens a terminal)")
    up = sub.add_parser("update", help="update Dolly with `omarchy plugin update`, then refresh hotkeys and the menu entry")
    up.add_argument("--no-restart", action="store_true",
                    help="do not restart the Omarchy shell (the new bar button then loads at the next shell start)")
    pn = sub.add_parser("panel")           # internal: what the bar panel shows, in one call
    pn.add_argument("--json", action="store_true")
    sub.add_parser("shell-start")          # internal: run by the bar widget when the shell loads the plugin
    dp = sub.add_parser("deps", help="list missing packages; --install opens a terminal that installs them")
    dp.add_argument("--install", action="store_true")
    dp.add_argument("--json", action="store_true")
    dm = sub.add_parser("demo")
    dm.add_argument("out", nargs="?")
    dm.add_argument("--fast", action="store_true", help="720p preview quality")
    lg = sub.add_parser("log-events")  # internal: event logger process (hidden from --help below)
    sub._choices_actions = [c for c in sub._choices_actions if c.dest not in ("log-events", "panel", "shell-start")]
    lg.add_argument("dir")
    lg.add_argument("--no-keys", action="store_true")
    lg.add_argument("--replay", type=int, default=0)

    a = ap.parse_args(argv)
    if a.cmd == "record":
        from . import headless
        if headless.wanted(a):
            headless.record(a)
        else:
            from .capture import start
            start(apply_record_defaults(a))
    elif a.cmd == "stop" and getattr(a, "json", False):
        from . import headless
        headless.stop_json(a)
    elif a.cmd == "run":
        from .script import cmd_run
        raise SystemExit(cmd_run(a))
    elif a.cmd in ("stop", "toggle", "cancel", "status"):
        from . import capture
        getattr(capture, a.cmd)(a)
    elif a.cmd == "recover":
        from . import capture
        capture.recover_all(a)
    elif a.cmd == "library":
        cmd_library(a)
    elif a.cmd == "settings":
        from .editor.server import serve
        serve(None, port=0, open_browser=not a.no_browser, view="settings", section=a.section)
    elif a.cmd == "config":
        cmd_config(a)
    elif a.cmd == "share":
        cmd_share(a)
    elif a.cmd == "speed":
        cmd_speed(a)
    elif a.cmd == "scrub":
        cmd_scrub(a)
    elif a.cmd == "captions":
        cmd_captions(a)
    elif a.cmd == "replay":
        from . import replay
        getattr(replay, a.replay_cmd)(a)
    elif a.cmd == "camera":
        from . import capture
        capture.camera_cmd(a)
    elif a.cmd == "audio":
        from . import audio
        if a.audio_cmd == "devices":
            print(json.dumps(audio.devices()))
        else:
            audio.meter(a.source, a.seconds)
    elif a.cmd in ("pause", "mark", "mute"):
        from . import capture
        getattr(capture, a.cmd)(a)
    elif a.cmd == "integration":
        from . import integration
        raise SystemExit(integration.main([a.action] + (["--json"] if a.json else [])))
    elif a.cmd == "panel":
        cmd_panel(a)
    elif a.cmd == "shell-start":
        cmd_shell_start(a)
    elif a.cmd == "update":
        cmd_update(a)
    elif a.cmd == "setup":
        import shutil, shlex, subprocess as sp
        setup = Path(__file__).resolve().parent.parent / "bin" / "dolly-setup"
        term = shutil.which("omarchy-launch-floating-terminal-with-presentation")
        # the button that runs this says what it installs, so setup does not ask again
        if term:
            sp.Popen([term, f"DOLLY_SETUP_YES=1 {shlex.quote(str(setup))}"], stdin=sp.DEVNULL, stdout=sp.DEVNULL,
                     stderr=sp.DEVNULL, start_new_session=True)
        else:
            raise SystemExit(sp.call([str(setup)], env={**os.environ, "DOLLY_SETUP_YES": "1"}))
    elif a.cmd == "deps":
        from . import deps
        m = deps.missing()
        if a.install:
            r = deps.install(m["packages"])
            if a.json:
                print(json.dumps({**m, **r}))
            elif not r["packages"]:
                print("Nothing to install: Dolly has everything it needs.")
            elif r["launched"]:
                print(f"Opened a terminal to install: {' '.join(r['packages'])}")
            else:
                print(f"Run this in a terminal:\n  {r['command']}")
        elif a.json:
            print(json.dumps(m))
        else:
            for p in m["packages"]:
                print(f"{p:16s} {m['why'].get(p, '')}")
            if not m["packages"]:
                print("Nothing missing.")
    elif a.cmd == "log-events":
        from .logger import run_logger
        run_logger(Path(a.dir), keys=not a.no_keys, replay=a.replay)
    elif a.cmd == "finalize":
        cmd_finalize(a)
    elif a.cmd == "auto":
        cmd_auto(a)
    elif a.cmd == "export":
        cmd_export(a)
    elif a.cmd == "edit":
        cmd_edit(a)
    elif a.cmd == "last":
        p = projmod.latest_project()
        if not p:
            sys.exit(1)
        print(p)
    elif a.cmd == "doctor":
        if a.report:
            from . import report
            path = report.write()
            print(f"{path}\nAttach this file to your bug report: it has Dolly's checks, versions, settings and "
                  "logs; no video, no typed keys.")
            return
        from .doctor import run
        sys.exit(run(as_json=a.json))
    elif a.cmd == "demo":
        cmd_demo(a)


if __name__ == "__main__":
    main()

"""`dolly doctor`: prerequisite check + which capture/encode/input path applies.

Exit code 0 when Dolly can record and export, 1 otherwise. `--json` prints a
machine-readable report (used by the editor and by the test plan).
"""
from __future__ import annotations

import glob
import importlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sh(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def detect_gpu() -> dict:
    out = {"render_nodes": sorted(glob.glob("/dev/dri/renderD*")), "virtual": None, "renderer": None,
           "vaapi_h264_encode": None, "nvenc": False}
    rc, virt = sh(["systemd-detect-virt"])
    out["virtual"] = virt if rc == 0 and virt != "none" else None
    if shutil.which("glxinfo"):
        rc, g = sh(["glxinfo", "-B"])
        m = re.search(r"OpenGL renderer string: (.*)", g)
        out["renderer"] = m.group(1) if m else None
    elif shutil.which("eglinfo"):
        rc, g = sh(["eglinfo", "-B"])
        m = re.search(r"OpenGL.*renderer: (.*)", g)
        out["renderer"] = m.group(1) if m else None
    if shutil.which("vainfo"):
        rc, v = sh(["vainfo"])
        out["vaapi_h264_encode"] = bool(re.search(r"VAProfileH264\w*\s*:\s*VAEntrypointEncSlice", v))
    if shutil.which("nvidia-smi"):
        rc, _ = sh(["nvidia-smi", "-L"])
        out["nvenc"] = rc == 0
    r = (out["renderer"] or "").lower()
    out["software_rendering"] = any(k in r for k in ("llvmpipe", "softpipe", "swrast", "software"))
    return out


def check(as_json=False) -> dict:
    rep = {"checks": [], "paths": {}}

    def add(name, ok, detail="", level="error"):
        rep["checks"].append({"name": name, "ok": bool(ok), "detail": detail, "level": "ok" if ok else level})

    # --- platform
    ver = None
    if shutil.which("omarchy-version"):
        rc, ver = sh(["omarchy-version"])
    add("Omarchy", ver is not None, ver or "omarchy-version not found (plugin needs Omarchy 4.x)", "warn")
    hypr = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
    hv = ""
    if shutil.which("hyprctl"):
        rc, hv = sh(["hyprctl", "version"])
        hv = hv.splitlines()[0] if hv else ""
    add("Hyprland session", hypr, hv or "HYPRLAND_INSTANCE_SIGNATURE not set", "error")
    if hypr:
        rc, out = sh(["hyprctl", "eval", "return 1"])
        lua_ok = rc == 0 and "error" not in out.lower()
        add("Hyprland Lua eval (click/key bridge)", lua_ok, "hyprctl eval works" if lua_ok else out[:160], "warn")
    shell_ok = shutil.which("omarchy-shell") is not None
    add("omarchy-shell (bar widget IPC)", shell_ok, "", "warn")
    from . import health as _h
    vg = _h.virtual_gpu()
    rep["virtual_gpu"] = vg
    if vg:
        sw = _h.software_gl_env().get("LIBGL_ALWAYS_SOFTWARE") == "1"
        add("virtual GPU", True,
            f"{vg['name']} ({vg['driver']}): gpu-screen-recorder cannot capture here, so wf-recorder records; "
            + ("Dolly starts mpv with LIBGL_ALWAYS_SOFTWARE=1. " if sw else "software GL off (DOLLY_SOFTWARE_GL=0). ")
            + "If mpv or the browser crash in this VM, start them with LIBGL_ALWAYS_SOFTWARE=1 "
              "(the browser also with --disable-gpu)", "warn")

    # --- python
    add("Python >= 3.10", sys.version_info >= (3, 10), platform.python_version())
    for mod, pkg in (("numpy", "python-numpy"), ("cv2", "python-opencv"), ("PIL", "python-pillow")):
        try:
            m = importlib.import_module(mod)
            add(f"python: {mod}", True, getattr(m, "__version__", ""))
        except Exception:
            add(f"python: {mod}", False, f"missing - {pkg} (dolly deps --install)")

    # --- ffmpeg
    ff = shutil.which("ffmpeg")
    add("ffmpeg", ff, ff or "pacman -S ffmpeg")
    if ff:
        rc, enc = sh(["ffmpeg", "-hide_banner", "-encoders"])
        add("ffmpeg libx264 encoder", "libx264" in enc, "needed for export")
    # --- capture backends
    gsr = shutil.which("gpu-screen-recorder")
    wf = shutil.which("wf-recorder")
    from . import deps
    gsr_bad = deps.gsr_unusable() if gsr else None
    add("gpu-screen-recorder", gsr, gsr or "pacman -S gpu-screen-recorder (Omarchy ships it)", "warn")
    if gsr:
        add("gpu-screen-recorder can capture on this GPU", not gsr_bad,
            "yes (or not probed)" if not gsr_bad else f"no: {gsr_bad} - common in VMs; install wf-recorder",
            "warn" if wf else "error")
    add("wf-recorder (software fallback)", wf, wf or "pacman -S wf-recorder  (recommended in VMs)", "warn")
    if (gsr and not gsr_bad) or wf:
        add("a capture backend", True, "gpu-screen-recorder" if gsr and not gsr_bad else "wf-recorder")
    else:
        add("a capture backend", False,
            "need wf-recorder (dolly deps --install)" if gsr_bad else "need gpu-screen-recorder or wf-recorder")
    for tool in ("slurp", "jq"):
        add(tool, shutil.which(tool), "", "warn")

    # --- round 2 features (all optional)
    add("wl-copy (copy file / link to clipboard)", shutil.which("wl-copy"), shutil.which("wl-copy") or "pacman -S wl-clipboard (ships with Omarchy)", "warn")
    add("tesseract (scrub secrets OCR)", shutil.which("tesseract"), shutil.which("tesseract") or "pacman -S tesseract tesseract-data-eng", "warn")
    add("rclone (links via Drive/Dropbox/OneDrive...)", shutil.which("rclone"), shutil.which("rclone") or "optional: pacman -S rclone", "warn")
    try:
        from .captions import backend as cap_backend
        cb = cap_backend()
    except Exception:
        cb = None
    add("captions transcriber", cb, cb or "optional: whisper.cpp (whisper-cli + ggml model) or pip install faster-whisper", "warn")
    try:
        from . import camera as cammod
        cams = cammod.devices()
        cam = ", ".join(f"{c['device']} {c['name']}" for c in cams)
    except Exception:
        cam = None
    add("camera (v4l2)", cam, cam or "no capture device (fine in a VM; the camera bubble stays off)", "warn")
    add("mpv (camera preview / live bubble)", shutil.which("mpv"), shutil.which("mpv") or "pacman -S mpv (ships with Omarchy)", "warn")
    try:
        from . import audio as audiomod
        dv = audiomod.devices()
        add("audio (pactl, separate mic/system tracks)", dv["available"],
            f"mic: {dv['default_source'] or '?'} · system: {dv['default_sink'] or '?'}.monitor" if dv["available"]
            else "pactl missing: audio recording off (pacman -S libpulse)", "warn")
    except Exception as e:
        add("audio (pactl, separate mic/system tracks)", False, str(e)[:120], "warn")
    try:
        from .share import config as scfg
        cfg = scfg.load()
        names = ", ".join(f"{n} ({t['type']}{', PUBLIC' if t['type'] == 'litterbox' else ''})" for n, t in cfg["targets"].items())
        add("share targets", True, names or f"local-only (no uploads). Template: dolly share init -> {scfg.config_path()}", "ok")
    except Exception as e:
        add("share targets", False, str(e), "error")
    # --- round 3: system integration
    from . import health
    from . import project as projmod
    root = projmod.videos_root()
    free = health.disk_free_bytes(root)
    plan = health.disk_plan(root)
    rep["disk"] = {k: plan[k] for k in ("path", "free", "minutes", "rate", "rate_source", "fs", "mount", "tmpfs", "level")}
    mins = "?" if plan["minutes"] is None else health.minutes_text(plan["minutes"])
    detail = (f"{(free or 0) / (1 << 30):.1f} GB free in {root}" + (f" ({plan['fs']})" if plan["fs"] else "")
              + f": about {mins} of recording at {plan['rate'] * 60 / (1 << 20):.0f} MB/min ({plan['rate_source']}); "
              f"refused below {health.BLOCK_MINUTES:g} min (Record anyway), warned below {health.WARN_MINUTES:g} min, "
              f"stops cleanly below {health.MIN_FREE_RECORDING >> 20} MB")
    if plan["tmpfs"]:
        detail += "; this folder is in memory (tmpfs): lost on reboot - set Settings > Storage to a folder on disk"
    add("disk space for recordings", plan["level"] == "ok", detail, "error" if plan["level"] == "block" else "warn")
    dnd = health.dnd_state() if shell_ok else None
    add("Do Not Disturb while recording", dnd is not None,
        ("Omarchy notifications service answers (currently " + ("on" if dnd else "off") + "); "
         "Dolly turns DND on while recording and restores it (DOLLY_DND=0 to disable)") if dnd is not None
        else "omarchy-shell notifications isDnd did not answer - notifications will appear in recordings", "warn")
    if shell_ok:
        rc, out = sh(["omarchy-shell", "io.github.jagritvats.dolly", "refresh"])
        add("bar widget loaded (sync marker + phases)", rc == 0,
            "omarchy-shell io.github.jagritvats.dolly answers" if rc == 0 else
            "plugin not enabled: omarchy plugin add file://$PWD --enable; without it no sync marker "
            "(events are timed from the logger start)", "warn")
    add("gdbus (stop cleanly on suspend/lock)", shutil.which("gdbus"), shutil.which("gdbus") or "pacman -S glib2", "warn")
    cs = os.environ.get("HYPRCURSOR_SIZE") or os.environ.get("XCURSOR_SIZE")
    add("cursor theme size", True, f"{cs or 24} px (HYPRCURSOR_SIZE/XCURSOR_SIZE) x monitor scale", "ok")
    try:
        from . import integration
        ist = integration.status()
        if not ist["set_up"]:
            add("shortcuts & menu (dolly-setup)", False,
                "not set up: run `dolly setup` (or the panel's Set up button) for hotkeys and menu rows", "warn")
        elif ist["conflicts"]:
            add("shortcuts & menu (dolly-setup)", False, "; ".join(
                f"{c['combo']} ({c['dolly']}) is also bound to: {', '.join(c['others'])}" for c in ist["conflicts"])
                + " - rerun `dolly setup` to drop the clash", "warn")
        else:
            add("shortcuts & menu (dolly-setup)", True,
                f"{len(ist['binds'])} hotkeys, menu rows {'on' if ist['menu'] else 'off'}", "ok")
    except Exception as e:  # never break doctor over a status read
        add("shortcuts & menu (dolly-setup)", False, f"could not check: {e}", "warn")
    from .capture import TERMINALS
    have_terms = [t for t in ("alacritty", "ghostty", "kitty", "foot", "wezterm") if shutil.which(t)]
    add("terminals (I-beam cursor, typing framing)", True,
        (", ".join(have_terms) or "none found") + f"; recognised window classes: {', '.join(TERMINALS)}", "ok")
    st = None
    try:
        from .capture import read_state, state_path
        if state_path().exists() and not read_state():
            st = "an interrupted recording is waiting: run `dolly recover`"
    except Exception:
        pass
    add("no interrupted recordings", st is None, st or "", "warn")
    gpu = detect_gpu()
    rep["gpu"] = gpu
    # --- choose capture path
    gsr_ok = bool(gsr) and not gsr_bad
    if os.environ.get("DOLLY_BACKEND"):
        cap = os.environ["DOLLY_BACKEND"] + " (forced by DOLLY_BACKEND)"
    elif gsr and gsr_bad:
        cap = ("wf: wf-recorder + libx264 CFR (gpu-screen-recorder cannot capture here: "
               f"{gsr_bad}; cursor baked in, Dolly cursor disabled)" if wf
               else f"none: gpu-screen-recorder cannot capture here ({gsr_bad}); install wf-recorder (dolly deps --install)")
    elif gsr and (gpu["vaapi_h264_encode"] or gpu["nvenc"]) and not gpu["software_rendering"]:
        cap = "gsr: gpu-screen-recorder, hardware H.264, cursor hidden, CFR"
    elif gsr and not gpu["software_rendering"] and gpu["render_nodes"]:
        cap = "gsr-cpu: gpu-screen-recorder -encoder cpu (no VAAPI/NVENC H.264), cursor hidden, CFR"
    elif wf:
        cap = "wf: wf-recorder + libx264 CFR (software path; cursor baked in, Dolly cursor disabled)"
    elif gsr:
        cap = "gsr-cpu (will try; software rendering detected - install wf-recorder as fallback)"
    else:
        cap = "none"
    rep["paths"]["capture"] = cap
    if gpu["virtual"]:
        rep["paths"]["vm"] = f"running in a VM ({gpu['virtual']}); renderer: {gpu['renderer'] or 'unknown'}"
    # --- input path
    evdev = any(os.access(p, os.R_OK) for p in glob.glob("/dev/input/event*"))
    rc, groups = sh(["id", "-nG"])
    pref = os.environ.get("DOLLY_CLICK_SOURCE", "auto")
    # order of preference: Lua bridge (no permissions) -> evdev (input group) -> none (cursor
    # polling still works: hyprctl cursorpos; clicks are inferred from dwell)
    if os.environ.get("DOLLY_EVENTS_FROM_HARNESS") == "1":
        inp = "harness: clicks/keys from the test harness input driver (DOLLY_EVENTS_FROM_HARNESS=1) - not for real use"
    elif hypr and pref in ("auto", "lua") and any(c["name"].startswith("Hyprland Lua") and c["ok"] for c in rep["checks"]):
        inp = "lua: Hyprland Lua bridge (non-consuming mouse binds + input.keyboard.key) - no extra permissions"
    elif evdev and pref in ("auto", "evdev"):
        inp = "evdev: /dev/input (user is in the input group)"
    else:
        inp = "none: no click source - auto-zoom uses cursor dwell detection (add yourself to the 'input' group or fix hyprctl eval)"
    rep["paths"]["clicks_keys"] = inp
    rep["paths"]["cursor"] = "Hyprland IPC socket j/cursorpos polled at 125 Hz" if hypr else "unavailable outside Hyprland"
    from .workers import default_workers      # no numpy/OpenCV needed: doctor must work without them
    rep["paths"]["render"] = (f"CPU (numpy + OpenCV) - no GPU needed; libx264 -preset veryfast; "
                              f"{default_workers(10 ** 6, 60)} parallel workers (DOLLY_WORKERS); unchanged frames reused; "
                              "`dolly export --quality draft` for a fast half-size preview")
    rep["paths"]["sync"] = "on-screen marker at start/stop (bar widget overlay) -> offset + drift fit; fallback: logger start"
    rep["paths"]["auto_zoom"] = "area of interest (frame diffs + Hyprland windows/layers + typing) - set zoom_config.use_aoi=false for cursor-only"
    rep["paths"]["replay"] = ("gpu-screen-recorder replay buffer (-r, SIGUSR1/SIGRTMIN+n)" if gsr_ok
                              else "unavailable (needs a working gpu-screen-recorder; wf-recorder has no replay mode)")
    rep["paths"]["pause"] = "gsr SIGUSR2" if gsr_ok else "unavailable on the wf-recorder path"
    try:
        from .script import check_report
        sc = check_report()
        rep["paths"]["scripted"] = ("dolly run: typing and keys " + ("ok (wtype)" if sc["keys"] else "need wtype")
                                    + " · pointer and clicks " + ("ok (Hyprland)" if sc["pointer"] else "need Hyprland")
                                    + ' · wait "quiet" ' + ("ok (grim)" if sc["quiet"] else "fixed (no grim)"))
    except Exception as e:
        rep["paths"]["scripted"] = f"unknown: {e}"
    rep["paths"]["user_groups"] = groups
    from . import project as projmod
    rep["paths"]["recordings"] = str(projmod.videos_root())
    rep["paths"]["theme"] = f"{projmod.theme_name()} ({projmod.theme_mode()}), font {projmod.theme_font()}"
    try:
        from .config import load, path as cpath
        c = load()
        rep["paths"]["settings"] = (f"{cpath()} · audio {c['audio']['mode']} · camera {c['camera']['mode']} · "
                                    f"capture {c['capture']['mode']}")
    except Exception as e:
        rep["paths"]["settings"] = f"unreadable: {e}"
    from .health import other_recorder
    rep["paths"]["other_recorder"] = other_recorder() or "none running"
    rep["ok"] = all(c["ok"] for c in rep["checks"] if c["level"] == "error")
    from . import deps
    rep["missing_packages"] = deps.missing()
    return rep


def run(as_json=False) -> int:
    rep = check()
    if as_json:
        print(json.dumps(rep, indent=2))
        return 0 if rep["ok"] else 1
    for c in rep["checks"]:
        mark = {"ok": "\033[32m ok \033[0m", "warn": "\033[33mwarn\033[0m", "error": "\033[31mFAIL\033[0m"}[c["level"]]
        print(f"[{mark}] {c['name']}" + (f"  - {c['detail']}" if c["detail"] else ""))
    print()
    for k, v in rep["paths"].items():
        print(f"  {k:12s} {v}")
    print("\n" + ("Dolly is ready." if rep["ok"] else "Dolly is NOT ready - fix the FAIL lines above."))
    mp = rep.get("missing_packages") or {}
    if mp.get("packages"):
        from . import deps
        print("\nMissing packages:")
        for p in mp["packages"]:
            print(f"  {p:16s} {mp['why'].get(p, '')}")
        print(f"Install them: dolly deps --install   (runs: {deps.install_command(mp['packages'])})")
    return 0 if rep["ok"] else 1

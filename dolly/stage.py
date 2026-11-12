"""The hidden stage (beta): a second desktop for takes nobody has to watch.

A stage is a second Hyprland started inside the session. Its only screen is an invisible
one of its own, so it never puts a window on the real desktop, and it has its own pointer,
its own keyboard focus and its own clipboard:

    dolly stage start [--size 1080x1920]   dolly stage status   dolly stage stop
    dolly stage shot [FILE]                dolly stage exec -- foot
    dolly run demo.toml --stage            dolly record --stage --for 10 --json

How it stays out of the way:

* The second Hyprland's window output (WAYLAND-1) is switched off in its generated config,
  before it ever draws; it then gets a headless output of its own (`STAGE`).
* A take on the stage keeps its state in its own folder ($XDG_RUNTIME_DIR/dolly/stage/rt),
  so the bar button does not react and a recording on the real desktop is not blocked.
* No notification cards, no Do Not Disturb, no Stay Awake, no editor window for a stage take.
* The compositor runs in a systemd scope with a memory limit and a time limit, its recorder
  under a memory limit of its own, and sound from its programs goes to a private output.

Dolly never picks a stage by accident: when the environment names no session, the real one
is used (`real_session`), and a stage is only entered when asked for (`--stage`).

Known limits are listed in the README ("Hidden stage (beta)"). Everything here has only been
run in a VirtualBox VM so far.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import fsutil
from . import log as _dlog
from .logger import on_stage

MARK = "DOLLY_STAGE_INSTANCE"       # in the environment of the stage's compositor and all it starts
OUTPUT = "STAGE"
NESTED_OUTPUT = "WAYLAND-1"         # the window a nested Hyprland would open on the real desktop: never
DEFAULT_SIZE = (1920, 1080)
SINK = "dolly_stage"
MEMORY_MB = 1500                    # the compositor and the programs it starts
RECORDER_MEMORY_MB = 1500
MAX_MINUTES = 30
_SIZE = re.compile(r"^\s*(\d{3,5})\s*[xX]\s*(\d{3,5})\s*$")
_SOCK = re.compile(r"^wayland-\d+$")


class StageError(Exception):
    pass


active = on_stage


# ------------------------------------------------------------------ places
def rt_root() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def base_dir() -> Path:
    d = rt_root() / "dolly" / "stage"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def state_file() -> Path:
    return base_dir() / "stage.json"


def take_dir() -> Path:
    """Where a take on the stage keeps its state (what logger.runtime_dir() returns on a stage)."""
    return base_dir() / "rt"


def profile_dir() -> Path:
    d = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "dolly" / "stage-profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _alive(pid) -> bool:
    from .health import pid_alive
    return pid_alive(pid)


def parse_size(text) -> tuple[int, int]:
    if isinstance(text, (tuple, list)) and len(text) == 2:
        w, h = int(text[0]), int(text[1])
    else:
        m = _SIZE.match(str(text or ""))
        if not m:
            raise StageError(f"a stage size looks like 1920x1080, not {text!r}")
        w, h = int(m.group(1)), int(m.group(2))
    if not (320 <= w <= 7680 and 240 <= h <= 7680):
        raise StageError(f"a stage can be 320x240 to 7680x7680, not {w}x{h}")
    return w - w % 2, h - h % 2                      # the recorder wants even numbers


def background_choice(text) -> str | None:
    """"wallpaper", or "color" (the theme's background colour; "colour" is understood too)."""
    t = str(text or "").strip().lower()
    return "wallpaper" if t == "wallpaper" else "color" if t in ("color", "colour") else None


def settings() -> dict:
    """Limits, the default size and the background: Settings > Hidden stage, or DOLLY_STAGE_* for one run."""
    out = {"size": "%dx%d" % DEFAULT_SIZE, "memory_mb": MEMORY_MB, "recorder_memory_mb": RECORDER_MEMORY_MB,
           "max_minutes": MAX_MINUTES, "background": "wallpaper"}
    try:
        from . import config
        out.update({k: v for k, v in (config.load().get("stage") or {}).items() if k in out})
    except Exception:
        _dlog.swallowed()
    for key, env in (("memory_mb", "DOLLY_STAGE_MEMORY_MB"), ("recorder_memory_mb", "DOLLY_STAGE_RECORDER_MEMORY_MB"),
                     ("max_minutes", "DOLLY_STAGE_MAX_MINUTES")):
        try:
            if os.environ.get(env):
                out[key] = max(1, int(float(os.environ[env])))
        except ValueError:
            pass
    out["background"] = (background_choice(os.environ.get("DOLLY_STAGE_BACKGROUND"))
                         or background_choice(out.get("background")) or "wallpaper")
    return out


# ------------------------------------------------------------------ which Hyprland is which
def _environ_of(pid) -> dict:
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except (OSError, ValueError, TypeError):
        return {}
    out = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep:
            out[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return out


def known() -> list[dict]:
    """Stages Dolly started and has not seen removed: [{"sig", "wl"}]."""
    try:
        d = json.loads((base_dir() / "known.json").read_text())
        return [k for k in d if isinstance(k, dict) and k.get("sig")]
    except (OSError, ValueError):
        return []


def _remember(sig: str, wl: str | None):
    ks = [k for k in known() if k["sig"] != sig] + [{"sig": sig, "wl": wl}]
    fsutil.atomic_write_json(base_dir() / "known.json", ks, durable=False)


def _forget(sig: str):
    ks = [k for k in known() if k["sig"] != sig]
    try:
        if ks:
            fsutil.atomic_write_json(base_dir() / "known.json", ks, durable=False)
        else:
            (base_dir() / "known.json").unlink(missing_ok=True)
    except OSError:
        _dlog.swallowed()


def instances(rt: Path | None = None) -> list[dict]:
    """Every Hyprland instance folder of this user:
    {"sig", "pid", "wl", "alive", "sock", "stage", "mtime"}."""
    rt = rt or rt_root()
    marked = {k["sig"] for k in known()} if rt == rt_root() else set()
    out = []
    try:
        dirs = sorted(p for p in (rt / "hypr").iterdir() if p.is_dir())
    except OSError:
        return out
    for p in dirs:
        pid = wl = None
        try:
            words = (p / "hyprland.lock").read_text().split()
            pid, wl = int(words[0]), (words[1] if len(words) > 1 else None)
        except (OSError, ValueError, IndexError):
            pass
        alive = bool(pid) and _alive(pid)
        stage = p.name in marked or (alive and _environ_of(pid).get(MARK) == "1")
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append({"sig": p.name, "pid": pid, "wl": wl, "alive": alive, "sock": (p / ".socket.sock").exists(),
                    "stage": stage, "mtime": mtime})
    return out


def real_session(rt: Path | None = None, prefer_wl: str | None = None) -> dict | None:
    """The desktop session's own Hyprland: never a stage, never a dead instance's leftovers.
    Of several, the one that owns `prefer_wl`, else the newest."""
    cands = [i for i in instances(rt) if i["sock"] and not i["stage"]]
    live = [i for i in cands if i["alive"]] or [i for i in cands if i["pid"] is None]   # no lock file: old Hyprland
    if not live:
        return None
    if prefer_wl:
        for i in live:
            if i["wl"] == prefer_wl:
                return i
    return max(live, key=lambda i: i["mtime"])


# ------------------------------------------------------------------ talking to a stage
def hyprctl(sig: str, *args: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        r = subprocess.run(["hyprctl", "--instance", sig, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out


def hypr_json(sig: str, what: str):
    ok, out = hyprctl(sig, "-j", what)
    if not ok:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def dispatch(sig: str, lua: str) -> bool:
    ok, out = hyprctl(sig, "dispatch", lua)
    return ok and out.strip() == "ok"


def _read() -> dict | None:
    try:
        d = json.loads(state_file().read_text())
        return d if isinstance(d, dict) and d.get("sig") else None
    except (OSError, ValueError):
        return None


def info() -> dict | None:
    """The running stage, or None (a stage whose compositor is gone is not running)."""
    d = _read()
    if not d or not _alive(d.get("pid")):
        return None
    return d


# ------------------------------------------------------------------ the generated config
def _omarchy_look() -> str | None:
    root = Path(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy")
    return str(root) if (root / "default" / "hypr" / "bootstrap.lua").exists() else None


def theme_background() -> str:
    """The theme's background colour as six hex digits: what the stage shows behind its windows
    when it has no picture."""
    try:
        from .project import theme_colors
        bg = str(theme_colors().get("background", "#1a1b26")).lstrip("#")
    except Exception:
        bg = "1a1b26"
    return bg if re.fullmatch(r"[0-9a-fA-F]{6}", bg) else "1a1b26"


def config_text(w: int, h: int, look: bool = True) -> str:
    """The stage's Hyprland config. It takes the LOOK of the user's Omarchy (window gaps,
    borders, colours, fonts, cursor, input settings, window rules) and none of the parts
    that own a session: no autostart, no key bindings, no shell, never the user's own
    hyprland.lua (its autostart would point systemd and D-Bus at the stage and start
    second copies of session services)."""
    bg = theme_background()
    lines = ["-- Dolly's hidden stage. Generated on every `dolly stage start`: edits are lost.",
             "-- A second Hyprland with one invisible screen of its own; it never shows a window on the real desktop."]
    omarchy = _omarchy_look() if look else None
    if omarchy:
        lines += ["pcall(function()",
                  f"  dofile({json.dumps(omarchy)} .. \"/default/hypr/bootstrap.lua\")",
                  '  require("default.hypr.helpers")',
                  '  local require_optional = require("default.hypr.require_optional")',
                  '  require("default.hypr.envs")',
                  '  require("default.hypr.looknfeel")',
                  '  require("default.hypr.input")',
                  '  require("default.hypr.windows")',
                  '  require_optional.module("omarchy.current.theme.hyprland")',
                  '  require_optional.module("hypr.looknfeel")',
                  "end)"]
    else:
        lines += ['hl.env("XCURSOR_SIZE", "24")', 'hl.env("HYPRCURSOR_SIZE", "24")',
                  "hl.config({ general = { gaps_in = 5, gaps_out = 10, border_size = 2, layout = \"dwindle\" },",
                  "            animations = { enabled = false } })"]
    lines += [f'hl.monitor({{ output = "", mode = "{w}x{h}@60", position = "auto", scale = 1 }})',
              f'hl.monitor({{ output = "{NESTED_OUTPUT}", disabled = true }})',
              f'hl.env("{MARK}", "1")',
              "hl.config({",
              # A stage is driven by commands, not by a hand: nothing is resized by its border.
              # With Omarchy's look the pointer turns into the resize pointer near a window's
              # corner, and the stage's sync marker is its pointer in the screen's corner.
              "  general = { resize_on_border = false, hover_icon_on_border = false },",
              "  misc = {",
              "    disable_hyprland_logo = true,",
              "    disable_splash_rendering = true,",
              "    force_default_wallpaper = 0,",
              "    disable_autoreload = true,",
              "    disable_watchdog_warning = true,",
              f'    background_color = "rgb({bg})",',
              "  },",
              "  ecosystem = { no_update_news = true, no_donation_nag = true },",
              "  xwayland = { enabled = false },",
              "})", ""]
    return "\n".join(lines)


def write_config(w: int, h: int) -> Path:
    """Write the config and let Hyprland check it; the Omarchy look is dropped if it does not load."""
    p = base_dir() / "hyprland.lua"
    for look in (True, False):
        p.write_text(config_text(w, h, look))
        if not look or not _omarchy_look():
            break
        try:
            r = subprocess.run(["Hyprland", "--verify-config", "--config", str(p)], capture_output=True, text=True,
                               timeout=8)
            if r.returncode == 0 and "ok" in (r.stdout + r.stderr).lower():
                break
            _dlog.get().info("stage: the Omarchy look did not load (%s); plain look",
                             (r.stdout + r.stderr).strip()[-200:])
        except (OSError, subprocess.TimeoutExpired):
            break                                   # cannot check: keep the look, the start itself will tell
    return p


# ------------------------------------------------------------------ the picture behind the windows
# Omarchy's own shell draws the desktop picture, and a stage has no shell. Quickshell (it
# ships with Omarchy, the shell is written in it) draws one still picture on the stage's
# background layer from a few generated lines of QML: no new package, nothing of the user's
# shell is loaded. The picture is cut to the stage's size once and kept, so a later start
# only opens a file. Whenever any of this does not work, the stage shows the theme's
# background colour, as it did before.
WALLPAPER_NS = "dolly-stage-wallpaper"
WALLPAPER_KEEP = 8                  # cut pictures kept in the cache (one per picture and size)


def wallpaper_source() -> Path | None:
    """The picture on the desktop right now: the link `omarchy theme bg set` keeps."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    for link in (state / "omarchy" / "current" / "background",
                 Path.home() / ".config" / "omarchy" / "current" / "background"):
        try:
            p = link.resolve(strict=True)
            if p.is_file() and p.stat().st_size > 0:
                return p
        except (OSError, RuntimeError):
            continue
    return None


def wallpaper_cache() -> Path:
    d = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "dolly" / "stage-wallpaper"
    d.mkdir(parents=True, exist_ok=True)
    return d


def prepare_wallpaper(src: Path, w: int, h: int) -> Path | None:
    """`src` cut to fill a w x h screen (it covers the screen and keeps its proportions; what
    does not fit is cut off evenly), as a PNG in the cache. A video background gives its
    first frame. None when the picture cannot be read."""
    import hashlib
    try:
        st = src.stat()
    except OSError:
        return None
    key = hashlib.sha1(f"{src}|{st.st_mtime_ns}|{st.st_size}|{w}x{h}".encode()).hexdigest()[:20]
    out = wallpaper_cache() / f"{key}.png"
    try:
        if out.stat().st_size > 0:
            os.utime(out)
            return out
    except OSError:
        pass
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    tmp = out.with_name(f"{key}.{os.getpid()}.tmp.png")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(src), "-frames:v", "1", "-update", "1",
           "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}", str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=25)
        if r.returncode != 0 or tmp.stat().st_size == 0:
            raise OSError((r.stderr or b"").decode("utf-8", "replace")[-200:])
        tmp.replace(out)
    except (OSError, subprocess.TimeoutExpired) as e:
        _dlog.get().info("stage: the desktop picture could not be prepared (%s)", str(e).strip()[:160])
        tmp.unlink(missing_ok=True)
        return None
    try:
        old = sorted(wallpaper_cache().glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[WALLPAPER_KEEP:]
        for p in old:
            p.unlink(missing_ok=True)
    except OSError:
        _dlog.swallowed()
    return out


def wallpaper_qml(image: Path, bg: str) -> str:
    from urllib.parse import quote
    return "\n".join([
        "// Dolly's hidden stage: the desktop picture behind the stage's windows. Generated: edits are lost.",
        "import Quickshell",
        "import Quickshell.Wayland",
        "import QtQuick",
        "",
        "ShellRoot {",
        "  PanelWindow {",
        "    anchors { top: true; bottom: true; left: true; right: true }",
        "    exclusionMode: ExclusionMode.Ignore",
        f'    color: "#{bg}"',
        f'    WlrLayershell.namespace: "{WALLPAPER_NS}"',
        "    WlrLayershell.layer: WlrLayer.Background",
        "    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None",
        "    Image {",
        "      anchors.fill: parent",
        f'      source: "file://{quote(str(image))}"',
        "      fillMode: Image.PreserveAspectCrop",
        "      asynchronous: false",
        "      cache: false",
        "    }",
        "  }",
        "}", ""])


def _wallpaper_dir() -> Path:
    return base_dir() / "wallpaper"


def _layer_up(sig: str, namespace: str) -> bool:
    layers = hypr_json(sig, "layers") or {}
    for mon in layers.values() if isinstance(layers, dict) else []:
        for level in ((mon or {}).get("levels") or {}).values():
            if any((l or {}).get("namespace") == namespace for l in level or []):
                return True
    return False


def _screen_is_only(inf: dict, bg: str) -> bool | None:
    """Is the background colour `bg` all there is on the stage (no picture drawn)? None when
    the stage cannot be looked at."""
    if not shutil.which("grim"):
        return None
    try:
        raw = subprocess.run(["grim", "-o", inf.get("output") or OUTPUT, "-t", "ppm", "-s", "0.1", "-"],
                             env=dict(os.environ, WAYLAND_DISPLAY=inf["wl"]), capture_output=True, timeout=6).stdout
        pixels = raw.split(b"\n", 3)[3]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None
    if len(pixels) < 300:
        return None
    for ch in range(3):
        want = int(bg[ch * 2:ch * 2 + 2], 16)
        sample = pixels[ch::21]                      # this channel of every seventh pixel
        if max(sample) - want > 10 or want - min(sample) > 10:
            return False
    return True


def _wallpaper_stop() -> None:
    """The stage's picture program, and only it (it carries the stage's mark and its own folder)."""
    want = str(_wallpaper_dir())
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return
    for pid in pids:
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if want in cmd and "quickshell" in cmd and _environ_of(pid).get(MARK) == "1":
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


def wallpaper_start(inf: dict, wait: float = 5.0) -> tuple[str, str]:
    """Put the desktop picture on the stage. -> ("wallpaper" | "color", why it is the colour)."""
    t0 = time.monotonic()
    qs = shutil.which("quickshell") or shutil.which("qs")
    if not qs:
        return "color", "Quickshell is not installed"
    src = wallpaper_source()
    if not src:
        return "color", "no desktop picture was found"
    w, h = inf["size"]
    image = prepare_wallpaper(src, int(w), int(h))
    if not image:
        return "color", f"the picture could not be read ({src.name})"
    d = _wallpaper_dir()
    bg = theme_background()
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "shell.qml").write_text(wallpaper_qml(image, bg))
    except OSError as e:
        return "color", f"could not write the picture's program ({e.strerror or e})"
    # drawn on the CPU: one still picture costs nothing there, and no graphics driver is involved
    command = " ".join(["env", "QT_QPA_PLATFORM=wayland", "QT_QUICK_BACKEND=software", "QS_NO_RELOAD_POPUP=1",
                        "QS_DISABLE_FILE_WATCHER=1", shlex.quote(qs), "-p", shlex.quote(str(d))])
    if not dispatch(inf["sig"], f"hl.dsp.exec_cmd({json.dumps(command)})"):
        return "color", "the stage refused to start the picture's program"
    end = t0 + wait
    up = False
    while time.monotonic() < end:
        if not up:
            up = _layer_up(inf["sig"], WALLPAPER_NS)
        if up:
            bare = _screen_is_only(inf, bg)
            if bare is False or bare is None:       # drawn (or it cannot be looked at: the layer is there)
                _dlog.get().info("stage: desktop picture %s in %.2f s", src.name, time.monotonic() - t0)
                return "wallpaper", ""
        time.sleep(0.1)
    _wallpaper_stop()
    return "color", ("the picture was not drawn in time" if up else "the picture's program did not come up")


# ------------------------------------------------------------------ limits
def limit_cmd(cmd: list[str], memory_mb: int, unit: str | None = None) -> list[str]:
    """`cmd` inside a systemd scope with a memory limit and no swap: a runaway process is
    killed there instead of taking the session down (it happened once, in a VM with 16 GB of
    swap). The scope execs the command, so the pid stays the command's own."""
    if not shutil.which("systemd-run"):
        return list(cmd)
    out = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
    if unit:
        out.append(f"--unit={unit}")
    return out + ["-p", f"MemoryMax={int(memory_mb)}M", "-p", "MemorySwapMax=0", *cmd]


def recorder_cmd(cmd: list[str]) -> list[str]:
    """The stage's screen recorder, under its own memory limit."""
    return limit_cmd(cmd, settings()["recorder_memory_mb"])


# ------------------------------------------------------------------ sound
def _pactl(*args) -> tuple[bool, str]:
    if not shutil.which("pactl"):
        return False, ""
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=4)
        return r.returncode == 0, r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def _sink_modules() -> list[int]:
    ok, out = _pactl("list", "short", "modules")
    mods = []
    for line in out.splitlines() if ok else []:
        parts = line.split("\t")
        if len(parts) >= 3 and "module-null-sink" in parts[1] and f"sink_name={SINK}" in parts[2]:
            try:
                mods.append(int(parts[0]))
            except ValueError:
                pass
    return mods


def _sink_create() -> tuple[str | None, int | None]:
    """A private output for the stage's programs, so a demo is never heard on the speakers."""
    for m in _sink_modules():                       # a stage that died without cleaning up
        _pactl("unload-module", str(m))
    ok, out = _pactl("load-module", "module-null-sink", f"sink_name={SINK}",
                     "sink_properties=device.description=Dolly-stage")
    try:
        return (SINK, int(out)) if ok else (None, None)
    except ValueError:
        return None, None


def system_audio_device() -> str | None:
    """What "system audio" means for a take on the stage: the stage's own output."""
    inf = info()
    return f"{inf['sink']}.monitor" if inf and inf.get("sink") else None


# ------------------------------------------------------------------ start
def _log_tail(n: int = 6) -> str:
    try:
        lines = (base_dir() / "stage.log").read_text(errors="replace").strip().splitlines()
        return " | ".join(l.strip() for l in lines[-n:])[-400:]
    except OSError:
        return ""


def start(size=None, max_minutes: float | None = None, owner: int | None = None,
          background: str | None = None) -> dict:
    """Start the stage and give it its invisible screen. Returns its description.
    `background`: "wallpaper" or "color" for this stage (default: Settings > Hidden stage)."""
    if background is not None and not background_choice(background):
        raise StageError(f"a stage's background is wallpaper or colour, not {background!r}")
    if info():
        raise StageError("a stage is already running (dolly stage status, dolly stage stop)")
    sweep()
    for need, why in (("Hyprland", "this is not a Hyprland session"), ("hyprctl", "hyprctl is missing"),
                      ("wf-recorder", "the stage is recorded with wf-recorder: omarchy-pkg-add wf-recorder")):
        if not shutil.which(need):
            raise StageError(f"a stage cannot start here: {why}")
    real = real_session(prefer_wl=None if active() else os.environ.get("WAYLAND_DISPLAY"))
    if not real or not real.get("wl"):
        raise StageError("a stage needs the desktop session to be running (no Hyprland session found)")
    cfg = settings()
    w, h = parse_size(size or cfg["size"])
    minutes = float(max_minutes or cfg["max_minutes"])
    conf = write_config(w, h)
    sink, module = _sink_create()
    unit = f"dolly-stage-{os.getpid()}-{int(time.time())}"
    before = {i["sig"] for i in instances()}
    fsutil.atomic_write_json(base_dir() / "starting.json", {"before": sorted(before), "t": time.time(), "unit": unit,
                                                           "sink_module": module, "pid": os.getpid()}, durable=False)
    env = dict(os.environ, WAYLAND_DISPLAY=real["wl"])
    for k in ("HYPRLAND_INSTANCE_SIGNATURE", "DISPLAY", "DOLLY_STAGE"):
        env.pop(k, None)                            # DISPLAY: X11 programs must not land on the real X server
    inner = ["env", f"{MARK}=1", "HYPRLAND_NO_SD_VARS=1", "HYPRLAND_NO_SD_NOTIFY=1", "HYPRLAND_NO_RT=1",
             "HYPRLAND_NO_CRASHREPORTER=1"]
    if sink:
        # the stage's programs play into its private output: PULSE_SINK for programs that speak
        # PulseAudio, PIPEWIRE_NODE for those that speak PipeWire directly (mpv, pw-play)
        inner += [f"PULSE_SINK={sink}", f"PIPEWIRE_NODE={sink}"]
    inner += ["timeout", "-k", "3", str(int(minutes * 60)), "Hyprland", "--config", str(conf)]
    cmd = limit_cmd(inner, cfg["memory_mb"], unit)
    log = open(base_dir() / "stage.log", "ab")
    log.write(f"\n=== {time.strftime('%F %T')} stage {w}x{h}, {cfg['memory_mb']} MB, {minutes:g} min\n".encode())
    log.flush()
    proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    found = None
    end = time.monotonic() + 15.0
    while time.monotonic() < end and not found:
        for i in instances():
            if i["sig"] in before or not i["sock"] or not i["alive"] or not i["wl"] or i["sig"] == real["sig"]:
                continue
            if hyprctl(i["sig"], "version", timeout=2.0)[0]:
                found = i
                break
        if found or proc.poll() is not None:
            break
        time.sleep(0.1)
    if not found:
        _abort(proc, unit, module)
        raise StageError("the stage did not start" + (": " + _log_tail() if _log_tail() else ""))
    sig = found["sig"]
    _remember(sig, found["wl"])
    inf = {"sig": sig, "wl": found["wl"], "pid": found["pid"], "unit": unit, "launcher_pid": proc.pid,
           "size": [w, h], "output": OUTPUT, "started": time.time(), "until": time.time() + minutes * 60,
           "max_minutes": minutes, "memory_mb": cfg["memory_mb"], "recorder_memory_mb": cfg["recorder_memory_mb"],
           "sink": sink, "sink_module": module, "owner": owner,
           "real": {"sig": real["sig"], "wl": real["wl"]}, "look": "omarchy" if _omarchy_look() else "plain"}
    fsutil.atomic_write_json(state_file(), inf, durable=False)
    (base_dir() / "starting.json").unlink(missing_ok=True)
    hyprctl(sig, "output", "create", "headless", OUTPUT)
    mon = None
    end = time.monotonic() + 6.0
    while time.monotonic() < end and not mon:
        mon = next((m for m in hypr_json(sig, "monitors") or [] if m.get("name") == OUTPUT), None)
        if not mon:
            time.sleep(0.1)
    if not mon:
        stop()
        raise StageError("the stage started but its screen could not be created" +
                         (": " + _log_tail() if _log_tail() else ""))
    dispatch(sig, f'hl.dsp.focus({{ monitor = "{OUTPUT}" }})')
    inf["size"] = [int(mon.get("width") or w), int(mon.get("height") or h)]
    inf["background"], inf["background_note"] = "color", ""
    fsutil.atomic_write_json(state_file(), inf, durable=False)
    if (background_choice(background) or cfg["background"]) == "wallpaper":
        try:
            inf["background"], inf["background_note"] = wallpaper_start(inf)
        except Exception as e:                      # noqa: BLE001 - a stage without its picture is still a stage
            inf["background_note"] = f"{type(e).__name__}: {e}"[:160]
        if inf["background"] != "wallpaper":
            _dlog.get().info("stage: theme colour as background (%s)", inf["background_note"])
        fsutil.atomic_write_json(state_file(), inf, durable=False)
    if owner:
        _watch_owner(owner)
    _dlog.get().info("stage started: %s %sx%s on %s", sig[-12:], inf["size"][0], inf["size"][1], found["wl"])
    return inf


def _abort(proc, unit: str, module):
    try:
        proc.terminate()
    except OSError:
        pass
    subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True, timeout=10) \
        if shutil.which("systemctl") else None
    if module is not None:
        _pactl("unload-module", str(module))
    sweep()


def _watch_owner(owner: int):
    """A stage a command started for itself goes when that command goes, however it ends."""
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root))
    env.pop("DOLLY_STAGE", None)
    sh = 'tail --pid="$1" -f /dev/null 2>/dev/null; sleep 1; exec "$2" -m dolly.stage reap "$1"'
    try:
        subprocess.Popen(["sh", "-c", sh, "dolly-stage-watch", str(int(owner)), sys.executable],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    except (OSError, ValueError):
        _dlog.swallowed()


# ------------------------------------------------------------------ stop and cleaning up
def take_running() -> bool:
    """A recording is running on the stage right now."""
    try:
        st = json.loads((take_dir() / "state.json").read_text())
    except (OSError, ValueError):
        return False
    return bool(st.get("recorder_pid")) and _alive(st["recorder_pid"])


def _stop_take():
    """End a take that is still running on the stage the proper way, so it is kept."""
    if not take_running():
        return
    root = Path(__file__).resolve().parents[1]
    inf = _read() or {}
    env = dict(os.environ, PYTHONPATH=str(root), **env_for(inf))
    env.pop("DISPLAY", None)
    try:
        subprocess.run([sys.executable, "-m", "dolly.cli", "stop"], env=env, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        _dlog.swallowed()


def _kill_marked(wl: str | None, real_wl: str | None) -> int:
    """Everything the stage started (it carries the stage's mark), and helper programs still
    connected to its socket. Never anything of the real session."""
    helpers = ("wf-recorder", "grim", "wtype", "slurp")
    me, n = os.getpid(), 0
    uid = os.getuid()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        hit = 0
        try:
            pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return n
        for pid in pids:
            if pid in (me, os.getppid()):
                continue
            try:
                if os.stat(f"/proc/{pid}").st_uid != uid:
                    continue
            except OSError:
                continue
            env = _environ_of(pid)
            if not env:
                continue
            if (env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}") != str(rt_root()):
                continue                    # another session's programs (or a test's): never ours to stop
            mine = env.get(MARK) == "1"
            if not mine and wl and wl != real_wl and env.get("WAYLAND_DISPLAY") == wl:
                try:
                    exe = os.readlink(f"/proc/{pid}/exe").rsplit("/", 1)[-1]
                except OSError:
                    exe = ""
                mine = exe in helpers
            if mine:
                try:
                    os.kill(pid, sig)
                    hit += 1
                except OSError:
                    pass
        n += hit
        if not hit:
            break
        time.sleep(0.5)
    return n


def _remove_instance(sig: str, wl: str | None, real: dict | None) -> bool:
    """A dead stage's instance folder and socket would mislead anything that looks for sessions."""
    rt = rt_root()
    if real and sig == real.get("sig"):
        return False
    done = False
    d = rt / "hypr" / sig
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        done = True
    if wl and _SOCK.match(wl) and not (real and wl == real.get("wl")):
        if not any(i["alive"] and i["wl"] == wl for i in instances()):
            for f in (rt / wl, rt / f"{wl}.lock"):
                try:
                    f.unlink()
                    done = True
                except OSError:
                    pass
    return done


def stop() -> dict:
    """Stop the stage: end a running take properly, close its windows, leave nothing behind."""
    d = _read()
    removed: list[str] = []
    if d:
        sig, pid, wl = d["sig"], d.get("pid"), d.get("wl")
        real = d.get("real") or real_session()
        if _alive(pid):
            _stop_take()
            for c in hypr_json(sig, "clients") or []:          # an exit with windows open crashed once in five
                if c.get("address"):
                    dispatch(sig, f'hl.dsp.window.close({{ window = "address:{c["address"]}" }})')
            time.sleep(0.3)
            hyprctl(sig, "dispatch", "hl.dsp.exit()", timeout=3.0)
            end = time.monotonic() + 3.0
            while time.monotonic() < end and _alive(pid):
                time.sleep(0.1)
        if d.get("unit") and shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "stop", f"{d['unit']}.scope"], capture_output=True, timeout=15)
        for p in (d.get("launcher_pid"), pid):
            if p and _alive(p):
                try:
                    os.kill(int(p), signal.SIGKILL)
                except OSError:
                    pass
        _kill_marked(wl, (real or {}).get("wl"))
        time.sleep(0.2)
        if not _alive(pid):
            if _remove_instance(sig, wl, real):
                removed.append("the stage's session files")
            _forget(sig)
        if d.get("sink_module") is not None:
            _pactl("unload-module", str(d["sink_module"]))
        state_file().unlink(missing_ok=True)
        _clear_take_state()
        _dlog.get().info("stage stopped: %s", sig[-12:])
    removed += sweep()
    return {"ok": True, "was_running": bool(d), "removed": removed}


def _clear_take_state():
    """State files of a take whose recorder is gone (never while one is recording)."""
    if take_running():
        return
    for name in ("state.json", "phase.json", "recording.pid"):
        try:
            (take_dir() / name).unlink()
        except OSError:
            pass


def sweep() -> list[str]:
    """Remove what a stage that crashed, was killed or never came up left behind. Returns
    what was removed, in plain words. Safe to call at any time: a running stage, the real
    session and anything Dolly did not start are left alone."""
    out: list[str] = []
    real = real_session()
    d = _read()
    if d and not _alive(d.get("pid")):
        _kill_marked(d.get("wl"), (real or {}).get("wl"))
        if d.get("unit") and shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "stop", f"{d['unit']}.scope"], capture_output=True, timeout=15)
        if _remove_instance(d["sig"], d.get("wl"), real):
            out.append("session files of a stage that died")
        if d.get("sink_module") is not None:
            _pactl("unload-module", str(d["sink_module"]))
        _forget(d["sig"])
        state_file().unlink(missing_ok=True)
        _clear_take_state()
        d = None
    # a start that never finished: folders that appeared during it and belong to nothing alive
    sf = base_dir() / "starting.json"
    try:
        st = json.loads(sf.read_text())
    except (OSError, ValueError):
        st = None
    theirs = st and st.get("pid") != os.getpid() and _alive(st.get("pid")) and time.time() - float(st.get("t") or 0) < 30
    if st and not theirs:                           # (another command's start that is still under way is left alone)
        before = set(st.get("before") or [])
        for i in instances():
            if i["sig"] in before or i["alive"] or (real and i["sig"] == real["sig"]):
                continue
            if _remove_instance(i["sig"], i["wl"], real):
                out.append("session files of a stage that did not start")
        if st.get("unit") and shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "stop", f"{st['unit']}.scope"], capture_output=True, timeout=15)
        if st.get("sink_module") is not None and not d:
            _pactl("unload-module", str(st["sink_module"]))
        sf.unlink(missing_ok=True)
    live = d["sig"] if d else None
    for k in known():
        if k["sig"] == live:
            continue
        inst = next((i for i in instances() if i["sig"] == k["sig"]), None)
        if inst and inst["alive"]:
            continue                                # a stage with no state file: `dolly stage stop --all` territory
        if _remove_instance(k["sig"], k.get("wl"), real):
            out.append("session files of an old stage")
        _forget(k["sig"])
    if not d:
        for m in _sink_modules():
            _pactl("unload-module", str(m))
            out.append("the stage's private sound output")
    if out:
        _dlog.get().info("stage sweep: %s", "; ".join(out))
    return out


# ------------------------------------------------------------------ using it
def env_for(inf: dict) -> dict:
    """What a process needs in its environment to work on the stage."""
    env = {"DOLLY_STAGE": "1", "WAYLAND_DISPLAY": inf.get("wl") or "", "HYPRLAND_INSTANCE_SIGNATURE": inf.get("sig") or "",
           "DOLLY_OPEN_EDITOR": "0"}
    if inf.get("sink"):
        env["PULSE_SINK"] = env["PIPEWIRE_NODE"] = inf["sink"]
    return env


def enter(inf: dict | None = None) -> dict:
    """Point this process (and everything it starts) at the stage."""
    inf = inf or info()
    if not inf:
        raise StageError("no stage is running (dolly stage start)")
    os.environ.update(env_for(inf))
    os.environ.pop("DISPLAY", None)
    return inf


def ensure(size=None, owner: int | None = None) -> tuple[dict, bool]:
    """(the running stage, whether this call started it)."""
    inf = info()
    if inf:
        if size and list(parse_size(size)) != list(inf.get("size") or []):
            raise StageError(f"the running stage is {inf['size'][0]}x{inf['size'][1]}; stop it first for another size "
                             "(dolly stage stop)")
        return inf, False
    return start(size, owner=owner), True


class Session:
    """`--stage` on a command: enter the stage (starting one if needed) and tidy up after."""

    def __init__(self, size=None):
        self.inf, self.started = ensure(size, owner=os.getpid())
        enter(self.inf)

    def end(self):
        if self.started and not take_running():
            stop()


def shot(path: str | Path | None = None) -> Path:
    """A screenshot of the stage: how a person, or an agent, looks at what is on it."""
    inf = info()
    if not inf:
        raise StageError("no stage is running (dolly stage start)")
    if not shutil.which("grim"):
        raise StageError("screenshots need grim (it ships with Omarchy)")
    out = Path(path).expanduser() if path else Path.cwd() / f"stage-{time.strftime('%Y%m%d-%H%M%S')}.png"
    env = dict(os.environ, WAYLAND_DISPLAY=inf["wl"])
    r = subprocess.run(["grim", "-o", inf.get("output") or OUTPUT, str(out)], env=env, capture_output=True, text=True,
                       timeout=15)
    if r.returncode != 0 or not out.exists():
        raise StageError("the screenshot failed: " + (r.stderr.strip() or "grim gave no picture")[:200])
    return out


def exec_on(argv: list[str] | str) -> bool:
    """Start a program on the stage, as a child of its compositor (inside its limits)."""
    inf = info()
    if not inf:
        raise StageError("no stage is running (dolly stage start)")
    command = argv if isinstance(argv, str) else " ".join(shlex.quote(a) for a in argv)
    return dispatch(inf["sig"], f"hl.dsp.exec_cmd({json.dumps(wrap_command(command))})")


# ------------------------------------------------------------------ programs that keep one running copy
_CHROMIUM = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave", "brave-browser",
             "vivaldi", "vivaldi-stable", "microsoft-edge", "microsoft-edge-stable", "helium", "helium-browser",
             "thorium-browser", "ungoogled-chromium", "omarchy-browser")
_FIREFOX = ("firefox", "firefox-developer-edition", "firefox-esr", "librewolf", "zen-browser", "floorp", "waterfox")
_LAUNCHERS = ("omarchy-launch-browser",)


def default_browser() -> str | None:
    """The program behind Omarchy's default browser (the Exec of its desktop entry)."""
    entry = ""
    for cmd in (["omarchy-cmd-default-browser"], ["xdg-settings", "get", "default-web-browser"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            entry = subprocess.run(cmd, capture_output=True, text=True, timeout=4).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            entry = ""
        if entry:
            break
    if not entry:
        return None
    dirs = [os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")]
    dirs += (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    for d in dirs:
        f = Path(d) / "applications" / entry
        try:
            for line in f.read_text(errors="replace").splitlines():
                if line.startswith("Exec="):
                    words = shlex.split(line[5:])
                    if not words or os.path.basename(words[0]) in _LAUNCHERS:
                        return None                  # an entry that points back at the launcher
                    return words[0]
        except (OSError, ValueError):
            continue
    return None


def _family(program: str) -> str | None:
    name = os.path.basename(program)
    if name in _CHROMIUM or "chrom" in name:
        return "chromium"
    if name in _FIREFOX:
        return "firefox"
    return None


def wrap_command(command: str) -> str:
    """A command line as it should be started on the stage.

    * `uwsm-app -- ` is dropped: it would put the program in a scope of its own, outside the
      stage's memory limit.
    * A browser gets a profile folder of its own. With the user's profile, a browser that is
      already running on the real desktop would open the new window there.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return command
    if not words:
        return command
    while len(words) > 1 and words[0] == "uwsm-app":
        words = words[1:]
        if words and words[0] == "--":
            words = words[1:]
    i = 0
    while i < len(words) - 1 and (words[i] in ("exec", "setsid", "env") or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[i])):
        i += 1
    prog = words[i]
    launcher = os.path.basename(prog) in _LAUNCHERS
    if launcher:
        real = default_browser()
        if real and _family(real):
            words[i] = prog = real
            launcher = False
    # Omarchy's launcher hands its arguments to the browser; its default is Chromium-based
    fam = "chromium" if launcher else _family(prog)
    if fam == "chromium" and not any(w.startswith("--user-data-dir") for w in words):
        d = profile_dir() / "chromium"
        d.mkdir(parents=True, exist_ok=True)
        words[i + 1:i + 1] = [f"--user-data-dir={d}", "--no-first-run", "--no-default-browser-check"]
    elif fam == "firefox" and not any(w in ("--profile", "-profile", "-P") for w in words):
        d = profile_dir() / "firefox"
        d.mkdir(parents=True, exist_ok=True)
        words[i + 1:i + 1] = ["--profile", str(d), "--new-instance"]
    return " ".join(shlex.quote(w) for w in words)


# ------------------------------------------------------------------ the pointer as a clock
# The stage's sync marker is its pointer, shown for a moment in the top-left corner: in the
# outermost 10 pixels, the gap around the windows, where nothing else draws. At (5, 5) the
# arrow fills the inner half of that square (measured in recorded video: 43 grey levels of
# difference there, 18 at (2, 2), where most of the arrow is outside the square).
MARK_AT = (5, 5)
MARK_WIGGLE = (6, 6)                # ... and here for an instant now and then, so the stage keeps repainting
MARK_HOLD_S = 0.3


def _socket_dispatch(lua: str) -> bool:
    from .logger import hypr_request
    out = hypr_request("/dispatch " + lua, timeout=1.0)
    if out is not None and out.strip() == "ok":
        return True
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or ""
    return bool(sig) and dispatch(sig, lua)


def pointer() -> tuple[float, float] | None:
    from .logger import hypr_json as hj
    p = hj("cursorpos")
    try:
        return float(p["x"]), float(p["y"])
    except (TypeError, KeyError, ValueError):
        return None


def nudge() -> None:
    """One repaint of the stage: the pointer one pixel aside and back. wf-recorder only acts
    on its stop signal when a frame arrives, and a still stage sends none."""
    p = pointer()
    if not p:
        return
    x, y = int(p[0]), int(p[1])
    if (x, y) in (MARK_AT, MARK_WIGGLE):            # a stop mark is on show: it stays where it is seen best
        _blip()
        return
    _socket_dispatch(f"hl.dsp.cursor.move({{ x = {x + 1}, y = {y} }})")
    time.sleep(0.03)
    _socket_dispatch(f"hl.dsp.cursor.move({{ x = {x}, y = {y} }})")


def _move(x, y) -> bool:
    return _socket_dispatch(f"hl.dsp.cursor.move({{ x = {int(x)}, y = {int(y)} }})")


def _blip() -> None:
    """One repaint while a mark is on show: the pointer a pixel aside and back at once, so
    nearly every frame shows it at MARK_AT."""
    _move(*MARK_WIGGLE)
    _move(*MARK_AT)


def _repaints(x: int, y: int, seconds: float, step: float = 0.04) -> None:
    """Keep the stage repainting for `seconds` with the pointer at (x, y), one pixel to and fro.
    A recorder only gets a frame when something changes; one that was busy when the pointer
    moved gets the next change, so there has to be one."""
    end, flip = time.monotonic() + seconds, False
    while time.monotonic() < end:
        flip = not flip
        _move(x + (1 if flip else 0), y)
        time.sleep(step)
    _move(x, y)


def _cpu_ticks(pid) -> int | None:
    """Processor time a process has used so far (all its threads), in clock ticks."""
    try:
        rest = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return int(rest[11]) + int(rest[12])        # utime + stime
    except (OSError, ValueError, IndexError, TypeError):
        return None


def recorder_drained(pid, least: float = 0.5, most: float = 6.0, log=None) -> float:
    """Leave the stage still until its recorder has encoded what it holds; returns the seconds
    waited (between `least` and `most`).

    wf-recorder keeps the frames it has captured and not yet encoded in a small queue, and
    drops that queue when it is told to stop. On a busy machine the encoder is behind, the
    queue is full, and what is dropped is the last second or more of the take (11 Nov, a
    stage take and a desktop take at once: the stop mark was shown for 1.3 s before the stop
    signal and not one frame of it was in the file). A recorder that gets no new frames
    works its queue off and then sits idle, and that can be seen from outside: it stops
    using the processor. So: nothing repaints the stage, and the stop signal waits until
    the recorder has been idle for a moment. A stage that is not still (a video playing on
    it) never lets it idle; then `most` seconds of frames have gone through the queue, the
    stop mark with them."""
    try:
        hz = float(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError):
        hz = 100.0
    t0 = time.monotonic()
    prev, prev_t, idle, seen = _cpu_ticks(pid), t0, 0, []
    while True:
        time.sleep(0.12)
        now, now_t = _cpu_ticks(pid), time.monotonic()
        waited = now_t - t0
        if now is None or prev is None:             # gone, or it cannot be watched: a fixed wait
            time.sleep(max(0.0, least - waited))
            break
        busy = (now - prev) / hz / max(0.01, now_t - prev_t)      # share of one processor
        seen.append(round(busy, 2))
        prev, prev_t = now, now_t
        idle = idle + 1 if busy < 0.1 else 0
        if (waited >= least and idle >= 2) or waited >= most:
            break
    waited = time.monotonic() - t0
    if log:
        log(f"stage: waited {waited:.2f} s for the recorder to finish its frames (processor share: {seen})")
    return waited


def is_still(gap: float = 0.3) -> bool | None:
    """Does the stage look the same now and `gap` seconds from now? None when it cannot be
    looked at."""
    inf = info()
    if not inf or not shutil.which("grim"):
        return None

    def look():
        try:
            return subprocess.run(["grim", "-o", inf.get("output") or OUTPUT, "-t", "ppm", "-s", "0.1", "-"],
                                  env=dict(os.environ, WAYLAND_DISPLAY=inf["wl"]), capture_output=True, timeout=6).stdout
        except (OSError, subprocess.TimeoutExpired):
            return b""
    a = look()
    time.sleep(max(0.0, gap))
    b = look()
    return (a == b) if a and b else None


def drain_limit(take_seconds: float) -> float:
    """The longest a stage take's stop waits for its recorder (recorder_drained).

    The recorder writes 60 frames for every second, also for seconds in which nothing
    changed, and it writes those only when the next change comes: after a still stretch it
    has that whole stretch to encode before it gets to the stop mark. Where encoding is
    slow that takes about as long as the stretch itself (VirtualBox, no GPU: 441 frames
    after 7 still seconds took 9 s). So on a stage that is still now the wait may be as
    long as the take was, and a little more; on one that is not still (a video playing)
    frames have been flowing, the recorder is not far behind, and 3 s is plenty."""
    if is_still() is False:
        return 3.0
    return max(6.0, min(60.0, 4.0 + 1.3 * max(0.0, float(take_seconds))))


def _home_file() -> Path:
    return take_dir() / "pointer-home.json"


def pointer_home() -> bool:
    """Take the pointer back to where it was before a stop mark left it in the corner."""
    f = _home_file()
    try:
        d = json.loads(f.read_text())
        x, y = int(d["x"]), int(d["y"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    f.unlink(missing_ok=True)
    return _move(x, y)


def pointer_mark(hold: float = MARK_HOLD_S, stay: bool = False) -> dict | None:
    """The stage's sync marker: there is no shell on a stage to flash one, but the stage's
    pointer is Dolly's own and is drawn into the video. It jumps into the top-left corner
    and is shown there for `hold` seconds. Returns {"t": the monotonic time of the jump (the
    middle of the call that made it), "rtt": how long that call took}, or None when the
    pointer could not be moved.

    Made to survive a busy machine (a stage take and a desktop take at once lost one):

    * the stage repaints before the jump (the recorder has a frame without the pointer in
      the corner), all the time the pointer is shown there (a recorder that was busy at the
      jump still gets a frame with it), and after it has gone back;
    * `stay` (the stop mark): the pointer does not go back. It is still in the corner when
      the recorder is told to stop, however late that is; `pointer_home()` takes it back
      once the recorder has gone."""
    pointer_home()                                  # a stop mark nobody tidied up after
    p = pointer()
    if not p:
        return None
    x, y = int(p[0]), int(p[1])
    if x <= 60 and y <= 60:                         # already in the corner: start from somewhere else
        x, y = 200, 200
        _move(x, y)
        time.sleep(0.12)
    _repaints(x, y, 0.16)
    t0 = time.monotonic()
    ok = _move(*MARK_AT)
    t1 = time.monotonic()
    if not ok:
        return None
    if stay:
        try:
            take_dir().mkdir(parents=True, exist_ok=True)
            fsutil.atomic_write_json(_home_file(), {"x": x, "y": y}, durable=False)
        except OSError:
            _dlog.swallowed()
    end = time.monotonic() + max(0.1, hold)
    while time.monotonic() < end:
        time.sleep(0.04)
        _blip()
    if not stay:
        _move(x, y)
        _repaints(x, y, 0.16)
    return {"t": (t0 + t1) / 2, "rtt": t1 - t0}


# ------------------------------------------------------------------ driving it: type, key, click, move
# Every key on a stage comes from a typing tool, and Hyprland reports those as codes that say
# nothing about the key (dolly/logger.py, on_unnamed_key). These commands type and click AND
# tell the event logger of a take on the stage what they did, by name, at the moment they do
# it: the take shows the right keycaps and follows the typing. An agent that drives a stage
# by hand uses them instead of wtype.
def _driver():
    inf = enter()
    from .inject import Desktop, Feed
    feed = Feed()
    return inf, Desktop(feed), feed


def _origin(inf: dict) -> tuple[int, int]:
    mon = next((m for m in hypr_json(inf["sig"], "monitors") or [] if m.get("name") == (inf.get("output") or OUTPUT)),
               None) or {}
    return int(mon.get("x") or 0), int(mon.get("y") or 0)


def _point(inf: dict, x, y) -> tuple[int, int]:
    try:
        x, y = int(round(float(x))), int(round(float(y)))
    except (TypeError, ValueError):
        raise StageError(f"a point on the stage is two numbers (pixels from its top-left corner), not {x!r} {y!r}") \
            from None
    w, h = inf.get("size") or DEFAULT_SIZE
    if not (0 <= x < w and 0 <= y < h):
        raise StageError(f"{x},{y} is not on the stage (it is {w}x{h})")
    ox, oy = _origin(inf)
    return ox + x, oy + y


def do_type(text: str, speed: float | None = None, enter_key: bool = False) -> dict:
    """Type `text` into the stage's focused window, at a person's pace (`speed` characters a
    second; 0 = at once), then Enter if asked."""
    if not text and not enter_key:
        raise StageError("nothing to type")
    if not shutil.which("wtype"):
        raise StageError("typing needs wtype (it ships with Omarchy): omarchy-pkg-add wtype")
    import random

    from . import storyboard as sb
    _inf, desk, feed = _driver()
    cps = sb.DEFAULT_SPEED if speed is None else max(0.0, float(speed))
    delays = sb.keystroke_delays(text, cps, random.Random()) if cps > 0 else [0.0] * len(text)
    t0 = time.monotonic()
    held = desk.hold_key_repeat()                   # a stall must not repeat a typed key
    try:
        feed.hold(2.5)
        time.sleep(0.03)                            # the logger reads that line before the first key arrives
        for ch, d in zip(text, delays):
            if d > 0:
                time.sleep(d)
            feed.hold(2.5)
            if ch == "\n":
                desk.press([], "enter")
            elif ch == "\t":
                desk.press([], "tab")
            else:
                desk.type_char(ch)
        if enter_key:
            if cps > 0:
                time.sleep(0.45)                    # people look at what they typed before pressing Enter
            feed.hold(2.5)
            desk.press([], "enter")
    except (subprocess.SubprocessError, OSError) as e:
        raise StageError(f"could not type on the stage ({type(e).__name__})") from None
    finally:
        feed.hold(0.3)
        feed.close()
        if held:
            desk.restore_key_repeat()
    return {"ok": True, "typed": len(text), "enter": bool(enter_key), "seconds": round(time.monotonic() - t0, 2)}


def do_key(combos: list[str]) -> dict:
    """Press keys or chords on the stage: "enter", "ctrl+l", "ctrl+shift+t"."""
    if not combos:
        raise StageError("which key? (dolly stage key ctrl+l)")
    if not shutil.which("wtype"):
        raise StageError("keys need wtype (it ships with Omarchy): omarchy-pkg-add wtype")
    from . import storyboard as sb
    parsed = []
    for c in combos:
        try:
            parsed.append(sb.parse_chord(c))
        except sb.StoryboardError as e:
            raise StageError(e.msg) from None
    _inf, desk, feed = _driver()
    try:
        feed.hold(2.5)
        time.sleep(0.03)
        for i, (mods, key) in enumerate(parsed):
            if i:
                time.sleep(0.16)
            feed.hold(2.5)
            desk.press(list(mods), key)
    except (subprocess.SubprocessError, OSError) as e:
        raise StageError(f"could not press the key on the stage ({type(e).__name__})") from None
    finally:
        feed.hold(0.3)
        feed.close()
    return {"ok": True, "keys": [c.lower() for c in combos]}


def do_move(x, y, instant: bool = False) -> dict:
    """Move the stage's pointer to (x, y), the way a hand would (or at once)."""
    inf, desk, feed = _driver()
    try:
        px, py = _point(inf, x, y)
        desk.move_to(px, py, seconds=0.0 if instant else None)
    finally:
        feed.close()
    return {"ok": True, "x": int(round(float(x))), "y": int(round(float(y)))}


def do_click(x=None, y=None, button: str = "left", instant: bool = False) -> dict:
    """Click on the stage: at (x, y), or where the pointer is."""
    inf, desk, feed = _driver()
    try:
        if x is not None and y is not None:
            px, py = _point(inf, x, y)
            desk.move_to(px, py, seconds=0.0 if instant else None)
            time.sleep(0.03 if instant else 0.14)
        elif x is not None or y is not None:
            raise StageError("a point is two numbers: dolly stage click 640 360")
        at = desk.pointer()
        try:
            desk.click(button)
        except RuntimeError as e:
            raise StageError(str(e)) from None
    finally:
        feed.close()
    ox, oy = _origin(inf)
    return {"ok": True, "x": int(at[0]) - ox, "y": int(at[1]) - oy, "button": button}


# ------------------------------------------------------------------ doctor
SELFTEST_TONES = {"PulseAudio": 660, "PipeWire": 1000}      # Hz: one per way a program can play sound


def _sound_begin(inf: dict) -> dict | None:
    """Let two programs on the stage play a tone each (one speaks PulseAudio, one PipeWire)
    and listen on the stage's private output. Returns what `_sound_end` needs."""
    ffmpeg = shutil.which("ffmpeg")
    if not inf.get("sink") or not ffmpeg:
        return None
    wavs = {}
    for name, hz in SELFTEST_TONES.items():          # the tones as files, made before anything listens
        w = base_dir() / f"selftest-tone-{hz}.wav"
        try:
            subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                            f"sine=frequency={hz}:duration=1.2", str(w)], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if w.exists() and w.stat().st_size > 1000:
            wavs[name] = w
    cap = base_dir() / "selftest-sound.raw"
    cap.unlink(missing_ok=True)
    try:
        proc = subprocess.Popen([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "pulse", "-i", f"{inf['sink']}.monitor",
                                 "-t", "3.2", "-ac", "1", "-ar", "16000", "-f", "s16le", str(cap)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    time.sleep(0.4)
    played = []
    if "PulseAudio" in wavs:
        paplay = shutil.which("paplay")
        cmd = [paplay, str(wavs["PulseAudio"])] if paplay else \
            [ffmpeg, "-v", "error", "-nostdin", "-re", "-i", str(wavs["PulseAudio"]), "-f", "pulse", "dolly-stage-check"]
        if exec_on(cmd):
            played.append("PulseAudio")
    if "PipeWire" in wavs and shutil.which("pw-play") and exec_on(["pw-play", str(wavs["PipeWire"])]):
        played.append("PipeWire")
    return {"proc": proc, "file": cap, "played": played}


def tones_in(pcm: bytes, rate: int, tones: dict) -> dict:
    """Which of `tones` ({name: Hz}) are in this sound (16-bit mono): {name: True/False}."""
    import numpy as np
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], np.int16).astype(np.float32)
    if len(x) < rate // 4:
        return {k: False for k in tones}
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1.0 / rate)
    top = float(spec[freqs > 100].max())            # the loudest tone in it
    out = {}
    for name, hz in tones.items():
        band = spec[(freqs > hz - 12) & (freqs < hz + 12)]
        # A tone that was played is about as strong as the strongest. One that was not still
        # shows faintly: a tone that starts and stops spreads a little over all frequencies
        # (0.1 % of its strength 300 Hz away), which a fixed threshold took for the tone.
        out[name] = bool(len(band) and top > 2e5 and float(band.max()) >= 0.1 * top)
    return out


def _sound_end(snd: dict | None, inf: dict) -> tuple[bool | None, str]:
    if not inf.get("sink"):
        return False, ("the stage has no sound output of its own (pactl is missing or the sound server refused): "
                       "its programs would be heard on your speakers")
    if not snd:
        return None, "not checked (ffmpeg could not listen)"
    try:
        snd["proc"].wait(timeout=6)
    except subprocess.TimeoutExpired:
        snd["proc"].kill()
    try:
        pcm = snd["file"].read_bytes()
    except OSError:
        pcm = b""
    for f in (snd["file"], *base_dir().glob("selftest-tone*.wav")):
        f.unlink(missing_ok=True)
    if not snd["played"]:
        return None, "not checked (no program could be started to play a tone)"
    heard = tones_in(pcm, 16000, {k: SELFTEST_TONES[k] for k in snd["played"]})
    lost = [k for k, v in heard.items() if not v]
    if lost:
        return False, ("a program on the stage that plays through " + " and ".join(lost) + " was NOT heard on the "
                       "stage's private output: it may be heard on your speakers")
    return True, "programs on the stage play into its private output, not your speakers (" + ", ".join(heard) + ")"


def _selftest_record(inf: dict, seconds: float = 1.4) -> tuple[Path, float | None, str]:
    """Record the stage for a moment with a pointer mark in it. -> (file, the mark's time
    after the recorder was launched or None, what went wrong)."""
    out = base_dir() / "selftest.mkv"
    out.unlink(missing_ok=True)
    env = dict(os.environ, **env_for(inf))
    env.pop("DISPLAY", None)
    cmd = recorder_cmd(["wf-recorder", "-o", inf.get("output") or OUTPUT, "-r", "60", "-c", "libx264",
                        "-p", "preset=veryfast", "-p", "crf=16", "-x", "yuv420p", "-f", str(out)])
    log = open(base_dir() / "selftest-recorder.log", "wb")
    old = dict(os.environ)
    try:
        proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except OSError as e:
        log.close()
        return out, None, f"wf-recorder could not be started ({e.strerror or e})"
    t_launch = time.monotonic()
    mark = None
    try:
        os.environ.update(env_for(inf))             # the pointer calls below talk to the stage
        _move(300, 300)
        end = time.monotonic() + 0.6
        while time.monotonic() < end and proc.poll() is None:
            nudge()
            time.sleep(0.05)
        if proc.poll() is None:
            mark = pointer_mark(MARK_HOLD_S)
            end = time.monotonic() + max(0.3, seconds - (time.monotonic() - t_launch))
            while time.monotonic() < end:
                nudge()
                time.sleep(0.06)
            recorder_drained(proc.pid, least=0.3, most=2.5)     # as a take does before it stops its recorder
            os.kill(proc.pid, signal.SIGINT)
            end = time.monotonic() + 12.0
            while time.monotonic() < end and proc.poll() is None:
                nudge()                             # it only acts on the signal when a frame arrives
                time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()
            return out, None, "wf-recorder did not stop"
    finally:
        os.environ.clear()
        os.environ.update(old)
        log.close()
    return out, (mark["t"] - t_launch if mark else None), ""


def selftest(size=None) -> dict:
    """`dolly doctor --stage`: use a stage the way a take does and say, step by step, what
    worked: start (or the running stage), a drawn frame, a second of recording, the sync
    mark in that recording, where the stage's sound goes, stop. Each step is
    {"name", "ok": True | False | None (not tried), "detail", "seconds"}."""
    res = {"ok": False, "started": False, "seconds": None, "problems": [], "steps": []}
    t0 = time.monotonic()
    tmp = base_dir() / "selftest.png"
    inf = None

    def step(name: str, fn):
        t = time.monotonic()
        try:
            ok, detail = fn()
        except StageError as e:
            ok, detail = False, str(e)
        except Exception as e:                       # noqa: BLE001 - a self test reports, it never raises
            ok, detail = False, f"{type(e).__name__}: {e}"
        res["steps"].append({"name": name, "ok": ok, "detail": detail, "seconds": round(time.monotonic() - t, 2)})
        if ok is False:
            res["problems"].append(f"{name}: {detail}")
        return ok

    def s_start():
        nonlocal inf
        inf = info()
        if inf:
            how = "the running stage"
        else:
            inf = start(size)
            res["started"] = True
            how = f"started in {time.monotonic() - t0:.1f} s"
        res["size"] = inf.get("size")
        limit = (f"memory limit {inf.get('memory_mb')} MB" if shutil.which("systemd-run")
                 else "NO memory limit (systemd-run is missing)")
        return True, f"{inf['size'][0]}x{inf['size'][1]}, {how}, {limit}, stops by itself after {inf.get('max_minutes'):g} min"

    def s_frame():
        shot(tmp)
        from PIL import Image
        with Image.open(tmp) as im:
            res["frame"] = list(im.size)
        if list(res["frame"]) != list(inf.get("size") or res["frame"]):
            return False, f"the frame is {res['frame'][0]}x{res['frame'][1]}, not the stage's size"
        bg = "the desktop picture" if inf.get("background") == "wallpaper" else \
            "the theme's colour" + (f" ({inf['background_note']})" if inf.get("background_note") else "")
        res["background"] = inf.get("background")
        return True, f"{res['frame'][0]}x{res['frame'][1]} drawn; background: {bg}"

    rec: dict = {}

    def s_record():
        if take_running():
            return None, "not tried: a take is being recorded on the stage"
        if not shutil.which("wf-recorder"):
            return False, "wf-recorder is not installed: omarchy-pkg-add wf-recorder"
        f, mark_at, err = _selftest_record(inf)
        rec.update({"file": f, "mark_at": mark_at})
        if err:
            return False, err
        from .capture import video_info
        try:
            vi = video_info(f)
        except Exception:                            # noqa: BLE001
            vi = {}
        if not f.exists() or not vi.get("w"):
            return False, "wf-recorder wrote no video (" + (_tail_of(base_dir() / "selftest-recorder.log") or
                                                            "it said nothing") + ")"
        rec["info"] = vi
        res["recording"] = {"w": vi.get("w"), "h": vi.get("h"), "seconds": round(float(vi.get("duration") or 0), 2)}
        return True, f"{vi.get('w')}x{vi.get('h')}, {float(vi.get('duration') or 0):.1f} s of video (wf-recorder)"

    def s_mark():
        if not rec.get("info"):
            return None, "not tried: there is no recording to look in"
        if rec.get("mark_at") is None:
            return False, "the stage's pointer could not be moved"
        from . import sync as syncmod
        vi = rec["info"]
        size = max(4, int(round(syncmod.MARKER_LOGICAL * (vi["w"] / (inf["size"][0] or vi["w"])))))
        frames, pts = syncmod._probe_window(rec["file"], size, shutil.which("ffmpeg") or "ffmpeg", head=8.0)
        runs = syncmod.pointer_runs(frames, pts) if len(frames) else []
        res["mark"] = {"frames": int(len(frames)), "found": len(runs)}
        if not runs:
            return False, (f"the pointer's jump into the corner was not found in the recording ({len(frames)} frames "
                           "read): takes on this stage would be timed by the clock, about 0.2 s loose")
        a, b = runs[0]
        return True, f"found in the recording ({b - a + 1.0 / max(vi.get('fps') or 60, 1):.2f} s long, {len(frames)} frames read)"

    def s_stop():
        if not res["started"]:
            return None, "not tried: the stage was already running and is left running"
        stop()
        left = sweep_report()
        return (False, "left behind: " + ", ".join(left)) if left else (True, "stopped, nothing left behind")

    snd = None
    try:
        if step("start", s_start):
            step("frame", s_frame)
            try:
                snd = _sound_begin(inf)
            except Exception:                        # noqa: BLE001
                snd = None
            step("recording", s_record)
            step("sync mark", s_mark)
            step("sound", lambda: _sound_end(snd, inf))
    finally:
        for f in base_dir().glob("selftest*"):
            f.unlink(missing_ok=True)
        if res["started"] or inf:
            step("stop", s_stop)
        if res["started"] and info():                # a step raised before the stage could be stopped
            try:
                stop()
            except Exception as e:                   # noqa: BLE001
                res["problems"].append(f"could not stop the stage: {e}")
    res["ok"] = bool(res["steps"]) and not res["problems"]
    res["seconds"] = round(time.monotonic() - t0, 2)
    return res


def _tail_of(p: Path, n: int = 3) -> str:
    try:
        return " | ".join(l.strip() for l in p.read_text(errors="replace").strip().splitlines()[-n:])[-300:]
    except OSError:
        return ""


def selftest_lines(res: dict) -> list[str]:
    """The self test in plain words, one line a step."""
    word = {True: "ok     ", False: "FAILED ", None: "skipped"}
    out = [f"  {word[s['ok']]} {s['name']}: {s['detail']}" for s in res.get("steps", [])]
    out.append(("hidden stage: ok" if res.get("ok") else "hidden stage: NOT working") + f" ({res.get('seconds'):g} s)")
    return out


def sweep_report() -> list[str]:
    """What of a stage is still around although none is running (plain words; nothing is changed)."""
    if info():
        return []
    out = []
    real = real_session()
    for i in instances():
        if i["stage"] and not (real and i["sig"] == real["sig"]):
            out.append(("a running stage with no record" if i["alive"] else "session files of a dead stage")
                       + f" ({i['sig'][-10:]})")
    if _sink_modules():
        out.append("the stage's sound output")
    if _read():
        out.append("a state file of a stage that is gone")
    return out


# ------------------------------------------------------------------ status
def status() -> dict:
    inf = info()
    if not inf:
        return {"running": False, "leftovers": sweep_report()}
    sig = inf["sig"]
    clients = hypr_json(sig, "clients") or []
    left = max(0.0, float(inf.get("until") or 0) - time.time())
    return {"running": True, "size": inf.get("size"), "output": inf.get("output"), "wayland_display": inf.get("wl"),
            "signature": sig, "pid": inf.get("pid"), "minutes_left": round(left / 60, 1),
            "memory_mb": inf.get("memory_mb"), "sound": inf.get("sink"), "look": inf.get("look"),
            "background": inf.get("background") or "color", "background_note": inf.get("background_note") or "",
            "recording": take_running(),
            "windows": [{"class": c.get("class"), "title": str(c.get("title", ""))[:80], "address": c.get("address")}
                        for c in clients],
            "env": env_for(inf)}


# ------------------------------------------------------------------ command line
def cli(a) -> int:
    """dolly stage start|stop|status|shot|exec|type|key|click|move"""
    as_json = bool(getattr(a, "json", False))

    def out(obj: dict, text: str, code: int = 0) -> int:
        print(json.dumps(obj) if as_json else text, file=sys.stdout if as_json or code == 0 else sys.stderr)
        return code
    try:
        if a.stage_cmd == "start":
            inf = start(getattr(a, "size", None), background=getattr(a, "background", None))
            w, h = inf["size"]
            back = "the desktop picture" if inf.get("background") == "wallpaper" else \
                "the theme's colour" + (f" ({inf['background_note']})" if inf.get("background_note") else "")
            return out({"ok": True, **status()},
                       f"stage started: {w}x{h}, invisible, its own pointer and keyboard\n"
                       f"  background: {back}\n"
                       f"  limits: {inf['memory_mb']} MB, stops by itself after {inf['max_minutes']:g} min\n"
                       "  use it:  dolly run demo.toml --stage   |   dolly record --stage --for 10 --json\n"
                       "  drive it:  dolly stage exec -- foot   |   dolly stage type 'ls' --enter   |   "
                       "dolly stage key ctrl+l   |   dolly stage click 640 360\n"
                       "  look at it:  dolly stage shot   |   stop it:  dolly stage stop")
        if a.stage_cmd == "type":
            r = do_type(a.text, speed=getattr(a, "speed", None), enter_key=bool(getattr(a, "enter", False)))
            return out(r, f"typed {r['typed']} character{'s' if r['typed'] != 1 else ''}"
                       + (" and Enter" if r["enter"] else ""))
        if a.stage_cmd == "key":
            r = do_key(list(a.keys or []))
            return out(r, "pressed " + " ".join(r["keys"]))
        if a.stage_cmd == "move":
            r = do_move(a.x, a.y, instant=bool(getattr(a, "instant", False)))
            return out(r, f"pointer at {r['x']},{r['y']}")
        if a.stage_cmd == "click":
            r = do_click(getattr(a, "x", None), getattr(a, "y", None), button=getattr(a, "button", None) or "left",
                         instant=bool(getattr(a, "instant", False)))
            return out(r, f"clicked {r['button']} at {r['x']},{r['y']}")
        if a.stage_cmd == "stop":
            r = stop()
            text = "stage stopped" if r["was_running"] else "no stage was running"
            if r["removed"]:
                text += "; removed " + ", ".join(sorted(set(r["removed"])))
            return out(r, text)
        if a.stage_cmd == "status":
            s = status()
            if not s["running"]:
                text = "no stage is running" + ("; left behind: " + ", ".join(s["leftovers"]) +
                                                " (dolly stage stop removes it)" if s["leftovers"] else "")
                return out(s, text, 0 if as_json else 1)
            w, h = s["size"]
            lines = [f"stage running: {w}x{h}, {s['minutes_left']:g} min left, "
                     + ("recording" if s["recording"] else "not recording"),
                     "  background: " + ("the desktop picture" if s["background"] == "wallpaper" else "the theme's colour"
                                         + (f" ({s['background_note']})" if s["background_note"] else "")),
                     f"  windows: {len(s['windows'])}" + "".join(f"\n    {x['class']}: {x['title']}" for x in s["windows"])]
            return out(s, "\n".join(lines))
        if a.stage_cmd == "shot":
            p = shot(getattr(a, "file", None))
            return out({"ok": True, "file": str(p)}, str(p))
        if a.stage_cmd == "exec":
            argv = [x for x in (a.command or []) if x != "--"] if a.command and a.command[0] == "--" else (a.command or [])
            if not argv:
                return out({"ok": False, "error": "nothing to start"}, "dolly stage exec: which program? "
                           "(dolly stage exec -- foot)", 2)
            ok = exec_on(argv)
            return out({"ok": ok}, "started" if ok else "the stage refused to start it", 0 if ok else 1)
    except StageError as e:
        return out({"ok": False, "error": str(e)}, f"dolly stage: {e}", 1)
    return 2


def main(argv=None) -> int:
    """`python -m dolly.stage reap OWNER`: run by the watcher when the command that started a
    stage for itself has gone."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "reap":
        d = _read()
        try:
            owner = int(argv[1])
        except ValueError:
            return 2
        if d and d.get("owner") == owner and not take_running():
            stop()
        elif not d:
            sweep()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Recording orchestration.

    dolly record  -> pick target, start recorder + event logger, write state
    dolly stop    -> SIGINT recorder, stop logger, finalize, notify, open editor
    dolly toggle  -> stop if recording else record
    dolly status  -> exit 0 while recording (prints elapsed seconds / JSON)

Backends (first that starts wins; `doctor` reports which applies):

    gsr       gpu-screen-recorder, GPU encode (VAAPI/NVENC), -cursor no, -fm cfr
    gsr-cpu   gpu-screen-recorder -encoder cpu  (x264; VMs with GL but no VAAPI)
    wf        wf-recorder -c libx264 -r FPS (CFR; captures via wlr-screencopy,
              works with llvmpipe/software rendering). wf-recorder has no
              option to hide the cursor, so the cursor is baked in and Dolly
              does not draw its own.

The recording is raw material (high bitrate). Polishing happens in
`dolly export`, which always re-encodes to clean CFR H.264.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import project as projmod
from .i18n import t
from .logger import hypr_json, on_stage, runtime_dir
from . import fsutil
from . import log as _dlog

STATE = "state.json"


# ---------------------------------------------------------------- helpers
def _have(cmd):
    return shutil.which(cmd) is not None


def notify(title, body="", *actions, urgency="normal", timeout=4000, image=None):
    """omarchy-notification-send (glyph, urgency, actions, preview image) like
    Omarchy's own recorder; notify-send elsewhere. Strings come from dolly.i18n.
    Nothing is shown for a take on the hidden stage: cards go over D-Bus, which both
    desktops share, so they would land on the real one. The log keeps the words."""
    if on_stage():
        _dlog.get().info("stage (no card): %s%s", title, f": {body}" if body else "")
        return
    if _have("omarchy-notification-send"):
        cmd = ["omarchy-notification-send", "--app-name", "Dolly", "-g", "󰑊", "-u", urgency, "-t", str(timeout)]
        if image and Path(image).exists():
            cmd += ["--image", str(image)]
        cmd.append(title)
        if body:
            cmd.append(body)
        for label, *prog in actions:
            cmd += ["--action", label, *prog]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    elif _have("notify-send"):
        subprocess.Popen(["notify-send", "-u", urgency, "-t", str(timeout), title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print(f"{title}: {body}", file=sys.stderr)


GONE_PATIENCE = 75                    # 0.02 s steps: how long a recorder without a monitor gets
NUDGE_AT = (3, 25, 75, 150, 300)      # 0.02 s steps after SIGINT: 0.06 s, 0.5 s, 1.5 s, 3 s, 6 s


def _nudge_screen(region=None):
    """wf-recorder notices SIGINT only when its next frame arrives, and on a still screen
    none does: it sat out the 10 s limit and was killed (seen once the bar button stopped
    repainting every few seconds). Mapping and unmapping Dolly's transparent marker surface
    repaints one corner of the screen, which is enough. It comes after the stop marker, so
    it is outside the exported range, and it draws nothing. On the hidden stage there is no
    shell: the stage's own pointer moves one pixel aside and back.
    `region` is the recorded area: the corner has to be on the monitor being recorded
    (the first monitor's corner gives a recorder of the second one no frame)."""
    if on_stage():
        try:
            from . import stage
            stage.nudge()
        except Exception:
            _dlog.swallowed()
        return
    if not _have("omarchy-shell"):
        return
    geo = json.dumps({"x": region[0], "y": region[1], "size": 40}) if region else "{}"
    for call in (("syncArm", geo), ("syncDisarm",)):
        try:
            subprocess.run(["omarchy-shell", "io.github.jagritvats.dolly", *call], timeout=1.5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return


def _stage_pointer_home():
    """On the hidden stage the stop mark leaves the pointer in the corner until the recorder
    has gone: take it back to where the take left it."""
    if on_stage():
        try:
            from . import stage
            stage.pointer_home()
        except Exception:
            _dlog.swallowed()


def _dismiss_own_toasts() -> bool:
    """Dolly's own card from the last take stays for 10 s; a take started within that time
    must not open with it. Other apps' notifications are left alone (Do Not Disturb and
    the automatic privacy blur deal with those). True when the shell says a card went."""
    if on_stage() or not _have("omarchy-shell"):
        return False
    try:
        r = subprocess.run(["omarchy-shell", "notifications", "dismiss", t("notify.recorded")], timeout=1.5,
                           capture_output=True, text=True)
        return (getattr(r, "stdout", "") or "").strip() == "ok"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _dismiss_toast(summary: str | None):
    """Take one of Dolly's own cards off the screen (by its title)."""
    if not summary or on_stage() or not _have("omarchy-shell"):
        return
    try:
        subprocess.run(["omarchy-shell", "-q", "notifications", "dismiss", summary], timeout=1.5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _toasts_at_start() -> int:
    """Other apps' cards on screen when Record is pressed. Dolly's own card from the last
    take is dismissed first and given a moment to leave the shell's count."""
    n = _toast_count()
    if _dismiss_own_toasts() and n:
        end = time.monotonic() + 0.4
        while time.monotonic() < end:
            m = _toast_count()
            if m < n:
                return m
            time.sleep(0.05)
    return n


def _wanted_fps(a) -> int:
    try:
        if getattr(a, "fps", None):
            return int(a.fps)
        from .config import load
        return int(load()["capture"]["fps"])
    except Exception:
        _dlog.swallowed()
        return 60


def _record_argv(a) -> list[str]:
    """`dolly record ...` with the same choices, for the "Record anyway" action."""
    out = ["record"]
    for k in ("mode", "audio", "camera", "fps", "countdown", "solid"):
        v = getattr(a, k, None)
        if v is not None:
            out += [f"--{k}", str(v)]
    if getattr(a, "backend", None) not in (None, "auto"):
        out += ["--backend", a.backend]
    for flag in ("no_keys", "no_dnd"):
        if getattr(a, flag, None):
            out.append("--" + flag.replace("_", "-"))
    return out + ["--ignore-disk"]


def _unwritable(root: Path) -> str | None:
    """Why recordings can't be written to `root` (None = fine). Checked before any picker,
    countdown or DND, so a read-only or missing-permission folder fails at once with a toast
    instead of a traceback after the countdown, and leaves no half-made project folder."""
    import tempfile
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".dolly-write-test-", dir=root)
        os.close(fd)
        os.unlink(probe)
        return None
    except OSError as e:
        return e.strerror or str(e)


def _notify_cannot_record(root: Path, why: str):
    dolly = str(Path(__file__).resolve().parents[1] / "bin" / "dolly")
    notify(t("notify.cannotRecordHere"), t("notify.cannotRecordHereBody", str(root), why),
           (t("notify.chooseFolder"), dolly, "settings"), urgency="critical", timeout=10000)


def _notify_disk(a, plan: dict):
    """Too little space (block) or little / RAM-backed space (warn), with what to do:
    Record anyway (block only), Choose folder (Settings > Storage), Clean up (raw files
    of exported recordings, when there are any)."""
    dolly = str(Path(__file__).resolve().parents[1] / "bin" / "dolly")
    acts = []
    if plan["level"] == "block":
        acts.append((t("notify.recordAnyway"), dolly, *_record_argv(a)))
    acts.append((t("notify.chooseFolder"), dolly, "settings"))
    try:
        from .library import _load_cache
        cleanable = int(_load_cache().get("cleanable_bytes") or 0)
    except Exception:
        cleanable = 0
    if cleanable >= 50 << 20:
        acts.append((t("notify.cleanUp", f"{cleanable / (1 << 30):.1f} GB"), dolly, "library", "cleanup"))
    if plan["level"] == "block":
        title = t("notify.lowDisk")
    elif plan["tmpfs"] and (plan["minutes"] is None or plan["minutes"] >= health_warn_minutes()):
        title = t("notify.diskTmpfs")
    else:
        title = t("notify.diskLow")
    notify(title, plan["message"], *acts, urgency="critical" if plan["level"] == "block" else "normal",
           timeout=12000 if plan["level"] == "block" else 8000)


def health_warn_minutes() -> float:
    from . import health
    return health.WARN_MINUTES


def _notify_missing(need: dict):
    """'Dolly needs: wf-recorder (gpu-screen-recorder cannot capture here ...)' + Install."""
    from . import deps
    dolly = str(Path(__file__).resolve().parents[1] / "bin" / "dolly")
    lines = [f"{p}: {need['why'].get(p, '')}" for p in need["packages"]]
    body = t("notify.needsPackagesBody", "\n".join(lines), deps.install_command(need["packages"]))
    notify(t("notify.needsPackages"), body, (t("notify.install"), dolly, "deps", "--install"),
           urgency="critical", timeout=0)


def shell_refresh():
    if _have("omarchy-shell") and not on_stage():
        subprocess.Popen(["omarchy-shell", "-q", "io.github.jagritvats.dolly", "refresh"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def state_path() -> Path:
    return runtime_dir() / STATE


def load_state_any() -> dict | None:
    """The state file even if the recorder already died (watchdog stop)."""
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return None


def read_state() -> dict | None:
    try:
        st = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return None
    pid = st.get("recorder_pid")
    if not pid or not _alive(pid):
        return None
    return st


def _alive(pid) -> bool:
    from .health import pid_alive
    return pid_alive(pid)


def focused_monitor() -> dict | None:
    mons = hypr_json("monitors") or []
    for m in mons:
        if m.get("focused"):
            return m
    return mons[0] if mons else None


def monitor_logical(m: dict) -> tuple[list[int], float]:
    """Logical rect + effective scale (see dolly/monitors.py: exact, not floor/round)."""
    from .monitors import effective_scale, logical_rect
    return logical_rect(m), effective_scale(m)


def _picker(kind: str) -> str | None:
    """omarchy-capture-region <kind> --match-monitor (prints "monitor:NAME" or
    "X,Y WxH"), else slurp. None = cancelled."""
    if _have("omarchy-capture-region"):
        # no timeout: an interactive picker waits for the user's pick (Esc cancels it)
        r = subprocess.run(["omarchy-capture-region", kind, "--match-monitor"], capture_output=True, text=True)
        return r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else None
    if _have("slurp"):
        return _slurp(["-r"] if kind == "windows" else [])
    raise SystemExit("dolly: no region picker (omarchy-capture-region or slurp)")


def pick_target(mode: str, pick: str | None = None, mons: list | None = None):
    """-> monitors.Target. Screen = the focused monitor (as Omarchy's fullscreen
    recording). Region = `smart --match-monitor` (a bare click snaps to a window,
    a whole-monitor pick records that monitor with -w NAME). Window = `windows`.
    The target's monitor is the one holding the pick, not the focused one."""
    from .monitors import SpanError, monitor_target, resolve_pick
    mons = mons if mons is not None else (hypr_json("monitors") or [])
    if not mons:
        raise SystemExit("dolly: cannot talk to Hyprland (is HYPRLAND_INSTANCE_SIGNATURE set?)")
    if mode == "screen" and pick is None:
        m = next((m for m in mons if m.get("focused")), mons[0])
        return monitor_target(m)
    if pick is None:
        pick = _picker("windows" if mode == "window" else "smart")
    if not pick:
        raise Cancelled()
    try:
        tg = resolve_pick(pick, mons)
    except SpanError as e:
        notify(t("notify.pickOneMonitor"), str(e), urgency="normal", timeout=5000)
        raise Cancelled() from e
    if tg.note:
        notify(t("notify.spanClampedTitle"), t("notify.spanClamped", tg.monitor), timeout=3500)
    return tg


class Cancelled(Exception):
    """Esc in the region/window picker (or the countdown cancelled): nothing to clean up."""


def window_at(geo_rect) -> dict | None:
    """The Hyprland client whose geometry matches a picked rect (logical)."""
    x, y, w, h = geo_rect
    best, err = None, 1e9
    for c in hypr_json("clients") or []:
        (cx, cy), (cw, ch) = c.get("at", (0, 0)), c.get("size", (0, 0))
        e = abs(cx - x) + abs(cy - y) + abs(cw - w) + abs(ch - h)
        if e < err:
            best, err = c, e
    return best if err < 40 else None


def _monitor_snapshot() -> list[dict]:
    from .monitors import effective_scale, logical_rect
    out = []
    for m in hypr_json("monitors") or []:
        try:
            out.append({"name": m["name"], "rect": logical_rect(m), "scale": effective_scale(m),
                        "scale_reported": m.get("scale"), "transform": m.get("transform", 0),
                        "physical": [m["width"], m["height"]]})
        except (KeyError, TypeError):
            pass
    return out


def monitor_of(x, y, mons: list | None = None) -> dict | None:
    from .monitors import monitor_at
    return monitor_at(mons if mons is not None else (hypr_json("monitors") or []), x, y)


def _slurp(args):
    # no timeout: an interactive picker waits for the user's pick (Esc cancels it)
    r = subprocess.run(["slurp", *args, "-f", "%x,%y %wx%h"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


class RecorderGone(Exception):
    """The recorder died while the countdown was running."""


def countdown(n: int, alive=None, last_call=None, last_call_s: float = 0.5):
    """3-2-1 in the bar (phase 'countdown') and as a toast; `dolly toggle`/`cancel`
    or a bar click during it cancels (SIGTERM to this process).

    The recorder and the event logger are already running when this is called (see
    `start`), so the take begins the instant it returns and the countdown lasts exactly
    n seconds. `alive()` returning False raises RecorderGone; `last_call()` runs once,
    `last_call_s` before the end (the camera preview closes there, before the take)."""
    from .health import set_phase

    def _cancel(*_):
        raise Cancelled()
    old = signal.signal(signal.SIGTERM, _cancel)
    shown = None
    try:
        t_end = time.monotonic() + n
        for i in range(n, 0, -1):
            set_phase("countdown", left=i)
            _dismiss_toast(shown)          # one card at a time, and none may outlive the countdown:
            shown = t("notify.countdown", i)       # the shell keeps a card longer than it is asked to
            notify(shown, t("notify.countdownBody", "Super+Alt+R"), timeout=900 if i > 1 else 550)
            tick_end = t_end - (i - 1)
            while True:
                left = tick_end - time.monotonic()
                if alive is not None and not alive():
                    raise RecorderGone()
                if i == 1 and last_call is not None and left <= last_call_s:
                    last_call()
                    last_call = None
                if i == 1 and shown and left <= 0.35:
                    _dismiss_toast(shown)  # gone before the take starts
                    shown = None
                if left <= 0:
                    break
                time.sleep(min(0.05, left))
    finally:
        signal.signal(signal.SIGTERM, old)
        _dismiss_toast(shown)


# ---------------------------------------------------------------- backends
def backend_order(pref: str) -> list[str]:
    if on_stage():
        return ["wf"]               # the GPU recorder cannot see an output that exists on no graphics card
    forced = os.environ.get("DOLLY_BACKEND", pref)
    if forced and forced != "auto":
        return ["wf" if forced == "wf-recorder" else forced]
    order = []
    if _have("gpu-screen-recorder"):
        order += ["gsr", "gsr-cpu"]
    if _have("wf-recorder"):
        if order and _gsr_known_unusable():
            order = []              # two failed launches on every start are time the take does not have
        order.append("wf")
    return order or ["gsr"]


def _gsr_known_unusable() -> bool:
    """A note from an earlier failed start or check says gpu-screen-recorder can never
    capture on this machine (a VM, no usable GPU; see dolly/deps.py). This only reads the
    note: nothing is probed while a recording is starting."""
    try:
        from . import deps
        gsr = shutil.which("gpu-screen-recorder")
        n = json.loads(deps._note_path().read_text())
        return bool(n.get("reason")) and n.get("gsr") == gsr and abs(n.get("mtime", 0) - os.path.getmtime(gsr)) < 1
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _recorder_up(proc, raw: Path, log_path: Path, log_from: int, backend: str, limit: float = 3.0) -> bool:
    """Wait until the recorder is capturing (True) or has died (False).

    Proof of capture is a video file with bytes in it, or the encoder's start-up lines in
    the recorder's output. wf-recorder gives neither while the screen is still: it gets a
    frame only when something on screen changes and holds the first one until the second
    (VM, Nov 11: 0 bytes after 5 s on a still desktop, so every take sat out the whole
    `limit` here before the event logger was even started). Once it has connected and
    picked its region it is listening; the bar changing a moment later gives it a frame."""
    t0 = time.monotonic()
    repaint_at = 0.1
    while True:
        if proc.poll() is not None:
            return False
        try:
            if raw.stat().st_size > 0:
                return True
        except OSError:
            pass
        el = time.monotonic() - t0
        if backend == "wf" and on_stage() and el >= repaint_at:
            # a stage with nothing moving on it never sends that first frame: the take began
            # two seconds late, and its clock was off by as much
            _nudge_screen()
            repaint_at = el + 0.15
        if backend == "wf" and el >= 0.15:
            try:
                with open(log_path, "rb") as f:
                    f.seek(log_from)
                    out = f.read()
            except OSError:
                out = b""
            if b"Output #0" in out or (b"selected region" in out and el >= 0.3):
                return True
        if el >= limit:
            return proc.poll() is None
        time.sleep(0.02)


def _end_process(pid, sig, wait: float):
    """Signal a process, give it `wait` seconds, then kill it."""
    if not pid or not _alive(pid):
        return
    try:
        os.kill(pid, sig)
    except OSError:
        return
    end = time.monotonic() + wait
    while time.monotonic() < end and _alive(pid):
        time.sleep(0.05)
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _toast_count() -> int:
    """Notification cards on screen right now (the shell's own count); 0 when unknown."""
    if on_stage() or not _have("omarchy-shell"):
        return 0
    try:
        r = subprocess.run(["omarchy-shell", "notifications", "count"], capture_output=True, text=True, timeout=1.5)
        return max(0, int((r.stdout or "").strip()))
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return 0


def _pactl_out(*args) -> str:
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        _dlog.swallowed("pactl " + " ".join(args))
        return ""


def recorder_cmd(backend: str, target, out: Path, fps: int, audio: list[str]) -> list[str]:
    """`target` is a monitors.Target. Geometry mirrors omarchy-capture-screenrecording:
    gsr `-w NAME -s RES` for a monitor (RES caps above 4K, else 0x0 = native) or
    `-w WxH+X+Y` (logical; gsr scales to physical itself); wf-recorder `-o NAME`
    or `-g "X,Y WxH"` (same logical space)."""
    if backend in ("gsr", "gsr-cpu"):
        cmd = ["gpu-screen-recorder", *target.gsr_args()]
        cmd += ["-f", str(fps), "-fm", "cfr", "-cursor", "no", "-k", "h264", "-q", "ultra",
                "-c", out.suffix.lstrip(".") or "mkv", "-o", str(out)]
        if backend == "gsr-cpu":
            cmd += ["-encoder", "cpu"]
        else:
            cmd += ["-fallback-cpu-encoding", "yes"]
        if audio:
            cmd += ["-a", "|".join(audio), "-ac", "aac"]
        return cmd
    if backend == "wf":
        if len(audio) > 1:
            # wf-recorder records ONE audio device: never drop a source silently. Dolly records
            # audio as separate tracks (dolly/audio.py), so this path is only for callers
            # that ask the recorder itself for audio.
            raise ValueError("wf-recorder records one audio device; record mic and system as separate tracks")
        cmd = ["wf-recorder", *target.wf_args(), "-r", str(fps), "-c", "libx264",
               "-p", "preset=veryfast", "-p", "crf=16", "-x", "yuv420p", "-f", str(out)]
        if audio:
            dev = None
            if "default_output" in audio and _have("pactl"):
                sink = _pactl_out("get-default-sink")
                dev = sink + ".monitor" if sink else None
            if not dev and _have("pactl") and any(a != "default_output" for a in audio):
                src = next(a for a in audio if a != "default_output")
                dev = src if src != "default_input" else \
                    _pactl_out("get-default-source")
            if dev:
                cmd += [f"--audio={dev}", "-R", "48000"]
        return cmd
    raise ValueError(backend)


# ---------------------------------------------------------------- commands
def _notification_layers() -> list:
    """Where the shell shows toasts ([x, y, w, h], logical). Hyprland only knows this area,
    not each toast; privacy.auto_boxes finds the toasts inside it."""
    out = []
    try:
        for mon in (hypr_json("layers") or {}).values():
            for level in (mon.get("levels") or {}).values():
                for lay in level:
                    if "notif" in str(lay.get("namespace", "")).lower():
                        out.append([lay.get("x", 0), lay.get("y", 0), lay.get("w", 0), lay.get("h", 0)])
    except (AttributeError, TypeError):
        pass
    return out


def new_take_folder(root: Path, stamp: str | None = None) -> Path:
    """Make the folder of a new take: the time it starts, to the second. A take's folder is
    its own. Two takes can start in the same second (one on the hidden stage, one on the
    desktop: on 11 Nov both recorders wrote into one raw.mkv), so the folder is made, never
    adopted; the later take gets "-2"."""
    stamp = stamp or time.strftime("%Y-%m-%d_%H-%M-%S")
    root.mkdir(parents=True, exist_ok=True)
    for n in range(1, 100):
        pdir = root / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            pdir.mkdir()
            return pdir
        except FileExistsError:
            continue
    raise FileExistsError(f"{root / stamp}: too many takes started in one second")


def start(a):
    """Pick -> countdown -> recorder -> logger. Raw video goes to raw.mkv: Matroska stays
    playable when the recorder is killed (crash, power loss), unlike an MP4 without
    its index. Window mode records the monitor and follows the picked window."""
    from . import health
    t_invoke = float(os.environ.get("DOLLY_T0") or time.time())
    if read_state():
        notify(t("notify.alreadyRecording"), t("notify.alreadyRecordingBody"), urgency="normal", timeout=5000)
        raise SystemExit("dolly: already recording; use `dolly stop` (or the record hotkey / bar timer)")
    _recover_stale()
    health.heal(key_repeat=False)            # whatever an earlier, killed recording left changed on the desktop
    # a fresh Omarchy lacks the python packages, and a VM can't use gpu-screen-recorder:
    # say what is missing, with an Install button, before any disk note, picker, countdown or DND
    if not os.environ.get("DOLLY_BACKEND") and not os.environ.get("DOLLY_SKIP_DEPS"):
        from . import deps
        need = deps.missing()
        if need["packages"]:
            health.set_phase("idle")
            _notify_missing(need)
            raise SystemExit("dolly: missing packages: " + " ".join(need["packages"]) + " (dolly deps --install)")
    root = projmod.videos_root()
    problem = _unwritable(root)
    if problem:
        health.set_phase("idle")
        _notify_cannot_record(root, problem)
        raise SystemExit(f"dolly: cannot record into {root}: {problem}")
    plan = health.disk_plan(root, _wanted_fps(a))
    if plan["level"] == "block" and not getattr(a, "ignore_disk", False):
        _notify_disk(a, plan)
        raise SystemExit(plan["message"])
    other = health.other_recorder()
    if other:
        notify(t("notify.recordBusyTitle"), t("notify.recordBusyBody", other), urgency="normal", timeout=6000)
        health.set_phase("idle")
        raise SystemExit(f"dolly: {other} is already recording; stop it first")
    if plan["level"] == "warn":
        _notify_disk(a, plan)
    try:
        from . import hypr
        if not on_stage():
            hypr.marker_no_anim()  # a fading sync marker costs the first seconds of a still take
    except Exception:              # never refuse a recording over this
        pass
    toasts_before = _toasts_at_start()     # cards whose pixels can outlast the shell's count
    t_pick = time.monotonic()
    try:
        tgt = pick_target(a.mode, getattr(a, "pick", None))      # pick: named by --window/--monitor/--region
    except Cancelled:
        health.set_phase("idle")
        return
    if time.monotonic() - t_pick > 0.5:
        toasts_before = _toast_count()     # a pick by hand took a while: count again
    target_addr = None
    if a.mode == "window" and tgt.kind == "region":
        from .monitors import monitor_target
        addr = getattr(a, "window_address", None)                # named with --window: no geometry guess
        win = next((c for c in hypr_json("clients") or [] if c.get("address") == addr), None) if addr else None
        win = win or window_at(tgt.region)
        x, y, w, h = tgt.region
        mon = monitor_of(x + w / 2, y + h / 2)
        if win and mon:
            target_addr = win.get("address")
            tgt = monitor_target(mon)      # record the monitor, follow the window
    region, scale, monitor = tgt.region, tgt.scale, tgt.monitor
    try:
        pdir = new_take_folder(root)
    except OSError as e:                     # the folder changed under us since the check above
        health.set_phase("idle")
        _notify_cannot_record(root, e.strerror or str(e))
        raise SystemExit(f"dolly: cannot record into {root}: {e.strerror or e}") from None
    raw = pdir / ("raw.mp4" if os.environ.get("DOLLY_CONTAINER") == "mp4" else "raw.mkv")
    # from here until the state file exists the desktop is being changed for this take: say so,
    # so nothing (the watcher of the previous take, `dolly recover`) mistakes it for leftovers
    health.set_phase("starting")
    # quiet notifications while recording (Settings > Privacy; restored on stop)
    dnd_prev = health.dnd_begin() if getattr(a, "dnd", True) else None
    awake = health.awake_begin()             # no screensaver (150 s) or lock (300 s) inside a take
    from . import solid
    solid.restore()                         # leftovers from a crash: never keep the user's windows changed
    solid_on = solid.wanted(getattr(a, "solid", None))
    if solid_on:
        solid.make_solid()                  # Glassy: record solid windows, per window, reversibly
    cam_mode = getattr(a, "camera", "off") or "off"
    bubble = None
    if cam_mode in ("edit", "live"):
        # edit: a preview to frame yourself during the countdown only; live: stays on screen
        bubble = start_bubble(getattr(a, "camera_device", None), tgt.region, live=cam_mode == "live",
                              countdown=bool(a.countdown))

    def undo_setup():
        health.dnd_end(dnd_prev)
        health.awake_end(awake)
        solid.restore()
        stop_bubble(bubble)
        health.set_phase("idle")
    # Pre-roll: the recorder and the event logger start now, before the countdown, so the
    # take begins the instant the countdown ends ("go") and nothing pressed right after it
    # is lost. Before this, both were started after the countdown and needed 3-4 more
    # seconds (VM, Nov 10: a 3 s countdown took 6.7 s, and a key pressed in the gap was
    # not in the take). The countdown itself is recorded and cut by the default trim.
    n_count = max(0, int(a.countdown or 0))
    log = open(pdir / "recorder.log", "a")
    # audio: separate tracks (mic / system), started just before the recorder
    from . import audio as audiomod
    clock_wall, clock_mono = time.time(), time.monotonic()
    tracks, warn = [], []
    try:
        system_dev = getattr(a, "system_device", None) or audiomod.DEFAULT
        if on_stage():
            from . import stage as stagemod      # "system audio" of a stage take is the stage's own output
            system_dev = stagemod.system_audio_device() or system_dev
        tracks = audiomod.start_tracks(pdir, audiomod.resolve(getattr(a, "audio", "none") or "none",
                                                              getattr(a, "mic_device", None) or audiomod.DEFAULT,
                                                              system_dev), log)
    except ValueError as ex:
        warn.append(str(ex))
    for tr in tracks:
        if tr.get("error"):
            warn.append(t("notify.trackFailed", t(f"audio.{tr['track']}"), tr["error"]))
    chosen, proc = None, None
    for backend in backend_order(a.backend):
        cmd = recorder_cmd(backend, tgt, raw, a.fps, [])
        if on_stage():
            from . import stage as stagemod
            cmd = stagemod.recorder_cmd(cmd)        # a memory limit: nothing on a stage may take the session down
        log.write(f"\n=== {time.strftime('%F %T')} backend={backend}\n$ {' '.join(cmd)}\n")
        log.flush()
        try:
            log_from = os.path.getsize(pdir / "recorder.log")
        except OSError:
            log_from = 0
        try:
            proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
        except FileNotFoundError:
            continue
        if _recorder_up(proc, raw, pdir / "recorder.log", log_from, backend):
            chosen = backend
            break
        raw.unlink(missing_ok=True)
    if not chosen:
        audiomod.stop_tracks(tracks)
        undo_setup()
        log.flush()
        from . import deps
        try:
            reason = deps.note_gsr_failure((pdir / "recorder.log").read_text(errors="replace"))
        except OSError:
            reason = None
        need = deps.missing()
        if reason and need["packages"]:
            _notify_missing(need)
        else:
            notify(t("notify.cannotStart"), t("notify.cannotStartBody", f"{pdir}/recorder.log"),
                   urgency="critical", timeout=10000)
        raise SystemExit(1)

    t_ready = time.monotonic()
    ttff = time.time() - clock_wall
    if chosen == "wf" and backend_order(a.backend)[0] != "wf":
        try:                        # the GPU recorder was tried first and failed: if it can never
            from . import deps      # work here, remember that and go straight to wf-recorder next time
            log.flush()
            deps.note_gsr_failure((pdir / "recorder.log").read_text(errors="replace"))
        except Exception:
            _dlog.swallowed()
    cam_proc, cam_clock, cam_launch = None, None, None

    def start_camera():
        nonlocal cam_proc, cam_clock, cam_launch
        from . import camera as cammod
        cam_proc, cam_clock, err = cammod.start_recording(pdir, getattr(a, "camera_device", None) or None, log)
        cam_launch = time.monotonic() if cam_proc else None
        if err:
            warn.append(t("notify.cameraFailed", err))
    if cam_mode == "edit" and not n_count:
        stop_bubble(bubble)                 # the camera is recorded separately; never burned in
        bubble = None
        start_camera()
    elif cam_mode == "live" and bubble is None:
        warn.append(t("notify.cameraFailed", "mpv could not show the camera"))
    live_tracks = [tr for tr in tracks if tr.get("pid")]
    meta = {"backend": chosen, "region": region, "scale": scale, "monitor": monitor, "fps": a.fps,
            "target_info": tgt.to_meta(), "monitors": _monitor_snapshot(),
            "xwayland_zero_scaling": health.xwayland_zero_scaling(),
            "cursor_baked": chosen == "wf", "mode": a.mode, "target": target_addr, "started": time.time(),
            "t0_mono": t_ready, "clock_wall": clock_wall, "clock_mono": clock_mono,
            "recorder_pid": proc.pid, "container": raw.suffix.lstrip("."),
            "audio_mode": getattr(a, "audio", "none"), "audio_tracks": live_tracks, "audio_warnings": warn,
            "mic": any(tr["track"] == "mic" for tr in live_tracks),
            "desktop_audio": any(tr["track"] == "system" for tr in live_tracks),
            "camera_mode": cam_mode, "webcam_clock": cam_clock, "live_bubble": bool(bubble),
            "cursor_size": int(os.environ.get("HYPRCURSOR_SIZE") or os.environ.get("XCURSOR_SIZE") or 24),
            "notification_layers": _notification_layers(), "toasts_before": toasts_before,
            "terminals": TERMINALS, "solid_windows": solid_on, "ttff_ms": round(ttff * 1000), "hotkey_to_first_frame_ms":
            round((time.time() - t_invoke) * 1000), "countdown": n_count,
            "webcam": bool(cam_proc), "webcam_launch_mono": cam_launch}
    if on_stage():
        meta["stage"] = True                        # timed by the pointer mark, no cards, no shell
    fsutil.atomic_write_json(pdir / "capture.json", meta, indent=2)
    lg_cmd = [sys.executable, "-m", "dolly.cli", "log-events", str(pdir)] + (["--no-keys"] if a.no_keys else [])
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    logger = subprocess.Popen(lg_cmd, stdout=log, stderr=log, env=env, start_new_session=True)
    # "preroll": the countdown is still running; `dolly status` says countdown, `dolly stop`
    # cancels. The file exists from here on so a recorder is never left without an owner.
    st = {"dir": str(pdir), "recorder_pid": proc.pid, "logger_pid": logger.pid, "backend": chosen,
          "started": meta["started"], "webcam_pid": cam_proc.pid if cam_proc else None,
          "track_pids": [tr["pid"] for tr in live_tracks], "bubble_pid": bubble.pid if bubble else None,
          "region": region, "paused_total": 0.0, "paused_at": None, "dnd_prev": dnd_prev, "muted": False,
          "preroll": bool(n_count), "awake": awake}
    fsutil.atomic_write_json(state_path(), st, durable=False)
    health.watch_recorder(proc.pid)          # if the recorder (or Dolly) is killed, the desktop is still put back
    if n_count:
        def close_preview():
            nonlocal bubble
            if cam_mode == "edit":
                stop_bubble(bubble)         # the camera is recorded separately; never burned in
                bubble = None
        try:
            countdown(n_count, alive=lambda: proc.poll() is None, last_call=close_preview)
        except (Cancelled, RecorderGone) as ex:
            _end_process(logger.pid, signal.SIGTERM, 2.5)    # it switches the input bridge off as it leaves
            _end_process(proc.pid, signal.SIGINT, 3.0)
            proc.poll()
            audiomod.stop_tracks(tracks)
            state_path().unlink(missing_ok=True)
            undo_setup()
            log.close()
            if isinstance(ex, Cancelled):
                shutil.rmtree(pdir, ignore_errors=True)
                notify(t("notify.cancelled"), "", timeout=1500)
                return
            for f in list(pdir.iterdir()):                   # keep only the recorder's own words
                if f.name != "recorder.log":
                    shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
            notify(t("notify.cannotStart"), t("notify.cannotStartBody", f"{pdir}/recorder.log"),
                   urgency="critical", timeout=10000)
            raise SystemExit(1) from None
        if cam_mode == "edit":
            close_preview()
    # ---- "go": the take starts here
    go_wall, go_mono = time.time(), time.monotonic()
    if n_count:
        st.update({"started": go_wall, "preroll": False, "bubble_pid": bubble.pid if bubble else None})
        fsutil.atomic_write_json(state_path(), st, durable=False)
    from .guards import set_recording
    set_recording(proc.pid)                 # menu guard file (no Python on menu open)
    health.set_phase("recording", dir=str(pdir), pid=proc.pid)
    if n_count:
        # the cards on screen at "go", read directly: the logger's own count lags by its
        # polling interval, and Dolly's last countdown card was taken down a moment ago
        toasts_go = _toast_count()
        if cam_mode == "edit":
            start_camera()                  # the preview had the camera until now
            st["webcam_pid"] = cam_proc.pid if cam_proc else None
            fsutil.atomic_write_json(state_path(), st, durable=False)
        meta.update({"started": go_wall, "go_wall": go_wall, "go_mono": go_mono, "toasts_at_go": toasts_go,
                     "live_bubble": bool(bubble), "audio_warnings": warn, "webcam_clock": cam_clock,
                     "webcam": bool(cam_proc), "webcam_launch_mono": cam_launch})
        fsutil.atomic_write_json(pdir / "capture.json", meta, indent=2)
    if warn:
        notify(t("notify.recordingWithout"), "\n".join(warn), urgency="critical", timeout=8000)
    if not getattr(a, "json", False):
        print(f"dolly: recording to {pdir} (stop with: dolly stop)", file=sys.stderr)
    print(pdir)


def start_bubble(dev, region, live: bool, countdown: bool = True):
    """The on-screen camera bubble (mpv, Omarchy WebcamOverlay style). Returns the process."""
    from . import camera as cammod
    from . import health
    from .config import load
    if not _have("mpv") or (not live and not countdown):
        return None
    c = load()["camera"]
    cmd = cammod.bubble_cmd(dev or c["device"] or None, c["size"], c["mirror"], c["shape"])
    tries = 40
    if on_stage():
        # On the hidden stage mpv's GPU output cannot hand its pictures to the second
        # compositor (VirtualBox, 11 Nov: "invalid arguments for wl_surface.attach", and no
        # window). Plain shared-memory pictures always can; a 280 px bubble costs little.
        cmd.append("--vo=wlshm")
        tries = 120
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, env=health.software_gl_env())
    except OSError:
        return None
    accent = projmod.theme_colors().get("accent", "#7aa2f7")
    for _ in range(tries):                   # wait for the window to map, then place it
        if p.poll() is not None:
            return None
        if cammod.place_bubble(region, c["size"], c["corner"], c["shape"], accent, c["border"], c["shadow"]):
            break
        time.sleep(0.05)
    else:
        if on_stage():                       # never placed: a window as large as the stage is no bubble
            stop_bubble(p)
            return None
    return p


def stop_bubble(p):
    if p is None:
        return
    try:
        p.terminate()
        p.wait(timeout=2)
    except Exception:
        try:
            p.kill()
        except Exception:
            _dlog.swallowed()
            pass


TERMINALS = ["Alacritty", "com.mitchellh.ghostty", "kitty", "foot", "footclient", "org.wezfurlong.wezterm"]


def _recover_stale():
    """A state file whose recorder is gone (crash, reboot): finalize what was captured."""
    try:
        st = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return
    if st.get("recorder_pid") and _alive(st["recorder_pid"]):
        return
    state_path().unlink(missing_ok=True)
    from .guards import set_recording
    from .health import awake_end, dnd_end
    set_recording(None)
    from .solid import restore as solid_restore
    solid_restore()
    dnd_end(st.get("dnd_prev"))
    awake_end(st.get("awake"))
    pdir = Path(st.get("dir", ""))
    if pdir.is_dir():
        try:
            recover_dir(pdir)
            notify(t("notify.recovered"), t("notify.recoveredBody", pdir.name))
        except Exception as e:
            notify(t("notify.recoverFailed"), f"{pdir}: {e} (dolly doctor)",
                   urgency="critical")


STOP_REASONS = ("recorder-died", "stopped-outside", "disk-full", "suspend", "lock", "monitor-removed",
                "window-closed", "window-left-monitor", "hyprland-exit")


def reason_text(reason: str) -> str:
    return t(f"reason.{reason}") if reason in STOP_REASONS else reason


def _counting_down(st: dict | None) -> bool:
    """The countdown is still running: the recorder and the logger are up (pre-roll), the
    take has not started. False once the process that counts down is gone."""
    if not st or not st.get("preroll"):
        return False
    from . import health
    ph = health.get_phase()
    return ph.get("phase") == "countdown" and bool(ph.get("pid")) and _alive(ph["pid"])


def _live_state() -> dict | None:
    """The state of a take that has started (None while idle or still counting down)."""
    st = read_state()
    return None if _counting_down(st) else st


def _clock(seconds: float) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    return f"{m}:{s:02d}"


def _recording_details(pdir: Path, dur: float | None = None) -> str:
    """"1:23 · 41 MB" like Omarchy's "Screen recording saved" toast. `dur`: the length when
    the caller already knows it (saves reading the file again)."""
    try:
        raw = projmod.Project(pdir).raw
        dur = dur or video_info(raw).get("duration", 0)
        if not dur:                 # a file a killed recorder left without its length
            try:
                dur = float(json.loads((pdir / "events.json").read_text())["video"]["duration"])
            except (OSError, ValueError, KeyError, TypeError):
                dur = 0
        m, sec = divmod(int(dur), 60)                    # same rounding as the editor (0:10.52 -> 0:10)
        from .units import human_size
        return t("notify.recordedDetails", f"{m}:{sec:02d}", human_size(raw.stat().st_size))
    except Exception:
        _dlog.swallowed()
        return pdir.name


def _poster(pdir: Path, duration: float | None = None) -> Path | None:
    try:
        from .library import poster
        return poster(pdir, duration=duration)
    except Exception:
        _dlog.swallowed()
        return None


def stop(_a=None, defer: bool | None = None):
    """Stop order matters: the logger first (it flashes the end sync marker while the
    recorder still runs), then the recorder, then finalize.

    `defer` (the default, unless DOLLY_ANALYSIS=now): the "Demo recorded" card and the
    editor come as soon as the file is safe; the zoom plan is worked out afterwards by a
    background job (dolly.analysis). Callers that report or use the plan right away (the
    JSON commands) pass defer=False."""
    from . import analysis, health
    reason = getattr(_a, "reason", None) if _a is not None else None
    if defer is None:
        defer = os.environ.get("DOLLY_ANALYSIS", "later") != "now"
    t_stop = time.monotonic()
    st = read_state() or (load_state_any() if reason else None)
    if not st:
        if reason:
            return            # a watchdog raced a manual stop
        print("not recording", file=sys.stderr)
        raise SystemExit(1)
    if _counting_down(st):
        if not reason:             # nothing of the take exists yet: stopping the countdown is a cancel
            cancel(_a)
        return                     # (a watchdog's stop: the countdown notices a dead recorder itself)
    pdir = Path(st["dir"])
    stopped_at = time.time()
    health.set_phase("finalizing", dir=str(pdir))
    if st.get("paused_at"):  # resume first so both finalise normally
        _signal(st, signal.SIGUSR2)
    if _alive(st.get("logger_pid", 0)):
        os.kill(st["logger_pid"], signal.SIGTERM)
        for _ in range(40):
            if not _alive(st["logger_pid"]):
                break
            time.sleep(0.05)
    import threading
    early: dict = {}
    scan = None
    if not reason and elapsed(st) > 2 * 4.0 + 2.0:
        # the first seconds of a take this long are safely on disk: scan them for the start
        # marker now, while the recorder winds down (the wf-recorder tail alone is 1 s)
        scan = threading.Thread(target=_scan_head_early, args=(pdir, early), daemon=True)
        scan.start()
    if _alive(st["recorder_pid"]):
        if st.get("backend") == "wf" and not reason:
            # wf-recorder loses its last frames on SIGINT: without this tail the end sync
            # marker (and the user's last half second) never reaches raw.mkv. The export
            # range ends at the marker, so the tail itself is not shown.
            tail = float(os.environ.get("DOLLY_WF_STOP_TAIL", "1.0"))
            if on_stage():
                # The stop mark is on show (the pointer waits in the corner, and the logger kept
                # the stage repainting while it put it there). Now the stage is left still
                # until the recorder has encoded what it holds: it drops its queue of frames
                # when it is told to stop, and on a busy machine that queue is the last
                # second and more of the take (dolly/stage.py, recorder_drained).
                try:
                    from . import stage
                    with open(pdir / "recorder.log", "a") as lf:
                        stage.recorder_drained(st["recorder_pid"], least=max(0.3, tail * 0.5),
                                               most=stage.drain_limit(elapsed(st)),
                                               log=lambda m: lf.write(m + "\n"))
                except Exception:
                    _dlog.swallowed()
                    time.sleep(tail)
            else:
                time.sleep(tail)
        os.kill(st["recorder_pid"], signal.SIGINT)
        # a recorder whose monitor is gone will never see the frame it is waiting for (and
        # there is no corner left to repaint): 1.5 s for it to finish by itself, not 10
        for i in range(GONE_PATIENCE if reason == "monitor-removed" else 500):
            if not _alive(st["recorder_pid"]):
                break
            if st.get("backend") == "wf" and i in NUDGE_AT:
                _nudge_screen(st.get("region"))
            time.sleep(0.02)
        else:
            os.kill(st["recorder_pid"], signal.SIGKILL)
    _stage_pointer_home()
    side = [p for p in [st.get("webcam_pid"), *(st.get("track_pids") or [])] if p and _alive(p)]
    for p in side:
        os.kill(p, signal.SIGINT)                 # ffmpeg finalises camera / audio files on SIGINT
    for _ in range(250):
        if not any(_alive(p) for p in side):
            break
        time.sleep(0.02)
    for p in side:
        if _alive(p):
            os.kill(p, signal.SIGKILL)
    if st.get("bubble_pid") and _alive(st["bubble_pid"]):
        os.kill(st["bubble_pid"], signal.SIGTERM)
    if _alive(st.get("logger_pid", 0)):
        os.kill(st["logger_pid"], signal.SIGKILL)
    state_path().unlink(missing_ok=True)
    t_recorder = time.monotonic()
    from .guards import set_recording
    set_recording(None)
    from .solid import restore as solid_restore
    solid_restore()
    health.dnd_end(st.get("dnd_prev"))
    health.awake_end(st.get("awake"))
    meta = json.loads((pdir / "capture.json").read_text())
    meta["stopped"] = stopped_at
    if reason:
        meta["stop_reason"] = reason
    fsutil.atomic_write_json(pdir / "capture.json", meta, indent=2)
    raw = projmod.Project(pdir).raw
    if not raw.exists() or raw.stat().st_size == 0:
        health.set_phase("idle")
        notify(t("notify.nothingRecorded"), t("notify.nothingRecordedBody", f"{pdir}/recorder.log"),
               urgency="critical", timeout=10000)
        raise SystemExit(1)
    if reason:
        try:                       # a recorder that died leaves a file without its length or index
            _repair_raw(pdir)
        except Exception as ex:
            (pdir / "recorder.log").open("a").write(f"video repair failed: {ex}\n")
    posters: list = []

    def start_poster(info):                  # the card's picture, made while the markers are looked for
        th = threading.Thread(target=_poster, args=(pdir, info.get("duration")), daemon=True)
        posters.append(th)
        th.start()
    if scan is not None:
        scan.join(timeout=20)
    try:
        doc = finalize(pdir, plan=not defer, on_video=start_poster, head=early.get("head"))
    except Exception as e:  # keep the raw files; tell the user
        health.set_phase("idle")
        notify(t("notify.analysisFailed"), t("notify.analysisFailedBody", str(e)[:160], pdir),
               urgency="critical", timeout=10000)
        raise
    health.set_phase("idle")
    for th in posters:
        th.join(timeout=10)
    t_safe = time.monotonic()
    dolly = str(Path(__file__).resolve().parents[1] / "bin" / "dolly")
    info = _recording_details(pdir, (doc.get("video") or {}).get("duration"))
    title = t("notify.recorded") if not reason or reason == "stopped-outside" else t("notify.stoppedReason", reason_text(reason))
    body = info if not reason or reason == "stopped-outside" else t("notify.savedUpToThere", info)
    if reason == "stopped-outside":
        body = info + "\n" + reason_text(reason)
    notify(title, body,
           (t("notify.edit"), dolly, "edit", str(pdir)), (t("notify.quickExport"), dolly, "export", str(pdir)),
           urgency="critical" if reason in ("recorder-died", "disk-full") else "normal", timeout=10000,
           image=_poster(pdir))
    if os.environ.get("DOLLY_OPEN_EDITOR", "1") == "1" and reason not in ("suspend", "lock", "hyprland-exit"):
        # the editor opens without taking focus: you keep typing where you were
        subprocess.Popen([dolly, "edit", str(pdir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=dict(os.environ, DOLLY_KEEP_FOCUS="1"))
    t_card = time.monotonic()
    if analysis.pending(pdir) is not None:
        analysis.spawn(pdir)                 # the zoom plan: a background job the bar shows
    try:                                     # Library: index for the panel's Recent list
        from . import library
        library.poster(pdir)
        library.auto_cleanup()
        library.index(refresh=True)
    except Exception as ex:
        (pdir / "recorder.log").open("a").write(f"library index: {ex}\n")
    shell_refresh()
    try:
        (pdir / "recorder.log").open("a").write(
            "stop: recorder and logger down after %.2f s, file safe after %.2f s, card after %.2f s%s\n"
            % (t_recorder - t_stop, t_safe - t_stop, t_card - t_stop, " (zoom plan follows)" if defer else ""))
    except OSError:
        pass
    print(pdir)


def cancel(_a=None):
    from . import health
    ph = health.get_phase()
    if ph.get("phase") == "countdown" and ph.get("pid") and _alive(ph["pid"]):
        os.kill(int(ph["pid"]), signal.SIGTERM)       # the countdown cleans up after itself
        print("countdown cancelled")
        return
    st = read_state()
    if not st:
        print("nothing to cancel: not recording")
        return
    for p in [st.get("logger_pid"), st.get("recorder_pid"), st.get("webcam_pid"), st.get("bubble_pid"),
              *(st.get("track_pids") or [])]:
        if p and _alive(p):
            os.kill(p, signal.SIGTERM)
    state_path().unlink(missing_ok=True)
    from .guards import set_recording
    set_recording(None)
    from .solid import restore as solid_restore
    solid_restore()
    health.dnd_end(st.get("dnd_prev"))
    health.awake_end(st.get("awake"))
    if on_stage():
        for _ in range(40):                 # the logger makes its stop mark as it leaves: the pointer
            if not _alive(st.get("logger_pid", 0)):       # goes to the corner and would stay there
                break
            time.sleep(0.05)
        _stage_pointer_home()
    shutil.rmtree(st["dir"], ignore_errors=True)
    health.set_phase("idle")
    print("recording discarded")


def toggle(a):
    """One hotkey: start; during the countdown: cancel; while recording: stop."""
    from . import health
    if health.get_phase().get("phase") == "countdown":
        cancel(a)
    elif read_state():
        stop(a)
    else:
        class A:  # a bare toggle: Settings (config.json), env overrides kept for scripts
            mode = os.environ.get("DOLLY_MODE")
            mic = desktop_audio = webcam = no_keys = fps = countdown = solid = no_dnd = None
            backend = "auto"
        if os.environ.get("DOLLY_COUNTDOWN"):
            A.countdown = int(os.environ["DOLLY_COUNTDOWN"])
        from .cli import apply_record_defaults
        apply_record_defaults(A)
        start(A)


def status(a):
    """`dolly status`: one line for people, `--json` for the bar and scripts. Exit code 0
    while a take is running (recording or paused), 1 otherwise."""
    st = read_state()
    from . import health
    ph = health.get_phase()
    phase = ph.get("phase", "idle")
    if _counting_down(st):
        st = None                 # recorder and logger are up (pre-roll); the take has not started
    if st:
        phase = "paused" if st.get("paused_at") else "recording"
    if getattr(a, "json", False):
        last = projmod.latest_project()
        rep = {}
        try:
            from .replay import replay_state
            rs = replay_state()
            rep = {"replay": bool(rs), "replay_seconds": rs.get("seconds") if rs else None}
        except Exception:
            rep = {"replay": False}
        print(json.dumps({**rep, "recording": bool(st), "elapsed": round(elapsed(st), 1) if st else 0,
                          "phase": phase, "countdown": ph.get("left", 0) if phase == "countdown" else 0,
                          "jobs": health.jobs(), "bar": _bar_view(),
                          "paused": bool(st and st.get("paused_at")), "muted": bool(st and st.get("muted")),
                          "backend": st.get("backend") if st else None, "dir": st.get("dir") if st else None,
                          "last": str(last) if last else None}))
    elif st:
        print(f"{phase} {_clock(elapsed(st))}" + (", microphone muted" if st.get("muted") else ""))
    elif phase == "countdown":
        print(f"countdown {ph.get('left', 0)}")
    else:
        print("not recording" + (f" ({phase})" if phase != "idle" else ""))
    raise SystemExit(0 if st else 1)


def _bar_view() -> dict:
    try:
        from .config import bar_view
        return bar_view()
    except Exception:
        _dlog.swallowed()
        return {}


def elapsed(st: dict) -> float:
    paused = float(st.get("paused_total", 0.0))
    if st.get("paused_at"):
        paused += time.time() - st["paused_at"]
    return time.time() - st["started"] - paused


def _signal(st, sig):
    for k in ("recorder_pid", "logger_pid"):
        if st.get(k) and _alive(st[k]):
            os.kill(st[k], sig)


def pause(_a=None):
    """Toggle pause (gpu-screen-recorder SIGUSR2). The logger drops events while
    paused and finalize closes the gap, so events stay in sync with the video."""
    st = _live_state()
    if not st:
        print("not recording", file=sys.stderr)
        raise SystemExit(1)
    if st["backend"] == "wf":
        notify(t("notify.pauseNeedsGsr"), t("notify.pauseNeedsGsrBody"))
        print("cannot pause: this recording uses wf-recorder, and pausing needs gpu-screen-recorder",
              file=sys.stderr)
        raise SystemExit(2)
    _signal(st, signal.SIGUSR2)
    if st.get("paused_at"):
        st["paused_total"] = float(st.get("paused_total", 0)) + time.time() - st["paused_at"]
        st["paused_at"] = None
        notify(t("notify.resumed"), "", timeout=1200)
    else:
        st["paused_at"] = time.time()
        notify(t("notify.paused"), t("notify.pausedBody", "Super+Alt+P"), timeout=2000)
    fsutil.atomic_write_json(state_path(), st, durable=False)
    shell_refresh()
    print(f"paused at {_clock(elapsed(st))}" if st.get("paused_at") else f"resumed at {_clock(elapsed(st))}")


def mute(_a=None):
    """Mute / unmute the microphone track while recording: a muted span (silence at
    export), not a cut. The logger records the span on the shared time base."""
    st = _live_state()
    if not st:
        print("not recording", file=sys.stderr)
        raise SystemExit(1)
    if st.get("logger_pid") and _alive(st["logger_pid"]):
        os.kill(st["logger_pid"], signal.SIGRTMIN + 1)
    st["muted"] = not st.get("muted", False)
    fsutil.atomic_write_json(state_path(), st, durable=False)
    notify(t("notify.micMuted") if st["muted"] else t("notify.micOn"), "", timeout=1200)
    shell_refresh()
    print("microphone muted" if st["muted"] else "microphone on")


def camera_cmd(a):
    """dolly camera devices|formats|snap|size|preview."""
    from . import camera as cammod
    from .config import load, set_many
    if a.cam_cmd == "devices":
        print(json.dumps(cammod.devices()))
        return
    if a.cam_cmd == "formats":
        print(json.dumps(cammod.formats(a.device)))
        return
    st = read_state() or {}
    region = st.get("region")
    if not region:
        m = focused_monitor()
        region = monitor_logical(m)[0] if m else [0, 0, 1920, 1080]
    c = load()["camera"]
    accent = projmod.theme_colors().get("accent", "#7aa2f7")
    if a.cam_cmd == "snap":
        corner = cammod.snap_bubble(region, None, c["shape"], accent=accent, border=c["border"], shadow=c["shadow"])
        if corner:
            set_many({"camera.corner": corner})
    elif a.cam_cmd == "size":
        size = cammod.next_size(c["size"])
        set_many({"camera.size": size})
        cammod.snap_bubble(region, size, c["shape"], accent=accent, border=c["border"], shadow=c["shadow"])
    elif a.cam_cmd == "preview":
        p = start_bubble(None, region, live=True)
        if p is None:
            raise SystemExit("dolly: no camera preview (mpv missing or no camera)")
        time.sleep(a.seconds)
        stop_bubble(p)


def mark(_a=None):
    """'Zoom here' marker at the cursor (logger SIGUSR1) -> becomes a zoom segment."""
    st = _live_state()
    if not st:
        print("not recording: there is nothing to mark", file=sys.stderr)
        raise SystemExit(1)
    if st.get("logger_pid") and _alive(st["logger_pid"]):
        os.kill(st["logger_pid"], signal.SIGUSR1)
        notify(t("notify.marked"), "", timeout=900)
        print(f"marked a zoom point at {_clock(elapsed(st))}")
    else:
        print("nothing marked: the event logger of this recording is not running", file=sys.stderr)
        raise SystemExit(1)


def webcam_device() -> str | None:
    dev = os.environ.get("DOLLY_WEBCAM")
    if dev:
        return dev if Path(dev).exists() else None
    for d in sorted(Path("/dev").glob("video*")):
        # skip metadata nodes: a capture node has a 'name' and index 0 under sysfs
        idx = Path(f"/sys/class/video4linux/{d.name}/index")
        try:
            if idx.read_text().strip() != "0":
                continue
        except OSError:
            pass
        return str(d)
    return None


def start_webcam(pdir: Path, log):
    """Record the webcam alongside (v4l2 -> webcam.mkv) with absolute capture
    timestamps, so finalize can line it up with the screen recording."""
    dev = webcam_device()
    if not dev or not _have("ffmpeg"):
        log.write("webcam: no /dev/video* capture device, skipped\n")
        return None
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "v4l2", "-timestamps", "abs", "-input_format", "mjpeg",
           "-framerate", "30", "-video_size", "1280x720", "-i", dev, "-copyts",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p", str(pdir / "webcam.mkv")]
    log.write(f"$ {' '.join(cmd)}\n")
    log.flush()
    try:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except OSError:
        return None


def _pause_intervals(lines) -> list[tuple[float, float]]:
    out, start = [], None
    for e in lines:
        if e.get("type") == "pause" and start is None:
            start = e.get("ms", 0)
        elif e.get("type") == "resume" and start is not None:
            out.append((start, e.get("ms", 0)))
            start = None
    if start is not None:
        out.append((start, float("inf")))
    return out


def _unpause(ms: float, intervals) -> float | None:
    """Event time with paused stretches removed (None if inside a pause)."""
    shift = 0.0
    for a, b in intervals:
        if ms >= b:
            shift += b - a
        elif ms >= a:
            return None
    return ms - shift


# ---------------------------------------------------------------- recovery
def _repair_raw(pdir: Path) -> bool:
    """A recorder that was killed leaves a video without its length and its seek index.
    Rewrite the container (a stream copy; the original is kept as *.damaged) so the toast,
    the library, the editor and the export all see a normal file. True when rewritten."""
    raw = projmod.Project(pdir).raw
    if video_info(raw).get("duration"):
        return False
    from .render import ffmpeg_bin
    fixed = pdir / "raw.recovered.mkv"
    # no timeout: a stream copy of the whole recording, its time grows with its length
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-err_detect", "ignore_err", "-i", str(raw),
                    "-c", "copy", str(fixed)], capture_output=True)
    if fixed.exists() and video_info(fixed).get("duration"):
        raw.rename(raw.with_suffix(raw.suffix + ".damaged"))
        fixed.rename(pdir / "raw.mkv")
        return True
    fixed.unlink(missing_ok=True)
    return False


def recover_dir(pdir: Path) -> dict:
    """Make an interrupted recording usable: kill leftovers, make sure the video
    is readable (remux a damaged MKV/MP4 if needed) and finalize it."""
    pdir = Path(pdir)
    raw = projmod.Project(pdir).raw
    if not raw.exists() or raw.stat().st_size == 0:
        raise RuntimeError("no video was written")
    _repair_raw(pdir)
    if not (pdir / "events.raw.jsonl").exists():
        (pdir / "events.raw.jsonl").write_text("")
    meta_p = pdir / "capture.json"
    try:
        meta = fsutil.read_json(meta_p) if meta_p.exists() else {}
    except fsutil.DamagedFile:
        meta = {}                            # recover what we can from the video and the log
    meta.setdefault("stop_reason", "recovered")
    fsutil.atomic_write_json(meta_p, meta, indent=2)
    return finalize(pdir)


def recover_all(_a=None) -> list[Path]:
    """`dolly recover`: finalize every recording that was interrupted."""
    _recover_stale()
    if not read_state():
        from .solid import restore as solid_restore
        solid_restore()                      # windows forced solid by a recording that crashed
        from . import health
        for what in health.heal():           # Do Not Disturb, Stay Awake, key repeat, the input bridge
            print(what)
    out = []
    root = projmod.videos_root()
    if not root.is_dir():
        return out
    st = read_state()
    live = Path(st["dir"]) if st else None
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d == live or (d / "events.json").exists():
            continue
        if projmod.usable(d) and projmod.Project(d).raw.exists() and (d / "capture.json").exists():
            try:
                recover_dir(d)
                out.append(d)
                print(f"recovered {d}")
            except Exception as e:
                print(f"could not recover {d}: {e}", file=sys.stderr)
    from . import analysis
    for d in analysis.unfinished(root):      # the card was sent, then a crash or logout killed the plan
        if d == live:
            continue
        try:
            analysis.analyse(d)
            out.append(d)
            print(f"finished the analysis of {d}")
        except Exception as e:
            print(f"could not finish the analysis of {d}: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- finalize
def video_info(path: Path) -> dict:
    from .render import probe
    p = probe(path)
    v = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), {})
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "60/1").split("/")
    return {"w": int(v.get("width", 0)), "h": int(v.get("height", 0)),
            "fps": round(float(num) / max(float(den), 1), 3),
            "duration": float(p.get("format", {}).get("duration", 0) or 0)}


def webcam_offset(pdir: Path, meta: dict) -> float:
    """Seconds from video t=0 to the first webcam frame (v4l2 abs timestamps are CLOCK_MONOTONIC)."""
    cam = pdir / "webcam.mkv"
    if not cam.exists() or not meta.get("t0_mono"):
        return 0.0
    try:
        from .render import probe
        st = float(probe(cam).get("format", {}).get("start_time", 0) or 0)
    except Exception:
        st = 0.0
    off = st - meta["t0_mono"]
    if not -5 < off < 5:  # timestamps were not monotonic-based; fall back to the launch time
        off = (meta.get("webcam_launch_mono") or meta["t0_mono"]) - meta["t0_mono"] + 0.3
    return round(off, 3)


def aoimod_activity(pdir, tl):
    from .timemap import cached_activity
    return cached_activity(pdir)


def window_left_at(snapshots, address, frame) -> float | None:
    """Time the followed window last moved (mostly) off the recorded frame for good.
    `snapshots` are Timeline snapshots: window rects are corner boxes (x0, y0, x1, y1)."""
    from .logger import window_on_region
    left = None
    for snap in snapshots:
        w = next((w for w in snap["windows"] if w["address"] == address), None)
        if w is not None:
            x0, y0, x1, y1 = w["rect"]
        on = w is not None and window_on_region((x0, y0, x1 - x0, y1 - y0), frame)
        if on:
            left = None
        elif left is None:
            left = snap["t"]
    return left


def _map_rect(r, rx, ry, sx, sy):
    x, y, w, h = r
    return [round((x - rx) * sx, 1), round((y - ry) * sy, 1), round(w * sx, 1), round(h * sy, 1)]


def _go_time(meta: dict, lines: list, fit) -> float | None:
    """Video time of "go" (the end of the countdown) for a take recorded with pre-roll;
    None when there was no countdown. Both clocks are CLOCK_MONOTONIC: capture.json has
    go, the logger's first line has its own start."""
    if not meta.get("countdown") or meta.get("go_mono") is None:
        return None
    t0 = next((e.get("t0_mono") for e in lines if e.get("type") == "start" and e.get("t0_mono") is not None), None)
    if t0 is None:
        return None
    try:
        t = round(float(fit.map(float(meta["go_mono"]) - float(t0))), 4)
    except (TypeError, ValueError):
        return None
    return t if t > 0 else None


def _from_go(segs: list, go_t: float | None) -> list:
    """No zoom planned from what happened during the countdown may reach into the take."""
    if go_t is None:
        return segs
    out = []
    for s in segs:
        if s.end <= go_t + 0.3:
            continue
        if s.start < go_t:
            s.start = go_t
        out.append(s)
    return out


def _scan_head_early(pdir: Path, out: dict):
    """While the recorder is still shutting down: look for the start marker in the first
    seconds of the file (they have been on disk since the take began). Stop is that much
    shorter. Anything that goes wrong here only means finalize does it itself."""
    try:
        from . import sync as syncmod
        from .render import ffmpeg_bin
        meta = json.loads((pdir / "capture.json").read_text())
        if meta.get("stage"):
            return                           # a stage take has pointer marks: finalize reads them itself
        raw = projmod.Project(pdir).raw
        info = video_info(raw)               # the size is known while the file grows; the length is not
        rw = meta["region"][2]
        ew = ((meta.get("target_info") or {}).get("exact") or (rw, 0))[0]
        if info["w"] and ew:
            out["head"] = syncmod.scan_head(raw, info["w"] / ew, ffmpeg_bin())
    except Exception:
        _dlog.swallowed()


def finalize(pdir: Path, aoi: bool = True, plan: bool = True, on_video=None, head: dict | None = None):
    """events.raw.jsonl + raw video -> events.json (video px, seconds) + project.json,
    then the zoom plan.

    The first half is quick and does not grow with the length of the take: the sync
    markers are looked for in the head and the tail of the file only. The plan (screen
    activity analysis, auto segments, idle speed-ups) is dolly.analysis: done here when
    `plan` is true, otherwise left pending for the caller to start after the "Demo
    recorded" card. `on_video(info)` is called as soon as the video's size and length
    are known; `head` is an earlier sync.scan_head() of this file.

    Time base: sync markers (see dolly/sync.py) when present, else the logger
    start (expect 0-150 ms skew; style.sync_offset fixes it by hand)."""
    from . import analysis
    from . import sync as syncmod
    from .events import Timeline

    meta = json.loads((pdir / "capture.json").read_text())
    raw = projmod.Project(pdir).raw
    info = video_info(raw)
    if on_video:
        try:
            on_video(info)
        except Exception:
            _dlog.swallowed()
    rx, ry, rw, rh = meta["region"]
    # logical -> video px from the real video size over the exact logical size of
    # the target (fractional scales, gsr -s caps, even-dimension rounding)
    ew, eh = (meta.get("target_info") or {}).get("exact") or (rw, rh)
    sx = info["w"] / ew if ew else 1.0
    sy = info["h"] / eh if eh else 1.0
    lines = []
    with open(pdir / "events.raw.jsonl") as f:
        for line in f:
            try:
                lines.append(json.loads(line))
            except ValueError:
                pass      # a crash can leave a half-written last line
    click_source = "none"
    pauses = _pause_intervals(lines)
    # ---- sync markers -> t_video = a + b * t_logger
    log_marks = []
    for e in lines:
        if e.get("type") == "sync":
            ms = _unpause(e.get("ms", 0), pauses)
            if ms is not None:
                log_marks.append(ms / 1000.0)
    clock_a = syncmod.clock_offset(meta, lines)
    fit = syncmod.SyncFit(clock_a, 1.0, [], [], "clock") if clock_a is not None else syncmod.SyncFit()
    runs = []
    marks = None                             # the hidden stage: {"start", "stop"} pointer marks
    if log_marks and meta.get("stage"):
        try:
            from .render import ffmpeg_bin
            marks = syncmod.detect_pointer_marks(
                raw, sx, info["fps"] or 60, ffmpeg_bin(), log_marks, clock_a, info["duration"],
                log=lambda m: (pdir / "recorder.log").open("a").write(m + "\n"))
            fit = syncmod.fit_pointer(log_marks, marks, info["fps"] or 60, clock_a)
        except Exception as ex:  # never lose a recording over sync
            fit = syncmod.SyncFit(method=f"failed: {ex}"[:80])
    elif log_marks:
        try:
            from .render import ffmpeg_bin
            if clock_a is None:
                # no clock to say where the start marker should be: read the whole file, as before
                runs = syncmod.detect_markers(raw, sx, info["fps"] or 60, ffmpeg_bin())
            else:
                runs = syncmod.detect_markers_bounded(
                    raw, sx, info["fps"] or 60, ffmpeg_bin(), want_stop=len(log_marks) >= 2,
                    head_until=clock_a + log_marks[0], head=head,
                    stop_at=clock_a + log_marks[-1], duration=info["duration"],
                    log=lambda m: (pdir / "recorder.log").open("a").write(m + "\n"))
            fit = syncmod.fit(log_marks, runs, info["fps"] or 60, clock_a)
        except Exception as ex:  # never lose a recording over sync
            fit = syncmod.SyncFit(method=f"failed: {ex}"[:80])
    go_t = _go_time(meta, lines, fit)
    events = []
    for e in lines:
        ms = _unpause(e.get("ms", 0), pauses)
        if ms is None:
            continue
        t = round(fit.map(ms / 1000.0), 4)
        kind = e.get("type")
        if kind == "info" and e.get("click_source"):
            click_source = e["click_source"]
        if go_t is not None and t < go_t and kind in ("click", "key", "mark"):
            continue                # pressed during the countdown: recorded (pre-roll), not part of the take
        if kind in ("move", "click"):
            ev = {"t": t, "type": kind, "x": round((e["x"] - rx) * sx, 2), "y": round((e["y"] - ry) * sy, 2)}
            if kind == "click":
                ev["button"] = e.get("button", "left")
            events.append(ev)
        elif kind == "mark":
            events.append({"t": t, "type": "mark", "x": round((e["x"] - rx) * sx, 2),
                           "y": round((e["y"] - ry) * sy, 2)})
        elif kind == "key":
            # a raw log written before 0.7.2 still names typed characters: never carry them over
            from .keymap import stored
            events.append({"t": t, "type": "key", **stored(str(e["key"]), e.get("mods", []), bool(e.get("lalt")))})
        elif kind == "window" and e.get("rect"):
            events.append({"t": t, "type": "window", "rect": _map_rect(e["rect"], rx, ry, sx, sy),
                           "title": e.get("title", ""), "class": e.get("class", ""),
                           "address": e.get("address", "")})
        elif kind == "clients":
            wins = [{**w, "rect": _map_rect(w["rect"], rx, ry, sx, sy)} for w in e.get("windows", []) if w.get("rect")]
            events.append({"t": t, "type": "clients", "focus": e.get("focus"), "windows": wins})
        elif kind == "workspace":
            events.append({"t": t, "type": "workspace", "name": e.get("name", ""), "special": e.get("special", False)})
        elif kind == "layer":
            events.append({"t": t, "type": "layer", "namespace": e.get("namespace", ""), "open": e.get("open", True),
                           "keyboard": e.get("keyboard", False),
                           "rect": _map_rect(e["rect"], rx, ry, sx, sy) if e.get("rect") else None})
        elif kind == "monitor":
            events.append({"t": t, "type": "monitor", "reason": e.get("reason", ""), "name": e.get("name", "")})
        elif kind == "sync":
            events.append({"t": t, "type": "sync", "kind": e.get("kind", "start")})
        elif kind == "toasts":
            events.append({"t": max(0.0, t), "type": "toasts", "count": int(e.get("count", 0))})
        elif kind == "warn":
            events.append({"t": t, "type": "warn", "msg": e.get("msg", "")})
        elif kind == "mute":
            events.append({"t": t, "type": "mute", "on": bool(e.get("on"))})
        elif kind == "start" and e.get("t0_mono") is not None:
            meta.setdefault("logger_t0_mono", e["t0_mono"])
    duration = info["duration"] or (max((e["t"] for e in events), default=0) + 0.5)
    # separate audio tracks + camera: on the same time base through the sync fit
    from . import audio as audiomod
    track_files = sorted((pdir / "tracks").glob("*.mka")) if (pdir / "tracks").is_dir() else []
    audio_tracks = audiomod.align(meta, fit, [f for f in track_files if f.stat().st_size > 0])
    mic_mutes = audiomod.mute_spans(events, duration)
    cam_align = None
    if (pdir / "webcam.mkv").exists() and meta.get("webcam_clock"):
        from . import camera as cammod
        cam_align = cammod.align(meta, fit, pdir / "webcam.mkv")
    cursor_size = int(meta.get("cursor_size") or os.environ.get("HYPRCURSOR_SIZE") or os.environ.get("XCURSOR_SIZE") or 24)
    doc = {"version": 2, "video": {**info, "duration": duration},
           "capture": {"backend": meta.get("backend"), "cursor_baked": bool(meta.get("cursor_baked")),
                       "click_source": click_source, "region": meta["region"], "scale": sx,
                       "mic": bool(meta.get("mic")),
                       "webcam_offset": cam_align["offset"] if cam_align else webcam_offset(pdir, meta),
                       "webcam_scale": cam_align["scale"] if cam_align else 1.0,
                       "camera_mode": meta.get("camera_mode", "edit" if meta.get("webcam") else "off"),
                       "audio_mode": meta.get("audio_mode"), "audio_tracks": audio_tracks, "mic_mutes": mic_mutes,
                       "cursor_size": cursor_size, "mode": meta.get("mode", "screen"),
                       "target": meta.get("target"), "terminals": meta.get("terminals"),
                       "sync": fit.to_dict(), "monitor": meta.get("monitor"),
                       # "go": where the take starts when a countdown was recorded before it
                       "go": go_t, "toasts_before": int(meta.get("toasts_before") or 0),
                       "toasts_at_go": meta.get("toasts_at_go") if go_t is not None else None},
           "events": events}
    if meta.get("stage"):
        doc["capture"]["stage"] = True       # recorded on the hidden stage (dolly/stage.py)
    # privacy by default: what was typed into a password manager or an authentication prompt
    # is not kept (no keycaps, no typing zoom, nothing in events.json)
    try:
        from .sensitive import strip_secret_keys
        doc["events"], dropped = strip_secret_keys(events, duration)
        if dropped:
            (pdir / "recorder.log").open("a").write(
                f"privacy: {dropped} key events typed into a secret-bearing window were not kept\n")
        doc["capture"]["notification_areas"] = [
            [r[0], r[1], r[0] + r[2], r[1] + r[3]]
            for r in (_map_rect(n, rx, ry, sx, sy) for n in meta.get("notification_layers") or [])]
    except Exception as ex:  # never lose a recording over this
        (pdir / "recorder.log").open("a").write(f"privacy filter failed: {ex}\n")
    fsutil.atomic_write_json(pdir / "events.json", doc, backup=True)
    proj = projmod.Project(pdir)
    new_project = not proj.file.exists()
    data = proj.load()
    if new_project:
        # Settings (config.json) -> this project's look, camera and export defaults; the
        # Omarchy theme is pinned in the project so re-exports stay stable
        from . import config as cfgmod
        try:
            conf = cfgmod.load()
            sd = cfgmod.style_defaults(conf)
            data["style"] = projmod.deep_merge(data["style"], sd)
            data["style"]["background"] = sd["background"]      # replaced, not merged
            data["zoom_config"] = {**cfgmod.zoom_defaults(conf), **(data.get("zoom_config") or {})}
        except Exception as ex:  # never lose a recording over settings
            (pdir / "recorder.log").open("a").write(f"settings not applied: {ex}\n")
    tl = Timeline.from_dict(doc)
    if click_source == "none":
        data["zoom_config"] = {**data.get("zoom_config", {}), "use_dwell": True}
    # no plan yet: null means "plan it" to the renderer and the editor; dolly.analysis fills it
    # in (below when `plan`, otherwise after the card) and until then the marker file says so
    data["segments"] = None
    trim = list(syncmod.marker_trim(runs, info["fps"] or 60, duration) or (0.0, duration))
    if marks is not None:
        trim = list(syncmod.pointer_trim(marks, info["fps"] or 60, duration) or (0.0, duration))
    if go_t is not None and go_t < duration:
        trim[0] = round(max(trim[0], go_t), 4)       # the countdown is in the file, not in the clip
    target = meta.get("target") if meta.get("mode") == "window" else None
    if target and tl.snapshots:
        # window mode: the recording ends when the followed window closes, or when it
        # moved to another monitor for good (stop_reason window-left-monitor)
        seen = [s["t"] for s in tl.snapshots if any(w["address"] == target for w in s["windows"])]
        if seen and seen[-1] < tl.snapshots[-1]["t"]:
            closed = next(s["t"] for s in tl.snapshots if s["t"] > seen[-1])
            trim[1] = min(trim[1], round(closed - 0.05, 3))
            doc["capture"]["target_closed"] = closed
        if meta.get("stop_reason") == "window-left-monitor":
            left = window_left_at(tl.snapshots, target, [0, 0, info["w"], info["h"]])
            if left is not None:
                trim[1] = min(trim[1], round(left - 0.05, 3))
                doc["capture"]["target_left"] = left
        fsutil.atomic_write_json(pdir / "events.json", doc, backup=True)
    if not data.get("trim") and (trim[0] > 0 or trim[1] < duration):
        data["trim"] = trim
    if doc["capture"]["cursor_baked"]:
        data["style"]["cursor"]["enabled"] = False
    if new_project and data["style"].get("privacy", {}).get("auto", True):
        # Settings > Privacy > automatic blur: secret-bearing windows and notification toasts
        # become ordinary boxes in the editor's privacy lane (computed once; removing one is final)
        try:
            from . import privacy as privmod
            boxes = privmod.auto_boxes(tl)
            if boxes:
                data["privacy"] = [b for b in data.get("privacy") or [] if b.get("source") != "auto"] + boxes
        except Exception as ex:
            (pdir / "recorder.log").open("a").write(f"automatic privacy blur failed: {ex}\n")
    proj.save(data)
    analysis.set_pending(pdir, new_project, aoi)
    try:
        from .guards import set_last
        set_last(pdir)
    except OSError:
        pass
    if plan:
        analysis.analyse(pdir, sync=True)
    return doc

"""System integration helpers: disk space, Do Not Disturb, phases and jobs,
opening windows without stealing focus.

Every helper degrades to a no-op (or a clear message) when the piece of the
desktop it needs is missing, so a VM or a non-Omarchy Hyprland still works.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from . import fsutil
from . import log as _dlog

MIN_FREE_START = int(float(os.environ.get("DOLLY_MIN_FREE_GB", "2")) * (1 << 30))
MIN_FREE_RECORDING = int(float(os.environ.get("DOLLY_STOP_FREE_MB", "300")) * (1 << 20))
DOCTOR = "run `dolly doctor` for details"


def disk_free_bytes(path) -> int | None:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def check_disk(path, need: int = MIN_FREE_START) -> str | None:
    """None if fine, else a user-facing message (exports: a fixed need)."""
    free = disk_free_bytes(path)
    if free is None or free >= need:
        return None
    return (f"Only {free / (1 << 30):.1f} GB free in {path} (Dolly needs {need / (1 << 30):.1f} GB to start "
            f"recording). Free some space or set DOLLY_DIR to another disk; {DOCTOR}.")


# ---------------------------------------------------------- recording space
# A fixed 2 GB refused to record at all on a small VM disk and said nothing useful
# (VM, Nov 10). Recordings are judged in minutes instead: the rate is measured from
# the last recordings (raw bytes / duration), else estimated; below BLOCK_MINUTES
# recording is refused (with "Record anyway"), below WARN_MINUTES it starts with a
# warning, and a RAM tmpfs folder is always called out (lost on reboot, eats memory).
BLOCK_MINUTES = float(os.environ.get("DOLLY_BLOCK_MINUTES", "2"))
WARN_MINUTES = float(os.environ.get("DOLLY_WARN_MINUTES", "10"))
FALLBACK_RATE = 1.4e6            # bytes/s: 1920x1200@60 screen content at gsr "ultra" / crf 16


def fs_type(path) -> tuple[str, str]:
    """(filesystem type, mount point) of the mount holding `path`."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        p = p.resolve()
        best = ("", "/")
        for line in Path("/proc/self/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            mnt = parts[1].replace("\\040", " ")
            if (str(p) == mnt or str(p).startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best[1]):
                best = (parts[2], mnt)
        return best
    except OSError:
        return ("", "/")


def recording_rate(fps: int = 60) -> tuple[float, str]:
    """(bytes per second, "measured" | "estimated") for a new recording."""
    try:
        from .library import _load_cache          # the cached index: instant at record start
        items = [e for e in _load_cache().get("items", [])
                 if e.get("raw_bytes") and (e.get("duration") or 0) >= 5][:5]
        rates = sorted(e["raw_bytes"] / e["duration"] for e in items)
        if rates:
            return rates[len(rates) // 2], "measured"
    except Exception:
        _dlog.swallowed()
        pass
    return FALLBACK_RATE * max(fps, 1) / 60, "estimated"


def disk_plan(path, fps: int = 60) -> dict:
    """How much can be recorded into `path`: {"level": ok|warn|block, "minutes", "free",
    "rate", "rate_source", "fs", "mount", "tmpfs", "message"}."""
    free = disk_free_bytes(path)
    fs, mount = fs_type(path)
    rate, src = recording_rate(fps)
    tmpfs = fs in ("tmpfs", "ramfs")
    out = {"path": str(path), "free": free, "rate": rate, "rate_source": src, "fs": fs, "mount": mount,
           "tmpfs": tmpfs, "minutes": None, "level": "ok", "message": ""}
    if free is None:
        return out
    usable = max(0, free - MIN_FREE_RECORDING)      # recording stops cleanly at MIN_FREE_RECORDING
    minutes = usable / rate / 60
    out["minutes"] = round(minutes, 1)
    where = f"{path}" + (f" ({fs} at {mount})" if fs else "")
    gb = free / (1 << 30)
    explicit = os.environ.get("DOLLY_MIN_FREE_GB")
    if minutes < BLOCK_MINUTES or (explicit and free < MIN_FREE_START):
        out["level"] = "block"
        out["message"] = (f"Only {gb:.1f} GB free in {where}: about {minutes_text(minutes)} of recording. "
                          f"Free some space, choose another folder, or record anyway; {DOCTOR}.")
    elif minutes < WARN_MINUTES:
        out["level"] = "warn"
        out["message"] = (f"About {minutes_text(minutes)} of recording left: {gb:.1f} GB free in {where}. "
                          "Recording stops cleanly if the disk fills up.")
    if tmpfs:
        if out["level"] == "ok":
            out["level"] = "warn"
        out["message"] = ((out["message"] + " ") if out["message"] else "") + (
            f"{path} is in memory (tmpfs): recordings use RAM and are lost on reboot; "
            "choose a folder on disk in Settings > Storage.")
    return out


def minutes_text(m: float) -> str:
    if m < 1:
        return "less than a minute"
    if m < 90:
        return f"{int(m)} min"
    return f"{m / 60:.1f} h"


# ------------------------------------------------------------------ DND
def _shell(*args, timeout=1.5) -> str | None:
    """omarchy-shell <target> <method> [args] (ShellIpc targets, e.g. notifications)."""
    if not shutil.which("omarchy-shell"):
        return None
    try:
        r = subprocess.run(["omarchy-shell", *args], capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        _dlog.swallowed()
        return None


def dnd_state() -> bool | None:
    """True/False from Omarchy's notification service, None if unknown."""
    out = _shell("notifications", "isDnd")
    if out is None:
        return None
    return out.lower() in ("on", "true", "1", "yes")


def dnd_set(on: bool) -> bool:
    return _shell("notifications", "setDnd", "on" if on else "off") is not None


def dnd_begin() -> bool | None:
    """Turn DND on for the recording; returns the previous state (None = untouched)."""
    if os.environ.get("DOLLY_DND", "1") == "0":
        return None
    prev = dnd_state()
    if prev is None or prev:
        return prev
    return False if dnd_set(True) else None


def dnd_end(prev: bool | None):
    if prev is False:
        dnd_set(False)


# ------------------------------------------------------------------ phases / jobs
def runtime_dir() -> Path:
    from .logger import runtime_dir as rd
    return rd()


def set_phase(phase: str, **extra):
    """idle | countdown | recording | paused | finalizing | rendering | exporting (bar + panel + editor)."""
    p = runtime_dir() / "phase.json"
    if phase == "idle":
        p.unlink(missing_ok=True)
    else:
        # unique temp file: two processes setting the phase at once can't interleave
        fsutil.atomic_write_json(p, {"phase": phase, "since": time.time(), "pid": os.getpid(), **extra},
                                 durable=False)
    _refresh()


def get_phase() -> dict:
    p = runtime_dir() / "phase.json"
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return {"phase": "idle"}
    if d.get("pid") and not _alive(d["pid"]):
        p.unlink(missing_ok=True)
        return {"phase": "idle"}
    return d


class Job:
    """An export/render registered for the bar ('rendering 42 %')."""

    def __init__(self, kind: str, project: str, out: str = ""):
        self.path = runtime_dir() / "jobs" / f"{os.getpid()}-{int(time.time() * 1000)}.json"
        self.path.parent.mkdir(exist_ok=True)
        self.info = {"kind": kind, "project": project, "out": out, "pid": os.getpid(), "progress": 0.0}
        self.last = 0.0
        self.update(0.0, force=True)

    def update(self, frac: float, force=False):
        if getattr(self, "finished", False):
            return                      # a late progress callback must not resurrect the job file
        now = time.time()
        if not force and now - self.last < 0.5:
            return
        self.last = now
        self.info["progress"] = round(float(frac), 3)
        try:
            fsutil.atomic_write_json(self.path, self.info, durable=False)
        except OSError:
            pass
        if force:
            _refresh()

    def done(self):
        self.finished = True
        self.path.unlink(missing_ok=True)
        _refresh()


def jobs() -> list[dict]:
    d = runtime_dir() / "jobs"
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            j = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if _alive(j.get("pid", 0)):
            out.append(j)
        else:
            f.unlink(missing_ok=True)
    return out


def pid_alive(pid) -> bool:
    """Running (not gone, not a zombie waiting to be reaped)."""
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except OSError:
        return True


_alive = pid_alive


def _refresh():
    if shutil.which("omarchy-shell"):
        subprocess.Popen(["omarchy-shell", "-q", "io.github.jagritvats.dolly", "refresh"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------------ windows without focus
def exec_unfocused(cmd: list[str]) -> bool:
    """Open a window without taking keyboard focus (Hyprland exec rule
    no_initial_focus), so a recording-stop never yanks the user out of what
    they were doing. Falls back to a plain launch."""
    if shutil.which("hyprctl") and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        line = " ".join(_q(c) for c in cmd)
        lua = f'hl.exec_cmd({json.dumps(line)}, {{ no_initial_focus = true }})'
        try:
            r = subprocess.run(["hyprctl", "eval", lua], capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and "error" not in (r.stdout + r.stderr).lower():
                return True
            r = subprocess.run(["hyprctl", "dispatch", "exec", f"[noinitialfocus] {line}"], capture_output=True,
                               text=True, timeout=3)
            if r.returncode == 0 and "ok" in r.stdout.lower():
                return True
        except Exception:
            _dlog.swallowed()
            pass
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def _q(s: str) -> str:
    import shlex
    return shlex.quote(str(s))


# ------------------------------------------------------------------ Omarchy's own recorder
RECORDER_EXES = ("gpu-screen-recorder", "wf-recorder")


def _proc_exe(pid: int, proc_root: str = "/proc") -> str:
    try:
        exe = os.readlink(f"{proc_root}/{pid}/exe")
    except OSError:
        return ""
    return exe.removesuffix(" (deleted)").rsplit("/", 1)[-1]


def recorder_pids(proc_root: str | None = None) -> list[int]:
    """The user's gpu-screen-recorder / wf-recorder processes, matched by executable
    like omarchy-capture-screenrecording-process does."""
    root = proc_root or os.environ.get("DOLLY_PROC_ROOT", "/proc")
    uid = os.getuid()
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for n in names:
        if not n.isdigit():
            continue
        try:
            if os.stat(f"{root}/{n}").st_uid != uid:
                continue
        except OSError:
            continue
        if _proc_exe(int(n), root) in RECORDER_EXES:
            out.append(int(n))
    return out


def own_recorder_pids() -> set[int]:
    """Recorders Dolly started (recording and instant replay)."""
    own = set()
    for f in (runtime_dir() / "state.json", runtime_dir() / "replay.json"):
        try:
            p = json.loads(f.read_text()).get("recorder_pid")
            if p:
                own.add(int(p))
        except (OSError, ValueError, TypeError):
            pass
    return own


def other_recorder() -> str | None:
    """A screen recording Dolly did not start (Omarchy's Alt+Print recorder, OBS's
    gsr, a terminal wf-recorder): its name, else None. Dolly never starts a second
    recorder next to one."""
    rt = Path(os.environ.get("XDG_RUNTIME_DIR") or (Path.home() / ".local/state/omarchy"))
    own = own_recorder_pids()
    # TEST HARNESS ONLY: the walkthrough's outer screen capture (it films Dolly itself)
    ign = os.environ.get("DOLLY_IGNORE_RECORDERS", "")
    try:
        ign += "," + (runtime_dir() / "ignore-recorders").read_text().replace("\n", ",")
    except OSError:
        pass
    own |= {int(p) for p in ign.split(",") if p.strip().isdigit()}
    starting = rt / "omarchy-screenrecord-starting"
    try:
        sp = int(starting.read_text().strip())
        if pid_alive(sp):
            return "Omarchy's screen recorder"
    except (OSError, ValueError):
        pass
    try:
        op = int((rt / "omarchy-screenrecord-pid").read_text().strip())
        if op not in own and pid_alive(op) and _proc_exe(op) in RECORDER_EXES:
            return "Omarchy's screen recorder"
    except (OSError, ValueError):
        pass
    for p in recorder_pids():
        if p not in own and pid_alive(p):
            return f"{_proc_exe(p)} (pid {p})"
    return None


def recorder_exit_kind(pdir) -> str:
    """The recorder is gone but Dolly did not stop it. A container with a written
    duration was closed by the recorder itself after a SIGINT (Omarchy's
    screen-recording key signals every recorder when it has no pid file of its
    own) -> "stopped-outside"; a torn file -> "recorder-died"."""
    try:
        from .project import Project
        from .render import probe
        raw = Project(pdir).raw
        dur = float(probe(raw).get("format", {}).get("duration", 0) or 0)
        return "stopped-outside" if dur > 0 else "recorder-died"
    except Exception:
        _dlog.swallowed()
        return "recorder-died"


def xwayland_zero_scaling() -> bool | None:
    """Hyprland `xwayland:force_zero_scaling` (Omarchy default: true). It only changes how
    X11 apps draw their content; their geometry in `hyprctl clients` stays logical, so
    Dolly's mapping is the same either way (recorded for diagnosis)."""
    if not shutil.which("hyprctl"):
        return None
    try:
        r = subprocess.run(["hyprctl", "getoption", "xwayland:force_zero_scaling", "-j"], capture_output=True,
                           text=True, timeout=1.5)
        return bool(json.loads(r.stdout).get("int", 0))
    except Exception:
        _dlog.swallowed()
        return None


# ------------------------------------------------------------------ virtual GPUs
# VirtualBox (VMSVGA) and VMware expose a vmwgfx GPU: gpu-screen-recorder cannot capture
# on it, and mpv/Chromium may crash on its OpenGL unless it is done in software (VM, Nov 10).
VIRTUAL_DRIVERS = {"vmwgfx": "VMware SVGA (VirtualBox/VMware)", "virtio_gpu": "virtio-gpu (QEMU/KVM)",
                   "virtio-pci": "virtio-gpu (QEMU/KVM)", "qxl": "QXL (QEMU/SPICE)", "vboxvideo": "VirtualBox VGA"}
VIRTUAL_VENDORS = {"0x15ad": "vmwgfx", "0x80ee": "vboxvideo"}


def virtual_gpu() -> dict | None:
    """{"driver", "name"} when the only GPUs are virtual, else None."""
    drm = Path(os.environ.get("DOLLY_SYSFS_DRM", "/sys/class/drm"))
    found, real = None, False
    try:
        cards = sorted(p for p in drm.iterdir() if p.name.startswith("card") and "-" not in p.name)
    except OSError:
        return None
    for c in cards:
        dev = c / "device"
        try:
            drv = os.path.basename(os.readlink(dev / "driver"))
        except OSError:
            drv = ""
        try:
            ven = (dev / "vendor").read_text().strip().lower()
        except OSError:
            ven = ""
        drv = drv if drv in VIRTUAL_DRIVERS else VIRTUAL_VENDORS.get(ven, drv)
        if drv in VIRTUAL_DRIVERS:
            found = found or {"driver": drv, "name": VIRTUAL_DRIVERS[drv]}
        elif drv or ven:
            real = True
    return None if real else found


def software_gl_env(base: dict | None = None) -> dict:
    """Environment for OpenGL helpers Dolly starts (mpv): software GL on a virtual GPU.
    DOLLY_SOFTWARE_GL=0 / 1 forces it off / on."""
    env = dict(os.environ if base is None else base)
    force = env.get("DOLLY_SOFTWARE_GL")
    if force == "0":
        return env
    if force == "1" or virtual_gpu():
        env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
    return env

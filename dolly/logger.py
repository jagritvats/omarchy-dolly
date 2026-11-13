"""Event logger: runs for the duration of a recording and writes
``events.raw.jsonl`` in the project directory.

Sources, all optional and degrading gracefully:

* cursor position - Hyprland request socket (``j/cursorpos``) polled at
  ~125 Hz, logged on change. Fallback: ``hyprctl -j cursorpos``.
* focused window geometry - Hyprland event socket (``.socket2.sock``),
  ``activewindowv2`` / ``movewindowv2`` / ``changefloatingmode`` -> ``j/activewindow``.
* clicks + keys, first available of:
    1. ``lua``   Dolly's Hyprland Lua bridge (hypr/dolly-input.lua), loaded with
                 ``hyprctl eval``; no extra permissions, sees touchpad taps.
    2. ``evdev`` /dev/input/event* (needs the ``input`` group); physical
                 buttons only (libinput tap-to-click is invisible here).
    3. ``none``  no clicks; the zoom engine falls back to cursor-dwell detection.

Every line: {"type": ..., "ms": <ms since logger start>, ...}; coordinates are
Hyprland logical global coordinates. ``dolly finalize`` maps them into video
pixels.
"""
from __future__ import annotations

import collections
import glob
import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from . import fsutil, keymap
from . import log as _dlog

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def on_stage() -> bool:
    """This process works on Dolly's hidden stage (dolly/stage.py), not on the real desktop."""
    return bool(os.environ.get("DOLLY_STAGE"))


def runtime_dir() -> Path:
    """Where the state of the take in progress lives. One folder per desktop: a take on the
    hidden stage keeps its own, so the bar button (which watches the real desktop's folder)
    does not react to it and neither desktop's recording blocks the other's."""
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.local/state")
    d = Path(base) / "dolly"
    if on_stage():
        d = d / "stage" / "rt"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def drop_bridge_file() -> None:
    """The input bridge's file is gone with the bridge: it lists raw key codes."""
    try:
        (runtime_dir() / "input.jsonl").unlink(missing_ok=True)
    except OSError:
        pass


def hypr_socket(kind: str = ".socket.sock") -> str | None:
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        return None
    for base in (os.environ.get("XDG_RUNTIME_DIR", ""), "/tmp"):
        p = os.path.join(base, "hypr", sig, kind)
        if os.path.exists(p):
            return p
    return None


def hypr_request(cmd: str, timeout: float = 0.5) -> str | None:
    path = hypr_socket()
    if not path:
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(cmd.encode())
            chunks = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
            return b"".join(chunks).decode(errors="replace")
    except OSError:
        return None


def hypr_json(cmd: str):
    out = hypr_request("j/" + cmd)
    if out is None:
        try:
            out = subprocess.run(["hyprctl", "-j", cmd], capture_output=True, text=True, timeout=1).stdout
        except Exception:
            _dlog.swallowed()
            return None
    try:
        return json.loads(out)
    except (TypeError, ValueError):
        return None


def hypr_eval(lua: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(["hyprctl", "eval", lua], capture_output=True, text=True, timeout=3)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0 and "error" not in out.lower(), out
    except Exception as e:
        _dlog.swallowed()
        return False, str(e)


def window_on_region(rect, region, frac: float = 0.5) -> bool:
    """At least `frac` of the window's area lies inside the recorded region."""
    x, y, w, h = rect
    rx, ry, rw, rh = region
    ow = min(x + w, rx + rw) - max(x, rx)
    oh = min(y + h, ry + rh) - max(y, ry)
    return w > 0 and h > 0 and max(0, ow) * max(0, oh) >= frac * w * h



ARM_SETTLE_S = 0.25          # syncArm -> syncFlash: the transparent surface is mapped by then


def software_rendering() -> bool:
    """Qt Quick drawing on the CPU (VMs: QT_QUICK_BACKEND=software): frames reach the
    compositor late and irregularly, so a 70 ms marker can fall between captured frames."""
    e = os.environ
    return (e.get("QT_QUICK_BACKEND", "").lower() == "software" or e.get("QSG_RHI_BACKEND", "").lower() == "software"
            or e.get("LIBGL_ALWAYS_SOFTWARE") == "1")


def flash_ms() -> int:
    """How long the sync marker stays lit; DOLLY_SYNC_FLASH_MS overrides."""
    try:
        return max(30, min(1000, int(os.environ["DOLLY_SYNC_FLASH_MS"])))
    except (KeyError, ValueError):
        return 160 if software_rendering() else 70

class Logger:
    def __init__(self, project_dir: Path, keys: bool = True, replay: int = 0):
        self.dir = project_dir
        self.keys = keys
        self.t0 = time.monotonic()
        self.replay = replay
        # replay mode keeps a rolling in-memory buffer (nothing hits disk until a save)
        self.ring = collections.deque() if replay else None
        self.f = None if replay else open(project_dir / "events.raw.jsonl", "a", buffering=1)
        self.paused = False
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.mods: set[str] = set()
        self.click_source = "none"
        self.last_pos = (0.0, 0.0)
        self.clients_dirty = threading.Event()
        self.trouble = None
        self.target = None
        self.monitor = None
        self.region = None
        self.recorder_pid = None
        self.solid = False
        self.last_ws = ""
        self.stage = on_stage()
        self.raw_hold_until = 0.0        # a `dolly stage type|key` is typing: it reports the keys by name
        self.feed_on = False             # a second source of clicks is followed (the same click may come twice)
        self._last_click = None

    def ms(self) -> float:
        return round((time.monotonic() - self.t0) * 1000.0, 1)

    def write(self, obj: dict):
        obj.setdefault("ms", self.ms())
        with self.lock:
            if self.ring is not None:
                obj["mono"] = round(time.monotonic(), 4)
                self.ring.append(obj)
                horizon = obj["mono"] - self.replay - 10
                while self.ring and self.ring[0].get("mono", 0) < horizon and self.ring[0]["type"] not in ("start", "info"):
                    self.ring.popleft()
            elif not self.paused or obj["type"] in ("pause", "resume", "stop", "info"):
                self.f.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def dump_ring(self):
        """Replay save: write the buffer (with absolute monotonic times) for `dolly replay save`."""
        with self.lock:
            items = list(self.ring or [])
        tmp = self.dir / "replay-events.jsonl.tmp"
        with open(tmp, "w") as f:
            for o in items:
                f.write(json.dumps(o, separators=(",", ":")) + "\n")
            f.write(json.dumps({"type": "dump", "mono": round(time.monotonic(), 4)}) + "\n")
        tmp.replace(self.dir / "replay-events.jsonl")

    def toggle_mute(self):
        self.muted = not getattr(self, "muted", False)
        self.write({"type": "mute", "on": self.muted})

    def mark(self):
        x, y = self.last_pos
        self.write({"type": "mark", "x": x, "y": y})

    def toggle_pause(self):
        if self.paused:
            self.paused = False
            self.write({"type": "resume"})
        else:
            self.write({"type": "pause"})
            self.paused = True

    # ---------------------------------------------------------- cursor
    def poll_cursor(self, hz: float = 125.0):
        use_socket = hypr_socket() is not None
        if not use_socket:
            hz = min(hz, 30.0)  # each sample spawns hyprctl; keep it cheap
        period = 1.0 / hz
        last = None
        nxt = time.monotonic()
        while not self.stop.is_set():
            pos = None
            if use_socket:
                out = hypr_request("j/cursorpos", timeout=0.2)
                try:
                    d = json.loads(out) if out else None
                    pos = (float(d["x"]), float(d["y"])) if d else None
                except (ValueError, KeyError, TypeError):
                    pos = None
            if pos is None:
                d = hypr_json("cursorpos")
                if d:
                    pos = (float(d["x"]), float(d["y"]))
            if pos and pos != last and not getattr(self, "marking", False):
                self.last_pos = pos
                self.write({"type": "move", "x": pos[0], "y": pos[1]})
                last = pos
            nxt += period
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.monotonic()

    # ---------------------------------------------------------- windows / workspaces / layers
    def watch_windows(self):
        """Hyprland event socket -> workspace/layer/monitor events; window geometry is
        polled (tiling, drags and resizes have no events) by poll_clients."""
        path = hypr_socket(".socket2.sock")
        if not path:
            return
        self.emit_window()
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
            s.settimeout(0.5)
        except OSError:
            return
        buf = b""
        while not self.stop.is_set():
            try:
                b = s.recv(4096)
                if not b:
                    self.on_trouble("hyprland-exit", "Hyprland went away (logout?)")
                    break
                buf += b
            except socket.timeout:
                continue
            except OSError:
                break
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                ev, _, data = line.decode(errors="replace").partition(">>")
                if ev == "activewindow":            # "class,title": known before the next clients poll
                    from .sensitive import secret_window
                    c, _, ttl = data.partition(",")
                    self.secret_focus = bool(secret_window(c, ttl))
                if ev in ("activewindowv2", "movewindowv2", "changefloatingmode", "fullscreen", "openwindow",
                          "closewindow"):
                    self.clients_dirty.set()
                    if ev == "openwindow" and self.solid:
                        from .solid import make_solid
                        threading.Thread(target=make_solid, args=([data.split(",")[0]],), daemon=True).start()
                    if ev == "closewindow" and self.target and data.strip() and ("0x" + data.strip()).endswith(
                            self.target.replace("0x", "")):
                        self.on_trouble("window-closed", "The recorded window was closed")
                if ev == "workspacev2":
                    wid, _, name = data.partition(",")
                    self.write({"type": "workspace", "name": name, "special": False})
                    self.clients_dirty.set()
                elif ev == "activespecial":
                    name, _, mon = data.partition(",")
                    self.write({"type": "workspace", "name": name or self.last_ws, "special": bool(name)})
                    self.clients_dirty.set()
                elif ev in ("openlayer", "closelayer"):
                    self.emit_layers(data.strip(), ev == "openlayer")
                elif ev in ("monitorremovedv2", "monitorremoved"):
                    name = data.split(",")[-1].strip()
                    self.write({"type": "monitor", "reason": "removed", "name": name})
                    if name and name == self.monitor:
                        self.on_trouble("monitor-removed", f"Monitor {name} was disconnected")
                elif ev in ("monitoraddedv2", "monitoradded"):
                    self.write({"type": "monitor", "reason": "added", "name": data.split(",")[-1].strip()})
                if ev == "workspacev2":
                    self.last_ws = data.partition(",")[2]
        s.close()

    def poll_toasts(self, every: float = 0.4):
        """Notification toasts on screen, from the shell (`omarchy-shell notifications count`).
        Hyprland cannot tell: the notification layer stays mapped while idle. Written on
        change, starting with what is there when the recording begins; privacy.auto_boxes
        blurs them. A shell without the call ends the polling quietly."""
        if on_stage():
            return                  # no shell and no notification cards on a stage
        prev = None
        while not self.stop.is_set():
            try:
                r = subprocess.run(["omarchy-shell", "notifications", "count"], capture_output=True, text=True,
                                   timeout=1.5)
                n = int(r.stdout.strip())
            except (OSError, ValueError, subprocess.TimeoutExpired):
                return
            if n != prev:
                self.write({"type": "toasts", "count": n})
                prev = n
            self.stop.wait(every)

    def emit_window(self):
        w = hypr_json("activewindow")
        if w and w.get("at"):
            self.write({"type": "window", "rect": [*w["at"], *w["size"]], "title": w.get("title", "")[:120],
                        "class": w.get("class", ""), "address": w.get("address", "")})

    def poll_clients(self, hz: float = 10.0):
        """Visible windows (rect, class, floating, fullscreen) + focus, written on change."""
        prev = None
        while not self.stop.is_set():
            self.clients_dirty.wait(1.0 / hz)
            self.clients_dirty.clear()
            cl = hypr_json("clients")
            aw = hypr_json("activewindow") or {}
            from .sensitive import secret_window
            self.secret_focus = bool(secret_window(aw.get("class"), aw.get("title")))
            mons = hypr_json("monitors") or []
            if cl is None:
                continue
            visible_ws = set()
            for m in mons:
                visible_ws.add((m.get("activeWorkspace") or {}).get("id"))
                sp = (m.get("specialWorkspace") or {}).get("id")
                if sp:
                    visible_ws.add(sp)
            wins = []
            for c in cl:
                if not c.get("mapped", True) or c.get("hidden"):
                    continue
                if visible_ws and (c.get("workspace") or {}).get("id") not in visible_ws:
                    continue
                wins.append({"address": c.get("address"), "class": c.get("class") or c.get("initialClass", ""),
                             "title": (c.get("title") or "")[:80], "rect": [*c["at"], *c["size"]],
                             "floating": bool(c.get("floating")), "fullscreen": bool(c.get("fullscreen")),
                             "xwayland": bool(c.get("xwayland")),
                             "workspace": (c.get("workspace") or {}).get("name")})
            snap = {"windows": wins, "focus": aw.get("address")}
            if self.target and self.region:
                self._check_target_left(cl, wins)
            key = json.dumps(snap, sort_keys=True)
            if key != prev:
                self.write({"type": "clients", **snap})
                prev = key

    LEAVE_GRACE = 1.5   # s a followed window may spend off the recorded monitor (a drag across)

    def _check_target_left(self, clients, wins):
        """Window mode: the recording is one monitor. When the followed window sits
        mostly on another monitor for LEAVE_GRACE, end the recording cleanly
        (finalize trims to the moment it left); shorter excursions are kept and the
        card shows the part still on the monitor."""
        tgt = self.target.replace("0x", "")
        if not any(str(c.get("address", "")).replace("0x", "") == tgt for c in clients):
            return              # closed: closewindow handles it
        w = next((w for w in wins if str(w.get("address", "")).replace("0x", "") == tgt), None)
        on = w is not None and window_on_region(w["rect"], self.region)
        now = time.monotonic()
        if on:
            self._left_since = None
        elif getattr(self, "_left_since", None) is None:
            self._left_since = now
        elif now - self._left_since >= self.LEAVE_GRACE:
            self.on_trouble("window-left-monitor", "The recorded window moved to another monitor")

    LAUNCHERS = ("omarchy", "menu", "launcher", "walker", "wofi", "rofi", "fuzzel", "anyrun", "quickshell")

    def emit_layers(self, namespace: str, opened: bool):
        rect = None
        if opened:
            layers = hypr_json("layers") or {}
            for mon in layers.values():
                for lvl in (mon.get("levels") or {}).values():
                    for l in lvl:
                        if l.get("namespace") == namespace:
                            rect = [l.get("x", 0), l.get("y", 0), l.get("w", 0), l.get("h", 0)]
        ns = namespace.lower()
        keyboard = any(k in ns for k in self.LAUNCHERS) and "notif" not in ns
        self.write({"type": "layer", "namespace": namespace, "open": opened, "keyboard": keyboard, "rect": rect})

    # ---------------------------------------------------------- sync marker
    def sync_flash(self, kind: str):
        """Ask the shell to flash the sync marker (magenta square, green centre) for a few
        frames at the recorded region's top-left; log the request time.

        Two steps (VM, Nov 10): mapping a new layer surface and lighting it in one go was
        never painted under QT_QUICK_BACKEND=software - Hyprland reported the dolly-sync
        layer open for ~70 ms, yet 0 of 632 frames showed it. So the surface is first
        mapped transparent (`syncArm`), and the flash only changes its colour. An older
        bar widget without syncArm still gets the one-step flash."""
        if on_stage():
            return self.stage_mark(kind)
        x, y = (self.region or [0, 0])[:2]
        size = int(40)
        ms = flash_ms()
        geo = {"x": x, "y": y, "size": size}
        ok = False
        if shutil.which("omarchy-shell"):
            try:
                armed = subprocess.run(["omarchy-shell", "io.github.jagritvats.dolly", "syncArm", json.dumps(geo)],
                                       capture_output=True, text=True, timeout=1.5)
                if armed.returncode == 0 and armed.stdout.strip() == "ok":
                    time.sleep(ARM_SETTLE_S)          # let the transparent surface map and paint
            except Exception:
                _dlog.swallowed()
                pass
            t_req = self.ms()
            try:
                r = subprocess.run(["omarchy-shell", "io.github.jagritvats.dolly", "syncFlash",
                                    json.dumps(dict(geo, ms=ms, disarm=True))],
                                   capture_output=True, text=True, timeout=1.5)
                ok = r.returncode == 0
            except Exception:
                ok = False
        if ok:
            # the overlay appears while the IPC call is in flight (the shell handles it, then
            # answers): its midpoint is the unbiased estimate; the time after return was
            # ~1-2 frames late (measured with the A/V test compositor)
            t_ack = self.ms()
            self.write({"type": "sync", "kind": kind, "method": "layer", "ms": round((t_req + t_ack) / 2, 1),
                        "rtt_ms": round(t_ack - t_req, 1), "flash_ms": ms})
        else:
            self.write({"type": "info", "msg": "sync marker unavailable (no omarchy-shell IPC); "
                                               "using logger start as time base"})
        return ok

    def stage_mark(self, kind: str) -> bool:
        """The sync marker on the hidden stage: no shell lives there to flash a square, so the
        stage's own pointer jumps into the top-left corner for a moment (dolly/stage.py,
        sync.detect_pointer_marks). The jump is not part of the pointer's path."""
        from . import stage
        hold = max(stage.MARK_HOLD_S, flash_ms() / 1000.0)
        stay = kind == "stop"                # the stop mark stays in the corner until the recorder has gone
        self.marking = True
        m = None
        try:
            m = stage.pointer_mark(hold + (0.15 if stay else 0.0), stay=stay)
            time.sleep(0.06)                 # the poller must not log the way back either
        finally:
            if not (stay and m):
                self.marking = False         # (after a stop mark the pointer is not where the take left it)
        if m is None:
            self.write({"type": "info", "msg": "no sync marker on the stage (the pointer could not be moved); "
                                               "timed by the clock"})
            return False
        self.write({"type": "sync", "kind": kind, "method": "pointer", "ms": round((m["t"] - self.t0) * 1000.0, 1),
                    "rtt_ms": round(m["rtt"] * 1000.0, 1), "flash_ms": round(hold * 1000), "stays": stay})
        return True

    # ---------------------------------------------------------- watchdog
    def on_trouble(self, reason: str, msg: str):
        """Something ended the recording from outside: stop cleanly (finalize, keep
        everything captured) and tell the user why."""
        if self.trouble or self.stop.is_set():
            return
        self.trouble = reason
        self.write({"type": "warn", "reason": reason, "msg": msg})
        dolly = str(PLUGIN_ROOT / "bin" / "dolly")
        try:
            subprocess.Popen([dolly, "stop", "--reason", reason], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass

    def watchdog(self):
        """Recorder death, low disk, lock/suspend: stop cleanly instead of losing data."""
        from .health import disk_free_bytes, MIN_FREE_RECORDING
        sleep_mon = None
        if shutil.which("gdbus"):
            try:
                sleep_mon = subprocess.Popen(["gdbus", "monitor", "--system", "--dest", "org.freedesktop.login1"],
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                os.set_blocking(sleep_mon.stdout.fileno(), False)
            except OSError:
                sleep_mon = None
        while not self.stop.is_set():
            self.stop.wait(0.5)
            if self.recorder_pid:
                from .health import pid_alive
                if not pid_alive(self.recorder_pid):
                    from .health import recorder_exit_kind
                    kind = recorder_exit_kind(self.dir)
                    self.on_trouble(kind, "The screen recorder was stopped outside Dolly" if kind == "stopped-outside"
                                    else "The screen recorder stopped unexpectedly")
            free = disk_free_bytes(self.dir)
            if free is not None and free < MIN_FREE_RECORDING:
                self.on_trouble("disk-full", f"Disk almost full ({free // (1 << 20)} MB left)")
            if sleep_mon is not None:
                try:
                    out = sleep_mon.stdout.read() or ""
                except (OSError, TypeError):
                    out = ""
                if "PrepareForSleep" in out and "true" in out:
                    self.on_trouble("suspend", "The computer is going to sleep")
                elif "Lock" in out and "Session" in out and not on_stage():
                    self.on_trouble("lock", "The session was locked")
            if not on_stage() and shutil.which("pgrep") and int(time.monotonic() * 2) % 4 == 0:
                try:
                    locked = subprocess.run(["pgrep", "-x", "hyprlock"], capture_output=True,
                                            timeout=2).returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    locked = False
                if locked:
                    self.on_trouble("lock", "The screen was locked")
        if sleep_mon is not None:
            sleep_mon.terminate()

    # ---------------------------------------------------------- input: lua
    def start_lua_bridge(self) -> bool:
        rd = runtime_dir()
        inp = rd / "input.jsonl"
        # the bridge appends every key code here: ours alone, and emptied for this take
        os.close(os.open(inp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        os.chmod(inp, 0o600)
        nk = rd / "nokeys"
        if self.keys:
            nk.unlink(missing_ok=True)
        else:
            nk.write_text("1")
        lua = PLUGIN_ROOT / "hypr" / "dolly-input.lua"
        # the bridge writes where this take's state lives (one folder per desktop, see runtime_dir)
        ok, out = hypr_eval(f'dolly_input_dir = {json.dumps(str(rd))}; dofile("{lua}")')
        if not ok:
            self.write({"type": "info", "msg": f"lua bridge failed: {out[:200]}"})
            drop_bridge_file()
            return False
        # confirm the bridge wrote its hello line
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            txt = inp.read_text()
            if '"bridge"' in txt:
                hello = json.loads(txt.splitlines()[0])
                if hello.get("clicks", 0) > 0:
                    self.click_source = "lua"
                    self.write({"type": "info", "msg": "lua bridge active", **hello})
                    threading.Thread(target=self.tail_lua, args=(inp,), daemon=True).start()
                    return True
                break
            time.sleep(0.05)
        self.stop_lua_bridge()
        return False

    BRIDGE_KEEP = 2.0       # s a line stays in the bridge's file after it was read

    def tail_lua(self, path: Path):
        """Follow the bridge's file. It holds the code of every key pressed, also those the
        logger drops (a password manager has focus) or keeps only as a time (typed text), so
        what has been read is erased from the file `BRIDGE_KEEP` seconds later: the file
        keeps its length, the bridge keeps appending and nothing is lost, and neither a take
        nor hours of instant replay pile key codes up in it. The delay is for a second logger
        following the same file (a recording made while instant replay runs)."""
        with open(path, "r+b", buffering=0) as f:           # writable: erasing needs it
            fd = f.fileno()
            buf, pos, done, erased, can_erase = b"", 0, 0, 0, True
            marks: collections.deque = collections.deque()      # (time, bytes handled by then)
            while not self.stop.is_set():
                chunk = f.read(65536)
                if not chunk:
                    try:
                        if os.fstat(fd).st_size < pos:         # emptied by a logger that started after us
                            f.seek(0)
                            buf, pos, done, erased = b"", 0, 0, 0
                            marks.clear()
                            continue
                    except OSError:
                        pass
                    now = time.monotonic()
                    upto = erased
                    while marks and now - marks[0][0] >= self.BRIDGE_KEEP:
                        upto = marks.popleft()[1]
                    if can_erase and upto > erased:
                        can_erase = fsutil.erase_range(fd, erased, upto - erased)
                        erased = upto
                    time.sleep(0.004)
                    continue
                pos += len(chunk)
                *lines, buf = (buf + chunk).split(b"\n")
                for line in lines:
                    done += len(line) + 1
                    self._bridge_line(line)
                if lines:
                    marks.append((time.monotonic(), done))

    def _bridge_line(self, line: bytes):
        try:
            d = json.loads(line)
            if d.get("type") == "click":
                self.on_click(d, fed=False)
            elif d.get("type") == "key" and self.keys:
                if self.stage:
                    self.on_unnamed_key(bool(d.get("down", True)))
                else:
                    self.on_key(int(d["code"]) - 8, bool(d.get("down", True)))
        except (ValueError, KeyError, TypeError, AttributeError):
            pass                    # a torn or erased line; the hello line has neither type

    def on_click(self, d: dict, fed: bool):
        """One click. With two sources followed (a stage: Hyprland's own report and the
        command that made the click) the same click can arrive from both: logged once."""
        x, y, button = d["x"], d["y"], d.get("button", "left")
        if self.feed_on:
            now, last = time.monotonic(), self._last_click
            if last and last[1] != fed and now - last[0] < 0.4 and last[4] == button \
                    and abs(last[2] - x) <= 3 and abs(last[3] - y) <= 3:
                return
            self._last_click = (now, fed, x, y, button)
        self.write({"type": "click", "x": x, "y": y, "button": button})

    def on_unnamed_key(self, down: bool):
        """A key pressed on the hidden stage, as Hyprland reports it: a code and nothing else.
        Every key on a stage comes from a typing tool (wtype and the like), and each of those
        brings a keymap of its own, so the code says nothing about which key it was (the
        letters of "ls" arrive as codes 9 and 10: "Escape" and "1" on a real keyboard).
        `dolly stage type` and `dolly stage key` report what they type by name; while they
        do, the codes are theirs and are dropped here. A key from any other tool is logged
        as a key without a name: typing is still followed, and no wrong key is shown."""
        if not down or time.monotonic() < self.raw_hold_until:
            return
        if getattr(self, "secret_focus", False):
            return
        self.write({"type": "key", "key": keymap.UNNAMED, "mods": []})

    def start_harness_input(self, name="harness-input.jsonl", source="harness",
                            msg="clicks/keys from the test harness input driver (not Hyprland)"):
        path = runtime_dir() / name
        if not path.exists():
            path.write_text("")
        if source:
            self.click_source = source
        self.write({"type": "info", "msg": msg})
        threading.Thread(target=self.tail_harness, args=(path,), daemon=True).start()

    def tail_harness(self, path: Path):
        """Like tail_lua, but shared: several loggers may follow the same file, so start
        at its end and never truncate it."""
        with open(path) as f:
            f.seek(0, 2)
            while not self.stop.is_set():
                line = f.readline()
                if not line:
                    time.sleep(0.004)
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "click":
                    self.on_click(d, fed=True)
                elif d.get("type") == "hold":
                    try:
                        self.raw_hold_until = float(d.get("until") or 0.0)
                    except (TypeError, ValueError):
                        pass
                elif d.get("type") == "key" and self.keys:
                    self.on_key(int(d["code"]) - 8, bool(d.get("down", True)))

    @staticmethod
    def stop_lua_bridge():
        hypr_eval("if dolly_input_stop then dolly_input_stop() end")
        drop_bridge_file()

    # ---------------------------------------------------------- input: evdev
    def start_evdev(self) -> bool:
        fds = []
        for p in sorted(glob.glob("/dev/input/event*")):
            try:
                fds.append(os.open(p, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                continue
        if not fds:
            return False
        self.click_source = "evdev"
        threading.Thread(target=self.read_evdev, args=(fds,), daemon=True).start()
        return True

    def read_evdev(self, fds):
        fmt = "llHHi"
        size = struct.calcsize(fmt)
        btn = {0x110: "left", 0x111: "right", 0x112: "middle"}
        while not self.stop.is_set():
            r, _, _ = select.select(fds, [], [], 0.3)
            for fd in r:
                try:
                    data = os.read(fd, size * 64)
                except OSError:
                    continue
                for off in range(0, len(data) - size + 1, size):
                    _s, _us, etype, code, value = struct.unpack_from(fmt, data, off)
                    if etype != 1:  # EV_KEY
                        continue
                    if code in btn and value == 1:
                        x, y = self.last_pos
                        self.write({"type": "click", "x": x, "y": y, "button": btn[code]})
                    elif code < 0x100 and self.keys and value in (0, 1):
                        self.on_key(code, value == 1)
        for fd in fds:
            os.close(fd)

    # ---------------------------------------------------------- keys
    def on_key(self, code: int, down: bool):
        mod = keymap.MODS.get(code)
        if mod:
            if mod == "alt":                    # which Alt key matters: see keymap.recorded
                alts = self.__dict__.setdefault("_alts", set())
                (alts.add if down else alts.discard)(code)
                down = bool(alts)
            (self.mods.add if down else self.mods.discard)(mod)
            return
        held = self.__dict__.setdefault("_held_keys", set())
        if not down:
            held.discard(code)
            return
        if code in held:
            return          # duplicate press (Hyprland may deliver one event per keyboard path) or auto-repeat
        held.add(code)
        if getattr(self, "secret_focus", False):
            if not getattr(self, "_secret_noted", False):
                self._secret_noted = True
                self.write({"type": "info",
                            "msg": "keys are not logged while a password manager or authentication prompt has focus"})
            return
        name = keymap.label(code)
        if name is None:
            return
        left_alt = keymap.LEFT_ALT in self.__dict__.get("_alts", ())
        self.write({"type": "key", **keymap.stored(name, self.mods, left_alt)})    # typed characters: the time only

    # ---------------------------------------------------------- main
    def run(self, meta: dict):
        self.region, self.monitor = meta.get("region"), meta.get("monitor")
        self.target, self.recorder_pid = meta.get("target"), meta.get("recorder_pid")
        self.solid = bool(meta.get("solid_windows"))
        self.write({"type": "start", **meta, "t0_mono": round(self.t0, 6),
                    "t0_wall": round(time.time() - (time.monotonic() - self.t0), 6)})
        self.muted = False
        threads = [threading.Thread(target=self.poll_cursor, daemon=True),
                   threading.Thread(target=self.watch_windows, daemon=True),
                   threading.Thread(target=self.poll_clients, daemon=True),
                   threading.Thread(target=self.poll_toasts, daemon=True)]
        if not self.replay:
            threads.append(threading.Thread(target=self.watchdog, daemon=True))
        for t in threads:
            t.start()
        pref = os.environ.get("DOLLY_CLICK_SOURCE", "auto")
        if os.environ.get("DOLLY_EVENTS_FROM_HARNESS") == "1":
            # TEST HARNESS ONLY: clicks/keys come from the harness input driver
            # (harness/dctl writes the bridge's line format), not from Hyprland
            self.start_harness_input()
        elif os.environ.get("DOLLY_EVENTS_FROM_SCRIPT") == "1":
            # `dolly run`: the storyboard runner types through wtype, whose private keymap makes
            # the keycodes Hyprland reports meaningless, so the runner reports what it typed
            self.start_harness_input("script-input.jsonl", "script",
                                     "clicks/keys reported by the storyboard runner (dolly run)")
        elif pref in ("auto", "lua") and self.start_lua_bridge():
            pass
        elif pref in ("auto", "evdev") and self.start_evdev():
            pass
        if self.stage and not any(os.environ.get(k) == "1" for k in ("DOLLY_EVENTS_FROM_HARNESS",
                                                                     "DOLLY_EVENTS_FROM_SCRIPT")):
            # a take somebody drives by hand on the stage: `dolly stage type|key|click` report
            # what they do by name, next to what Hyprland itself sees
            self.feed_on = True
            self.start_harness_input("script-input.jsonl", "" if self.click_source != "none" else "stage",
                                     "keys and clicks made with `dolly stage type|key|click` are reported by name")
        self.write({"type": "info", "click_source": self.click_source})
        if not self.replay and os.environ.get("DOLLY_SYNC", "1") != "0":
            time.sleep(0.15)          # the recorder is producing frames; mark t0
            self.sync_flash("start")

        def handle(_sig, _frm):
            self.stop.set()
        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)
        # SIGUSR1: replay mode -> dump buffer; recording -> "mark zoom here"
        signal.signal(signal.SIGUSR1, lambda *_: self.dump_ring() if self.replay else self.mark())
        signal.signal(signal.SIGUSR2, lambda *_: self.toggle_pause())  # pause / resume
        signal.signal(signal.SIGRTMIN + 1, lambda *_: self.toggle_mute())  # "mute mic": a muted span
        while not self.stop.is_set():
            self.stop.wait(0.2)
        if not self.replay and self.trouble is None and os.environ.get("DOLLY_SYNC", "1") != "0":
            # end marker while the recorder is still running (dolly stop stops us first)
            self.stop.clear()
            if self.sync_flash("stop") and not self.stage:
                time.sleep(0.25)     # (on a stage the mark stays; whoever stops the recorder keeps it repainting)
            self.stop.set()
        if self.click_source == "lua":
            self.stop_lua_bridge()
        self.write({"type": "stop"})
        time.sleep(0.05)
        if self.f:
            self.f.close()


def run_logger(project_dir: Path, keys: bool = True, replay: int = 0):
    meta_path = project_dir / "capture.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    Logger(project_dir, keys=keys, replay=replay).run({k: meta.get(k) for k in (
        "region", "scale", "monitor", "target", "recorder_pid", "mode", "cursor_size", "solid_windows")})

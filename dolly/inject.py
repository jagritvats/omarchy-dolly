"""Input and window control for scripted demos (`dolly run`).

What performs the input, and why the event log gets it from us:

* **Keys**: `wtype` (ships with Omarchy) types through a Wayland virtual keyboard. It
  brings its own keymap, so the keycodes Hyprland reports for it are meaningless
  (the letters of "ls" arrive as codes 9 and 10: "Escape" and "1"). The runner
  therefore tells the event logger exactly what it typed, through the same line
  format the Hyprland bridge uses (`Feed`), at the moment it types it.
* **Pointer travel**: Hyprland's own `hl.dsp.cursor.move`, stepped along an eased,
  slightly curved path. The logger polls the pointer as usual.
* **Clicks**: Hyprland's `hl.dsp.send_shortcut` with a mouse button, sent to the window
  under the pointer (which is focused first, as a real click would). Reported to the
  log by the runner, like the keys. No extra package and no daemon.
* **Windows**: hyprctl (clients, focus, workspace), as the rest of Dolly does.

Nothing here is imported unless a storyboard runs.
"""
from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from . import hypr, keymap
from .logger import hypr_json, hypr_request, runtime_dir
from .storyboard import KEYS, travel_seconds

FEED_NAME = "script-input.jsonl"
_UNSHIFTED = {v: k for k, v in keymap.NAMES.items() if len(v) == 1}
_UNSHIFTED[" "] = 57
_SHIFTED = dict(zip('!@#$%^&*()_+{}:"~|<>?', "1234567890-=[];'`\\,./"))
_MOD_CODE = {"ctrl": 29, "alt": 56, "shift": 42, "super": 125}
_WTYPE_MOD = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "super": "logo"}
_BUTTON = {"left": 272, "right": 273, "middle": 274}


def char_code(ch: str) -> tuple[int, bool]:
    """(evdev keycode, needs shift) for the log, US layout. Characters outside it are
    logged as a plain key press (the log needs to know *that* something was typed and when)."""
    if ch in _UNSHIFTED:
        return _UNSHIFTED[ch], False
    if ch.lower() in _UNSHIFTED and ch.isupper():
        return _UNSHIFTED[ch.lower()], True
    if ch in _SHIFTED:
        return _UNSHIFTED[_SHIFTED[ch]], True
    return 57, False


class Feed:
    """Lines for the event logger (it follows this file while DOLLY_EVENTS_FROM_SCRIPT=1)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else runtime_dir() / FEED_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a", buffering=1)

    def key(self, code: int, down: bool):
        self.f.write(json.dumps({"type": "key", "code": code + 8, "down": down}) + "\n")

    def tap(self, code: int, mods=()):
        held = [_MOD_CODE[m] for m in mods]
        try:
            for m in held:
                self.key(m, True)
            self.key(code, True)
            self.key(code, False)
        finally:
            for m in reversed(held):
                self.key(m, False)          # never leave a modifier "held" in the log

    def click(self, x: float, y: float, button: str = "left"):
        self.f.write(json.dumps({"type": "click", "x": round(x, 2), "y": round(y, 2), "button": button}) + "\n")

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass


def _dispatch(lua: str) -> bool:
    """One Hyprland dispatcher; the socket is used directly (pointer travel sends ~60 a second)."""
    out = hypr_request("/dispatch " + lua, timeout=1.0)
    if out is not None and out.strip() == "ok":
        return True
    try:
        r = subprocess.run(["hyprctl", "dispatch", lua], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "ok"


def capabilities() -> dict:
    """What a storyboard can do on this machine. {"keys", "pointer", "click", "quiet", "problems": [...]}"""
    probs = []
    keys = bool(shutil.which("wtype"))
    if not keys:
        probs.append("typing needs wtype (it ships with Omarchy): omarchy-pkg-add wtype")
    pointer = bool(shutil.which("hyprctl") and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
    if not pointer:
        probs.append("no Hyprland session found (HYPRLAND_INSTANCE_SIGNATURE is not set)")
    quiet = bool(shutil.which("grim"))
    return {"keys": keys, "pointer": pointer, "click": pointer, "quiet": quiet, "problems": probs,
            "click_note": "" if pointer else "click steps need a Hyprland session",
            "quiet_note": "" if quiet else 'wait = "quiet" falls back to a fixed 1.5 s without grim'}


class Desktop:
    """Performs steps on the real desktop. Tests replace this with a fake of the same shape."""

    def hold_key_repeat(self) -> bool:
        from . import hypr
        return hypr.hold_key_repeat()

    def restore_key_repeat(self) -> bool:
        from . import hypr
        return hypr.restore_key_repeat()

    def __init__(self, feed: Feed | None = None, sleep=time.sleep):
        self.feed, self.sleep = feed, sleep

    # ---------------------------------------------------------- keys
    def type_char(self, ch: str):
        code, shift = char_code(ch)
        if self.feed:
            self.feed.tap(code, ("shift",) if shift else ())
        subprocess.run(["wtype", "--", ch], timeout=5, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def press(self, mods: list, key: str):
        """A chord or a named key. `key` is one character or a name from storyboard.KEYS."""
        if key in KEYS:
            sym, code = KEYS[key]
            args = ["-k", sym]
        else:
            code, shift = char_code(key)
            if shift and "shift" not in mods:
                mods = [*mods, "shift"]
            args = ["-k", {" ": "space"}.get(key, key)] if mods else ["--", key]
        cmd = ["wtype"]
        for m in mods:
            cmd += ["-M", _WTYPE_MOD[m]]
        cmd += args
        for m in reversed(mods):
            cmd += ["-m", _WTYPE_MOD[m]]
        if self.feed:
            self.feed.tap(code, tuple(mods))
        subprocess.run(cmd, timeout=5, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ---------------------------------------------------------- pointer
    def pointer(self) -> tuple[float, float]:
        p = hypr_json("cursorpos") or {}
        return float(p.get("x", 0)), float(p.get("y", 0))

    def move_to(self, x: float, y: float, seconds: float | None = None, hz: float = 60.0):
        """Eased travel on a slightly bowed path: no teleport, no straight robot line."""
        x0, y0 = self.pointer()
        dist = math.hypot(x - x0, y - y0)
        if dist < 2:
            return
        seconds = travel_seconds(dist) if seconds is None else seconds
        n = max(2, int(seconds * hz))
        bow = min(40.0, dist * 0.06)                       # perpendicular offset at mid-path
        nx, ny = -(y - y0) / dist, (x - x0) / dist
        t_next = time.monotonic()
        for i in range(1, n + 1):
            u = i / n
            e = u * u * u * (u * (6 * u - 15) + 10)        # smootherstep: zero speed and acceleration at both ends
            b = math.sin(math.pi * e) * bow
            px, py = x0 + (x - x0) * e + nx * b, y0 + (y - y0) * e + ny * b
            _dispatch(f"hl.dsp.cursor.move({{ x = {px:.0f}, y = {py:.0f} }})")
            t_next += seconds / n
            self.sleep(max(0.0, t_next - time.monotonic()))

    def click(self, button: str = "left", address: str | None = None):
        """Click at the pointer: in the window `address`, or else the one under the pointer."""
        x, y = self.pointer()
        under = [self.window(address)] if address and self.window(address) else [c for c in self.clients() if c.get("mapped", True) and not c.get("hidden")
                 and c.get("workspace", {}).get("id") == self.workspace()
                 and c["at"][0] <= x < c["at"][0] + c["size"][0] and c["at"][1] <= y < c["at"][1] + c["size"][1]]
        if not under:
            raise RuntimeError("no window under the pointer to click")
        win = min(under, key=lambda c: (not c.get("floating"), c.get("focusHistoryID", 1 << 30)))
        if self.focused() != win["address"]:
            self.focus(win["address"])
            self.sleep(0.08)
        if self.feed:
            self.feed.click(x, y, button)
        key, to = f"mouse:{_BUTTON[button]}", f"address:{win['address']}"
        if not _dispatch(f'hl.dsp.send_shortcut({{ mods = "", key = "{key}", window = "{to}" }})'):
            raise RuntimeError("Hyprland refused the click")

    # ---------------------------------------------------------- windows
    def clients(self) -> list[dict]:
        return hypr_json("clients") or []

    def find(self, selector: str) -> dict | None:
        """"class:foot", "title:README", "address:0x…": the most recently focused match."""
        kind, _, want = selector.partition(":")
        want_l = want.lower()
        hits = []
        for c in self.clients():
            if kind == "class" and str(c.get("class", "")).lower() == want_l \
                    or kind == "title" and want_l in str(c.get("title", "")).lower() \
                    or kind == "address" and str(c.get("address", "")).lower() == want_l:
                hits.append(c)
        return min(hits, key=lambda c: c.get("focusHistoryID", 1 << 30)) if hits else None

    def window(self, address: str) -> dict | None:
        return next((c for c in self.clients() if c.get("address") == address), None)

    def launch(self, command: str, cwd: str | None = None) -> None:
        """Start a program through Hyprland (as a keybinding would). "terminal" = the user's terminal."""
        if command == "terminal":
            command = terminal_command()
        if cwd:
            command = f"cd {shlex.quote(os.path.expanduser(cwd))} && exec {command}"
        if not _dispatch(f"hl.dsp.exec_cmd({json.dumps(command)})"):
            subprocess.Popen(["sh", "-c", command], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)

    def focus(self, address: str) -> bool:
        ok = hypr.focus(address)
        if ok and (self.window(address) or {}).get("floating"):
            hypr.raise_top(address)             # a floating window may sit under another one
        return ok

    def close(self, address: str) -> bool:
        return hypr.close(address)

    def mark(self) -> None:
        """A "zoom here" mark at the pointer (the same signal the Super+Alt+M hotkey sends)."""
        import signal
        from .capture import read_state
        st = read_state() or {}
        if st.get("logger_pid"):
            os.kill(st["logger_pid"], signal.SIGUSR1)

    # ---------------------------------------------------------- workspaces
    def workspace(self) -> int | None:
        w = hypr_json("activeworkspace") or {}
        return w.get("id")

    def empty_workspace(self) -> int | None:
        """An empty workspace on the monitor in use: one that does not exist yet (Hyprland
        opens it on the focused monitor) or an empty one that is already there. Never one
        that lives on, or is bound to, another monitor: going there would move the take to
        a monitor you are not looking at."""
        mons = hypr_json("monitors") or []
        here = next((m.get("name") for m in mons if m.get("focused")), None)
        taken = set()
        for w in hypr_json("workspaces") or []:
            if w.get("windows") or (here and w.get("monitor") not in (None, "", here)):
                taken.add(w.get("id"))
        for r in hypr_json("workspacerules") or []:
            mon = r.get("monitor")
            if here and mon and mon != here and str(r.get("workspaceString", "")).isdigit():
                taken.add(int(r["workspaceString"]))
        return next((i for i in range(1, 11) if i not in taken), None)

    def go_workspace(self, wid: int) -> bool:
        return _dispatch(f'hl.dsp.focus({{ workspace = "{int(wid)}" }})')

    def monitor(self) -> str | None:
        """The focused monitor's name."""
        mons = hypr_json("monitors") or []
        return next((m.get("name") for m in mons if m.get("focused")), None)

    def go_monitor(self, name: str) -> bool:
        return _dispatch(f"hl.dsp.focus({{ monitor = {json.dumps(str(name))} }})")

    def focused(self) -> str | None:
        return (hypr_json("activewindow") or {}).get("address")

    # ---------------------------------------------------------- screen
    def screen_rect(self) -> tuple:
        """The focused monitor in layout pixels (x, y, w, h)."""
        from .capture import monitor_logical
        mons = hypr_json("monitors") or []
        m = next((m for m in mons if m.get("focused")), mons[0] if mons else None)
        return tuple(monitor_logical(m)[0]) if m else (0, 0, 1920, 1080)

    def screen_sample(self, rect=None):
        """A small grayscale snapshot (numpy array) of the screen or a rect, or None without grim."""
        if not shutil.which("grim"):
            return None
        import numpy as np
        cmd = ["grim", "-t", "ppm", "-s", "0.25"]
        if rect:
            cmd += ["-g", f"{int(rect[0])},{int(rect[1])} {int(rect[2])}x{int(rect[3])}"]
        try:
            raw = subprocess.run([*cmd, "-"], capture_output=True, timeout=5).stdout
            parts = raw.split(b"\n", 3)                      # P6, "W H", maxval, pixels
            w, h = (int(v) for v in parts[1].split())
            return np.frombuffer(parts[3][: w * h * 3], np.uint8).reshape(h, w, 3).mean(axis=2)
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return None


def terminal_command() -> str:
    """The user's terminal, the way Omarchy launches it."""
    if shutil.which("xdg-terminal-exec"):
        return "uwsm-app -- xdg-terminal-exec" if shutil.which("uwsm-app") else "xdg-terminal-exec"
    for t in (os.environ.get("TERMINAL"), "alacritty", "ghostty", "kitty", "foot"):
        if t and shutil.which(t):
            return t
    return "xterm"


def changed_fraction(a, b, thresh: int = 12) -> float:
    """Share of pixels that differ between two snapshots (1.0 when they cannot be compared)."""
    if a is None or b is None or a.shape != b.shape:
        return 1.0
    import numpy as np
    return float((np.abs(a - b) > thresh).mean())

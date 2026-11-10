"""Hyprland window dispatchers that work on both config styles.

Hyprland 0.56 (Omarchy 4.5) takes Lua dispatchers, `hl.dsp.window.pin({ ... })`,
and rejects the old `hyprctl dispatch pin address:0x..` form with a Lua parse
error. Older Hyprland only knows the old form. Each call tries Lua first and
falls back to the legacy dispatcher, remembering which one worked.

Argument shapes were verified on Hyprland 0.56.2 against `hyprctl clients -j`
(floating, pinned, at, size) and `hyprctl getprop` (rounding).
"""
from __future__ import annotations

import json
import subprocess

_MODE: str | None = None        # "lua" | "legacy", learned from the first answer


def _run(args: list[str], timeout: float = 2.0) -> tuple[bool, str]:
    try:
        r = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    out = (r.stdout + r.stderr).strip()
    low = out.lower()
    ok = r.returncode == 0 and "error" not in low and "invalid" not in low and "unknown" not in low
    return ok, out


def _lstr(s: str) -> str:
    return json.dumps(str(s))      # a valid Lua string literal for our inputs (JSON escapes are Lua escapes)


def dispatch(lua: str, legacy: list[str] | None) -> bool:
    """Run one dispatcher: `lua` is the hl.dsp.* expression, `legacy` the old
    `hyprctl dispatch` arguments (None when there is no old equivalent)."""
    global _MODE
    if _MODE != "legacy":
        ok, _ = _run(["dispatch", lua])
        if ok:
            _MODE = "lua"
            return True
    if legacy is not None and _MODE != "lua":
        ok, _ = _run(["dispatch", *legacy])
        if ok:
            _MODE = "legacy"
            return True
    return False


def _w(addr: str) -> str:
    return addr if addr.startswith("address:") else f"address:{addr}"


def float_on(addr: str) -> bool:
    w = _w(addr)
    return dispatch(f"hl.dsp.window.float({{ action = \"on\", window = {_lstr(w)} }})", ["setfloating", w])


def pin_on(addr: str) -> bool:
    w = _w(addr)
    return dispatch(f"hl.dsp.window.pin({{ action = \"on\", window = {_lstr(w)} }})", ["pin", w])


def resize_exact(addr: str, width: int, height: int) -> bool:
    w = _w(addr)
    return dispatch(f"hl.dsp.window.resize({{ x = {int(width)}, y = {int(height)}, window = {_lstr(w)} }})",
                    ["resizewindowpixel", f"exact {int(width)} {int(height)},{w}"])


def move_exact(addr: str, x: int, y: int) -> bool:
    w = _w(addr)
    return dispatch(f"hl.dsp.window.move({{ x = {int(x)}, y = {int(y)}, window = {_lstr(w)} }})",
                    ["movewindowpixel", f"exact {int(x)} {int(y)},{w}"])


def set_prop(addr: str, prop: str, value: str) -> bool:
    w = _w(addr)
    return dispatch(f"hl.dsp.window.set_prop({{ window = {_lstr(w)}, prop = {_lstr(prop)}, value = {_lstr(value)} }})",
                    ["setprop", w, prop, str(value)])


def focus(addr: str) -> bool:
    w = _w(addr)
    return dispatch(f"hl.dsp.focus({{ window = {_lstr(w)} }})", ["focuswindow", w])


def raise_top(addr: str) -> bool:
    """Put a window on top of the stack without focusing it (floating layouts)."""
    w = _w(addr)
    return dispatch(f"hl.dsp.window.alter_zorder({{ mode = \"top\", window = {_lstr(w)} }})",
                    ["alterzorder", f"top,{w}"])


def close(addr: str) -> bool:
    w = _w(addr)
    return dispatch(f"hl.dsp.window.close({{ window = {_lstr(w)} }})", ["closewindow", w])

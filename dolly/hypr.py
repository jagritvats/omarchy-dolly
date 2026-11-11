"""Hyprland window dispatchers that work on both config styles.

Hyprland 0.56 (Omarchy 4.5) takes Lua dispatchers, `hl.dsp.window.pin({ ... })`,
and rejects the old `hyprctl dispatch pin address:0x..` form with a Lua parse
error. Older Hyprland only knows the old form. Each call tries Lua first and
falls back to the legacy dispatcher, remembering which one worked.

Argument shapes were verified on Hyprland 0.56.2 against `hyprctl clients -j`
(floating, pinned, at, size) and `hyprctl getprop` (rounding).
"""
from __future__ import annotations

import re

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


# ---- key repeat (scripted takes switch it off so a stalled machine cannot repeat a key)
HELD_REPEAT_DELAY = 60000      # ms: longer than any stall


def key_repeat_delay() -> int | None:
    ok, out = _run(["getoption", "input:repeat_delay"])
    m = re.search(r"int:\s*(-?\d+)", out) if ok else None
    return int(m.group(1)) if m else None


def set_key_repeat_delay(ms: int) -> bool:
    ms = int(ms)
    ok, out = _run(["eval", f"hl.config({{ input = {{ repeat_delay = {ms} }} }})"])
    if not ok or "error" in out.lower():
        ok, _ = _run(["keyword", "input:repeat_delay", str(ms)])      # Hyprland with the old config format
    return ok


def _repeat_file():
    from .log import state_dir
    return state_dir() / "key-repeat"


def restore_key_repeat() -> bool:
    """Put back the delay a scripted take changed (also heals a take that was killed)."""
    f = _repeat_file()
    try:
        ms = int(f.read_text().strip())
    except (OSError, ValueError):
        return False
    done = set_key_repeat_delay(ms)
    if done:
        f.unlink(missing_ok=True)
    return done


def hold_key_repeat() -> bool:
    """Switch key repeat off until restore_key_repeat(). The old value is kept on disk first."""
    restore_key_repeat()                       # a killed take may have left it held
    old = key_repeat_delay()
    if old is None or old >= HELD_REPEAT_DELAY:
        return False
    f = _repeat_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(old))
    except OSError:
        return False                           # cannot remember the old value: leave it alone
    if set_key_repeat_delay(HELD_REPEAT_DELAY):
        return True
    f.unlink(missing_ok=True)
    return False


# ---- the sync marker must vanish at once
def marker_no_anim() -> bool:
    """No fade for the sync marker layer. A fading marker leaves ghost frames after the
    flash (on a still screen they stayed for two seconds), and the start trim has to cut
    all of them. A runtime rule, set once per Hyprland config load; nothing is written."""
    lua = ('if not _G.__dolly_sync_rule then _G.__dolly_sync_rule = hl.layer_rule({ '
           'name = "dolly-sync-no-anim", match = { namespace = "^dolly-sync$" }, no_anim = true }) end')
    ok, _ = _run(["eval", lua])
    if ok:
        return True
    # Hyprland with the old config format: a keyword rule, once per session
    import os
    from pathlib import Path
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or "x"
    flag = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "dolly" / f"marker-no-anim-{sig[:16]}"
    if flag.exists():
        return True
    ok, _ = _run(["keyword", "layerrule", "noanim, dolly-sync"])
    if ok:
        try:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
        except OSError:
            pass
    return ok

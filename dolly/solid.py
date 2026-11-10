"""Solid windows while recording (Omarchy "Glassy" window transparency).

With Glassy on, every window is 75 % opaque over a blurred wallpaper: a demo
would show see-through windows, and a window recording would show whatever is
behind the window. While recording, Dolly forces each visible window (and each
window opened during the recording) opaque with Hyprland's per-window
`opaque` property, never the user's global mode, and puts every window back
the way it was on stop, cancel, crash recovery and the next start.

Setting: `--solid auto|on|off` / DOLLY_SOLID (default auto = on only while
Glassy is active). The windows it touched are listed in
$XDG_RUNTIME_DIR/dolly/solid.json until restored (tmpfs: gone after a reboot,
and so are the per-window properties).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from . import log as _dlog


def toggles_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "omarchy" / "toggles" / "hypr"


def glassy_active() -> bool:
    """Omarchy's own test (omarchy-hyprland-window-transparency current_mode): a file check."""
    return (toggles_dir() / "window-glassy.lua").exists() and not (toggles_dir() / "window-opaque.lua").exists()


def wanted(mode: str | None = None) -> bool:
    mode = (mode or os.environ.get("DOLLY_SOLID") or "auto").lower()
    if mode in ("on", "1", "true", "yes"):
        return True
    if mode in ("off", "0", "false", "no"):
        return False
    return glassy_active()


def _ledger() -> Path:
    from .logger import runtime_dir
    return runtime_dir() / "solid.json"


def _hyprctl(*args, timeout=2.0) -> str:
    try:
        r = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:
        _dlog.swallowed()
        return ""


def _set(addr: str, value: str) -> bool:
    window = f"address:{addr}"
    try:
        r = subprocess.run(["hyprctl", "dispatch",
                            f'hl.dsp.window.set_prop({{ window = "{window}", prop = "opaque", value = "{value}" }})'],
                           capture_output=True, text=True, timeout=2.0)
    except Exception:
        _dlog.swallowed()
        return False
    out = (r.stdout + r.stderr).lower()
    return r.returncode == 0 and "error" not in out and "invalid" not in out


def _get(addr: str) -> str:
    v = _hyprctl("getprop", f"address:{addr}", "opaque").lower()
    return "true" if v in ("1", "true") else "false"


def _load() -> dict:
    try:
        return json.loads(_ledger().read_text())
    except (OSError, ValueError):
        return {}


def _save(d: dict):
    p = _ledger()
    if not d:
        p.unlink(missing_ok=True)
        return
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(p)


def make_solid(addresses=None) -> int:
    """Force windows opaque (all visible clients by default); remembers prior values."""
    if not shutil.which("hyprctl"):
        return 0
    if addresses is None:
        try:
            clients = json.loads(_hyprctl("-j", "clients") or "[]")
        except ValueError:
            clients = []
        addresses = [c.get("address") for c in clients if c.get("mapped", True)]
    led = _load()
    n = 0
    for a in addresses:
        if not a:
            continue
        a = a if a.startswith("0x") else "0x" + a
        if a in led:
            continue
        prior = _get(a)
        if prior == "true":
            continue            # already opaque by the user's choice: leave it alone
        if _set(a, "true"):
            led[a] = prior
            n += 1
    _save(led)
    return n


def restore() -> int:
    """Put every window Dolly touched back (closed windows are simply skipped)."""
    led = _load()
    n = 0
    for a, prior in led.items():
        if _set(a, "unset") or _set(a, prior):
            n += 1
    _save({})
    return n

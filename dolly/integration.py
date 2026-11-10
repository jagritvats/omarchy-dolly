"""Is Dolly wired into Omarchy (menu rows, hotkeys), and do its hotkeys still win?

Omarchy plugins have no install hooks, so `bin/dolly-setup` adds the menu rows and
keybinds; this reports what it left and what changed since. An Omarchy update can
claim a combination Dolly bound (Omarchy 4.5 took Super+Alt+L), so conflicts are
re-checked live against `hyprctl binds`, not only when setup ran.

`dolly integration --json` feeds the panel's first-run card and `dolly doctor`.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

MOD_BITS = {"SHIFT": 1, "CTRL": 4, "ALT": 8, "SUPER": 64}
BLOCK = re.compile(r"(--|//) >>> dolly.*?(--|//) <<< dolly", re.S)
BIND_LINE = re.compile(r'o\.bind\("([^"]+)",\s*"([^"]*)"')


def menu_file() -> Path:
    return Path.home() / ".config/omarchy/extensions/omarchy-menu.jsonc"


def bindings_file() -> Path:
    return Path.home() / ".config/hypr/bindings.lua"


def _block(p: Path) -> str | None:
    try:
        m = BLOCK.search(p.read_text())
    except OSError:
        return None
    return m.group(0) if m else None


def parse_combo(combo: str) -> tuple[int, str]:
    """"SUPER + ALT + R" -> (modmask, "r")."""
    parts = [x.strip().upper() for x in combo.split("+") if x.strip()]
    mods = sum(MOD_BITS.get(p, 0) for p in parts[:-1])
    return mods, parts[-1].lower() if parts else ""


def dolly_binds() -> list[dict]:
    blk = _block(bindings_file()) or ""
    return [{"combo": c, "description": d} for c, d in BIND_LINE.findall(blk)]


def live_binds(timeout: float = 2.0) -> list[dict] | None:
    try:
        r = subprocess.run(["hyprctl", "binds", "-j"], capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def conflicts(binds: list[dict], live: list[dict] | None) -> list[dict]:
    """Dolly combinations that something else is bound to as well."""
    if not live:
        return []
    out = []
    for b in binds:
        mask, key = parse_combo(b["combo"])
        others = [x.get("description") or x.get("dispatcher") or "?" for x in live
                  if x.get("modmask") == mask and str(x.get("key", "")).lower() == key
                  and "dolly" not in (str(x.get("arg", "")) + str(x.get("description", ""))).lower()]
        if others:
            out.append({"combo": b["combo"], "dolly": b["description"], "others": others})
    return out


def status(check_live: bool = True) -> dict:
    menu = _block(menu_file()) is not None
    binds = dolly_binds()
    clash = conflicts(binds, live_binds()) if check_live and binds else []
    return {"menu": menu, "binds": binds, "conflicts": clash,
            "set_up": menu or bool(binds),
            "files": {"menu": str(menu_file()), "bindings": str(bindings_file())}}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="dolly integration")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    st = status()
    if a.json:
        print(json.dumps(st))
        return 0
    print(f"menu rows: {'yes' if st['menu'] else 'no'} ({st['files']['menu']})")
    print(f"hotkeys:   {len(st['binds'])} ({st['files']['bindings']})")
    for b in st["binds"]:
        print(f"  {b['combo']:<24} {b['description']}")
    for c in st["conflicts"]:
        print(f"  CONFLICT {c['combo']}: also {', '.join(c['others'])}")
    if not st["set_up"]:
        print("not set up: run bin/dolly-setup (or the panel's 'Set up shortcuts & menu')")
    return 0

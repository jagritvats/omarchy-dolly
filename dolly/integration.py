"""Dolly's place in Omarchy: one menu entry, the hotkeys, the `dolly` command.

Omarchy plugins have no install or update hooks, so Dolly keeps four user-level
things itself, each inside its own marked block or link and nothing else:

  menu      ~/.config/omarchy/extensions/omarchy-menu.jsonc   "// >>> dolly" block
  hotkeys   ~/.config/hypr/bindings.lua                        "-- >>> dolly" block
  command   ~/.local/bin/dolly                                 a link to bin/dolly
  desktop   $XDG_DATA_HOME/applications/io.github.jagritvats.dolly.desktop

`apply()` writes them (Set up, a changed shortcut, the first run after an update),
`undo()` removes them, `status()` reports what is there and which hotkeys something
else has claimed since. Hotkeys come from Settings > Shortcuts (`shortcuts.*` in
config.json): each can be changed or switched off; a combination that is already bound
to something else is never taken.

After `omarchy plugin update` nothing of the plugin runs, so `auto_apply()` compares the
installed version with the one Set up was last applied for (state/setup.json) and
refreshes these things by itself: no password, no terminal, and only for someone
who ran Set up and did not undo it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "io.github.jagritvats.dolly"
INTEGRATION_REVISION = 1                 # migrate integration even within a local version

MOD_BITS = {"SHIFT": 1, "CTRL": 4, "ALT": 8, "SUPER": 64}
MOD_ORDER = ("SUPER", "CTRL", "ALT", "SHIFT")
MOD_ALIASES = {"SUPER": "SUPER", "WIN": "SUPER", "MOD": "SUPER", "META": "SUPER", "LOGO": "SUPER",
               "CTRL": "CTRL", "CONTROL": "CTRL", "ALT": "ALT", "SHIFT": "SHIFT"}
OFF_WORDS = ("", "off", "none", "disabled", "-")
BLOCK = re.compile(r"(--|//) >>> dolly.*?(--|//) <<< dolly", re.S)
BLOCK_LINES = re.compile(r"[ \t]*(--|//) >>> dolly.*?(--|//) <<< dolly[^\n]*\n?", re.S)
BIND_LINE = re.compile(r'o\.bind\("([^"]+)",\s*"([^"]*)"')
KEY_OK = re.compile(r"^[A-Za-z0-9_:]{1,32}$")

# id, default combination, the name Super+K shows, the dolly command it runs
SHORTCUTS: list[dict] = [
    {"id": "toggle", "default": "SUPER + ALT + R", "label": "Dolly: record / stop demo", "cmd": "toggle"},
    {"id": "pause", "default": "SUPER + ALT + P", "label": "Dolly: pause / resume recording", "cmd": "pause"},
    {"id": "mark", "default": "SUPER + ALT + M", "label": "Dolly: zoom here (while recording)", "cmd": "mark"},
    {"id": "replay_save", "default": "SUPER + ALT + SHIFT + R", "label": "Dolly: save instant replay",
     "cmd": "replay save --last 30"},
    {"id": "mute", "default": "SUPER + ALT + N", "label": "Dolly: mute / unmute microphone", "cmd": "mute"},
    {"id": "library", "default": "SUPER + ALT + L", "label": "Dolly: recordings library", "cmd": "library --open"},
]
CAMERA_RULE = ('  o.window({ class = "^DollyCam-(S|M|L)$" }, '
               "{ float = true, pin = true, no_initial_focus = true, no_dim = true })")


class ShortcutError(ValueError):
    pass


# ------------------------------------------------------------------ places
def menu_file() -> Path:
    return Path.home() / ".config/omarchy/extensions/omarchy-menu.jsonc"


def bindings_file() -> Path:
    return Path.home() / ".config/hypr/bindings.lua"


def link_file() -> Path:
    return Path.home() / ".local/bin/dolly"


def launcher() -> Path:
    return ROOT / "bin" / "dolly"


def stamp_file() -> Path:
    from .guards import state_dir
    return state_dir() / "setup.json"


def version() -> str:
    try:
        return str(json.loads((ROOT / "manifest.json").read_text())["version"])
    except (OSError, ValueError, KeyError):
        return "?"


def stamp() -> dict:
    try:
        d = json.loads(stamp_file().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_stamp(d: dict):
    from . import fsutil
    try:
        fsutil.atomic_write_json(stamp_file(), d, durable=False)
    except OSError:
        pass


def current_stamp() -> bool:
    st = stamp()
    return st.get("version") == version() and st.get("integration_revision") == INTEGRATION_REVISION


def _block(p: Path) -> str | None:
    try:
        m = BLOCK.search(p.read_text())
    except OSError:
        return None
    return m.group(0) if m else None


# ------------------------------------------------------------------ combinations
def normalize_combo(text) -> str:
    """"super+alt+r" -> "SUPER + ALT + R"; "" / "off" -> "" (switched off).
    ShortcutError says what is wrong in words a person can act on."""
    raw = str(text if text is not None else "").strip()
    if raw.lower() in OFF_WORDS:
        return ""
    parts = [p for p in re.split(r"[+\s]+", raw) if p]
    mods, keys = [], []
    for p in parts:
        m = MOD_ALIASES.get(p.upper())
        if m:
            if m not in mods:
                mods.append(m)
        else:
            keys.append(p)
    if len(keys) != 1:
        raise ShortcutError("a shortcut is modifiers plus one key, like SUPER + ALT + R (empty switches it off)")
    key = keys[0]
    if not KEY_OK.match(key):
        raise ShortcutError(f"{key!r} is not a key name")
    if len(key) == 1:
        key = key.upper()
        if not mods or mods == ["SHIFT"]:
            raise ShortcutError("a single letter or digit needs SUPER, CTRL or ALT with it")
    return " + ".join([m for m in MOD_ORDER if m in mods] + [key])


def parse_combo(combo: str) -> tuple[int, str]:
    """"SUPER + ALT + R" -> (modmask, "r")."""
    parts = [x.strip().upper() for x in combo.split("+") if x.strip()]
    mods = sum(MOD_BITS.get(p, 0) for p in parts[:-1])
    return mods, parts[-1].lower() if parts else ""


def chosen() -> dict[str, str]:
    """shortcut id -> combination from Settings ("" = off). Defaults when config is unreadable."""
    try:
        from . import config
        sec = config.load().get("shortcuts") or {}
    except Exception:
        sec = {}
    out = {}
    for s in SHORTCUTS:
        v = sec.get(s["id"], s["default"])
        try:
            out[s["id"]] = normalize_combo(v)
        except ShortcutError:
            out[s["id"]] = s["default"]
    return out


def duplicates(combos: dict[str, str]) -> list[tuple[str, str, str]]:
    """[(combo, first id, second id)] where two of Dolly's own shortcuts ask for the same keys."""
    seen: dict[tuple[int, str], str] = {}
    out = []
    for sid, c in combos.items():
        if not c:
            continue
        k = parse_combo(c)
        if k in seen:
            out.append((c, seen[k], sid))
        else:
            seen[k] = sid
    return out


# ------------------------------------------------------------------ what is there
def dolly_binds() -> list[dict]:
    blk = _block(bindings_file()) or ""
    return [{"combo": c, "description": d} for c, d in BIND_LINE.findall(blk)]


def live_binds(timeout: float = 2.0) -> list[dict] | None:
    if not shutil.which("hyprctl"):
        return None
    try:
        r = subprocess.run(["hyprctl", "binds", "-j"], capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _others(combo: str, live: list[dict] | None) -> list[str]:
    """What else is bound to this combination right now (never Dolly's own bind)."""
    if not live or not combo:
        return []
    mask, key = parse_combo(combo)
    return [str(x.get("description") or x.get("dispatcher") or "?") for x in live
            if x.get("modmask") == mask and str(x.get("key", "")).lower() == key
            and "dolly" not in (str(x.get("arg", "")) + str(x.get("description", ""))).lower()]


def conflicts(binds: list[dict], live: list[dict] | None) -> list[dict]:
    """Dolly combinations that something else is bound to as well."""
    out = []
    for b in binds:
        others = _others(b["combo"], live)
        if others:
            out.append({"combo": b["combo"], "dolly": b["description"], "others": others})
    return out


def status(check_live: bool = True) -> dict:
    from . import desktop_entry
    menu = _block(menu_file()) is not None
    binds = dolly_binds()
    clash = conflicts(binds, live_binds()) if check_live and binds else []
    desktop = desktop_entry.status(str(launcher()))
    return {"menu": menu, "binds": binds, "conflicts": clash, "desktop": desktop,
            "set_up": menu or bool(binds) or desktop_entry.ours(str(launcher())),
            "files": {"menu": str(menu_file()), "bindings": str(bindings_file()), "desktop": desktop["path"]}}


def shortcut_view(check_live: bool = True) -> list[dict]:
    """One row per shortcut for Settings: what was chosen and what became of it.
    state: active | off | taken (something else has those keys; Dolly left them alone) |
           pending (Set up has not been run, so no hotkey is written yet)."""
    combos = chosen()
    written = {(parse_combo(b["combo"]), b["description"]) for b in dolly_binds()}
    set_up = _block(bindings_file()) is not None
    live = live_binds() if check_live else None
    rows = []
    for s in SHORTCUTS:
        c = combos[s["id"]]
        row = {"id": s["id"], "key": f"shortcuts.{s['id']}", "label": s["label"], "combo": c,
               "default": s["default"], "others": []}
        if not c:
            row["state"] = "off"
        elif (parse_combo(c), s["label"]) in written:
            row["others"] = _others(c, live)
            row["state"] = "active"
        elif not set_up:
            row["state"] = "pending"
        else:
            row["others"] = _others(c, live)
            row["state"] = "taken" if row["others"] else "pending"
        rows.append(row)
    return rows


# ------------------------------------------------------------------ the menu block
def menu_rows(dolly: str) -> list[tuple[str, dict]]:
    """One identifiable Dolly Screen Recorder submenu under Trigger > Capture.
    While a take runs, stopping stays one step. Guards are pure shell file tests."""
    from .guards import GUARDS as G
    rows = [
        ("trigger.capture.dolly-stop", {"icon": "󰓛", "label": "Stop Dolly Screen Recorder", "description": "Dolly: stop recording and open the editor",
                                        "aliases": ["stop dolly", "stop screen recorder", "stop screencast", "stop demo recording", "dolly stop"], "when": G["recording"],
                                        "action": f"{dolly} stop"}),
        ("trigger.capture.dolly", {"icon": "󰑊", "label": "Dolly Screen Recorder", "description": "Dolly: record with auto-zoom, edit, share",
                                   "aliases": ["dolly", "screen recorder", "screencast", "demo", "record demo"], "when": G["not_recording"]}),
        ("trigger.capture.dolly.window", {"icon": "󰖯", "label": "Record a window",
                                          "when": G["not_recording"],
                                          "action": f"{dolly} record --mode window"}),
        ("trigger.capture.dolly.region", {"icon": "󰆞", "label": "Record a region",
                                          "when": G["not_recording"],
                                          "action": f"{dolly} record --mode region"}),
        ("trigger.capture.dolly.screen", {"icon": "󰍹", "label": "Record the screen",
                                          "when": G["not_recording"],
                                          "action": f"{dolly} record --mode screen"}),
        ("trigger.capture.dolly.screen-mic", {"icon": "󰍬", "label": "Record with mic", "description": "the screen and your microphone",
                                              "when": G["not_recording"],
                                              "action": f"{dolly} record --mode screen --audio mic"}),
        ("trigger.capture.dolly.last", {"icon": "󰕧", "label": "Last recording", "description": "open it in the Dolly editor",
                                        "when": G["last"], "action": f"{dolly} edit"}),
        ("trigger.capture.dolly.library", {"icon": "󰕧", "label": "Recordings",
                                           "description": "Library: find, rename, share, clean up",
                                           "action": f"{dolly} library --open"}),
        ("trigger.capture.dolly.share", {"icon": "󰒖", "label": "Share last",
                                         "description": "Dolly share presets", "when": G["last"]}),
        ("trigger.capture.dolly.share.github", {"icon": "󰊤", "label": "GitHub (10 MB)", "description": "export for GitHub and copy the file",
                                                "action": f"{dolly} share export github --copy"}),
        ("trigger.capture.dolly.share.discord", {"icon": "󰙯", "label": "Discord (20 MB)", "description": "export for Discord and copy the file",
                                                 "action": f"{dolly} share export discord --copy"}),
        ("trigger.capture.dolly.share.slack", {"icon": "󰒱", "label": "Slack", "description": "export for Slack and copy the file",
                                               "action": f"{dolly} share export slack --copy"}),
        ("trigger.capture.dolly.share.shorts", {"icon": "󰄄", "label": "Vertical 9:16", "description": "export 9:16 and copy the file",
                                                "action": f"{dolly} share export shorts --copy"}),
        ("trigger.capture.dolly.share.link", {"icon": "󰌷", "label": "GitHub + link", "description": "export for GitHub, upload and copy the link",
                                              "when": G["share_ready"],
                                              "action": f"{dolly} share export github --upload"}),
        ("trigger.capture.dolly.replay-on", {"icon": "󰑓", "label": "Instant replay on",
                                             "description": "keep the last 60 s in memory", "when": G["not_replay"],
                                             "action": f"{dolly} replay start"}),
        ("trigger.capture.dolly.replay-save", {"icon": "󰑓", "label": "Save last 30 s", "description": "from the instant replay buffer",
                                               "when": G["replay"], "action": f"{dolly} replay save --last 30"}),
        ("trigger.capture.dolly.replay-off", {"icon": "󰑓", "label": "Replay off", "when": G["replay"],
                                              "action": f"{dolly} replay stop"}),
        ("trigger.capture.dolly.settings", {"icon": "󰒓", "label": "Settings", "action": f"{dolly} settings"}),
    ]
    # a row whose plugin was removed (`omarchy plugin remove` runs nothing) must not show up
    present = f"[ -x {json.dumps(dolly)[1:-1]} ]"
    return [(k, {**v, "when": f"{present} && {v['when']}" if v.get("when") else present}) for k, v in rows]


def menu_block(dolly: str) -> str:
    # whole-line comments only (Omarchy's JSONC parser strips only full-line //)
    block = "  // >>> dolly (managed by dolly-setup; remove with dolly-setup --undo)\n"
    block += "".join(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)},\n" for k, v in menu_rows(dolly))
    return block + "  // <<< dolly\n"


def _menu_with(text: str, block: str | None) -> str:
    """The menu file with Dolly's block replaced, added before the closing brace, or
    (block None) removed. Every other line stays as it is."""
    if BLOCK.search(text):
        return BLOCK_LINES.sub(lambda _m: block or "", text, count=1)
    if block is None:
        return text
    s = text.rstrip()
    if not s:
        s = "{\n}"
    if not s.endswith("}"):
        raise ValueError("unexpected menu file format")
    body = s[:-1].rstrip()
    if body and not body.endswith("{") and not body.endswith(","):
        lines = body.split("\n")                       # the previous last entry lacks a trailing comma
        for i in range(len(lines) - 1, -1, -1):
            t = lines[i].strip()
            if t and not t.startswith("//"):
                if not t.endswith(",") and not t.endswith("{"):
                    lines[i] = lines[i] + ","
                break
        body = "\n".join(lines)
    return body + "\n" + block + "}\n"


# ------------------------------------------------------------------ the hotkey block
def binds_block(dolly: str, combos: dict[str, str], live: list[dict] | None) -> tuple[str, list[dict]]:
    """(the Lua block, one report row per shortcut: applied | off | taken)."""
    lines = ["-- >>> dolly (managed by dolly-setup)",
             # only while the plugin is installed: a removed plugin leaves no live hotkeys behind
             f'local dolly_present = io.open("{dolly}", "r")',
             "if dolly_present then dolly_present:close()"]
    report = []
    for s in SHORTCUTS:
        c = combos.get(s["id"], s["default"])
        if not c:
            report.append({"id": s["id"], "combo": "", "label": s["label"], "state": "off", "others": []})
            continue
        others = _others(c, live)
        if others:                                     # never take keys that belong to something else
            report.append({"id": s["id"], "combo": c, "label": s["label"], "state": "taken", "others": others})
            continue
        lines.append(f'  o.bind("{c}", "{s["label"]}", "{dolly} {s["cmd"]}")')
        report.append({"id": s["id"], "combo": c, "label": s["label"], "state": "applied", "others": []})
    # the live camera bubble (Dolly's own app id): float, pin, never take focus
    lines += [CAMERA_RULE, "end", "-- <<< dolly"]
    return "\n".join(lines) + "\n", report


def _binds_with(text: str, block: str | None) -> str:
    if BLOCK.search(text):
        return BLOCK_LINES.sub(lambda _m: block or "", text, count=1)
    if block is None:
        return text
    return text + ("" if not text or text.endswith("\n") else "\n") + block


# ------------------------------------------------------------------ writing
def _replace(p: Path, new: str) -> bool:
    """Write `new` if it differs (a `.dolly-bak` copy of the old file first). -> changed"""
    try:
        old = p.read_text()
    except OSError:
        old = None
    if old == new:
        return False
    from . import fsutil
    if old is not None:
        try:
            shutil.copyfile(p, p.with_name(p.name + ".dolly-bak"))
        except OSError:
            pass
    fsutil.atomic_write_text(p, new, durable=False)
    return True


def _recording() -> bool:
    """A take (or its countdown) is running: Hyprland must not be reloaded under it."""
    try:
        from .capture import read_state
        return read_state() is not None
    except Exception:
        return False


def _pending_reload() -> Path:
    from .guards import state_dir
    return state_dir() / "reload-pending"


def _reload_hyprland() -> bool:
    """Hyprland re-reads its config so the hotkeys take effect; put off while recording."""
    if not shutil.which("hyprctl") or os.environ.get("DOLLY_NO_HYPR_RELOAD") == "1":
        return False
    if _recording():
        try:
            _pending_reload().write_text("1\n")
        except OSError:
            pass
        return False
    try:
        subprocess.run(["hyprctl", "reload"], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    _pending_reload().unlink(missing_ok=True)
    return True


def reload_if_pending() -> bool:
    return _pending_reload().exists() and _reload_hyprland()


def _apply_link(dolly: str) -> dict:
    """`dolly` as a command: a link in ~/.local/bin. Never replaces a file or a link that is not Dolly's."""
    link = link_file()
    target = os.readlink(link) if link.is_symlink() else ""
    if target == dolly:
        return {"state": "unchanged", "path": str(link)}
    foreign = (link.exists() or link.is_symlink()) and not re.search(r"/plugins/[^/]*dolly[^/]*/bin/dolly$", target)
    if foreign:
        return {"state": "foreign", "path": str(link)}
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        tmp = link.with_name(f".dolly.{os.getpid()}.tmp")
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(dolly)
        tmp.replace(link)
    except OSError:
        return {"state": "failed", "path": str(link)}
    return {"state": "linked", "path": str(link)}


def apply(parts: tuple[str, ...] = ("menu", "binds", "link", "desktop"), reload: bool = True) -> dict:
    """Write Dolly's menu, hotkeys, command link and application entry. Idempotent;
    touches only Dolly's own blocks, link and managed desktop file."""
    dolly = str(launcher())
    res: dict = {"version": version(), "previous": stamp().get("version"), "changed": []}
    if "menu" in parts:
        mf = menu_file()
        try:
            mf.parent.mkdir(parents=True, exist_ok=True)
            old = mf.read_text() if mf.exists() else "{\n}\n"
            res["menu"] = "written" if _replace(mf, _menu_with(old, menu_block(dolly))) else "unchanged"
        except (OSError, ValueError) as e:
            res["menu"] = f"failed: {e}"
        if res["menu"] == "written":
            res["changed"].append("menu")
    if "binds" in parts:
        bf = bindings_file()
        if bf.is_file():
            block, report = binds_block(dolly, chosen(), live_binds())
            try:
                wrote = _replace(bf, _binds_with(bf.read_text(), block))
            except OSError as e:
                wrote, res["binds_error"] = False, str(e)
            res["binds"] = report
            res["binds_file"] = "written" if wrote else "unchanged"
            if wrote:
                res["changed"].append("binds")
                res["reloaded"] = _reload_hyprland() if reload else False
        else:
            res["binds"], res["binds_file"] = [], "missing"
    if "link" in parts:
        res["link"] = _apply_link(dolly)
        if res["link"]["state"] == "linked":
            res["changed"].append("link")
    if "desktop" in parts:
        from . import desktop_entry
        res["desktop"] = desktop_entry.apply(dolly)
        if res["desktop"]["state"] == "written":
            res["changed"].append("desktop")
    revision = INTEGRATION_REVISION if "desktop" in parts else stamp().get("integration_revision")
    _write_stamp({"version": version(), "integration_revision": revision,
                  "at": time.time(), "set_up": True, "changed": res["changed"],
                  "summary": summary(res)})
    return res


def undo() -> dict:
    """Remove Dolly's blocks, command link and desktop entry (only if ours)."""
    res: dict = {"changed": []}
    for name, f, cut in (("menu", menu_file(), _menu_with), ("binds", bindings_file(), _binds_with)):
        try:
            if f.is_file() and _replace(f, cut(f.read_text(), None)):
                res["changed"].append(name)
        except (OSError, ValueError):
            pass
    if "binds" in res["changed"]:
        res["reloaded"] = _reload_hyprland()
    link = link_file()
    if link.is_symlink() and os.readlink(link) == str(launcher()):
        link.unlink(missing_ok=True)
        res["changed"].append("link")
    from . import desktop_entry
    if desktop_entry.undo(str(launcher())):
        res["changed"].append("desktop")
    _write_stamp({"version": version(), "integration_revision": INTEGRATION_REVISION,
                  "at": time.time(), "set_up": False})
    return res


def _ours() -> bool | None:
    """Do the menu entry and hotkeys on this machine belong to THIS copy of Dolly?
    None: there are none (never set up, or undone). False: they point at another copy,
    e.g. the installed plugin while this is a source checkout. Only Set up itself
    (`apply()`) may point them somewhere new; nothing automatic ever does."""
    blocks = [b for b in (_block(menu_file()), _block(bindings_file())) if b]
    if not blocks:
        from . import desktop_entry
        if desktop_entry.managed():
            return desktop_entry.ours(str(launcher()))
        return None
    mine = str(launcher())
    return any(mine in b for b in blocks)


def apply_binds_if_set_up() -> dict | None:
    """A shortcut changed in Settings: rewrite the hotkey block, if Set up wrote one for this copy."""
    if not _ours():
        return None
    return apply(parts=("binds",))


def auto_apply() -> dict | None:
    """The first run after an update: refresh what Set up wrote before. None = nothing to
    do (same version, never set up, or undone). Safe to call from several processes."""
    cur = version()
    st = stamp()
    if current_stamp():
        reload_if_pending()
        return None
    ours = _ours()
    if ours is None:
        _write_stamp({"version": cur, "integration_revision": INTEGRATION_REVISION,
                      "at": time.time(), "set_up": False})     # ask the files only once per version/revision
        return None
    if not ours:
        return None                                    # another copy's entries: not this one's to refresh
    if _recording():
        return None                                    # retry after the take, without touching config
    import fcntl
    lock = stamp_file().with_name("setup.lock")
    try:
        with open(lock, "w") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return None                                    # another Dolly process is doing it
            if current_stamp():
                return None
            parts = tuple(n for n, f in (("menu", menu_file()), ("binds", bindings_file())) if _block(f) is not None)
            before = st.get("at")
            res = apply(parts=parts + ("link", "desktop"))
            res["ui_stale"] = ui_stale(before)
            return res
    except OSError:
        return None


def _proc_start(pid: int) -> float | None:
    """When a process started, in epoch seconds (None if it cannot be read)."""
    try:
        ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        btime = next(int(ln.split()[1]) for ln in Path("/proc/stat").read_text().splitlines()
                     if ln.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _shell_pid() -> int | None:
    try:
        r = subprocess.run(["pgrep", "-o", "-x", "quickshell|qs"], capture_output=True, text=True, timeout=2)
        return int(r.stdout.split()[0])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def ui_stale(since: float | None = None) -> bool:
    """The bar button's files changed after the running shell started (and after `since`).
    Quickshell keeps the QML it has loaded, and Omarchy's plugin reload cannot make it let
    go (Qt.clearComponentCache is not there), so a running shell goes on showing the old
    button and panel until it restarts. Everything else of an update works at once."""
    pid = _shell_pid()
    started = _proc_start(pid) if pid else None
    if started is None:
        return False
    try:
        newest = max((ROOT / n).stat().st_mtime for n in ("BarWidget.qml", "RecordPanel.qml"))
    except OSError:
        return False
    return newest > max(started, float(since or 0)) + 1.0


def summary(res: dict | None) -> str:
    """One line for a person: what apply() changed."""
    if not res:
        return "nothing to refresh"
    names = {"menu": "the menu entry", "link": "the dolly command", "desktop": "the application launcher"}
    bits = [names[c] for c in res.get("changed", []) if c in names]
    if "binds" in res.get("changed", []):
        n = sum(1 for b in res.get("binds") or [] if b["state"] == "applied")
        bits.insert(1 if bits[:1] == ["the menu entry"] else 0, f"{n} hotkey{'s' if n != 1 else ''}")
    if not bits:
        return "menu entry, hotkeys, command and application launcher were already current"
    return "refreshed " + (", ".join(bits[:-1]) + " and " + bits[-1] if len(bits) > 1 else bits[0])


# ------------------------------------------------------------------ command line
def _print_apply(res: dict):
    """The lines bin/dolly-setup shows."""
    m = res.get("menu")
    if m:
        print(f"menu: one \"Dolly Screen Recorder\" entry under Trigger > Capture ({m}; {menu_file()})")
    if res.get("binds_file") == "missing":
        print(f"keybind: {bindings_file()} not found; skipped (bind '{launcher()} toggle' yourself)")
    for b in res.get("binds") or []:
        keys = b["combo"].replace(" + ", "+")
        if b["state"] == "applied":
            print(f"keybind: {keys} -> {b['label']}")
        elif b["state"] == "off":
            print(f"keybind: off ({b['label']}; Settings > Shortcuts)")
        else:
            print(f"keybind: {keys} is already bound, skipped ({b['label']}; used by {', '.join(b['others'])})")
    if res.get("binds"):
        print("keybind: window rule for the live camera bubble (DollyCam)")
    desktop = res.get("desktop")
    if desktop:
        print(f"application: {desktop['state']} ({desktop['path']})")
        if desktop["state"] == "foreign":
            print("application: existing file is not Dolly's; left alone")
    ln = res.get("link")
    if ln:
        if ln["state"] in ("linked", "unchanged"):
            print(f"command: dolly is on your PATH ({ln['path']})")
        elif ln["state"] == "foreign":
            print(f"command: {ln['path']} exists and is not Dolly's; left alone (use {launcher()})")
        else:
            print(f"command: could not link {ln['path']} (use {launcher()})")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="dolly integration")
    ap.add_argument("action", nargs="?", choices=["status", "apply", "undo", "auto"], default="status")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.action == "apply":
        res = apply()
        print(json.dumps(res)) if a.json else _print_apply(res)
        return 0
    if a.action == "auto":
        res = auto_apply()
        print(json.dumps(res)) if a.json else print(summary(res))
        return 0
    if a.action == "undo":
        res = undo()
        if a.json:
            print(json.dumps(res))
            return 0
        print(f"menu: removed Dolly's entry from {menu_file()}" if "menu" in res["changed"]
              else f"menu: no Dolly entry in {menu_file()}")
        print(f"keybind: removed from {bindings_file()}" if "binds" in res["changed"]
              else f"keybind: no Dolly hotkeys in {bindings_file()}")
        if "link" in res["changed"]:
            print(f"command: removed {link_file()}")
        if "desktop" in res["changed"]:
            from . import desktop_entry
            print(f"application: removed {desktop_entry.path()}")
        return 0
    st = status()
    if a.json:
        print(json.dumps(st))
        return 0
    print(f"menu entry: {'yes' if st['menu'] else 'no'} ({st['files']['menu']})")
    print(f"hotkeys:    {len(st['binds'])} ({st['files']['bindings']})")
    print(f"application: {st['desktop']['state']} ({st['files']['desktop']})")
    for b in st["binds"]:
        print(f"  {b['combo']:<24} {b['description']}")
    for c in st["conflicts"]:
        print(f"  CONFLICT {c['combo']}: also {', '.join(c['others'])}")
    if not st["set_up"]:
        print("not set up: open the Dolly panel and click Set up (or run bin/dolly-setup)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Recording project on disk, style defaults, Omarchy theme lookup.

A project is a directory::

    ~/Videos/Dolly/2026-11-08_18-40-12/
        raw.mp4          capture (CFR, cursor hidden when the backend allows)
        events.raw.jsonl logger output (logical coordinates, monotonic time)
        events.json      normalized timeline (video px, seconds) - see events.py
        project.json     segments + style + trim, written by `dolly auto` / editor
        exports/         rendered MP4 / GIF
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from . import fsutil
from . import log as _dlog

DEFAULT_STYLE = {
    "output": {"width": 1920, "height": 1080, "fps": 60, "format": "mp4", "quality": "high"},
    "background": {"type": "gradient", "preset": "theme-glow"},
    "padding": 0.06,           # fraction of the video width kept as margin
    "radius": 14,              # corner radius in output px at 1x
    "shadow": 0.55,            # 0..1
    "cursor": {
        "enabled": True,       # draw a re-rendered cursor (auto-off when baked in capture)
        "size": 1.5,           # x the native cursor size
        "smoothing": 0.55,     # 0..1
        "motion_blur": False,
        "ripple": True,
        "hide_idle": 0,        # seconds of stillness before fading out (0 = never)
    },
    "keys": {"enabled": True, "position": "bottom"},
    "webcam": {"enabled": True, "corner": "bottom-left", "size": 0.22, "shape": "circle", "auto_move": True,
               "mirror": True, "border": 3, "shadow": True, "pos": None, "full": []},
    "audio": {"denoise": True, "normalize": True, "duck": True, "rnnoise_model": None,
              "mic": {"volume": 1.0, "mute": False}, "system": {"volume": 1.0, "mute": False}},
    "speed": {"enabled": True},
    "spotlight": {"enabled": False, "strength": 0.5},
    "privacy": {"enabled": True},
    "captions": {"enabled": True},
    "title": {"enabled": False, "text": "", "subtitle": "", "intro": 2.2, "outro": 2.0, "outro_text": "",
              "outro_subtitle": ""},
    "sync_offset": 0.0,        # seconds added to event times (fix capture start skew)
}

TOKYO_NIGHT = {
    "accent": "#7aa2f7", "background": "#1a1b26", "dark_background": "#13141c",
    "darker_background": "#0e0e14", "lighter_background": "#24283b", "foreground": "#a9b1d6",
    "bright_foreground": "#c0caf5", "muted": "#414868", "red": "#f7768e", "green": "#9ece6a",
    "yellow": "#e0af68", "cyan": "#449dab", "blue": "#7aa2f7", "magenta": "#ad8ee6",
    "bright_magenta": "#bb9af7", "bright_cyan": "#0db9d7", "orange": "#eb927b", "mode": "dark",
}


def deep_merge(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- theme
def omarchy_theme_dir() -> Path:
    return Path(os.environ.get("DOLLY_THEME_DIR",
                               Path.home() / ".local/state/omarchy/current/theme"))


def theme_colors() -> dict:
    """Colors of the active Omarchy theme (colors.toml), Tokyo Night fallback."""
    colors = dict(TOKYO_NIGHT)
    f = omarchy_theme_dir() / "colors.toml"
    try:
        for line in f.read_text().splitlines():
            m = re.match(r'\s*([a-z_]+)\s*=\s*"([^"]+)"', line)
            if m:
                colors[m.group(1)] = m.group(2)
    except OSError:
        pass
    return colors


def theme_mode(colors: dict | None = None) -> str:
    """"light" or "dark": colors.toml `mode`, or a `light.mode` marker in the theme."""
    c = colors or theme_colors()
    if (omarchy_theme_dir() / "light.mode").exists():
        return "light"
    return "light" if str(c.get("mode", "dark")).lower() == "light" else "dark"


def theme_font() -> str:
    """The Omarchy monospace font (`omarchy font current` = fontconfig's monospace)."""
    if os.environ.get("DOLLY_FONT"):
        return os.environ["DOLLY_FONT"]
    import shutil
    import subprocess
    if shutil.which("fc-match"):
        try:
            out = subprocess.run(["fc-match", "monospace", "-f", "%{family}\n"], capture_output=True, text=True,
                                 timeout=2).stdout
            fam = out.splitlines()[0].split(",")[0].strip() if out.strip() else ""
            if fam:
                return fam
        except Exception:
            _dlog.swallowed()
            pass
    return "JetBrainsMono Nerd Font"


def theme_snapshot() -> dict:
    c = theme_colors()
    return {"name": theme_name(), "colors": c, "mode": theme_mode(c), "font": theme_font()}


def style_colors(style: dict | None) -> dict:
    """Colours a render uses: the theme pinned in the project (stable re-exports),
    else the current Omarchy theme (old projects)."""
    th = (style or {}).get("theme") or {}
    if isinstance(th.get("colors"), dict) and th["colors"].get("background"):
        return {**TOKYO_NIGHT, **th["colors"]}
    return theme_colors()


def theme_name() -> str:
    for f in (omarchy_theme_dir().parent / "theme.name", Path.home() / ".local/state/omarchy/current/theme.name"):
        try:
            name = f.read_text().strip()
            if name:
                return name
        except OSError:
            pass
    return "tokyo-night (built-in)"


def wallpapers() -> list[Path]:
    """Current wallpaper first, then the theme's other backgrounds."""
    out: list[Path] = []
    cur = Path.home() / ".local/state/omarchy/current/background"
    if cur.exists():
        out.append(cur.resolve())
    for d in (omarchy_theme_dir() / "backgrounds",
              Path.home() / ".config/omarchy/backgrounds" / theme_name()):
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and p.resolve() not in out:
                    out.append(p.resolve())
    extra = os.environ.get("DOLLY_WALLPAPERS")
    if extra:
        for p in extra.split(":"):
            if p and Path(p).exists():
                out.append(Path(p))
    return out[:12]


def gradient_presets(colors: dict | None = None) -> dict:
    c = colors or theme_colors()
    return {
        "theme-glow": {"label": "Glow", "stops": [c["accent"], c.get("bright_magenta", c["magenta"]), c["red"]], "angle": 135},
        "theme-dusk": {"label": "Dusk", "stops": [c["darker_background"], c["lighter_background"], c["muted"]], "angle": 160},
        "theme-aurora": {"label": "Aurora", "stops": [c["green"], c.get("bright_cyan", c["cyan"]), c["accent"]], "angle": 125},
        "theme-ember": {"label": "Ember", "stops": [c["red"], c.get("orange", c["yellow"]), c["yellow"]], "angle": 140},
        "theme-ink": {"label": "Ink", "stops": [c["background"], c["background"]], "angle": 90},
    }


# -------------------------------------------------------------- projects
def videos_root() -> Path:
    """Where recordings go, Omarchy's order: DOLLY_DIR, Settings > Storage folder,
    $OMARCHY_SCREENRECORD_DIR/Dolly (Omarchy's recorder setting), XDG_VIDEOS_DIR/Dolly,
    ~/Videos/Dolly."""
    if os.environ.get("DOLLY_DIR"):
        return Path(os.environ["DOLLY_DIR"])
    try:
        from .config import load
        folder = load()["storage"]["folder"]
        if folder:
            return Path(os.path.expanduser(folder))
    except Exception:
        _dlog.swallowed()
        pass
    if os.environ.get("OMARCHY_SCREENRECORD_DIR"):
        return Path(os.path.expanduser(os.environ["OMARCHY_SCREENRECORD_DIR"])) / "Dolly"
    vids = None
    if os.environ.get("XDG_VIDEOS_DIR"):
        vids = Path(os.path.expanduser(os.environ["XDG_VIDEOS_DIR"]))
    ud = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    if vids is None:
        try:
            m = re.search(r'^XDG_VIDEOS_DIR="([^"]+)"', ud.read_text(), re.M)
            if m:
                vids = Path(m.group(1).replace("$HOME", str(Path.home())))
        except OSError:
            pass
    return (vids or Path.home() / "Videos") / "Dolly"


def latest_project() -> Path | None:
    root = videos_root()
    if not root.is_dir():
        return None
    cands = [p for p in root.iterdir() if usable(p) and Project(p).raw.exists() and (p / "events.json").exists()]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


class EmptyTrim(ValueError):
    """The selected trim leaves no video to export. The message is written for the person
    at the keyboard and is shown as it stands (no error type in front of it)."""


class UnsafeFolder(ValueError):
    """A recording folder with symbolic links inside. Dolly writes into a recording folder by
    name (a thumbnail, the share history, a log, exports) and moves its raw files to the
    Trash; in a folder someone else made, a link under one of those names points that write
    or that move at a file somewhere else in the home folder."""


def links_in(path: str | Path, depth: int = 3) -> list[str]:
    """Symbolic links inside a recording folder (not the folder itself: a library or a
    recording kept on another disk through a link is the user's own doing)."""
    top, found = str(path), []

    def walk(d: str, left: int):
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_symlink():
                        found.append(os.path.relpath(e.path, top))
                    elif left and e.is_dir(follow_symlinks=False):
                        walk(e.path, left - 1)
        except OSError:
            pass

    walk(top, depth)
    return sorted(found)


def usable(path: str | Path) -> bool:
    return not links_in(path)


class Project:
    def __init__(self, path: str | Path):
        self.dir = Path(path)
        found = links_in(self.dir)
        if found:
            shown = ", ".join(found[:3]) + (f" and {len(found) - 3} more" if len(found) > 3 else "")
            raise UnsafeFolder(f"{self.dir} contains symbolic links ({shown}). Dolly does not open a recording "
                               f"folder with links in it: remove them first (find {self.dir} -type l)")

    @property
    def raw(self) -> Path:
        """raw.mkv (crash-safe capture container, round 3) or raw.mp4."""
        mkv = self.dir / "raw.mkv"
        return mkv if mkv.exists() else self.dir / "raw.mp4"

    events = property(lambda s: s.dir / "events.json")
    raw_events = property(lambda s: s.dir / "events.raw.jsonl")
    file = property(lambda s: s.dir / "project.json")
    exports = property(lambda s: s.dir / "exports")
    name = property(lambda s: s.dir.name)

    def load(self) -> dict:
        data = {}
        if self.file.exists():
            data = fsutil.read_json(self.file)      # damaged: previous version from .bak, else DamagedFile
        data["style"] = deep_merge(DEFAULT_STYLE, data.get("style"))
        data.setdefault("segments", None)
        data.setdefault("trim", None)
        data.setdefault("zoom_config", {})
        data.setdefault("speed", None)       # speed ramps [{start,end,speed,...}] (None = not detected yet)
        data.setdefault("privacy", [])       # blur boxes
        return data

    def save(self, data: dict) -> None:
        # fsync + rename (never half a file), keeping the previous good version as project.json.bak
        fsutil.atomic_write_json(self.file, data, indent=2, backup=True)

"""Dolly settings: `~/.config/dolly/config.json` (schema-versioned, validated,
written atomically).

One source of truth. The bar widget's settings in Omarchy's shell.json
(`manifest.json` barWidget.schema) duplicate a few capture keys so they can be
changed from Omarchy's widget settings too; `BAR_KEYS` maps them, and the
widget mirrors both ways (a change in either place is written to the other
with `dolly config set`/`updateEntryInline`, only when the values differ).

Every setting is honoured somewhere (`dolly/config.py` `apply_*` helpers and
their callers); tests/test_settings.py checks one per group.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

VERSION = 1

# (key, type, default, extra)   types: bool, int, float, enum, str, color
SCHEMA: list[dict] = [
    # ---- Capture
    {"key": "capture.mode", "type": "enum", "default": "window", "options": ["screen", "window", "region"]},
    {"key": "capture.countdown", "type": "int", "default": 3, "min": 0, "max": 10},
    {"key": "capture.fps", "type": "enum", "default": "60", "options": ["30", "60"]},
    {"key": "capture.keys", "type": "bool", "default": True},
    {"key": "capture.replay_seconds", "type": "int", "default": 60, "min": 10, "max": 600},
    # ---- Look
    {"key": "look.follow_theme", "type": "bool", "default": True},
    {"key": "look.background", "type": "enum", "default": "theme-glow",
     "options": ["theme-glow", "theme-dusk", "theme-aurora", "theme-ember", "theme-ink", "solid", "wallpaper",
                 "image"]},
    {"key": "look.background_color", "type": "color", "default": "#1e1e2e"},
    # your own picture behind the recording (look.background = image); checked by dolly.images
    {"key": "look.background_image", "type": "image", "default": ""},
    {"key": "look.background_blur", "type": "float", "default": 0.0, "min": 0.0, "max": 40.0},
    {"key": "look.padding", "type": "float", "default": 0.06, "min": 0.0, "max": 0.2},
    {"key": "look.radius", "type": "int", "default": 14, "min": 0, "max": 40},
    {"key": "look.shadow", "type": "float", "default": 0.55, "min": 0.0, "max": 1.0},
    {"key": "look.cursor_size", "type": "float", "default": 1.5, "min": 0.5, "max": 3.0},
    {"key": "look.cursor_smoothing", "type": "float", "default": 0.55, "min": 0.0, "max": 1.0},
    {"key": "look.click_effect", "type": "enum", "default": "ripple", "options": ["ripple", "none"]},
    {"key": "look.keys_overlay", "type": "bool", "default": True},
    {"key": "look.keys_position", "type": "enum", "default": "bottom", "options": ["bottom", "top"]},
    # ---- Audio (separate tracks; none until the user picks, then remembered)
    {"key": "audio.mode", "type": "enum", "default": "none", "options": ["none", "mic", "system", "both"]},
    {"key": "audio.mic_device", "type": "str", "default": "default", "max_len": 200},
    {"key": "audio.system_device", "type": "str", "default": "default", "max_len": 200},
    {"key": "audio.denoise", "type": "bool", "default": True},
    {"key": "audio.duck", "type": "bool", "default": True},
    {"key": "audio.normalize", "type": "bool", "default": True},
    # ---- Camera (bubble; off by default)
    {"key": "camera.mode", "type": "enum", "default": "off", "options": ["off", "edit", "live"]},
    {"key": "camera.device", "type": "str", "default": "", "max_len": 200},
    {"key": "camera.shape", "type": "enum", "default": "circle", "options": ["circle", "rounded", "rect"]},
    {"key": "camera.size", "type": "enum", "default": "M", "options": ["S", "M", "L"]},
    {"key": "camera.corner", "type": "enum", "default": "bottom-left",
     "options": ["bottom-left", "bottom-right", "top-left", "top-right"]},
    {"key": "camera.mirror", "type": "bool", "default": True},
    {"key": "camera.border", "type": "int", "default": 3, "min": 0, "max": 12},
    {"key": "camera.shadow", "type": "bool", "default": True},
    {"key": "camera.avoid_cursor", "type": "bool", "default": True},
    # ---- Auto-zoom
    {"key": "zoom.enabled", "type": "bool", "default": True},
    {"key": "zoom.level", "type": "float", "default": 2.0, "min": 1.25, "max": 3.0},
    {"key": "zoom.max_zoom", "type": "float", "default": 2.5, "min": 1.25, "max": 4.0},
    {"key": "zoom.on_clicks", "type": "bool", "default": True},
    {"key": "zoom.follow_typing", "type": "bool", "default": True},
    {"key": "zoom.on_windows", "type": "bool", "default": True},
    {"key": "zoom.between", "type": "enum", "default": "glide", "options": ["glide", "zoom-out"]},
    {"key": "zoom.stay_seconds", "type": "float", "default": 6.0, "min": 2.0, "max": 12.0},
    {"key": "zoom.motion", "type": "enum", "default": "normal", "options": ["calm", "normal", "quick"]},
    {"key": "zoom.idle_speedup", "type": "bool", "default": False},
    # ---- Privacy
    {"key": "privacy.secret_blur", "type": "bool", "default": False},
    {"key": "privacy.auto", "type": "bool", "default": True},
    {"key": "privacy.dnd", "type": "bool", "default": True},
    {"key": "privacy.solid_windows", "type": "enum", "default": "auto", "options": ["auto", "on", "off"]},
    # ---- Export
    {"key": "export.format", "type": "enum", "default": "mp4", "options": ["mp4", "gif"]},
    {"key": "export.quality", "type": "enum", "default": "high", "options": ["draft", "fast", "high", "best"]},
    {"key": "export.size_cap_mb", "type": "int", "default": 0, "min": 0, "max": 4000},
    {"key": "export.aspect", "type": "enum", "default": "16:9", "options": ["16:9", "9:16", "1:1"]},
    {"key": "export.also_vertical", "type": "bool", "default": False},
    # ---- Sharing (targets live in share.json; only the default is chosen here)
    {"key": "sharing.default_target", "type": "str", "default": "", "max_len": 64},
    # ---- Storage
    {"key": "storage.folder", "type": "str", "default": "", "max_len": 400},
    {"key": "storage.keep_raw", "type": "bool", "default": True},
    {"key": "storage.cleanup_days", "type": "int", "default": 0, "min": 0, "max": 365},
]
BY_KEY = {s["key"]: s for s in SCHEMA}
GROUPS = ["capture", "audio", "camera", "look", "zoom", "privacy", "export", "sharing", "storage"]

SIZE_FRAC = {"S": 0.16, "M": 0.22, "L": 0.30}

# bar-widget schema key (shell.json) -> config key
BAR_KEYS = {"mode": "capture.mode", "countdown": "capture.countdown", "fps": "capture.fps",
            "keys": "capture.keys", "audioMode": "audio.mode", "camera": "camera.mode",
            "replaySeconds": "capture.replay_seconds", "solidWindows": "privacy.solid_windows"}


class ConfigError(ValueError):
    pass


def path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return Path(os.environ.get("DOLLY_CONFIG", base / "dolly" / "config.json"))


def defaults() -> dict:
    out: dict = {"version": VERSION}
    for s in SCHEMA:
        g, k = s["key"].split(".", 1)
        out.setdefault(g, {})[k] = copy.deepcopy(s["default"])
    return out


def coerce(key: str, value):
    """Validated value for `key` or ConfigError."""
    s = BY_KEY.get(key)
    if not s:
        raise ConfigError(f"unknown setting {key}")
    t = s["type"]
    try:
        if t == "bool":
            if isinstance(value, str):
                if value.lower() in ("1", "true", "on", "yes"):
                    return True
                if value.lower() in ("0", "false", "off", "no"):
                    return False
                raise ValueError
            return bool(value)
        if t in ("int", "float"):
            v = int(float(value)) if t == "int" else float(value)
            if v != v:                                   # NaN
                raise ValueError
            return min(max(v, s.get("min", v)), s.get("max", v))
        if t == "enum":
            v = str(value)
            if v not in s["options"]:
                raise ValueError
            return v
        if t == "color":
            v = str(value).strip()
            if not (len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:])):
                raise ValueError
            return v.lower()
        if t == "image":
            v = str(value).strip()
            if not v:
                return ""
            from . import images
            try:
                return str(images.check(v))
            except images.ImageError as e:
                raise ConfigError(f"invalid value for {key}: {e}") from None
        v = str(value)
        if "\n" in v or "\0" in v or len(v) > s.get("max_len", 200):
            raise ValueError
        return v
    except ConfigError:
        raise                                            # already says why (e.g. the image check)
    except (TypeError, ValueError):
        raise ConfigError(f"invalid value for {key}: {value!r}") from None


def validate(data: dict) -> dict:
    """Merge onto defaults; drop unknown keys and invalid values (kept at default)."""
    out = defaults()
    if not isinstance(data, dict):
        return out
    for g in GROUPS:
        sec = data.get(g)
        if not isinstance(sec, dict):
            continue
        for k, v in sec.items():
            key = f"{g}.{k}"
            if key in BY_KEY:
                try:
                    out[g][k] = coerce(key, v)
                except ConfigError:
                    pass
    return out


def migrate(data: dict) -> dict:
    """Older layouts -> VERSION. v1 drafts kept capture.mic/desktop_audio/webcam booleans
    and the auto-zoom keys under "camera"."""
    if not isinstance(data, dict):
        return {}
    data = json.loads(json.dumps(data))
    cap, cam = data.get("capture") or {}, data.get("camera") or {}
    if any(k in cap for k in ("mic", "desktop_audio")):
        mic, sysa = bool(cap.pop("mic", False)), bool(cap.pop("desktop_audio", False))
        data.setdefault("audio", {})["mode"] = "both" if mic and sysa else "mic" if mic else "system" if sysa else "none"
        if cap.get("mic_device"):
            data["audio"]["mic_device"] = cap.pop("mic_device")
    if "webcam" in cap:
        data.setdefault("camera", {})["mode"] = "edit" if cap.pop("webcam") else "off"
    for old, new in (("zoom_strength", "strength"), ("max_zoom", "max_zoom"), ("follow_typing", "follow_typing"),
                     ("idle_speedup", "idle_speedup")):
        if old in cam:
            data.setdefault("zoom", {})[new] = cam.pop(old)
    zm = data.get("zoom")
    if isinstance(zm, dict) and "strength" in zm:
        # 0.4.0 "Zoom strength" (x 2.0) became "Zoom level" (the zoom itself) in 0.4.1
        try:
            zm.setdefault("level", round(2.0 * float(zm.pop("strength")), 2))
        except (TypeError, ValueError):
            zm.pop("strength", None)
    data["version"] = VERSION
    return data


def load() -> dict:
    try:
        raw = json.loads(path().read_text())
    except (OSError, ValueError):
        raw = {}
    return validate(migrate(raw))


def save(cfg: dict) -> dict:
    cfg = validate(cfg)
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return cfg


def get(key: str, cfg: dict | None = None):
    cfg = cfg or load()
    g, k = key.split(".", 1)
    return cfg[g][k]


def set_many(updates: dict, sync_bar: bool = True) -> dict:
    """{"look.radius": 20, ...} -> validated, saved; ConfigError on any bad value.
    Keys the bar widget also stores (BAR_KEYS) are mirrored into Omarchy's shell.json
    with `omarchy bar set io.github.jagritvats.dolly KEY VALUE --json` (the widget mirrors the other
    way: a change made there is written here with `dolly config set --from-bar`)."""
    cfg = load()
    changed = {}
    for key, v in updates.items():
        g, k = key.split(".", 1)
        new = coerce(key, v)
        if cfg[g][k] != new:
            changed[key] = new
        cfg[g][k] = new
    out = save(cfg)
    if sync_bar and changed:
        push_bar({bk: changed[ck] for bk, ck in BAR_KEYS.items() if ck in changed})
    return out


def push_bar(values: dict) -> list[list[str]]:
    import shutil
    import subprocess
    cmds = []
    if not values or not shutil.which("omarchy-bar") or os.environ.get("DOLLY_NO_BAR_SYNC") == "1":
        return cmds
    for bk, v in values.items():
        cmd = ["omarchy-bar", "set", "io.github.jagritvats.dolly", bk, json.dumps(v), "--json"]
        cmds.append(cmd)
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            pass
    return cmds


def bar_view(cfg: dict | None = None) -> dict:
    """The bar-widget settings as the shell.json entry spells them."""
    cfg = cfg or load()
    return {bk: get(ck, cfg) for bk, ck in BAR_KEYS.items()}


# ------------------------------------------------------------------ applying
def record_defaults(cfg: dict | None = None) -> dict:
    cfg = cfg or load()
    c = cfg["capture"]
    au, cam = cfg["audio"], cfg["camera"]
    return {"mode": c["mode"], "countdown": c["countdown"], "fps": int(c["fps"]), "keys": c["keys"],
            "audio": au["mode"], "mic_device": au["mic_device"], "system_device": au["system_device"],
            "camera": cam["mode"], "camera_device": cam["device"],
            "solid": cfg["privacy"]["solid_windows"], "dnd": cfg["privacy"]["dnd"]}


def style_defaults(cfg: dict | None = None, theme: dict | None = None) -> dict:
    """Project style for a new recording. Follow-theme pins the current Omarchy theme's
    name + colours into the project, so a later theme switch never changes a re-export;
    otherwise the chosen background is used as is."""
    from . import project as projmod
    cfg = cfg or load()
    lk, ex = cfg["look"], cfg["export"]
    if lk["background"] == "solid":
        bg = {"type": "solid", "color": lk["background_color"]}
    elif lk["background"] == "wallpaper":
        walls = projmod.wallpapers()
        bg = {"type": "wallpaper", "path": str(walls[0]), "blur": 6.0} if walls else {"type": "gradient",
                                                                                       "preset": "theme-glow"}
    elif lk["background"] == "image" and lk.get("background_image") and Path(lk["background_image"]).is_file():
        bg = {"type": "wallpaper", "path": lk["background_image"], "blur": float(lk.get("background_blur", 0.0))}
    elif lk["background"] == "image":
        bg = {"type": "gradient", "preset": "theme-glow"}   # the picture was moved or deleted
    else:
        bg = {"type": "gradient", "preset": lk["background"]}
    st = {"background": bg, "padding": lk["padding"], "radius": lk["radius"], "shadow": lk["shadow"],
          "cursor": {"size": lk["cursor_size"], "smoothing": lk["cursor_smoothing"],
                     "ripple": lk["click_effect"] == "ripple"},
          "keys": {"enabled": lk["keys_overlay"], "position": lk["keys_position"]},
          "speed": {"enabled": cfg["zoom"]["idle_speedup"]},
          "webcam": {"enabled": cfg["camera"]["mode"] == "edit", "shape": cfg["camera"]["shape"],
                     "size": SIZE_FRAC[cfg["camera"]["size"]], "corner": cfg["camera"]["corner"],
                     "mirror": cfg["camera"]["mirror"], "border": cfg["camera"]["border"],
                     "shadow": cfg["camera"]["shadow"], "auto_move": cfg["camera"]["avoid_cursor"]},
          "audio": {"denoise": cfg["audio"]["denoise"], "duck": cfg["audio"]["duck"],
                    "normalize": cfg["audio"]["normalize"]},
          "privacy": {"enabled": True, "auto_scrub": cfg["privacy"]["secret_blur"],
                      "auto": cfg["privacy"]["auto"]},
          "output": {"format": ex["format"], "quality": ex["quality"], "aspect": ex["aspect"]}}
    th = theme if theme is not None else projmod.theme_snapshot()
    st["theme"] = {**th, "follow": bool(lk["follow_theme"])}
    if not lk["follow_theme"] and bg.get("type") == "gradient":
        # a fixed look: freeze the preset's colours now so it never follows the theme
        p = projmod.gradient_presets(th["colors"]).get(bg["preset"])
        if p:
            st["background"] = {"type": "gradient", "stops": p["stops"], "angle": p["angle"]}
    return st


def zoom_defaults(cfg: dict | None = None) -> dict:
    cfg = cfg or load()
    z = cfg["zoom"]
    return {"enabled": z["enabled"], "zoom_default": round(z["level"], 3), "zoom_max": max(z["max_zoom"], z["level"]),
            "zoom_clicks": z["on_clicks"], "follow_typing": z["follow_typing"], "zoom_windows": z["on_windows"],
            "glide": z["between"] == "glide", "stay_gap": z["stay_seconds"], "motion": z["motion"]}


def export_defaults(cfg: dict | None = None) -> dict:
    cfg = cfg or load()
    ex = cfg["export"]
    return {"format": ex["format"], "quality": ex["quality"], "aspect": ex["aspect"],
            "size_cap_mb": ex["size_cap_mb"], "also_vertical": ex["also_vertical"]}


def describe() -> dict:
    """Schema + values for the editor's Settings view."""
    cfg = load()
    return {"version": VERSION, "path": str(path()), "groups": GROUPS, "schema": SCHEMA, "values": cfg,
            "bar_keys": BAR_KEYS}

"""Settings (~/.config/dolly/config.json): validation, atomic writes, the bar-widget
key mirror, and one "is it actually honoured" check per group:
Capture, Look, Camera, Privacy, Export, Sharing, Storage."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("DOLLY_THEME_DIR", str(tmp_path / "theme"))
    monkeypatch.setenv("DOLLY_FONT", "JetBrainsMono Nerd Font")
    monkeypatch.delenv("DOLLY_DIR", raising=False)
    monkeypatch.delenv("OMARCHY_SCREENRECORD_DIR", raising=False)
    monkeypatch.delenv("XDG_VIDEOS_DIR", raising=False)
    (tmp_path / "run").mkdir()
    _theme(tmp_path / "theme", "tokyo-night", "#7aa2f7", "#1a1b26", "dark")
    return tmp_path


def _theme(d: Path, name, accent, bg, mode):
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath("colors.toml").write_text(f'mode = "{mode}"\naccent = "{accent}"\nbackground = "{bg}"\n'
                                         f'foreground = "#c0caf5"\nred = "#f7768e"\n')
    st = d.parent / "theme.name"
    st.write_text(name + "\n")


def make_project(root: Path, name="2026-11-09_10-00-00", n=90, fps=30, W=640, H=400, typing=True):
    """A small finalized recording: a click at 1 s, typing at 1.5-3.4 s, then idle."""
    from dolly import capture
    pdir = root / name
    pdir.mkdir(parents=True)

    def frame(i):
        img = np.full((H, W, 3), 30, np.uint8)
        t = i / fps
        if t > 1.1:
            img[100:220, 150:450] = (90, 90, 100)
            img[110:118, 160:160 + int(min(280, max(0, (t - 1.5) * 140)))] = (230, 230, 230)
        return img
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                          "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                          str(pdir / "raw.mkv")], stdin=subprocess.PIPE)
    for i in range(n):
        p.stdin.write(frame(i).tobytes())
    p.stdin.close()
    p.wait()
    from dolly.monitors import monitor_target
    tgt = monitor_target({"name": "DP-1", "width": W, "height": H, "scale": 1.0, "x": 0, "y": 0})
    (pdir / "capture.json").write_text(json.dumps({"backend": "gsr", "region": tgt.region, "scale": 1.0, "fps": fps,
                                                   "target_info": tgt.to_meta(), "mode": "screen", "started": 1.7e9}))
    ev = [{"ms": 0, "type": "info", "click_source": "lua"},
          {"ms": 900, "type": "move", "x": 290, "y": 150}, {"ms": 1000, "type": "click", "x": 300, "y": 160}]
    if typing:
        ev += [{"ms": 1500 + 100 * k, "type": "key", "key": "a", "mods": []} for k in range(20)]
    (pdir / "events.raw.jsonl").write_text("\n".join(json.dumps(e) for e in ev) + "\n")
    capture.finalize(pdir)
    return pdir


# ------------------------------------------------------------------ the store
def test_defaults_validation_atomic_and_version(home):
    from dolly import config as C
    cfg = C.load()
    assert cfg["version"] == C.VERSION and cfg["capture"]["mode"] == "window"
    C.set_many({"look.radius": "22", "capture.fps": "30", "look.background_color": "#AABBCC"})
    raw = json.loads(C.path().read_text())
    assert raw["look"]["radius"] == 22 and raw["capture"]["fps"] == "30" and raw["look"]["background_color"] == "#aabbcc"
    with pytest.raises(C.ConfigError):
        C.set_many({"capture.mode": "everything"})
    with pytest.raises(C.ConfigError):
        C.set_many({"look.background_color": "red; rm -rf"})
    with pytest.raises(C.ConfigError):
        C.set_many({"nope.key": 1})
    assert C.coerce("look.radius", 999) == 40                      # clamped
    # a hand-edited file with junk keeps the valid parts
    C.path().write_text(json.dumps({"version": 1, "look": {"radius": "x", "padding": 0.1}, "evil": {"a": 1}}))
    cfg = C.load()
    assert cfg["look"]["radius"] == 14 and cfg["look"]["padding"] == 0.1 and "evil" not in cfg
    assert not list(C.path().parent.glob(".config.*"))            # no temp files left behind


def test_bar_widget_keys_have_one_source_of_truth(home):
    from dolly import config as C
    man = json.loads((ROOT / "manifest.json").read_text())
    keys = {s["key"] for s in man["barWidget"]["schema"]}
    assert keys <= set(C.BAR_KEYS), keys - set(C.BAR_KEYS)          # every widget setting maps to config
    for k in keys:                                                  # same defaults in both places
        assert str(man["barWidget"]["defaults"][k]) == str(C.BY_KEY[C.BAR_KEYS[k]]["default"]), k
    C.set_many({"capture.countdown": 5})
    assert C.bar_view()["countdown"] == 5
    qml = (ROOT / "BarWidget.qml").read_text()
    assert "config.json" in qml and "updateEntryInline" in qml and "--from-bar" in qml   # mirrored both ways


# ------------------------------------------------------------------ Capture
def test_capture_settings_drive_record(home, monkeypatch):
    from dolly import capture, cli, config as C
    from dolly.monitors import Target
    C.set_many({"capture.mode": "region", "capture.countdown": 0, "capture.fps": "30", "audio.mode": "mic",
                "audio.mic_device": "alsa_input.usb-mic", "capture.keys": False, "privacy.dnd": False,
                "privacy.solid_windows": "off", "camera.mode": "live"})
    ns = argparse.Namespace(mode=None, mic=None, desktop_audio=None, webcam=None, no_keys=None, fps=None,
                            countdown=None, solid=None, no_dnd=None, backend="auto", audio=None, camera=None)
    a = cli.apply_record_defaults(ns)
    assert (a.mode, a.countdown, a.fps, a.audio, a.no_keys, a.solid, a.dnd, a.camera) == \
        ("region", 0, 30, "mic", True, "off", False, "live")
    assert a.mic_device == "alsa_input.usb-mic"
    ns2 = argparse.Namespace(mode="screen", mic=None, desktop_audio=True, webcam=None, no_keys=None, fps=60,
                             countdown=2, solid=None, no_dnd=None, backend="auto", audio=None, camera="off")
    a2 = cli.apply_record_defaults(ns2)                           # flags win (old --desktop-audio too)
    assert (a2.mode, a2.audio, a2.fps, a2.countdown, a2.camera) == ("screen", "system", 60, 2, "off")
    t = Target("monitor", "DP-1", [0, 0, 100, 100], 1.0, (100.0, 100.0), physical=(100, 100))
    from dolly import audio as A
    assert A.resolve(a.audio, a.mic_device) == [("mic", "alsa_input.usb-mic")]
    assert [n for n, _ in A.resolve("both")] == ["mic", "system"]
    cmd = capture.recorder_cmd("gsr", t, Path("/tmp/x.mkv"), a.fps, [])
    assert "-a" not in cmd and cmd[cmd.index("-f") + 1] == "30"


# ------------------------------------------------------------------ Look (+ theme pinning)
def test_look_settings_shape_new_projects_and_renders(home):
    from dolly import config as C, project as projmod
    from dolly.events import Timeline
    from dolly.render import Compositor
    from dolly.zoom import Segment, ZoomConfig
    C.set_many({"look.follow_theme": False, "look.background": "solid", "look.background_color": "#204060",
                "look.padding": 0.1, "look.radius": 30, "look.cursor_size": 2.4, "look.click_effect": "none",
                "look.keys_position": "top"})
    root = home / "Videos" / "Dolly"
    pdir = make_project(root)
    st = projmod.Project(pdir).load()["style"]
    assert st["background"]["type"] == "solid" and st["background"]["color"] == "#204060"
    assert st["padding"] == 0.1 and st["radius"] == 30 and st["cursor"]["size"] == 2.4
    assert st["cursor"]["ripple"] is False and st["keys"]["position"] == "top"
    assert st["theme"]["name"] == "tokyo-night" and st["theme"]["follow"] is False
    data = projmod.Project(pdir).load()
    tl = Timeline.load(pdir / "events.json")
    comp = Compositor(tl, data["style"], [Segment.from_dict(s) for s in data["segments"]],
                      ZoomConfig.from_dict(data["zoom_config"]), 0, 3)
    assert comp.ripple is False and comp.keys_top and comp.cursor_size == 2.4
    out = comp.compose_at(0.5, np.full((400, 640, 3), 30, np.uint8))
    assert np.allclose(out[5, 5].astype(int), (0x60, 0x40, 0x20), atol=4)    # BGR of #204060 (shadow-tinted edge)


def test_follow_theme_pins_colours_so_reexports_are_stable(home):
    from dolly import config as C, project as projmod
    C.set_many({"look.follow_theme": True, "look.background": "theme-ink"})
    pdir = make_project(home / "Videos" / "Dolly")
    st = projmod.Project(pdir).load()["style"]
    assert st["theme"]["name"] == "tokyo-night" and st["theme"]["colors"]["background"] == "#1a1b26"
    _theme(home / "theme", "catppuccin-latte", "#1e66f5", "#eff1f5", "light")     # user switches theme later
    assert projmod.theme_name() == "catppuccin-latte" and projmod.theme_mode() == "light"
    assert projmod.style_colors(st)["background"] == "#1a1b26"                      # export still uses the pinned one
    from dolly.render import build_background, make_layout
    lay = make_layout(640, 400, st)
    bg = build_background(lay, {**st, "shadow": 0})
    assert np.allclose(bg[2, 2].astype(int), (0x26, 0x1b, 0x1a), atol=3)      # ink = the pinned background


# ------------------------------------------------------------------ Camera
def test_camera_settings(home):
    from dolly import config as C, project as projmod
    C.set_many({"zoom.level": 2.5, "zoom.max_zoom": 3.2, "zoom.follow_typing": False,
                "zoom.idle_speedup": True})
    pdir = make_project(home / "Videos" / "Dolly", n=210)               # 7 s, idle after 3.4 s
    data = projmod.Project(pdir).load()
    assert data["zoom_config"]["zoom_default"] == 2.5 and data["zoom_config"]["zoom_max"] == 3.2
    assert data["zoom_config"]["follow_typing"] is False
    assert not any("typ" in (s.get("reason") or "") for s in data["segments"])
    assert data["style"]["speed"]["enabled"] is True
    assert any(s["reason"] == "idle" for s in data["speed"])            # idle tail sped up
    C.set_many({"zoom.follow_typing": True, "zoom.idle_speedup": False})
    pdir2 = make_project(home / "Videos" / "Dolly", name="2026-11-09_10-05-00", n=210)
    d2 = projmod.Project(pdir2).load()
    assert d2["speed"] is None and d2["style"]["speed"]["enabled"] is False


def test_auto_zoom_settings(home):
    from dolly import config as C, project as projmod
    from dolly.zoom import ZoomConfig
    zd = C.zoom_defaults()
    assert zd["enabled"] and zd["glide"] and zd["zoom_default"] == 2.0 and zd["stay_gap"] == 6.0
    C.set_many({"zoom.enabled": False})
    pdir = make_project(home / "Videos" / "Dolly", n=150)
    data = projmod.Project(pdir).load()
    assert data["zoom_config"]["enabled"] is False and data["segments"] == []      # no auto zooms
    C.set_many({"zoom.enabled": True, "zoom.on_clicks": False, "zoom.between": "zoom-out", "zoom.stay_seconds": 9,
                "zoom.motion": "calm", "zoom.level": 2.8})
    pdir2 = make_project(home / "Videos" / "Dolly", name="2026-11-09_10-06-00", n=150)
    d2 = projmod.Project(pdir2).load()
    zc = ZoomConfig.from_dict(d2["zoom_config"])
    assert (zc.zoom_clicks, zc.glide, zc.stay_gap, zc.motion, zc.zoom_default) == (False, False, 9.0, "calm", 2.8)
    assert zc.zoom_max >= 2.8                                                       # level never above the cap
    assert not any("click" in (s.get("reason") or "") for s in d2["segments"])
    with pytest.raises(C.ConfigError):
        C.set_many({"zoom.between": "teleport"})
    # 0.4.0 files: "strength" 1.25 -> level 2.5
    C.path().write_text(json.dumps({"version": 1, "zoom": {"strength": 1.25, "max_zoom": 3.0}}))
    assert C.load()["zoom"]["level"] == 2.5


def test_editor_replans_auto_zooms_and_keeps_manual(home):
    from dolly import project as projmod
    from dolly.editor.server import Editor
    pdir = make_project(home / "Videos" / "Dolly", n=150)
    ed = Editor(pdir)
    data = projmod.Project(pdir).load()
    manual = {"id": "m1", "start": 4.0, "end": 4.8, "zoom": 1.7, "focus": [300, 200], "follow": False,
              "source": "manual", "reason": ""}
    data["segments"] = (data["segments"] or []) + [manual]
    projmod.Project(pdir).save(data)
    r = ed.auto({"zoom_config": {"enabled": False, "evil": 1, "motion": "warp"}, "keep_manual": True})
    assert [s["id"] for s in r["segments"]] == ["m1"]                 # only the manual zoom is left
    assert r["zoom_config"]["enabled"] is False and r["zoom_config"]["motion"] == "normal"
    saved = projmod.Project(pdir).load()
    assert saved["zoom_config"]["enabled"] is False and "evil" not in saved["zoom_config"]
    r = ed.auto({"zoom_config": {"enabled": True, "zoom_default": 9}, "keep_manual": True})
    assert any(s["source"] == "auto" for s in r["segments"]) and any(s["id"] == "m1" for s in r["segments"])
    assert r["zoom_config"]["zoom_default"] == 3.0 and r["zoom_config"]["zoom_max"] >= 3.0
    assert all(a["end"] <= b["start"] + 1e-6 for a, b in zip(r["segments"], r["segments"][1:]))


# ------------------------------------------------------------------ Privacy
def test_privacy_secret_blur_runs_once_on_export(home, monkeypatch):
    from dolly import config as C, project as projmod, privacy, render
    C.set_many({"privacy.secret_blur": True})
    pdir = make_project(home / "Videos" / "Dolly")
    calls = []

    def fake_scan(raw, dur, every=1.0, progress=None):
        calls.append(raw)
        return [{"id": "b1", "start": 0, "end": 1, "rect": [10, 10, 50, 20], "mode": "blur", "source": "secret",
                 "label": "token", "enabled": True}]
    monkeypatch.setattr(privacy, "scan_secrets", fake_scan)
    render._auto_scrub(projmod.Project(pdir))
    render._auto_scrub(projmod.Project(pdir))
    data = projmod.Project(pdir).load()
    assert len(calls) == 1 and data["scrubbed"] and data["privacy"][0]["source"] == "secret"
    # DND and solid windows: Settings > Privacy reach the recorder
    from dolly.config import record_defaults
    C.set_many({"privacy.dnd": False, "privacy.solid_windows": "on"})
    d = record_defaults()
    assert d["dnd"] is False and d["solid"] == "on"


# ------------------------------------------------------------------ Export
def test_export_settings(home, monkeypatch):
    from dolly import config as C, project as projmod, render
    C.set_many({"export.format": "gif", "export.quality": "fast", "export.aspect": "9:16"})
    pdir = make_project(home / "Videos" / "Dolly")
    out = projmod.Project(pdir).load()["style"]["output"]
    assert (out["format"], out["quality"], out["aspect"]) == ("gif", "fast", "9:16")
    # size cap + an extra vertical copy + keep raw off
    C.set_many({"export.size_cap_mb": 1, "export.also_vertical": True, "storage.keep_raw": False})
    big = pdir / "exports" / "big.mp4"
    big.parent.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "nullsrc=s=640x360:r=30,geq=random(1)*255:128:128",
                    "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv420p", str(big)],
                   check=True)
    assert big.stat().st_size > 1 << 20
    seen = []
    monkeypatch.setattr(render, "export", lambda *a, **k: seen.append(k.get("aspect")))
    res = render._post_export(projmod.Project(pdir), big, "mp4", "16:9", "high", None, None, False)
    assert res.exists() and res.stat().st_size <= 1 << 20 and not big.exists()
    assert seen == ["9:16"]
    assert not (pdir / "raw.mkv").exists() and projmod.Project(pdir).load().get("raw_removed")
    assert any(p.name.startswith("raw.mkv") for p in (home / "data" / "Trash" / "files").iterdir())


# ------------------------------------------------------------------ Sharing
def test_sharing_default_target(home, monkeypatch):
    from dolly import config as C
    from dolly.share import config as scfg, targets
    p = scfg.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"default": "a", "targets": {
        "a": {"type": "rclone", "remote": "a:x"}, "b": {"type": "rclone", "remote": "b:y"}}}))
    os.chmod(p, 0o600)
    assert targets.get_target(None).name == "a"
    C.set_many({"sharing.default_target": "b"})
    assert targets.get_target(None).name == "b"
    C.set_many({"sharing.default_target": "gone"})                  # removed from share.json: fall back
    assert targets.get_target(None).name == "a"
    from dolly.editor.server import _public_target
    pt = _public_target("s3", {"type": "s3", "bucket": "demos", "access_key": "AKIAEXAMPLE", "secret_key": "s3cr3t",
                               "access_key_env": "R2_KEY"})
    assert "AKIAEXAMPLE" not in json.dumps(pt) and "s3cr3t" not in json.dumps(pt) and pt["bucket"] == "demos"


# ------------------------------------------------------------------ Storage
def test_storage_folder_and_omarchy_dirs(home, monkeypatch):
    from dolly import config as C, project as projmod
    assert projmod.videos_root() == home / "Videos" / "Dolly"
    (home / "cfg").mkdir(exist_ok=True)
    (home / "cfg" / "user-dirs.dirs").write_text('XDG_VIDEOS_DIR="$HOME/Movies"\n')
    assert projmod.videos_root() == home / "Movies" / "Dolly"
    monkeypatch.setenv("OMARCHY_SCREENRECORD_DIR", str(home / "Screencasts"))
    assert projmod.videos_root() == home / "Screencasts" / "Dolly"     # Omarchy's recorder setting wins
    C.set_many({"storage.folder": str(home / "Demos")})
    assert projmod.videos_root() == home / "Demos"                      # an explicit Dolly folder wins


def test_storage_auto_cleanup(home):
    from dolly import config as C, library
    root = home / "Videos" / "Dolly"
    old = make_project(root, name="2026-01-01_10-00-00")
    (old / "exports").mkdir(exist_ok=True)
    (old / "exports" / "x.mp4").write_bytes(b"0" * 100)
    new = make_project(root, name="2026-11-09_10-00-00")
    assert library.auto_cleanup() is None                               # 0 = never
    C.set_many({"storage.cleanup_days": 30})
    r = library.auto_cleanup()
    assert r["recordings"] == 1 and not (old / "raw.mkv").exists() and (new / "raw.mkv").exists()


def test_settings_push_bar_widget_values_and_accept_them_back(home, monkeypatch):
    from dolly import config as C
    import subprocess as sp
    monkeypatch.delenv("DOLLY_NO_BAR_SYNC", raising=False)      # conftest turns the mirror off; Popen is faked here
    calls = []
    monkeypatch.setattr(C.shutil if hasattr(C, "shutil") else __import__("shutil"), "which",
                        lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(sp, "Popen", lambda cmd, **k: calls.append(cmd))
    C.set_many({"capture.mode": "region", "look.radius": 20, "audio.mode": "both"})
    assert ["omarchy-bar", "set", "io.github.jagritvats.dolly", "mode", '"region"', "--json"] in calls
    assert ["omarchy-bar", "set", "io.github.jagritvats.dolly", "audioMode", '"both"', "--json"] in calls
    assert not any("radius" in " ".join(c) for c in calls)        # not a widget key
    calls.clear()
    C.set_many({"capture.mode": "region"})                         # unchanged: no echo
    assert calls == []
    C.set_many({"capture.countdown": 7}, sync_bar=False)          # from the widget: no echo
    assert calls == [] and C.load()["capture"]["countdown"] == 7
    # an old v1 file migrates
    C.path().write_text(json.dumps({"version": 1, "capture": {"mic": True, "desktop_audio": True, "webcam": True},
                                    "camera": {"zoom_strength": 1.2}}))
    cfg = C.load()
    assert cfg["audio"]["mode"] == "both" and cfg["camera"]["mode"] == "edit" and cfg["zoom"]["level"] == 2.4

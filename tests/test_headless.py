"""`dolly record` for scripts and coding agents: named targets, --for, --json (dolly/headless.py)."""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import headless as H  # noqa: E402
from tests.test_system import dolly, env  # noqa: E402,F401  (stub hyprctl + recorder)

CLIENTS = [
    {"address": "0xa1", "class": "kitty", "title": "~/app: vim", "focusHistoryID": 2, "mapped": True},
    {"address": "0xa2", "class": "kitty", "title": "logs", "focusHistoryID": 0, "mapped": True},
    {"address": "0xb1", "class": "chromium", "title": "Pull request #12", "focusHistoryID": 1, "mapped": True},
    {"address": "0xc1", "class": "org.keepassxc.KeePassXC", "title": "Passwords", "focusHistoryID": 5, "hidden": True},
]


def test_window_selectors():
    assert H.resolve_window("kitty", CLIENTS)["address"] == "0xa2"            # most recently focused of two
    assert H.resolve_window("chrom", CLIENTS)["address"] == "0xb1"            # class contains
    assert H.resolve_window("pull request", CLIENTS)["address"] == "0xb1"     # title contains
    assert H.resolve_window("title:vim", CLIENTS)["address"] == "0xa1"
    assert H.resolve_window("class:KITTY", CLIENTS)["address"] == "0xa2"
    assert H.resolve_window("address:0xA1", CLIENTS)["address"] == "0xa1"
    assert H.resolve_window("0xb1", CLIENTS)["address"] == "0xb1"
    assert H.resolve_window("active", CLIENTS)["address"] == "0xa2"
    with pytest.raises(H.HeadlessError) as e:
        H.resolve_window("keepass", CLIENTS)                                  # hidden windows do not count
    assert e.value.code == "window-not-found" and len(e.value.extra["windows"]) == 3


def test_region_forms():
    assert H.parse_region("100,200,800x600") == "100,200 800x600"
    assert H.parse_region(" 0,0 1280x720 ") == "0,0 1280x720"
    for bad in ("800x600", "1,2,3", "0,0,8x8", ""):
        with pytest.raises(H.HeadlessError):
            H.parse_region(bad)


def test_session_is_found_without_environment(tmp_path):
    rt = tmp_path / "run"
    (rt / "hypr" / "abc123").mkdir(parents=True)
    (rt / "hypr" / "abc123" / ".socket.sock").touch()
    (rt / "wayland-1").touch()
    (rt / "wayland-1.lock").touch()
    (rt / "bus").touch()
    e = {"XDG_RUNTIME_DIR": str(rt)}
    H.adopt_session_env(e)
    assert e["HYPRLAND_INSTANCE_SIGNATURE"] == "abc123" and e["WAYLAND_DISPLAY"] == "wayland-1"
    assert e["DBUS_SESSION_BUS_ADDRESS"].endswith("/bus")
    e2 = {"XDG_RUNTIME_DIR": str(rt), "WAYLAND_DISPLAY": "wayland-7", "HYPRLAND_INSTANCE_SIGNATURE": "mine"}
    H.adopt_session_env(e2)
    assert e2["WAYLAND_DISPLAY"] == "wayland-7" and e2["HYPRLAND_INSTANCE_SIGNATURE"] == "mine"   # never overridden


def test_error_codes():
    assert H.classify("dolly: already recording; use `dolly stop`") == "already-recording"
    assert H.classify("dolly: gpu-screen-recorder (pid 4) is already recording; stop it first") == "other-recorder"
    assert H.classify("dolly: missing packages: wf-recorder (install it with Omarchy menu > Install > Package)") == "missing-packages"
    assert H.classify("dolly: cannot record into /x: read-only") == "folder-not-writable"
    assert H.classify("Only 0.1 GB free in /x (ext4): about 1 min") == "disk-full"


def _one_json(stdout: str) -> dict:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, stdout                                            # exactly one object, nothing else
    return json.loads(lines[0])


def test_errors_are_one_json_object(env):  # noqa: F811
    r = dolly(env, "record", "--window", "no-such-window", "--json", check=False)
    out = _one_json(r.stdout)
    assert r.returncode == 1 and out == {**out, "ok": False, "error": "window-not-found"}
    assert out["windows"][0]["class"] == "Alacritty"
    r = dolly(env, "record", "--monitor", "HDMI-9", "--json", check=False)
    assert r.returncode == 1 and _one_json(r.stdout)["error"] == "monitor-not-found"
    r = dolly(env, "record", "--mode", "region", "--json", check=False)      # would need a click
    assert r.returncode == 1 and _one_json(r.stdout)["error"] == "needs-target"
    r = dolly(env, "stop", "--json", check=False)
    assert r.returncode == 1 and _one_json(r.stdout)["error"] == "not-recording"


def test_for_records_that_long_and_describes_the_take(env):  # noqa: F811
    t = time.time()
    r = dolly(env, "record", "--window", "alacritty", "--for", "2.5", "--fps", "30", "--json")
    took = time.time() - t
    out = _one_json(r.stdout)
    assert out["ok"] and out["recording"] is False and out["target"]["kind"] == "window"
    assert out["target"]["address"] == "0xa1" and out["backend"] and out["sync"]
    pdir = Path(out["dir"])
    assert Path(out["raw"]).exists() and Path(out["events"]).exists() and Path(out["project"]).exists()
    assert out["duration"] >= 2.4 and took < 40                                # at least the asked time is on tape
    meta = json.loads((pdir / "capture.json").read_text())
    assert meta["mode"] == "window" and meta["target"] == "0xa1"               # no picker: the named window
    assert json.loads(dolly(env, "status", "--json", check=False).stdout)["recording"] is False
    ex = dolly(env, "export", str(pdir), "--json", "--quality", "draft", "-o", str(pdir / "clip.mp4"))
    clip = _one_json(ex.stdout)
    assert clip["ok"] and Path(clip["output"]).stat().st_size == clip["bytes"] > 1000
    assert clip["format"] == "mp4" and clip["aspect"] == "16:9" and clip["duration"] > 1.0


def test_second_recording_is_a_json_error_and_stop_describes(env):  # noqa: F811
    first = _one_json(dolly(env, "record", "--monitor", "DP-1", "--fps", "30", "--json").stdout)
    assert first["ok"] and first["recording"] is True and first["target"] == {"kind": "monitor", "name": "DP-1"}
    r = dolly(env, "record", "--for", "1", "--json", check=False)
    assert r.returncode == 1 and _one_json(r.stdout)["error"] == "already-recording"
    time.sleep(1.5)
    done = _one_json(dolly(env, "stop", "--json").stdout)
    assert done["ok"] and done["dir"] == first["dir"] and done["duration"] > 1.0

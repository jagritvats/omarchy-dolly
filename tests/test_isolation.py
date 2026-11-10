"""The suite must never reach the user's desktop (see conftest.py)."""
import os
import shutil
import subprocess
from pathlib import Path


def test_no_desktop_is_reachable():
    home = Path(os.environ["HOME"])
    assert home.name == "home" and home.parent.name.startswith("dolly-tests-")
    for k in ("HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY", "DISPLAY", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        assert k not in os.environ, k
    assert Path(os.environ["XDG_RUNTIME_DIR"]).parent == home.parent
    for cmd in ("hyprctl", "omarchy-notification-send", "omarchy-shell", "omarchy-bar", "omarchy-pkg-add",
                "omarchy-hyprland-window-transparency", "notify-send", "wl-copy"):
        assert Path(shutil.which(cmd)).parent.name == "shims", cmd
    r = subprocess.run(["hyprctl", "dispatch", "workspace", "2"], capture_output=True, text=True)
    assert r.returncode != 0                                     # a shim, not the compositor


def test_stray_recorders_are_reaped(tmp_path):
    """What the autouse fixture does after every test: a process started from a test's
    folder (here one that ignores SIGTERM) is gone afterwards."""
    import sys
    import time
    from tests.conftest import reap
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    p = subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "videos" / "raw.mkv")], start_new_session=True)
    time.sleep(0.3)
    found = reap([str(tmp_path)], grace=0.5)
    assert [pid for pid, _ in found] == [p.pid]
    p.wait(timeout=5)
    assert p.returncode == -9

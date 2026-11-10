"""Keep the test suite off the user's desktop.

On a real Omarchy session (found on the VM, Nov 10) tests reached the live desktop:
real notifications, Do Not Disturb toggled, the bar widget showing a render, a
workspace switch, the real Glassy toggles read. Before any test runs this makes
the whole session (and every subprocess it starts) look like a machine with no
desktop:

- HOME is a temporary folder; XDG_CONFIG/STATE/CACHE/DATA_HOME are unset so they
  follow HOME (the user's own Dolly settings and Omarchy theme are never read);
  PYTHONUSERBASE keeps `pip install --user` packages importable
- XDG_RUNTIME_DIR is private; no Hyprland, Wayland, X11, sway or session bus
- desktop commands (notifications, omarchy-shell, omarchy-bar, hyprctl, theme
  and window helpers, package installs, clipboard, file managers, mpv) resolve to
  shims that log their arguments and do nothing. Tests that need a working stub
  put their own in front of PATH, as before.
"""
import os
import shutil
import site
import stat
import tempfile
from pathlib import Path

SHIMS = {
    # name: (stdout, exit code)
    "omarchy-notification-send": ("", 0),
    "notify-send": ("", 0),
    "makoctl": ("", 0),
    "omarchy-shell": ("", 1),
    "omarchy-bar": ("", 1),
    "hyprctl": ("HYPRLAND_INSTANCE_SIGNATURE not set! (is hyprland running?)", 1),
    "omarchy-hyprland-window-transparency": ("opaque", 0),
    "omarchy-theme-set": ("", 0),
    "omarchy-launch-webapp": ("", 0),
    "omarchy-launch-floating-terminal-with-presentation": ("", 0),
    "omarchy-launch-browser": ("", 0),
    "omarchy-pkg-add": ("", 1),
    "omarchy-menu": ("", 0),
    "omarchy-system": ("", 1),
    "uwsm": ("", 1),
    "systemctl": ("", 1),
    "loginctl": ("", 1),
    "gdbus": ("", 1),
    "wl-copy": ("", 0),
    "wl-paste": ("", 1),
    "xdg-open": ("", 0),
    "nautilus": ("", 0),
    "mpv": ("", 1),
    "grim": ("", 1),
    "slurp": ("", 1),
}


def pytest_configure(config):
    if os.environ.get("DOLLY_TEST_ISOLATED") == "1":
        return
    root = Path(tempfile.mkdtemp(prefix="dolly-tests-"))
    config._dolly_test_root = root
    shims, home, run = root / "shims", root / "home", root / "run"
    for d in (shims, home, run):
        d.mkdir()
    run.chmod(0o700)
    log = root / "shim-calls.log"
    for name, (out, rc) in SHIMS.items():
        p = shims / name
        p.write_text("#!/bin/sh\n"
                     f"printf '%s' \"{name}\" >> \"{log}\"; printf ' %s' \"$@\" >> \"{log}\"; echo >> \"{log}\"\n"
                     + (f"echo '{out}'\n" if out else "")
                     + f"exit {rc}\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    user_base = site.getuserbase()
    e = os.environ
    e["PYTHONUSERBASE"] = e.get("PYTHONUSERBASE") or user_base
    e["HOME"] = str(home)
    for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
              "HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY", "DISPLAY", "SWAYSOCK", "NIRI_SOCKET",
              "OMARCHY_SCREENRECORD_DIR", "DOLLY_BACKEND", "DOLLY_VENV"):
        e.pop(k, None)
    e["XDG_RUNTIME_DIR"] = str(run)
    e["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={run}/no-session-bus"
    e["PATH"] = f"{shims}:{e['PATH']}"
    e["DOLLY_DIR"] = str(root / "videos")
    e["DOLLY_OPEN_EDITOR"] = "0"
    e["DOLLY_NO_BAR_SYNC"] = "1"
    # /tmp is often a RAM tmpfs (Arch/Omarchy): recordings of a few MB must not trip the
    # 2 GB start / 300 MB stop guard. The guard's own tests set their own values.
    e["DOLLY_MIN_FREE_GB"] = "0.02"
    e["DOLLY_STOP_FREE_MB"] = "5"
    e["DOLLY_BLOCK_MINUTES"] = "0"
    e["DOLLY_WARN_MINUTES"] = "0"
    e["DOLLY_TEST_ISOLATED"] = "1"
    e["DOLLY_TEST_SHIM_LOG"] = str(log)


def pytest_unconfigure(config):
    root = getattr(config, "_dolly_test_root", None)
    if root and os.environ.get("DOLLY_KEEP_TEST_HOME") != "1":
        shutil.rmtree(root, ignore_errors=True)


def _stray_processes(markers):
    """Our processes whose command line mentions a test folder (fake recorders, loggers)."""
    me, uid = os.getpid(), os.getuid()
    found = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            if d.stat().st_uid != uid:
                continue
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            if b"DOLLY_TEST_FIXTURE=1" in (d / "environ").read_bytes():
                continue            # a fixture's own service (private dbus/pipewire), not a leak
        except OSError:
            continue
        if any(m in cmd for m in markers):
            found.append((int(d.name), cmd))
    return found


import pytest  # noqa: E402


def reap(markers, grace=3.0):
    """SIGTERM, then SIGKILL after `grace` s, every stray process; returns what it found."""
    import signal
    import time
    stray = _stray_processes(markers)
    for pid, _ in stray:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    left, deadline = list(stray), time.time() + grace
    while left and time.time() < deadline:
        left = [(p, c) for p, c in left if Path(f"/proc/{p}").exists()]
        time.sleep(0.1)
    for pid, _ in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return stray


@pytest.fixture(autouse=True)
def _no_stray_recorders(request):
    """A test that fails half-way never reaches its own stop/cancel; its fake recorder and
    logger (own sessions) then ran on, holding a deleted 2.5 GB raw.mkv in /tmp (VM, Nov 10)."""
    yield
    markers = [str(request.config._tmp_path_factory.getbasetemp())]
    root = getattr(request.config, "_dolly_test_root", None)
    if root:
        markers.append(str(root / "videos"))
    reap(markers)

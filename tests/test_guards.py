"""Omarchy menu guards must be cheap: every `when:` runs on each menu open
(budget <=100 ms per guard, <=200 ms per batch). Dolly's guards are pure shell
file tests (no Python, no `dolly` invocation) over state files that Dolly
keeps accurate. Also: solid-windows (Glassy) apply/restore bookkeeping."""
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def setup_menu(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / ".config"))
    from tests._pathutil import path_without
    env["PATH"] = path_without("hyprctl", tmp_path)
    r = subprocess.run([str(ROOT / "bin" / "dolly-setup")], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=120)
    assert r.returncode == 0, r.stderr
    menu = (tmp_path / ".config/omarchy/extensions/omarchy-menu.jsonc").read_text()
    body = "\n".join(l for l in menu.splitlines() if not l.strip().startswith("//"))
    return json.loads(re.sub(r",\s*([}\]])", r"\1", body))   # JSONC: trailing commas allowed


def test_every_guard_is_a_fast_pure_shell_test(tmp_path):
    rows = setup_menu(tmp_path)
    guards = [v["when"] for k, v in rows.items() if "dolly" in k and "when" in v]
    assert len(guards) >= 7
    run = tmp_path / "run"
    run.mkdir()
    env = dict(os.environ, HOME=str(tmp_path), XDG_RUNTIME_DIR=str(run), XDG_STATE_HOME=str(tmp_path / "state"))
    for g in guards:
        # rows start with an existence check of the plugin ([ -x .../bin/dolly ]): it tests a
        # file, it never runs Dolly; whatever follows must not start Dolly or Python either
        body = re.sub(r"^\[ -x [^\]]*/bin/dolly \](?: && |$)", "", g)
        assert not re.search(r"\bdolly\s", body) and "bin/dolly" not in body and "python" not in body, g
        for shell in ("sh", "bash"):
            best = float("inf")
            for _ in range(3):      # best of 3 batches: a busy machine (VM) adds noise, not cost
                t = time.perf_counter()
                for _ in range(20):
                    subprocess.run([shell, "-c", g], env=env, capture_output=True)
                best = min(best, (time.perf_counter() - t) / 20)
            assert best < 0.010, f"{shell}: {g} took {best * 1000:.1f} ms"


def guard_true(name, env):
    from dolly.guards import GUARDS
    return subprocess.run(["sh", "-c", GUARDS[name]], env=env).returncode == 0


def test_guard_files_track_state(tmp_path, monkeypatch):
    run, state = tmp_path / "run", tmp_path / "state"
    run.mkdir()
    env = dict(os.environ, HOME=str(tmp_path), XDG_RUNTIME_DIR=str(run), XDG_STATE_HOME=str(state))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    from dolly import guards
    assert not guard_true("recording", env) and guard_true("not_recording", env)
    assert not guard_true("last", env) and not guard_true("replay", env) and not guard_true("share_ready", env)
    guards.set_recording(os.getpid())
    assert guard_true("recording", env) and not guard_true("not_recording", env)
    dead = subprocess.Popen(["true"])
    dead.wait()
    guards.set_recording(dead.pid)                    # crashed recorder: the row disappears by itself
    assert not guard_true("recording", env)
    guards.set_recording(None)
    assert not (run / "dolly" / "recording.pid").exists()
    proj = tmp_path / "videos" / "p1"
    proj.mkdir(parents=True)
    guards.set_last(proj)
    assert guard_true("last", env)
    proj.rmdir()                                      # deleted recording: no "Open last" row
    assert not guard_true("last", env)
    guards.set_replay(os.getpid())
    assert guard_true("replay", env) and not guard_true("not_replay", env)
    guards.set_share_ready(True)
    assert guard_true("share_ready", env)
    assert not list((run / "dolly").glob(".*.tmp")) and not list((state / "dolly").glob(".*.tmp"))   # atomic


def test_share_ready_follows_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = tmp_path / "share.json"
    monkeypatch.setenv("DOLLY_SHARE_CONFIG", str(cfg))
    from dolly.share import config
    config.load()
    assert not (tmp_path / "state/dolly/share-ready").exists()
    cfg.write_text(json.dumps({"targets": {"r": {"type": "rclone", "remote": "x:y"}}}))
    config.load()
    assert (tmp_path / "state/dolly/share-ready").exists()


HYPRCTL = r'''#!/usr/bin/env bash
log="$STUB_LOG"; echo "$*" >> "$log"
case "$1" in
  -j) echo '[{"address":"0xa1","mapped":true},{"address":"0xb2","mapped":true},{"address":"0xc3","mapped":true}]';;
  getprop) case "$2" in address:0xc3) echo 1;; *) echo 0;; esac;;
  dispatch) echo ok;;
esac
'''


def test_solid_windows_apply_and_restore(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    h = bindir / "hyprctl"
    h.write_text(HYPRCTL)
    h.chmod(h.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from dolly import solid
    assert not solid.glassy_active() and not solid.wanted("auto") and solid.wanted("on")
    tog = tmp_path / "state/omarchy/toggles/hypr"
    tog.mkdir(parents=True)
    (tog / "window-glassy.lua").write_text("-- glassy")
    assert solid.glassy_active() and solid.wanted(None)
    n = solid.make_solid()
    assert n == 2                                      # 0xc3 was already opaque by choice: untouched
    led = json.loads((tmp_path / "run/dolly/solid.json").read_text())
    assert set(led) == {"0xa1", "0xb2"}
    calls = log.read_text()
    assert 'prop = "opaque", value = "true"' in calls and "0xc3\", prop" not in calls
    assert "omarchy-hyprland-window-transparency" not in calls     # the user's global mode is never touched
    solid.make_solid(["b2"])                           # a window opened during the recording, already done
    assert solid.restore() == 2
    assert not (tmp_path / "run/dolly/solid.json").exists()
    assert log.read_text().count('value = "unset"') == 2
    assert (tog / "window-glassy.lua").exists()        # mode unchanged

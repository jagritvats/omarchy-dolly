"""`dolly panel --json` (the panel's one call), `dolly update` (Omarchy's own command,
then a refresh) and the first-run-after-update backstop."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import cli  # noqa: E402
from dolly import integration as I  # noqa: E402

NL = chr(10)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from dolly import log
    log.get()       # Dolly's logger is opened once per test run: open it before this test moves XDG_STATE_HOME
    for k, v in (("HOME", tmp_path), ("XDG_STATE_HOME", tmp_path / "state"), ("XDG_CONFIG_HOME", tmp_path / ".config"),
                 ("XDG_RUNTIME_DIR", tmp_path / "run"), ("XDG_CACHE_HOME", tmp_path / "cache"),
                 ("DOLLY_DIR", tmp_path / "videos")):
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("DOLLY_CONFIG", raising=False)
    monkeypatch.setenv("DOLLY_NO_HYPR_RELOAD", "1")
    monkeypatch.setenv("DOLLY_NO_BAR_SYNC", "1")
    (tmp_path / "run").mkdir()
    (tmp_path / ".config/hypr").mkdir(parents=True)
    (tmp_path / ".config/omarchy/extensions").mkdir(parents=True)
    (tmp_path / ".config/hypr/bindings.lua").write_text("-- mine" + NL)
    monkeypatch.setattr(I, "live_binds", lambda timeout=2.0: [])
    monkeypatch.setattr(I, "_reload_hyprland", lambda: True)
    return tmp_path


def stub(bindir: Path, name: str, body: str):
    bindir.mkdir(exist_ok=True)
    p = bindir / name
    p.write_text("#!/usr/bin/env bash" + NL + body + NL)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


# ---------------------------------------------------------------- the panel's one call
def test_panel_json_has_everything_the_six_commands_gave(home, capfd):
    cli.cmd_panel(None)
    d = json.loads(capfd.readouterr().out)
    assert set(d) >= {"config", "recent", "audio", "cameras", "integration", "deps"}
    assert d["config"]["capture"]["mode"] in ("screen", "window", "region")
    assert d["recent"] == [] and isinstance(d["cameras"], list)
    assert {"sources", "sinks", "available"} <= set(d["audio"])
    assert d["integration"]["set_up"] is False and "packages" in d["deps"]


def test_one_broken_part_does_not_empty_the_panel(home, capfd, monkeypatch):
    from dolly import audio

    def boom():
        raise RuntimeError("pactl went away")
    monkeypatch.setattr(audio, "devices", boom)
    cli.cmd_panel(None)
    d = json.loads(capfd.readouterr().out)
    assert d["audio"] is None and "pactl went away" in d["errors"]["audio"]
    assert d["config"] and d["integration"] is not None and d["deps"] is not None


# ---------------------------------------------------------------- the backstop
def test_the_first_command_of_a_new_version_refreshes_set_up(home, monkeypatch):
    I.apply()
    I.stamp_file().write_text(json.dumps({"version": "0.0.1", "set_up": True}))      # as if Set up ran on an older version
    said = []
    monkeypatch.setattr(cli, "_say_refreshed", said.append)
    cli._after_update(["toggle"])
    assert I.stamp()["version"] == I.version() and len(said) == 1
    cli._after_update(["toggle"])                                                    # now current: two file reads, no more
    assert len(said) == 1


def test_internal_commands_never_trigger_it(home, monkeypatch):
    I.apply()
    I.stamp_file().write_text(json.dumps({"version": "0.0.1", "set_up": True}))
    monkeypatch.setattr(I, "auto_apply", lambda: pytest.fail("must not run here"))
    for cmd in ("log-events", "integration", "setup", "shell-start", "update"):
        cli._after_update([cmd])


def test_a_failure_in_the_backstop_never_stops_the_command(home, monkeypatch):
    I.stamp_file().parent.mkdir(parents=True, exist_ok=True)
    I.stamp_file().write_text(json.dumps({"version": "0.0.1"}))
    monkeypatch.setattr(I, "auto_apply", lambda: (_ for _ in ()).throw(OSError("disk")))
    cli._after_update(["status"])                                                    # swallowed, logged


# ---------------------------------------------------------------- shell-start
def test_shell_start_makes_the_folders_the_widget_watches(home, capfd):
    cli.cmd_shell_start(None)
    out = json.loads(capfd.readouterr().out)
    assert (home / "run/dolly/jobs").is_dir() and out["runtime"] == str(home / "run/dolly") and out["changed"] == []


# ---------------------------------------------------------------- dolly update
def run_update(home, omarchy_body):
    bindir = home / "bin"
    if omarchy_body is not None:
        stub(bindir, "omarchy", omarchy_body)
    from tests._pathutil import path_without
    bindir.mkdir(exist_ok=True)
    # the real `omarchy` is never run: only the stand-in, or nothing
    env = dict(os.environ, PATH=f"{bindir}:{path_without('omarchy', home, '/usr/bin:/bin')}", PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "dolly.cli", "update"], env=env, capture_output=True, text=True, timeout=60)


def test_update_runs_omarchys_own_command(home):
    r = run_update(home, 'echo "$@" > "$HOME/omarchy-args"')
    assert r.returncode == 0, r.stderr
    assert (home / "omarchy-args").read_text().strip() == f"plugin update {I.PLUGIN_ID} --yes"
    assert f"Dolly is up to date ({I.version()})." in r.stdout
    assert "Set up has not been run" in r.stdout                      # nothing to refresh on this machine


def test_update_says_what_it_refreshed(home):
    I.apply()
    I.stamp_file().write_text(json.dumps({"version": "0.0.1", "set_up": True}))
    (home / ".config/hypr/bindings.lua").write_text("-- mine" + NL)                  # an old block would be here; force a rewrite
    (home / ".config/omarchy/extensions/omarchy-menu.jsonc").write_text(
        "{" + NL + "  // >>> dolly (old)" + NL + '  "trigger.capture.dolly-record": {"label": "Record demo", "action": "'
        + str(I.launcher()) + ' record"},' + NL
        + "  // <<< dolly" + NL + "}" + NL)
    r = run_update(home, "exit 0")
    assert r.returncode == 0, r.stderr
    assert "Refreshed the menu entry." in r.stdout
    rows = (home / ".config/omarchy/extensions/omarchy-menu.jsonc").read_text()
    assert '"trigger.capture.dolly"' in rows and "dolly-record" not in rows


def test_update_stops_when_omarchy_fails_or_is_missing(home):
    r = run_update(home, "exit 3")
    assert r.returncode != 0 and "Omarchy could not update the plugin (exit code 3)" in r.stderr
    (home / "bin/omarchy").unlink()
    r = run_update(home, None)
    assert r.returncode != 0 and "omarchy plugin update io.github.jagritvats.dolly" in r.stderr

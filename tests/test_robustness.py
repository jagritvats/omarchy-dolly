"""Error paths a real user hits: unwritable folder, a second record, damaged project files,
a hung helper, unexpected errors (review items #3, #4, #8, #9, #10, #11)."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _env(tmp_path, **extra):
    """dolly as a subprocess, with notifications captured instead of shown."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "notify.log"
    p = bindir / "omarchy-notification-send"
    p.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "{log}"\necho --- >> "{log}"\n')
    p.chmod(0o755)
    e = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", PYTHONPATH=str(ROOT), DOLLY_SKIP_DEPS="1",
             DOLLY_DIR=str(tmp_path / "videos"))
    e.update(extra)
    return e, log


def _dolly(env, *args, timeout=60):
    return subprocess.run([sys.executable, "-m", "dolly.cli", *args], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout)


def _notified(log: Path, wait=5.0):
    import time
    end = time.time() + wait                     # toasts are sent from a detached process
    while time.time() < end:
        if log.exists() and "---" in log.read_text():
            break
        time.sleep(0.1)
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_unwritable_folder_fails_cleanly(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(stat.S_IRUSR | stat.S_IXUSR)        # 0500: read-only for us
    try:
        e, log = _env(tmp_path, DOLLY_DIR=str(ro / "Dolly"))
        r = _dolly(e, "record", "--mode", "screen", "--countdown", "0")
        assert r.returncode == 1, (r.returncode, r.stderr)
        assert "Traceback" not in r.stderr, r.stderr
        assert "cannot record into" in r.stderr
        lines = _notified(log)
        assert "Dolly can't record to this folder" in lines, lines
        i = lines.index("--action")
        assert lines[i + 1] == "Choose folder" and lines[i + 2].endswith("bin/dolly") and lines[i + 3] == "settings"
        assert list(ro.iterdir()) == []          # no half-made project folder
        run = Path(e["XDG_RUNTIME_DIR"]) / "dolly" / "phase.json"
        assert not run.exists() or json.loads(run.read_text()).get("phase") == "idle"
    finally:
        ro.chmod(0o700)


def test_second_record_says_already_recording(tmp_path):
    e, log = _env(tmp_path)
    from dolly.capture import state_path
    st = state_path()
    st.parent.mkdir(parents=True, exist_ok=True)
    sleeper = subprocess.Popen(["sleep", "30"])   # a live "recorder"
    try:
        st.write_text(json.dumps({"dir": str(tmp_path / "videos" / "x"), "recorder_pid": sleeper.pid}))
        r = _dolly(e, "record", "--mode", "screen", "--countdown", "0")
        assert r.returncode == 1, (r.returncode, r.stderr)
        assert "already recording" in r.stderr
        assert "Already recording" in _notified(log)
    finally:
        sleeper.kill()
        st.unlink(missing_ok=True)


def test_atomic_write_keeps_a_good_backup_only(tmp_path):
    from dolly import fsutil
    f = tmp_path / "project.json"
    fsutil.atomic_write_json(f, {"v": 1}, backup=True)
    assert not fsutil.backup_path(f).exists()             # nothing to back up yet
    fsutil.atomic_write_json(f, {"v": 2}, backup=True)
    assert json.loads(fsutil.backup_path(f).read_text()) == {"v": 1}
    f.write_text('{"v": 3')                                # damaged by a crash
    fsutil.atomic_write_json(f, {"v": 4}, backup=True)
    assert json.loads(fsutil.backup_path(f).read_text()) == {"v": 1}   # the damaged one never becomes the backup
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
    assert oct(f.stat().st_mode & 0o777) != oct(0o600)    # normal permissions, not mkstemp's 0600


def test_damaged_project_restores_from_backup(tmp_path, capsys):
    from dolly import fsutil
    from dolly.project import Project
    p = Project(tmp_path)
    p.save({"trim": [1.0, 2.0]})
    p.save({"trim": [1.5, 2.0]})
    p.file.write_text('{"trim": [1.5, ')                  # power loss mid-write (pre-atomic Dolly)
    data = p.load()
    assert data["trim"] == [1.0, 2.0]                      # the previous good version
    assert "restored the previous version" in capsys.readouterr().err
    assert json.loads(p.file.read_text())["trim"] == [1.0, 2.0]
    assert (tmp_path / "project.json.damaged").exists()
    (tmp_path / "project.json.bak").unlink()
    p.file.write_text("{oops")
    (tmp_path / "project.json.bak").write_text("also broken")
    with pytest.raises(fsutil.DamagedFile, match="damaged and has no usable backup"):
        p.load()


def test_export_of_a_damaged_recording_is_one_clear_line(tmp_path):
    e, log = _env(tmp_path)
    proj = tmp_path / "videos" / "2026-11-10_00-00-00"
    proj.mkdir(parents=True)
    (proj / "raw.mkv").write_bytes(b"\0" * 64)
    (proj / "events.json").write_text('{"video": {"w": 19')        # truncated
    r = _dolly(e, "export", str(proj), "-q")
    assert r.returncode == 1, (r.returncode, r.stderr[-400:])
    assert "Traceback" not in r.stderr, r.stderr[-800:]
    assert "damaged and has no usable backup" in r.stderr
    assert "Dolly ran into a problem" in _notified(log)   # stderr is not a TTY: a toast too


def test_unexpected_error_leaves_a_traceback_file(monkeypatch, capsys):
    from dolly import cli, log
    monkeypatch.setattr(cli.projmod, "latest_project", lambda: 1 / 0)
    with pytest.raises(SystemExit) as ex:
        cli.main(["last"])
    assert ex.value.code == 1
    err = capsys.readouterr().err
    assert "unexpected error (ZeroDivisionError" in err and "dolly doctor" in err
    tb = log.last_error_path().read_text()
    assert "ZeroDivisionError" in tb and "Traceback" in tb and "dolly last" in tb


def test_known_errors_are_actionable():
    import errno
    from dolly import cli
    assert "is not installed" in cli._known_error(FileNotFoundError(2, "No such file", "wf-recorder"))
    assert "disk is full" in cli._known_error(OSError(errno.ENOSPC, "No space left", "/v/raw.mkv"))
    assert "permission denied" in cli._known_error(PermissionError(13, "denied", "/v"))
    assert "did not respond within 3 s" in cli._known_error(subprocess.TimeoutExpired(["pactl", "info"], 3))
    assert cli._known_error(ZeroDivisionError()) is None


def test_hung_pactl_does_not_hang_recording(monkeypatch):
    from dolly import capture

    def hang(*a, **kw):
        assert kw.get("timeout"), "pactl called without a timeout"
        raise subprocess.TimeoutExpired(a[0], kw["timeout"])
    monkeypatch.setattr(capture.subprocess, "run", hang)
    assert capture._pactl_out("get-default-sink") == ""


def test_swallowed_errors_are_logged():
    from dolly import log
    try:
        raise ValueError("probe failed: test marker 7f3a")
    except ValueError:
        log.swallowed("unit-test")
    for h in log.get().handlers:
        h.flush()
    text = (log.state_dir() / "dolly.log").read_text()
    assert "ignored ValueError in unit-test" in text and "7f3a" in text

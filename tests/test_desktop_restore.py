"""A recording leaves the desktop as it found it: Do Not Disturb, Omarchy's Stay Awake
(no screensaver or lock inside a take), solid windows, key repeat. What a killed recorder
or a killed Dolly leaves behind is undone by the recorder's watcher, by the next
recording, by `dolly recover`, and is listed by `dolly doctor`."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import health  # noqa: E402


@pytest.fixture
def desk(monkeypatch, tmp_path):
    """A private state folder and a fake notification service: {"dnd": bool, "calls": [...]}."""
    # Dolly's log is one logger per process, bound to the state folder it sees when first
    # used: let that be the test session's own folder, before this test moves the state
    # folder, so the tests that run afterwards still find their log where they expect it
    from dolly import log
    log.get()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    monkeypatch.delenv("DOLLY_DND", raising=False)
    monkeypatch.delenv("DOLLY_STAY_AWAKE", raising=False)
    box = {"dnd": False, "calls": []}

    def shell(*args, timeout=1.5):
        box["calls"].append(args)
        if args[:2] == ("notifications", "isDnd"):
            return "on" if box["dnd"] else "off"
        if args[:2] == ("notifications", "setDnd"):
            box["dnd"] = args[2] == "on"
            return "ok"
        return None
    monkeypatch.setattr(health, "_shell", shell)
    return box


# ------------------------------------------------------------------ Stay Awake
def test_stay_awake_is_omarchys_marker_with_dollys_owner_line(desk):
    f = health.stay_awake_file()
    assert f.parts[-3:] == ("omarchy", "indicators", "stay-awake") and not f.exists()
    token = health.awake_begin()
    assert token and token.startswith("dolly:") and f.read_text().strip() == token
    assert health.ledger().get("awake") == token
    assert health.awake_end(token) is True
    assert not f.exists() and "awake" not in health.ledger()


def test_a_marker_that_is_not_dollys_is_never_touched(desk):
    f = health.stay_awake_file()
    f.parent.mkdir(parents=True)
    f.write_text("13855:10641:9663" + chr(10))           # Omarchy's updater, or the user's own Stay Awake
    assert health.awake_begin() is None
    assert health.awake_end("dolly:1:2") is False
    assert f.read_text().strip() == "13855:10641:9663"
    assert health.leftovers() == [] and health.heal() == []
    assert f.exists()


def test_the_user_switching_it_off_during_a_take_is_respected(desk):
    token = health.awake_begin()
    health.stay_awake_file().unlink()                    # the bar's Stay Awake indicator was clicked
    assert health.awake_end(token) is False and not health.stay_awake_file().exists()
    assert "awake" not in health.ledger()


def test_stay_awake_can_be_switched_off(desk, monkeypatch):
    monkeypatch.setenv("DOLLY_STAY_AWAKE", "0")
    assert health.awake_begin() is None and not health.stay_awake_file().exists()


# ------------------------------------------------------------------ leftovers and healing
def test_a_killed_dolly_leaves_traces_that_heal_undoes(desk):
    prev = health.dnd_begin()                            # ... and Dolly is killed before any state file exists
    token = health.awake_begin()
    assert prev is False and desk["dnd"] is True and token
    left = health.leftovers()
    assert any("Do Not Disturb" in x for x in left) and any("Stay Awake" in x for x in left)
    done = health.heal()
    assert desk["dnd"] is False and not health.stay_awake_file().exists()
    assert any("Do Not Disturb" in x for x in done) and any("Stay Awake" in x for x in done)
    assert health.leftovers() == [] and health.ledger() == {} and health.heal() == []


def test_a_normal_take_leaves_nothing(desk):
    prev, token = health.dnd_begin(), health.awake_begin()
    health.dnd_end(prev)
    health.awake_end(token)
    assert desk["dnd"] is False and health.leftovers() == [] and health.ledger() == {}


def test_dnd_that_was_on_before_the_take_stays_on(desk):
    desk["dnd"] = True
    prev = health.dnd_begin()
    assert prev is True and health.ledger() == {}
    health.dnd_end(prev)
    assert desk["dnd"] is True and health.heal() == []


@pytest.mark.parametrize("phase", ["starting", "countdown", "finalizing"])
def test_nothing_is_healed_while_a_take_is_in_progress(desk, phase):
    health.dnd_begin()
    token = health.awake_begin()
    health.set_phase(phase)                              # this process is the one starting / stopping
    assert health.leftovers() == [] and health.heal() == []
    assert desk["dnd"] is True and health.stay_awake_file().read_text().strip() == token
    health.set_phase("idle")
    assert len(health.heal()) == 2


def test_a_running_recorder_is_a_take_in_progress(desk, monkeypatch):
    from dolly import capture
    health.dnd_begin()
    monkeypatch.setattr(capture, "read_state", lambda: {"recorder_pid": 1, "dir": "/x"})
    assert health.leftovers() == [] and health.heal() == [] and desk["dnd"] is True


def test_key_repeat_is_healed_but_not_from_inside_a_scripted_take(desk, monkeypatch):
    from dolly import hypr
    f = hypr._repeat_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("250")
    restored = []

    def restore():
        restored.append(1)
        f.unlink()
        return True
    monkeypatch.setattr(hypr, "restore_key_repeat", restore)
    assert any("key repeat" in x for x in health.leftovers())
    assert health.heal(key_repeat=False) == [] and f.exists()       # `dolly record` started by `dolly run`
    assert any("key repeat" in x for x in health.heal()) and restored == [1]


def test_doctor_lists_leftovers(desk, monkeypatch):
    from dolly import doctor
    health.dnd_begin()
    health.awake_begin()
    rep = doctor.check()
    row = next(c for c in rep["checks"] if c["name"] == "desktop left as found")
    assert row["level"] != "ok" and "Do Not Disturb" in row["detail"] and "dolly recover" in row["detail"]
    health.heal()
    row = next(c for c in doctor.check()["checks"] if c["name"] == "desktop left as found")
    assert row["level"] == "ok"


# ------------------------------------------------------------------ the recorder's watcher
def test_the_watcher_waits_for_the_recorder_then_heals(monkeypatch, desk):
    got = {}
    monkeypatch.setattr(health.subprocess, "Popen", lambda cmd, **kw: got.update(cmd=cmd, kw=kw))
    health.watch_recorder(4242)
    assert got["cmd"][:2] == ["sh", "-c"] and "tail --pid=" in got["cmd"][2] and "dolly.health heal" in got["cmd"][2]
    assert got["cmd"][-2:] == ["4242", sys.executable] and got["kw"]["start_new_session"] is True


def test_a_killed_recorder_is_cleaned_up_after_without_anyone_starting_dolly(desk, tmp_path):
    """The real thing: a "recorder" is killed; a few seconds later its watcher has put the
    desktop back (here: Dolly's Stay Awake marker), with no Dolly command run by anyone."""
    rec = subprocess.Popen(["sleep", "60"])
    try:
        token = health.awake_begin()
        assert token and health.stay_awake_file().exists()
        health.watch_recorder(rec.pid)
        time.sleep(0.5)
        assert health.stay_awake_file().exists()         # the recorder is alive: nothing is touched
        rec.kill()
        rec.wait()
        end = time.monotonic() + 20
        while health.stay_awake_file().exists() and time.monotonic() < end:
            time.sleep(0.25)
        assert not health.stay_awake_file().exists()
        assert json.loads(json.dumps(health.ledger())).get("awake") is None
    finally:
        if rec.poll() is None:
            rec.kill()


def test_heal_command_line(desk):
    assert health.main(["nonsense"]) == 2
    health.awake_begin()
    assert health.main(["heal"]) == 0 and not health.stay_awake_file().exists()

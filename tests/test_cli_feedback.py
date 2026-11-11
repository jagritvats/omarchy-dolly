"""Every command says what happened, or why not (VM, Nov 10: `dolly replay start` exited 1
with no message; `dolly status`, `dolly mark` and `dolly pause` printed nothing either way).
Also: a recorder that was killed still gives the right length."""
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import capture  # noqa: E402
from tests.test_system import dolly, env, status  # noqa: E402,F401  (stub hyprctl + recorder)


def test_status_mark_and_record_say_what_happened(env):
    r = dolly(env, "status", check=False)
    assert r.returncode == 1 and r.stdout.strip() == "not recording"
    rec = dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "30")
    pdir = rec.stdout.strip().splitlines()[-1]
    assert Path(pdir).is_dir() and len(rec.stdout.strip().splitlines()) == 1     # stdout stays one line: the folder
    assert "recording to" in rec.stderr and "dolly stop" in rec.stderr
    time.sleep(1.2)
    r = dolly(env, "status", check=False)
    assert r.returncode == 0 and re.fullmatch(r"recording \d+:\d\d", r.stdout.strip())
    r = dolly(env, "mark", check=False)
    assert r.returncode == 0 and re.fullmatch(r"marked a zoom point at \d+:\d\d", r.stdout.strip())
    r = dolly(env, "mute", check=False)
    assert r.returncode == 0 and r.stdout.strip() == "microphone muted"
    assert dolly(env, "status", check=False).stdout.strip().endswith(", microphone muted")
    dolly(env, "stop")
    assert dolly(env, "status", check=False).stdout.strip() == "not recording"


def test_nothing_to_act_on_is_said_too(env):
    for cmd in ("mark", "pause", "mute", "stop"):
        r = dolly(env, cmd, check=False)
        assert r.returncode == 1 and "not recording" in r.stderr, cmd
    r = dolly(env, "cancel")
    assert "nothing to cancel" in r.stdout
    r = dolly(env, "replay", "stop")
    assert r.stdout.strip() == "replay buffer is not running"


def test_record_takes_the_frame_rates_settings_offers(env):
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "120", check=False)
    assert r.returncode == 2 and "invalid choice" in r.stderr and "30" in r.stderr and "60" in r.stderr
    assert not status(env)["recording"]
    from dolly import config
    fps = next(s for s in config.SCHEMA if s["key"] == "capture.fps")
    assert fps["options"] == ["30", "60"]


def test_a_killed_recorder_still_gives_the_right_length(env):
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "30")
    pdir = Path(r.stdout.strip().splitlines()[-1])
    time.sleep(2.5)
    os.kill(json.loads((pdir / "capture.json").read_text())["recorder_pid"], signal.SIGKILL)
    for _ in range(80):
        if (pdir / "events.json").exists() and status(env)["phase"] == "idle":
            break
        time.sleep(0.25)
    s = status(env)
    assert s["phase"] == "idle" and s["jobs"] == [] and not s["recording"]       # nothing left "rendering"
    assert json.loads((pdir / "capture.json").read_text())["stop_reason"] == "recorder-died"
    assert capture.video_info(pdir / "raw.mkv")["duration"] > 1.5                 # the container was repaired
    assert (pdir / "raw.mkv.damaged").exists()
    details = capture._recording_details(pdir)
    assert re.match(r"0:0[1-9]", details), details                                # was "0:00" for a 10 s take
    lib = json.loads(dolly(env, "library", "--json").stdout)
    e = next(x for x in lib["items"] if x["dir"] == str(pdir))
    assert e["partial"] and e["stop_reason"] == "recorder-died" and e["duration"] > 1.5

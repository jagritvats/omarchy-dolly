"""Commands that cannot work on this machine say why (stubs as in a VM: gpu-screen-recorder
exits at once, wf-recorder works)."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_capture_fallback import dolly, env  # noqa: E402,F401


def test_pause_on_wf_recorder_says_why(env):
    dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "30")
    time.sleep(1.0)
    r = dolly(env, "pause", check=False)
    assert r.returncode == 2 and "cannot pause" in r.stderr and "wf-recorder" in r.stderr
    dolly(env, "stop")


def test_replay_that_cannot_start_says_why(env):
    r = dolly(env, "replay", "start", check=False)
    assert r.returncode == 1
    assert "instant replay could not start" in r.stderr and "recorder.log" in r.stderr
    r = dolly(env, "replay", "status", check=False)
    assert r.returncode == 1

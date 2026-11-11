"""`dolly stop --json` during the countdown says the take was cancelled; it does not
describe a folder that no longer exists."""
import json
import types

from dolly import capture, headless


def test_stop_json_during_countdown_reports_cancelled(monkeypatch, capfd, tmp_path):
    gone = tmp_path / "2026-11-11_00-00-00"
    stopped = []
    monkeypatch.setattr(headless, "adopt_session_env", lambda *a, **k: {})
    monkeypatch.setattr(capture, "read_state", lambda: {"dir": str(gone), "preroll": True})
    monkeypatch.setattr(capture, "_counting_down", lambda st: True)
    monkeypatch.setattr(capture, "stop", lambda a=None: stopped.append(a))
    headless.stop_json(types.SimpleNamespace(json=True, reason=None))
    out = json.loads(capfd.readouterr().out)
    assert stopped and out == {"ok": True, "recording": False, "cancelled": True,
                               "message": "stopped during the countdown: nothing was recorded"}
    assert "dir" not in out and not gone.exists()

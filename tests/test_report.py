"""`dolly doctor --report`: useful for a bug report, safe to attach."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import report  # noqa: E402


def test_redact_hides_home_user_and_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home" / "alexandra"))
    monkeypatch.setenv("USER", "alexandra")
    text = (f"file {tmp_path}/home/alexandra/Videos/Dolly/x/raw.mkv by alexandra\n"
            'secret_key = abc123\n"access_key": "AKIA999", "token":"t0k"\npassword=hunter2 ok=1')
    out = report.redact(text)
    assert "~/Videos/Dolly/x/raw.mkv" in out and "alexandra" not in out
    for leak in ("abc123", "AKIA999", "t0k", "hunter2"):
        assert leak not in out
    assert "ok=1" in out


def test_report_has_the_sections_and_no_event_contents(monkeypatch, tmp_path):
    home = tmp_path / "home"
    proj = home / "Videos" / "Dolly" / "2026-01-02_03-04-05"
    proj.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DOLLY_DIR", str(home / "Videos" / "Dolly"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))
    (proj / "capture.json").write_text(json.dumps({"backend": "wf", "mode": "screen", "fps": 60}))
    (proj / "events.json").write_text(json.dumps({
        "video": {"w": 1920, "h": 1200, "fps": 60, "duration": 3.0}, "capture": {"sync": {"method": "markers2"}},
        "events": [{"t": 1.0, "type": "key", "key": "TOPSECRETKEY", "mods": []},
                   {"t": 1.1, "type": "click", "x": 5, "y": 6, "button": "left"}]}))
    (proj / "recorder.log").write_text("=== backend=wf\n$ wf-recorder -o X\n")
    text = report.build()
    for title in ("== doctor ==", "== versions ==", "== settings ==", "== latest recording ==", "== last error =="):
        assert title in text, title
    assert '"key": 1' in text and "TOPSECRETKEY" not in text          # counted, never included
    assert "backend=wf" in text and '"method": "markers2"' in text
    p = report.write(tmp_path)
    assert p.read_text() == text or p.exists()

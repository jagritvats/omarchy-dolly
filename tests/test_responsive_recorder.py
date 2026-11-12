"""The software recorder encodes lightly by default: a recorder that cannot keep up makes
the whole desktop, the pointer first, drag while a take runs (measured 12 Nov, see
capture.wf_preset)."""
import types
from pathlib import Path

from dolly import capture, config
from dolly.monitors import Target

MON = Target("monitor", "DP-1", [0, 0, 1920, 1080], 1.0, (1920.0, 1080.0), physical=(1920, 1080))


def preset(cmd):
    return next(c for c in cmd if c.startswith("preset="))


def test_light_encoding_is_the_default(monkeypatch):
    monkeypatch.delenv("DOLLY_WF_PRESET", raising=False)
    cmd = capture.recorder_cmd("wf", MON, Path("/tmp/x.mkv"), 60, [])
    assert preset(cmd) == "preset=ultrafast" and "crf=16" in cmd and cmd[cmd.index("-r") + 1] == "60"
    assert preset(capture.recorder_cmd("wf", MON, Path("/tmp/x.mkv"), 60, [], light=False)) == "preset=veryfast"


def test_the_environment_can_name_a_preset(monkeypatch):
    monkeypatch.setenv("DOLLY_WF_PRESET", "superfast")
    assert preset(capture.recorder_cmd("wf", MON, Path("/tmp/x.mkv"), 60, [])) == "preset=superfast"
    monkeypatch.setenv("DOLLY_WF_PRESET", "placebo; rm -rf")          # not a preset: ignored
    assert preset(capture.recorder_cmd("wf", MON, Path("/tmp/x.mkv"), 60, [])) == "preset=ultrafast"


def test_the_gpu_recorder_is_untouched(monkeypatch):
    monkeypatch.delenv("DOLLY_WF_PRESET", raising=False)
    a = capture.recorder_cmd("gsr", MON, Path("/tmp/x.mp4"), 60, [])
    b = capture.recorder_cmd("gsr", MON, Path("/tmp/x.mp4"), 60, [], light=False)
    assert a == b and not any("preset" in c for c in a)


def test_it_is_a_setting_on_by_default_and_reaches_the_take(monkeypatch, tmp_path):
    spec = next(s for s in config.SCHEMA if s["key"] == "capture.responsive")
    assert spec["type"] == "bool" and spec["default"] is True
    assert isinstance(config.record_defaults()["responsive"], bool)
    from dolly import cli
    monkeypatch.setattr(config, "record_defaults", lambda cfg=None: {
        "mode": "screen", "countdown": 0, "fps": 60, "keys": True, "responsive": False, "audio": "none",
        "mic_device": "default", "system_device": "default", "camera": "off", "camera_device": "",
        "solid": "auto", "dnd": True})
    a = types.SimpleNamespace(mode=None, countdown=None, fps=None, no_keys=None, solid=None, no_dnd=None,
                              audio=None, camera=None)
    cli.apply_record_defaults(a)
    assert a.responsive is False


def test_doctor_says_which_encoding_the_software_recorder_uses():
    src = (Path(capture.__file__).parent / "doctor.py").read_text()
    assert "light encoding" in src and "Keep the desktop responsive" in src
    page = (Path(capture.__file__).parent / "editor" / "static" / "index.html").read_text()
    assert "'capture.responsive'" in page

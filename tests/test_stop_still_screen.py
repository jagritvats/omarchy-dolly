"""Stopping on a still screen: wf-recorder needs one more frame to notice SIGINT, so Stop
repaints a corner (11 Nov: without it Stop took 13 s instead of 3 on a still desktop)."""
import inspect

from dolly import capture


def test_nudge_maps_and_unmaps_the_transparent_marker_surface(monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "_have", lambda name: True)
    monkeypatch.setattr(capture.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    capture._nudge_screen()
    assert calls == [["omarchy-shell", "io.github.jagritvats.dolly", "syncArm", "{}"],
                     ["omarchy-shell", "io.github.jagritvats.dolly", "syncDisarm"]]


def test_no_shell_no_call(monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "_have", lambda name: False)
    monkeypatch.setattr(capture.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    capture._nudge_screen()
    assert calls == []


def test_stop_nudges_while_the_software_recorder_is_still_alive():
    src = inspect.getsource(capture.stop)
    assert '_nudge_screen(st.get("region"))' in src and 'st.get("backend") == "wf"' in src
    assert capture.NUDGE_AT[0] <= 5 and max(capture.NUDGE_AT) < 500      # early, and before the kill


def test_the_corner_repainted_is_on_the_monitor_being_recorded(monkeypatch):
    """Two monitors, 11 Nov: the repaint was always at 0,0, on the first monitor, so a still
    take of the second one still waited for the recorder (6.6 s instead of about 2)."""
    import json
    calls = []
    monkeypatch.setattr(capture, "_have", lambda name: True)
    monkeypatch.setattr(capture.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    capture._nudge_screen([1920, 0, 1920, 1200])
    assert calls[0][:3] == ["omarchy-shell", "io.github.jagritvats.dolly", "syncArm"]
    assert json.loads(calls[0][3]) == {"x": 1920, "y": 0, "size": 40}
    calls.clear()
    capture._nudge_screen([-2560, 300, 800, 600])                    # a region on a monitor left of the first
    assert json.loads(calls[0][3]) == {"x": -2560, "y": 300, "size": 40}

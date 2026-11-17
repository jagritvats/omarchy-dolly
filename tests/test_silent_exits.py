"""Nothing ends silently when Dolly is started from the bar or a hotkey (0.8.1): window mode
with the desktop picked, hotkeys with nothing to act on, and any other exit with a message."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import capture, cli  # noqa: E402
from dolly.i18n import t  # noqa: E402
from dolly.monitors import Target  # noqa: E402

MON = {"name": "DP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0, "transform": 0,
       "focused": True, "activeWorkspace": {"id": 1}}
DESKTOP = Target("monitor", "DP-1", [0, 0, 1920, 1080], 1.0, (1920.0, 1080.0), 0, "", (1920, 1080))
WIN = {"address": "0xa", "at": [100, 100], "size": [800, 600], "mapped": True, "workspace": {"id": 1}}


@pytest.fixture
def toasts(monkeypatch):
    shown = []
    monkeypatch.setattr(capture, "notify", lambda title, body="", *actions, **kw: shown.append((title, body, actions)))
    monkeypatch.setattr(capture, "NOTIFIED", False)
    from dolly import health
    monkeypatch.setattr(health, "set_phase", lambda *a, **k: None)
    return shown


class _NoTTY:
    def __init__(self):
        self.text = ""

    def isatty(self):
        return False

    def write(self, s):
        self.text += s

    def flush(self):
        pass


def test_a_desktop_pick_in_window_mode_says_so_and_offers_the_screen(monkeypatch, toasts):
    monkeypatch.setattr(capture, "hypr_json", lambda what: {"clients": [WIN], "monitors": [MON]}[what])
    with pytest.raises(SystemExit) as e:
        capture.follow_window(DESKTOP)
    assert "desktop" in str(e.value.code)
    (title, body, actions), = toasts
    assert title == t("notify.pickedDesktopTitle") and "desktop" in body
    assert actions[0][0] == t("notify.recordScreen") and actions[0][-3:] == ("record", "--mode", "screen")


def test_a_window_that_went_away_is_said_too(monkeypatch, toasts):
    monkeypatch.setattr(capture, "hypr_json", lambda what: {"clients": [], "monitors": [MON]}[what])
    region = Target("region", "DP-1", [100, 100, 800, 600], 1.0, (800.0, 600.0), 0, "", (800, 600))
    with pytest.raises(SystemExit):
        capture.follow_window(region)
    assert [x[0] for x in toasts] == [t("notify.windowGoneTitle")]


def test_a_picked_window_is_followed(monkeypatch, toasts):
    monkeypatch.setattr(capture, "hypr_json", lambda what: {"clients": [WIN], "monitors": [MON]}[what])
    region = Target("region", "DP-1", [100, 100, 800, 600], 1.0, (800.0, 600.0), 0, "", (800, 600))
    addr, tgt = capture.follow_window(region)
    assert addr == "0xa" and tgt.kind == "monitor" and not toasts


@pytest.mark.parametrize("cmd,body", [(capture.pause, "notify.nothingToPause"), (capture.mute, "notify.nothingToMute"),
                                      (capture.mark, "notify.nothingToMark")])
def test_a_hotkey_with_nothing_to_act_on_shows_a_toast(monkeypatch, toasts, cmd, body):
    monkeypatch.setattr(capture, "_live_state", lambda: None)
    err = _NoTTY()
    monkeypatch.setattr(sys, "stderr", err)
    with pytest.raises(SystemExit) as e:
        cmd()
    assert e.value.code == 1 and "not recording" in err.text
    assert [(x[0], x[1]) for x in toasts] == [(t("notify.notRecordingTitle"), t(body))]


def test_any_other_exit_with_a_message_becomes_a_toast_once(monkeypatch, toasts):
    monkeypatch.setattr(sys, "stderr", _NoTTY())
    cli._toast_unseen_exit(SystemExit("dolly: no region picker (omarchy-capture-region or slurp)"), ["record"])
    assert [x[1] for x in toasts] == ["no region picker (omarchy-capture-region or slurp)"]
    toasts.clear()
    monkeypatch.setattr(capture, "NOTIFIED", True)          # it already told the user
    cli._toast_unseen_exit(SystemExit("dolly: anything"), ["record"])
    monkeypatch.setattr(capture, "NOTIFIED", False)
    cli._toast_unseen_exit(SystemExit("dolly: anything"), ["record", "--json"])     # scripts read JSON
    cli._toast_unseen_exit(SystemExit("dolly: anything"), ["status"])               # quiet commands
    cli._toast_unseen_exit(SystemExit(1), ["record"])                                # no message
    assert toasts == []


def test_a_terminal_gets_the_line_and_no_toast(monkeypatch, toasts):
    class TTY(_NoTTY):
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stderr", TTY())
    cli._toast_unseen_exit(SystemExit("dolly: anything"), ["record"])
    monkeypatch.setattr(capture, "_live_state", lambda: None)
    with pytest.raises(SystemExit):
        capture.pause()
    assert toasts == []

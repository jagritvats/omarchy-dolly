"""A new take dismisses Dolly's own "Demo recorded" card, and only that."""
from dolly import capture


class Out:
    def __init__(self, stdout):
        self.stdout = stdout


def test_only_dollys_own_card_is_dismissed(monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "_have", lambda name: True)
    monkeypatch.setattr(capture.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Out("ok"))
    assert capture._dismiss_own_toasts() is True
    assert calls == [["omarchy-shell", "notifications", "dismiss", capture.t("notify.recorded")]]


def test_nothing_to_dismiss(monkeypatch):
    monkeypatch.setattr(capture, "_have", lambda name: True)
    monkeypatch.setattr(capture.subprocess, "run", lambda cmd, **kw: Out("none"))
    assert capture._dismiss_own_toasts() is False


def test_no_shell_no_call(monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "_have", lambda name: False)
    monkeypatch.setattr(capture.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    assert capture._dismiss_own_toasts() is False
    assert calls == []

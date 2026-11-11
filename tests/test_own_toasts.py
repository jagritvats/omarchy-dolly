"""A new take dismisses Dolly's own "Demo recorded" card, and only that."""
from dolly import capture


def test_only_dollys_own_card_is_dismissed(monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "_have", lambda name: True)
    monkeypatch.setattr(capture.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    capture._dismiss_own_toasts()
    assert calls == [["omarchy-shell", "-q", "notifications", "dismiss", capture.t("notify.recorded")]]


def test_no_shell_no_call(monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "_have", lambda name: False)
    monkeypatch.setattr(capture.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    capture._dismiss_own_toasts()
    assert calls == []

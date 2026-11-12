"""A new take dismisses Dolly's own cards, and only those."""
from dolly import capture


class Out:
    def __init__(self, stdout):
        self.stdout = stdout


def patched(monkeypatch, stdout="ok", have=True):
    runs, popens = [], []
    monkeypatch.setattr(capture, "_have", lambda name: have)
    monkeypatch.setattr(capture.subprocess, "run", lambda cmd, **kw: runs.append(cmd) or Out(stdout))
    monkeypatch.setattr(capture.subprocess, "Popen", lambda cmd, **kw: popens.append(cmd))
    return runs, popens


def test_demo_recorded_is_dismissed_and_its_answer_used(monkeypatch):
    runs, _ = patched(monkeypatch)
    assert capture._dismiss_own_toasts() is True
    assert runs == [["omarchy-shell", "notifications", "dismiss", capture.t("notify.recorded")]]


def test_problem_cards_go_in_the_background(monkeypatch):
    runs, popens = patched(monkeypatch)
    capture._dismiss_own_toasts()
    assert len(runs) == 1 and len(popens) == 1          # Record waits for one call, as before
    cmd = popens[0]
    assert cmd[:2] == ["sh", "-c"] and "notifications dismiss" in cmd[2]
    titles = cmd[4:]
    for key in ("notify.nothingRecorded", "notify.recoverFailed", "notify.analysisFailed", "notify.cannotStart"):
        assert capture.t(key) in titles
    assert "Recording stopped:" in titles                # whatever the reason after the colon
    assert capture.t("notify.recorded") not in titles
    assert all(len(s) >= 12 for s in titles)             # nothing short enough to hit another program's card


def test_nothing_to_dismiss(monkeypatch):
    patched(monkeypatch, stdout="none")
    assert capture._dismiss_own_toasts() is False


def test_no_shell_no_call(monkeypatch):
    runs, popens = patched(monkeypatch, have=False)
    assert capture._dismiss_own_toasts() is False
    assert runs == [] and popens == []

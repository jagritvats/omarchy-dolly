"""Do Not Disturb: a take turns it on only when it was off, and always puts it back off,
even when the shell answers late (11 Nov: a late answer left DND on for half an hour)."""
from dolly import health


def fake(monkeypatch, states, set_ok=True):
    """states: what isDnd answers, call by call (the last one repeats)."""
    calls, popen = [], []
    seq = list(states)

    def shell(*args, timeout=1.5):
        calls.append(args)
        if args[1] == "isDnd":
            v = seq.pop(0) if len(seq) > 1 else seq[0]
            return v
        return "ok" if set_ok else None            # None = the call timed out or failed
    monkeypatch.setattr(health, "_shell", shell)
    monkeypatch.setattr(health.subprocess, "Popen", lambda *a, **k: popen.append(a[0]))
    monkeypatch.delenv("DOLLY_DND", raising=False)
    return calls, popen


def test_a_late_answer_still_means_dolly_turned_it_on(monkeypatch):
    calls, _ = fake(monkeypatch, ["off"], set_ok=False)
    assert health.dnd_begin() is False             # not None: the end of the take must undo it
    assert ("notifications", "setDnd", "on") in calls


def test_dnd_that_was_already_on_or_unknown_is_left_alone(monkeypatch):
    calls, _ = fake(monkeypatch, ["on"])
    assert health.dnd_begin() is True and not any(c[1] == "setDnd" for c in calls)
    calls, _ = fake(monkeypatch, [None])
    assert health.dnd_begin() is None and not any(c[1] == "setDnd" for c in calls)
    health.dnd_end(True)
    health.dnd_end(None)
    assert not any(c[1] == "setDnd" for c in calls)


def test_the_end_puts_it_back_and_checks(monkeypatch):
    calls, popen = fake(monkeypatch, ["off"])
    health.dnd_end(False)
    assert calls == [("notifications", "setDnd", "off"), ("notifications", "isDnd")] and popen == []


def test_an_unconfirmed_restore_keeps_trying_in_the_background(monkeypatch):
    for answer in ("on", None):                     # still on, or the shell did not answer
        calls, popen = fake(monkeypatch, [answer], set_ok=False)
        health.dnd_end(False)
        assert len(popen) == 1 and popen[0][0] == "sh" and "setDnd off" in popen[0][2]


def test_setting_off_disables_all_of_it(monkeypatch):
    calls, _ = fake(monkeypatch, ["off"])
    monkeypatch.setenv("DOLLY_DND", "0")
    assert health.dnd_begin() is None and calls == []

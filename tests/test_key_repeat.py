"""Scripted takes hold key repeat off and always put it back."""
from dolly import hypr


def _fake(monkeypatch, tmp_path, start=250):
    box = {"delay": start, "sets": []}
    monkeypatch.setattr(hypr, "key_repeat_delay", lambda: box["delay"])

    def setd(ms):
        box["delay"] = ms
        box["sets"].append(ms)
        return True
    monkeypatch.setattr(hypr, "set_key_repeat_delay", setd)
    monkeypatch.setattr(hypr, "_repeat_file", lambda: tmp_path / "key-repeat")
    return box


def test_hold_then_restore(monkeypatch, tmp_path):
    box = _fake(monkeypatch, tmp_path)
    assert hypr.hold_key_repeat() and box["delay"] == hypr.HELD_REPEAT_DELAY
    assert (tmp_path / "key-repeat").read_text() == "250"
    assert hypr.restore_key_repeat() and box["delay"] == 250
    assert not (tmp_path / "key-repeat").exists()
    assert not hypr.restore_key_repeat()          # nothing left to restore


def test_a_killed_take_is_healed_by_the_next_hold(monkeypatch, tmp_path):
    box = _fake(monkeypatch, tmp_path)
    hypr.hold_key_repeat()                        # ... and the process dies here
    assert hypr.hold_key_repeat()                 # the next take restores 250 first, then holds
    assert (tmp_path / "key-repeat").read_text() == "250"
    hypr.restore_key_repeat()
    assert box["delay"] == 250


def test_unknown_delay_is_left_alone(monkeypatch, tmp_path):
    box = _fake(monkeypatch, tmp_path, start=None)
    assert not hypr.hold_key_repeat() and box["sets"] == []

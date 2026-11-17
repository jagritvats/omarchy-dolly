"""inject.Desktop.clients: no answer from Hyprland is not "no windows"."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dolly import inject  # noqa: E402


def test_a_missed_answer_is_asked_again(monkeypatch):
    answers = [None, None, [{"address": "0x1", "class": "foot"}]]
    monkeypatch.setattr(inject, "hypr_json", lambda cmd: answers.pop(0))
    monkeypatch.setattr(inject.time, "sleep", lambda s: None)
    d = inject.Desktop.__new__(inject.Desktop)
    assert d.window("0x1") == {"address": "0x1", "class": "foot"}


def test_an_empty_desktop_is_believed_at_once(monkeypatch):
    calls = []
    monkeypatch.setattr(inject, "hypr_json", lambda cmd: calls.append(cmd) or [])
    d = inject.Desktop.__new__(inject.Desktop)
    assert d.window("0x1") is None and len(calls) == 1


def test_no_answer_at_all_ends(monkeypatch):
    monkeypatch.setattr(inject, "hypr_json", lambda cmd: None)
    monkeypatch.setattr(inject.time, "sleep", lambda s: None)
    d = inject.Desktop.__new__(inject.Desktop)
    assert d.clients() == []

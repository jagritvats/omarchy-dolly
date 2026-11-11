"""A running Omarchy shell keeps the bar button it has loaded (its plugin reload cannot
drop QML), so after an update Dolly says so and offers a shell restart."""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import cli  # noqa: E402
from dolly import integration as I  # noqa: E402


def test_a_process_start_time_is_read_from_proc():
    t = I._proc_start(os.getpid())
    assert t is not None and 0 <= time.time() - t < 6 * 3600
    assert I._proc_start(2 ** 22 + 12345) is None            # no such process


def test_the_bar_is_stale_when_its_files_are_newer_than_the_shell(monkeypatch):
    newest = max((ROOT / n).stat().st_mtime for n in ("BarWidget.qml", "RecordPanel.qml"))
    monkeypatch.setattr(I, "_shell_pid", lambda: 123)
    monkeypatch.setattr(I, "_proc_start", lambda pid: newest - 100)
    assert I.ui_stale() is True
    assert I.ui_stale(since=newest + 5) is False             # Set up ran after these files arrived
    monkeypatch.setattr(I, "_proc_start", lambda pid: newest + 100)
    assert I.ui_stale() is False                             # the shell started after the update
    monkeypatch.setattr(I, "_shell_pid", lambda: None)
    assert I.ui_stale() is False                             # no shell running: nothing to say


def test_the_update_card_offers_a_restart_only_when_the_bar_is_stale(monkeypatch):
    from dolly import capture
    cards = []
    monkeypatch.setattr(capture, "notify",
                        lambda title, body="", *actions, **kw: cards.append((title, body, actions)))
    cli._say_refreshed(None)
    cli._say_refreshed({"version": "9.9", "changed": [], "ui_stale": False})
    assert cards == []                                        # nothing changed: no card
    cli._say_refreshed({"version": "9.9", "changed": ["menu"], "ui_stale": False})
    assert cards[-1] == ("Dolly 9.9", "Refreshed the menu entry.", ())
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    cli._say_refreshed({"version": "9.9", "changed": ["menu"], "ui_stale": True})
    title, body, actions = cards[-1]
    assert body.startswith("Refreshed the menu entry.") and "when the shell restarts" in body
    assert actions == (("Restart shell", "/usr/bin/omarchy-restart-shell"),)

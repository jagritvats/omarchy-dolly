"""The countdown shows on Omarchy's on-screen display where the shell has one, as cards elsewhere."""
import json

from dolly import capture, health


def setup(monkeypatch, tmp_path, mode, with_osd=True):
    if with_osd:
        (tmp_path / "shell" / "plugins" / "osd").mkdir(parents=True)
    monkeypatch.setenv("OMARCHY_PATH", str(tmp_path))
    monkeypatch.setenv("DOLLY_COUNTDOWN", mode)
    monkeypatch.setattr(capture, "_have", lambda name: True)
    osd, cards, gone = [], [], []
    monkeypatch.setattr(capture, "_osd", lambda *a: osd.append(a))
    monkeypatch.setattr(capture, "notify", lambda *a, **k: cards.append(a[0]))
    monkeypatch.setattr(capture, "_dismiss_toast", lambda s: gone.append(s))
    monkeypatch.setattr(health, "set_phase", lambda *a, **k: None)
    return osd, cards, gone


def test_osd_when_the_shell_has_one(monkeypatch, tmp_path):
    osd, cards, _ = setup(monkeypatch, tmp_path, "osd")
    capture.countdown(1)
    assert cards == []
    assert osd[0][0] == "show" and json.loads(osd[0][1])["message"] == capture.t("notify.countdown", 1)
    assert json.loads(osd[0][1])["icon"]                  # an empty icon makes the shell draw its muted speaker
    assert osd[-1] == ("close",) and len(osd) == 2       # shown once, closed before the take starts


def test_cards_where_there_is_no_osd(monkeypatch, tmp_path):
    osd, cards, gone = setup(monkeypatch, tmp_path, "osd", with_osd=False)
    capture.countdown(1)
    assert osd == [] and cards == [capture.t("notify.countdown", 1)]
    assert capture.t("notify.countdown", 1) in gone      # and the card is taken down before the take


def test_cards_on_request(monkeypatch, tmp_path):
    osd, cards, _ = setup(monkeypatch, tmp_path, "cards")
    capture.countdown(1)
    assert osd == [] and len(cards) == 1

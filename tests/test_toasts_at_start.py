"""Notification cards that are on screen when Record is pressed. A card that expires while
the recorder starts is gone from the shell's count at the logger's first sample, yet its
pixels are in the first frames (found Nov 11 on a real take). And: what was gone before the
end of a recorded countdown is not in the take."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import capture, privacy  # noqa: E402
from tests.test_privacy_auto import timeline  # noqa: E402

AREA = (1500, 0, 1920, 1080)


def test_cards_up_at_record_are_covered_from_the_first_frame():
    tl = timeline(toasts=[(0.6, 0)], areas=[AREA])
    tl.capture["toasts_before"] = 1
    assert privacy.toast_spans(tl) == [(0.0, 0.6 + privacy.START_LINGER, 1)]
    boxes = privacy.auto_boxes(tl)
    assert len(boxes) == 1 and boxes[0]["start"] == 0.0 and boxes[0]["end"] >= 0.6 + privacy.START_LINGER
    assert boxes[0]["source"] == "auto" and boxes[0]["label"] == "notification"


def test_no_logger_sample_at_all_still_covers_the_start():
    tl = timeline(toasts=[], areas=[AREA])
    tl.capture["toasts_before"] = 2
    assert privacy.toast_spans(tl) == [(0.0, privacy.START_LINGER, 2)]


def test_the_count_already_showing_them_changes_nothing():
    tl = timeline(toasts=[(0.6, 1), (4.0, 0)], areas=[AREA])
    tl.capture["toasts_before"] = 1
    assert privacy.toast_spans(tl) == [(0.0, 4.0, 1)]
    tl = timeline(toasts=[(0.6, 0), (5.0, 1), (8.0, 0)], areas=[AREA])            # nothing was up at Record
    assert privacy.toast_spans(tl) == [(5.0, 8.0, 1)]


def test_a_new_card_during_the_linger_counts_too():
    tl = timeline(toasts=[(0.6, 0), (1.0, 2), (3.0, 0)], areas=[AREA])
    tl.capture["toasts_before"] = 1
    assert privacy.toast_spans(tl) == [(0.0, 1.0, 1), (1.0, 3.0, 2)]


def test_what_was_gone_before_go_gets_no_box():
    tl = timeline(toasts=[(0.5, 1), (2.4, 0), (6.0, 1), (7.0, 0)], areas=[AREA])   # 3-2-1 cards, then a real one
    tl.capture["go"] = 3.0
    assert privacy.toast_spans(tl) == [(6.0, 7.0, 1)]
    tl = timeline(toasts=[(0.5, 1), (4.0, 0)], areas=[AREA])                         # still up at go
    tl.capture["go"] = 3.0
    assert privacy.toast_spans(tl) == [(3.0, 4.0, 1)]
    tl = timeline(toasts=[(0.5, 0)], areas=[AREA])                                   # up at Record, long gone by go
    tl.capture.update({"go": 3.0, "toasts_before": 1})
    assert privacy.toast_spans(tl) == []


def test_dollys_last_countdown_card_leaves_no_box_on_an_empty_corner():
    """Real take, Nov 11: the card was taken down 0.35 s before go and was not in the clip,
    but the logger (polling every 0.4 s) logged it leaving 0.3 s after go."""
    tl = timeline(toasts=[(0.35, 1), (3.47, 0)], areas=[AREA])
    tl.capture.update({"go": 3.15, "toasts_at_go": 0})
    assert privacy.toast_spans(tl) == [] and privacy.auto_boxes(tl) == []
    tl.capture["toasts_at_go"] = None                 # a take from before the count was read at go
    assert privacy.toast_spans(tl) == [(3.15, 3.47, 1)]


def test_another_apps_card_still_up_at_go_is_covered():
    tl = timeline(toasts=[(0.35, 1), (5.0, 0)], areas=[AREA])
    tl.capture.update({"go": 3.15, "toasts_at_go": 1})
    assert privacy.toast_spans(tl) == [(3.15, 5.0, 1)]
    tl = timeline(toasts=[(0.35, 0), (3.4, 1), (6.0, 0)], areas=[AREA])      # it appeared just before go
    tl.capture.update({"go": 3.15, "toasts_at_go": 1})
    assert privacy.toast_spans(tl) == [(3.15, 6.0, 1)]


class Shell:
    """omarchy-shell notifications count / dismiss, with Dolly's own card on screen."""

    def __init__(self, cards, own=True, leaves_after=1):
        self.cards, self.own, self.leaves_after, self.calls = cards, own, leaves_after, []

    def run(self, cmd, **kw):
        self.calls.append(cmd)

        class R:
            stdout = ""
        if cmd[1:3] == ["notifications", "count"]:
            if self.own is None and self.leaves_after > 0:           # dismissed: leaves the count a moment later
                self.leaves_after -= 1
                R.stdout = str(self.cards + 1)
            else:
                R.stdout = str(self.cards + (1 if self.own else 0))
        elif cmd[1:3] == ["notifications", "dismiss"]:
            R.stdout = "ok" if self.own else "none"
            if self.own:
                self.own = None
        return R


def test_dollys_own_card_is_dismissed_and_not_counted(monkeypatch):
    sh = Shell(cards=0)
    monkeypatch.setattr(capture, "_have", lambda name: True)
    monkeypatch.setattr(capture.subprocess, "run", sh.run)
    assert capture._toasts_at_start() == 0
    assert ["omarchy-shell", "notifications", "dismiss", capture.t("notify.recorded")] in sh.calls


def test_other_apps_cards_are_counted(monkeypatch):
    sh = Shell(cards=2, own=False)
    monkeypatch.setattr(capture, "_have", lambda name: True)
    monkeypatch.setattr(capture.subprocess, "run", sh.run)
    assert capture._toasts_at_start() == 2
    sh = Shell(cards=1)                                                # one of theirs, plus ours
    monkeypatch.setattr(capture.subprocess, "run", sh.run)
    assert capture._toasts_at_start() == 1


def test_no_shell_means_no_cards(monkeypatch):
    monkeypatch.setattr(capture, "_have", lambda name: False)
    assert capture._toasts_at_start() == 0 and capture._toast_count() == 0

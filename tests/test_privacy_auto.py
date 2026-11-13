"""Privacy by default: secret-bearing windows and notification toasts are blurred with no
settings, as ordinary boxes; nothing typed into such a window is kept."""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import privacy, sensitive  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.logger import Logger  # noqa: E402

TERM = {"address": "0x1", "class": "kitty", "title": "zsh", "rect": [0, 0, 1920, 1080]}


def win(cls, rect, title="", address="0x9"):
    return {"address": address, "class": cls, "title": title, "rect": list(rect)}


def timeline(snaps=(), toasts=(), areas=(), duration=10.0):
    tl = Timeline(1920, 1080, 60.0, duration)
    tl.snapshots = [{"t": t, "focus": focus, "windows": ws} for t, focus, ws in snaps]
    tl.toasts = [{"t": t, "count": n} for t, n in toasts]
    tl.capture = {"scale": 1.0, "notification_areas": [list(a) for a in areas]}
    return tl


def test_which_windows_hold_secrets():
    yes = [("org.keepassxc.KeePassXC", ""), ("Bitwarden", ""), ("1Password", ""), ("gcr-prompter", ""),
           ("pinentry-gtk", ""), ("polkit-gnome-authentication-agent-1", ""), ("org.gnome.seahorse.Application", ""),
           ("hyprpolkitagent", ""), ("zenity", "Authentication Required"), ("foo", "Unlock Login Keyring")]
    no = [("kitty", "zsh"), ("chromium", "Bitwarden pricing - Chromium"), ("code", "password.py"), (None, None)]
    assert all(sensitive.secret_window(c, t) for c, t in yes)
    assert not any(sensitive.secret_window(c, t) for c, t in no)
    assert sensitive.secret_window("org.keepassxc.KeePassXC") == "KeePassXC"


def test_password_manager_is_one_removable_auto_box():
    pm = win("org.keepassxc.KeePassXC", (600, 300, 1200, 800))
    snaps = [(t / 10, "0x1", [TERM] + ([pm] if 20 <= t < 60 else [])) for t in range(100)]
    boxes = privacy.auto_boxes(timeline(snaps))
    assert len(boxes) == 1
    b = boxes[0]
    assert b["source"] == "auto" and b["mode"] == "blur" and b["enabled"] and b["label"] == "KeePassXC"
    assert b["start"] <= 2.0 and 5.9 <= b["end"] <= 6.2                       # there before it appears, gone after
    x, y, w, h = b["rect"]
    assert x <= 600 and y <= 300 and x + w >= 1200 and y + h >= 800           # the whole window
    frame = np.random.default_rng(1).integers(0, 255, (1080, 1920, 3), dtype=np.uint8)
    out = privacy.apply_boxes(frame, boxes, 4.0)                              # the normal privacy pipeline
    inside, outside = (slice(350, 750), slice(650, 1150)), (slice(0, 200), slice(0, 500))
    assert out[inside].std() < 0.3 * frame[inside].std() and (out[outside] == frame[outside]).all()
    assert privacy.apply_boxes(frame, boxes, 8.0) is frame


def test_moving_window_is_covered_along_its_path():
    snaps = []
    for i in range(100):                              # dragged right between 3.0 s and 4.0 s, then rests
        dx = min(max(i - 30, 0), 10) * 40
        snaps.append((i / 10, "0x1", [TERM, win("Bitwarden", (100 + dx, 200, 500 + dx, 600))]))
    tl = timeline(snaps)
    boxes = privacy.auto_boxes(tl)
    assert 2 <= len(boxes) <= 3
    for s in tl.snapshots:                            # at every moment some active box covers the window
        x0, y0, x1, y1 = s["windows"][1]["rect"]
        assert any(b["start"] <= s["t"] <= b["end"] and b["rect"][0] <= x0 and b["rect"][1] <= y0
                   and b["rect"][0] + b["rect"][2] >= x1 and b["rect"][1] + b["rect"][3] >= y1 for b in boxes), s["t"]
    last = max(boxes, key=lambda b: b["start"])
    assert last["rect"][2] < 500                      # once it rests, the box is the window again (not the path)


def test_polkit_prompt_with_focus_keeps_no_keys():
    prompt = win("hyprpolkitagent", (700, 400, 1220, 680), "Authentication Required")
    events = [{"t": 0.5, "type": "key", "key": "L", "mods": []}]
    for i in range(100):
        on = 30 <= i < 60
        events.append({"t": i / 10, "type": "clients", "focus": "0x9" if on else "0x1",
                       "windows": [TERM] + ([prompt] if on else [])})
    typed = [{"t": 3.4 + k * 0.2, "type": "key", "key": c, "mods": []} for k, c in enumerate("hunter2")]
    events += typed + [{"t": 8.0, "type": "key", "key": "S", "mods": ["ctrl"]}]
    events.sort(key=lambda e: e["t"])
    kept, dropped = sensitive.strip_secret_keys(events, 10.0)
    assert dropped == 7
    assert [e["key"] for e in kept if e["type"] == "key"] == ["L", "S"]       # the rest of the take keeps its keys
    assert sensitive.strip_secret_keys([e for e in events if e["type"] != "clients"], 10.0)[1] == 0
    tl = timeline([(e["t"], e["focus"], e["windows"]) for e in events if e["type"] == "clients"])
    (box,) = privacy.auto_boxes(tl)
    assert box["label"] == "hyprpolkitagent" and box["start"] <= 3.0 <= 6.0 <= box["end"]


def test_logger_drops_keys_while_a_secret_window_has_focus():
    lg, seen = Logger.__new__(Logger), []
    lg.mods, lg.write = set(), seen.append
    lg.on_key(30, True), lg.on_key(30, False)                                 # A, logged
    lg.secret_focus = True
    for code in (35, 22, 49):                                                 # typed into the prompt
        lg.on_key(code, True), lg.on_key(code, False)
    lg.secret_focus = False
    lg.on_key(31, True)
    assert [e["key"] for e in seen if e["type"] == "key"] == ["char", "char"]   # A and S (typed: no names); none between
    assert sum(e["type"] == "info" for e in seen) == 1                        # said once, not per key


AREA = (1521, 0, 1920, 1080)


def test_toast_appears_and_disappears():
    tl = timeline(toasts=[(0.0, 0), (3.0, 1), (7.5, 0)], areas=[AREA])
    assert privacy.toast_spans(tl) == [(3.0, 7.5, 1)]
    (b,) = privacy.auto_boxes(tl)
    assert b["label"] == "notification" and b["source"] == "auto" and b["mode"] == "blur"
    assert b["start"] <= 2.4 and 7.9 <= b["end"] <= 8.2                      # the slide in and out are covered
    x, y, w, h = b["rect"]
    assert (x, y) == (1521, 0) and x + w == 1920 and 150 <= h <= 260          # the top of the column, not all of it
    assert privacy.auto_boxes(timeline(toasts=[(0.0, 0)], areas=[AREA])) == []       # none: nothing blurred
    assert privacy.auto_boxes(timeline(toasts=[(0.0, 1)], areas=[(2000, 0, 2400, 1080)])) == []   # other monitor


def test_toasts_already_there_and_a_new_one_on_top():
    tl = timeline(toasts=[(0.7, 2), (4.0, 3), (8.0, 2)], areas=[AREA])        # two sit there for the whole take
    boxes = privacy.auto_boxes(tl)
    assert [b["label"] for b in boxes] == ["2 notifications", "3 notifications", "2 notifications"]
    assert boxes[0]["start"] == 0.0 and boxes[-1]["end"] == 10.0
    assert boxes[1]["start"] <= 3.3 and boxes[1]["end"] >= 8.4                # the taller box while there are three
    assert boxes[1]["rect"][3] > boxes[0]["rect"][3] == boxes[2]["rect"][3]
    for t in (0.0, 3.9, 4.0, 7.9, 8.0, 9.9):                                  # never a moment without a box
        assert any(b["start"] <= t <= b["end"] for b in boxes)


def test_setting_is_on_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from dolly import config
    assert config.load()["privacy"]["auto"] is True

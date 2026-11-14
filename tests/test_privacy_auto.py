"""Privacy by default: secret-bearing windows and notification toasts are blurred with no
settings, as ordinary boxes; nothing typed into such a window is kept."""
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import gfx, privacy, sensitive  # noqa: E402
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
    frame = gfx.frame_from(bytearray(random.Random(1).randbytes(4 * 1920 * 1080)), 1920, 1080)     # noise
    before = gfx.copy(frame)
    out = privacy.apply_boxes(frame, boxes, 4.0)                              # the normal privacy pipeline
    assert out is frame                                                       # blurred where it lies
    inside, outside = (650, 350, 500, 400), (0, 0, 500, 200)
    spread = lambda f, box: statistics.pstdev(gfx.plane(gfx.crop(f, *box), "g")[::7])
    assert spread(out, inside) < 0.3 * spread(before, inside)
    assert gfx.same(gfx.crop(out, *outside), gfx.crop(before, *outside))
    blurred = gfx.copy(frame)
    assert privacy.apply_boxes(frame, boxes, 8.0) is frame and gfx.same(frame, blurred)     # no box at that time


def _lines_of_text(w, h):
    """White strokes 2 px wide on black, 6 px apart, in rows 14 px high: about what small text is."""
    f = gfx.new(w, h)
    for y in range(4, h - 10, 14):
        for x in range(4, w - 4, 6):
            gfx.fill_rect(f, x, y, 2, 9, (1, 1, 1))
    return f


def _levels(f, x, y, w, h):
    g = gfx.plane(gfx.crop(f, x, y, w, h), "g")
    return min(g), max(g)


def _step(f, x, y, w, h):
    """The largest difference between two neighbouring pixels (side by side or one above the other)."""
    g = gfx.plane(gfx.crop(f, x, y, w, h), "g")
    across = max(abs(a - b) for i in range(0, len(g), w) for a, b in zip(g[i:i + w], g[i + 1:i + w]))
    return max(across, max(abs(a - b) for a, b in zip(g, g[w:])))


def test_what_is_under_a_box_cannot_be_read():
    box = {"start": 0, "end": 9, "rect": [100, 60, 400, 140]}
    f = _lines_of_text(640, 360)
    assert _levels(f, 100, 60, 400, 140) == (0, 255) and _step(f, 100, 60, 400, 140) == 255
    privacy.apply_boxes(f, [box], 1.0)
    lo, hi = _levels(f, 100, 60, 400, 140)
    assert _step(f, 100, 60, 400, 140) <= 3 and hi < 200  # no stroke left, no edge of one: a smooth grey
    assert _levels(f, 100, 210, 400, 100) == (0, 255)     # below the box the text is as sharp as before
    # pixelate: a few blocks, each of one colour
    g = _lines_of_text(640, 360)
    privacy.apply_boxes(g, [{**box, "mode": "pixelate"}], 1.0)
    lo, hi = _levels(g, 100, 60, 400, 140)
    assert hi - lo < 120 and _step(g, 130, 90, 12, 12) == 0
    assert len(set(gfx.plane(gfx.crop(g, 100, 60, 400, 140), "g"))) <= (400 // 23) * (140 // 23)
    # a disabled box, a box of another time and a sliver do nothing
    h = _lines_of_text(640, 360)
    same = gfx.copy(h)
    privacy.apply_boxes(h, [{**box, "enabled": False}, {**box, "start": 5}, {**box, "rect": [10, 10, 1.4, 300]}], 1.0)
    assert gfx.same(h, same)


def test_nothing_next_to_a_box_gets_into_it_and_the_edge_of_the_frame_is_fine():
    f = gfx.new(320, 200)
    gfx.fill_rect(f, 0, 0, 320, 200, (1, 1, 1))
    gfx.fill_rect(f, 120, 80, 100, 60, (0, 0, 0))         # a black field on a white page, the box exactly on it
    privacy.apply_boxes(f, [{"start": 0, "end": 9, "rect": [120, 80, 100, 60]}], 1.0)
    assert _levels(f, 120, 80, 100, 60) == (0, 0)         # still black: no white was blurred in
    assert _levels(f, 0, 0, 320, 80) == (255, 255) and gfx.pixel(f, 119, 100) == (255, 255, 255)
    g = _lines_of_text(320, 200)                          # a box that sticks out of the frame on two sides
    privacy.apply_boxes(g, [{"start": 0, "end": 9, "rect": [-50, -40, 200, 150]}], 1.0)
    assert _step(g, 0, 0, 150, 110) <= 3 and _levels(g, 160, 120, 150, 70) == (0, 255)


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

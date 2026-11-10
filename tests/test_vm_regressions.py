"""Regressions found by recording on a real Omarchy 4.5 VM (VirtualBox, wf-recorder)."""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly.events import Timeline  # noqa: E402
from dolly import aoi as aoimod  # noqa: E402
from dolly.zoom import around_caret  # noqa: E402

W, H = 1920, 1200


def test_duplicate_key_presses_count_once(tmp_path):
    """Hyprland can deliver input.keyboard.key twice per press: one keystroke, not two
    ("Enter x2" in the shortcut overlay for a single Enter)."""
    from dolly import logger as lg
    L = lg.Logger(tmp_path, keys=True)
    for down in (True, True, False, False):      # g pressed (twice), released (twice)
        L.on_key(34, down)
    for down in (True, True, False, False):      # Enter
        L.on_key(28, down)
    L.on_key(34, True)                            # g again: a real second press counts
    L.on_key(34, False)
    L.f.close()
    keys = [json.loads(x)["key"] for x in (tmp_path / "events.raw.jsonl").read_text().splitlines()
            if json.loads(x).get("type") == "key"]
    assert len(keys) == 3, keys


def test_typing_shot_is_around_the_caret_not_the_middle_of_the_union():
    """After `clear`, the typing rect spans most of the terminal; the shot must frame the caret."""
    r = around_caret((12, 48, 735, 1123), (202, 110), 16.0, W)
    cy = (r[1] + r[3]) / 2
    assert r[1] <= 110 <= r[3] and abs(cy - 110) < 40, r
    assert r[0] <= 202 <= r[2]


def test_caret_outside_rect_is_trusted():
    r = around_caret((12, 600, 700, 700), (100, 200), 16.0, W)
    assert r[1] <= 200 <= r[3] and r[0] <= 100 <= r[2]


def _act(blobs_at):
    """A synthetic Activity: 0.1 s samples, blobs per sample."""
    n = 60
    t = [i * 0.1 for i in range(n)]
    blobs = [blobs_at.get(i, []) for i in range(n)]
    import numpy as np
    frac = np.array([sum(b[4] for b in bl) for bl in blobs])
    return aoimod.Activity(t=np.array(t), frac=frac, blobs=blobs, w=W, h=H)


def test_resting_pointer_is_not_pointer_activity():
    """The logger writes where the pointer rests at the start. That is not movement: typing
    right after the start must frame the text, not the parked (baked-in) pointer."""
    win = {"address": "0x1", "class": "foot", "title": "t", "rect": [12.0, 38.0, 1896.0, 1150.0],
           "floating": False, "fullscreen": False, "xwayland": False, "workspace": "1"}
    ev = [{"t": 0.0, "type": "move", "x": 696.0, "y": 1092.0},
          {"t": 0.01, "type": "window", "rect": win["rect"], "title": "t", "class": "foot", "address": "0x1"},
          {"t": 0.02, "type": "clients", "focus": "0x1", "windows": [win]}]
    ev += [{"t": 1.0 + 0.07 * i, "type": "key", "key": "a", "mods": []} for i in range(12)]
    tl = Timeline.from_dict({"video": {"w": W, "h": H, "fps": 60, "duration": 6.0},
                             "capture": {"cursor_baked": True, "scale": 1.0}, "events": ev})
    # glyphs appear along the prompt line; the parked pointer blinks (Omarchy hides it on key press)
    text = {i: [(20 + 18 * (i - 10), 100, 38 + 18 * (i - 10), 120, 0.0002)] for i in range(10, 20)}
    for i in range(10, 20, 2):
        text[i] = text[i] + [(672, 1080, 716, 1104, 0.0001)]
    act = _act(text)
    tr = aoimod.build_aoi(tl, act)
    for t in (1.3, 1.6, 1.9):
        r, kind, foc = tr.at(t)
        assert kind == "typing", (t, kind)
        assert foc is not None and foc[1] < 300, (t, foc)     # the prompt line, not the pointer at y=1092


def test_export_progress_is_quiet_in_logs(monkeypatch):
    from dolly import render
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    for i in range(0, 1001, 5):
        render.cli_progress(i, 1000, 1.0 + i / 100)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert "\r" not in buf.getvalue()
    assert 5 <= len(lines) <= 12, len(lines)
    assert lines[-1].strip().startswith("rendering 1000/1000")


def test_typing_without_a_fresh_caret_uses_the_nearest_one():
    """Keys whose glyphs are not seen yet use the nearest caret (ahead first, then behind),
    not the whole window (a 1.2x centred shot that made zoom pump in and out)."""
    import numpy as np
    from dolly.zoom import last_caret
    from dolly.aoi import AOITrack
    t = np.arange(0, 6, 0.1)
    kind = [None] * len(t)
    foc = [None] * len(t)
    for i in range(30, 35):
        kind[i], foc[i] = "typing", (200.0, 110.0)
    tr = AOITrack(t, [None] * len(t), kind, foc, [], 16.0)
    assert last_caret(tr, 1.5) == (200.0, 110.0)      # 1.5 s before the glyphs appear
    assert last_caret(tr, 5.0) == (200.0, 110.0)      # after them
    assert last_caret(tr, 0.2) is None                # nothing within 2 s ahead

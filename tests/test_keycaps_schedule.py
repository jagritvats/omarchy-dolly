"""Keycaps pressed close together: each is readable, none is held back for long."""
from dolly import render


def caps(*events):
    return render.schedule_keycaps([{"t": t, "t_last": t, "caps": c, "count": 1} for t, c in events])


def full(ev, lo=0.0, hi=6.0, step=0.01):
    """Seconds this keycap is at full strength."""
    n = int((hi - lo) / step)
    return sum(step for i in range(n) if render.keycap_alpha(ev, lo + i * step) >= 0.999)


def test_a_chord_right_after_enter_is_readable():
    enter, chord = caps((1.0, ["Enter"]), (1.25, ["Ctrl", "L"]))
    assert enter["show"] == 1.0 and abs(enter["hide"] - chord["show"]) < 1e-9      # one at a time
    assert chord["show"] - 1.25 <= render.KEY_MAX_LAG + 1e-9                        # held back 0.35 s at most
    assert enter["hide"] - enter["show"] >= render.KEY_MIN_SHOW - 1e-9
    assert full(chord) >= 0.7 and full(enter) >= 0.35
    # before: the chord was drawn only from 2.10 s (when Enter left) to 2.35 s, fading
    assert render.keycap_alpha(chord, 1.9) == 1.0


def test_a_single_keycap_is_unchanged():
    (ev,) = caps((2.0, ["Super", "K"]))
    assert ev["show"] == 2.0 and ev["hide"] == 2.0 + render.KEY_HOLD and ev["fade_out"] == render.KEY_FADE_OUT
    assert render.keycap_alpha(ev, 1.9) == 0.0 and render.keycap_alpha(ev, 2.5) == 1.0
    assert render.keycap_alpha(ev, 3.2) == 0.0


def test_keys_far_apart_do_not_touch_each_other():
    a, b = caps((1.0, ["Enter"]), (5.0, ["Ctrl", "C"]))
    assert a["hide"] == 1.0 + render.KEY_HOLD and b["show"] == 5.0


def test_a_burst_never_falls_far_behind():
    evs = caps(*[(round(1.0 + 0.2 * i, 2), ["Ctrl", chr(65 + i)]) for i in range(6)])
    for ev in evs:
        assert 0 <= ev["show"] - ev["t"] <= render.KEY_MAX_LAG + 1e-9
        assert ev["hide"] > ev["show"]
    for a, b in zip(evs, evs[1:]):
        assert a["hide"] <= b["show"] + 1e-9                                          # never two at once

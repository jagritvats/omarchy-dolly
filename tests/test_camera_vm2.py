"""Camera/sync regressions from the second VM pass (Omarchy 4.5, VirtualBox, wf-recorder)."""
import bisect
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import sync  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.zoom import ZoomConfig, build_segments  # noqa: E402

W, H = 1920, 1200


def _marker(rgb_edge, rgb_centre):
    """The marker square as the decoder gives it: PROBE x PROBE pixels, rows of r, g, b."""
    f = bytearray(bytes(rgb_edge) * (sync.PROBE * sync.PROBE))
    for y in (sync.PROBE // 2 - 1, sync.PROBE // 2):
        for x in (sync.PROBE // 2 - 1, sync.PROBE // 2):
            f[(y * sync.PROBE + x) * 3:(y * sync.PROBE + x) * 3 + 3] = bytes(rgb_centre)
    return bytes(f)


def test_fading_in_marker_is_still_a_marker():
    """The overlay fades in on software rendering: edge red 142 -> 166 -> 192 (VM, Nov 10).
    The first dim frames are the marker's real start; missing them biases sync by ~50 ms."""
    for edge, centre in (((142, 27, 148), (24, 143, 31)), ((166, 25, 168), (23, 166, 28)),
                         ((214, 19, 216), (17, 215, 18))):
        assert sync.is_marker(_marker(edge, centre)), edge
    assert not sync.is_marker(_marker((40, 36, 90), (40, 36, 90)))      # a dark UI corner
    assert not sync.is_marker(_marker((200, 200, 200), (40, 200, 40)))  # grey + green is not it


def test_no_marker_falls_back_to_the_clocks_not_zero():
    """4 of 7 VM recordings had no visible marker; a = 0 put every key ~3.3 s early
    (typing matched to frames where nothing happened: wide, pumping shots)."""
    meta = {"clock_mono": 13705.509747}
    lines = [{"type": "start", "t0_mono": 13709.10141, "ms": 0}]
    a = sync.clock_offset(meta, lines)
    assert a is not None and abs(a - (13709.10141 - 13705.509747 - sync.FIRST_FRAME_LATENCY)) < 1e-6
    f = sync.fit([0.34, 22.2], [], 60.0, clock_a=a)
    assert f.method == "clock" and abs(f.a - a) < 1e-9
    assert sync.fit([0.34], [], 60.0).method == "none"                 # no clocks either: unchanged


def test_marker_times_use_frame_timestamps(monkeypatch, tmp_path):
    """wf-recorder writes frames only on damage (avg 62.5 fps): index / fps is not a time."""
    frames = [bytes(sync.FRAME_BYTES)] * 6
    frames[3] = frames[4] = _marker((214, 19, 216), (17, 215, 18))
    pts = [0.0, 0.5, 1.0, 3.25, 3.267, 3.5]
    monkeypatch.setattr(sync, "frame_times", lambda raw, ffmpeg="ffmpeg": pts)

    class R:  # decoding is replaced: subprocess returns our frames
        stdout = b"".join(frames)
    monkeypatch.setattr(sync.subprocess, "run", lambda *a, **k: R())
    runs = sync.detect_markers(tmp_path / "raw.mkv", 1.0, 60.0, "ffmpeg")
    assert runs == [(3.25, 3.267)]


def _typing_tl(bursts, duration=20.0):
    """Keys in bursts [(t0, n)], a full-screen foot window."""
    win = {"address": "0x1", "class": "foot", "title": "t", "rect": [12.0, 38.0, 1908.0, 1188.0],
           "floating": False, "fullscreen": False, "xwayland": False, "workspace": "1"}
    ev = [{"t": 0.0, "type": "move", "x": 696.0, "y": 1092.0},
          {"t": 0.01, "type": "window", "rect": win["rect"], "title": "t", "class": "foot", "address": "0x1"},
          {"t": 0.02, "type": "clients", "focus": "0x1", "windows": [win]}]
    for t0, n in bursts:
        ev += [{"t": t0 + 0.1 * i, "type": "key", "key": "a", "mods": []} for i in range(n)]
    return Timeline.from_dict({"video": {"w": W, "h": H, "fps": 60, "duration": duration},
                               "capture": {"cursor_baked": True, "scale": 1.0}, "events": ev})


class _Track:
    """Minimal AOITrack: typing at the bottom line, then (after `clear`) at the top line."""
    def __init__(self, spans, line_h=16.0):
        self.t = [i * 0.1 for i in range(200)]
        self.rect, self.kind, self.focus = [], [], []
        for t in self.t:
            hit = next(((r, f) for a, b, r, f in spans if a <= t < b), None)
            self.rect.append(hit[0] if hit else None)
            self.kind.append("typing" if hit else None)
            self.focus.append(hit[1] if hit else None)
        self.transitions, self.line_h = [], line_h

    def at(self, t):
        i = min(max(bisect.bisect_right(self.t, t) - 1, 0), len(self.t) - 1)
        return self.rect[i], self.kind[i], self.focus[i]

    def in_transition(self, t, pad=0.0):
        return False


def test_close_ups_far_apart_are_not_merged_into_nothing():
    """Typing at the bottom, then at the top 1.3 s later: merging both into one shot fitted
    ~1.0x and the >= 1.08 filter then deleted both close-ups (VM recording 13-31-02)."""
    tl = _typing_tl([(3.5, 8), (4.8, 6)])
    aoi = _Track([(3.5, 4.7, (12, 1136, 400, 1195), (274.0, 1166.0)),
                  (4.8, 6.0, (12, 36, 400, 90), (256.0, 60.0))])
    segs = build_segments(tl, ZoomConfig(), aoi)
    assert len(segs) >= 2 and all(s.zoom >= 1.6 for s in segs), [(s.start, s.end, s.zoom) for s in segs]


def test_long_text_in_a_vertical_crop_keeps_the_start_of_lines():
    from dolly.reframe import text_x
    half = 300.0
    # fits: untouched
    assert text_x(500.0, 400.0, 900.0, half) == 500.0
    # wider than the crop: the crop's left edge sits on the start of the lines
    c = text_x(900.0, 12.0, 1500.0, half)
    assert abs((c - half) - 12.0) < 1e-6
    # ... unless the caret is further right: it stays in view
    c = text_x(900.0, 12.0, 1500.0, half, anchor=1200.0)
    assert c - half <= 1200.0 <= c + half
    assert not math.isnan(c)

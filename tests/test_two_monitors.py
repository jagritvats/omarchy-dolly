"""Two monitors (checked live on 11 Nov 2026 with a second output added to an Omarchy 4.5
session: right of, left of and above the first one, at other sizes and at scale 1.5 and 2).

What these pin:
- everything in a take is measured from the recorded monitor's corner, wherever it sits;
- a storyboard's clean stage is on the monitor you are on, never on the other one;
- a region or a window cut at a monitor edge is recorded as the part on one monitor, and
  the JSON result says so.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import headless, inject  # noqa: E402
from dolly.monitors import SpanError, resolve_pick  # noqa: E402


def mon(name, x, y, w, h, scale=1.0, focused=False, mid=0):
    return {"id": mid, "name": name, "x": x, "y": y, "width": w, "height": h, "scale": scale, "transform": 0,
            "focused": focused, "disabled": False}


RIGHT = [mon("A", 0, 0, 1920, 1200, focused=True), mon("B", 1920, 0, 1920, 1200, mid=1)]
LEFT = [mon("A", 0, 0, 1920, 1200), mon("B", -2560, 0, 2560, 1440, focused=True, mid=1)]
ABOVE = [mon("A", 0, 0, 1920, 1200), mon("B", 0, -1200, 1920, 1200, focused=True, mid=1)]
SCALED = [mon("A", 0, 0, 1920, 1200), mon("B", 1920, 0, 2880, 1800, scale=1.5, focused=True, mid=1)]


# ------------------------------------------------------------------ which monitor, which pixels
def test_a_region_is_measured_from_its_own_monitor_wherever_that_sits():
    t = resolve_pick("-2400,100 800x600", LEFT)
    assert (t.monitor, t.region) == ("B", [-2400, 100, 800, 600])
    assert t.extra["physical_rect"] == [160, 100, 800, 600]          # from B's corner at -2560,0
    t = resolve_pick("100,-1000 800x600", ABOVE)
    assert t.monitor == "B" and t.extra["physical_rect"] == [100, 200, 800, 600]


def test_a_region_on_a_scaled_monitor_is_in_its_real_pixels():
    t = resolve_pick("2020,100 800x600", SCALED)                     # B is 1920x1200 in layout units
    assert t.monitor == "B" and t.scale == 1.5
    assert t.extra["physical_rect"] == [150, 150, 1200, 900]


def test_a_named_monitor_is_recorded_whole():
    for mons in (RIGHT, LEFT, ABOVE, SCALED):
        t = resolve_pick("monitor:B", mons)
        assert t.kind == "monitor" and t.monitor == "B"
    assert resolve_pick("monitor:B", SCALED).region == [1920, 0, 1920, 1200]


def test_a_region_across_both_is_cut_to_the_monitor_holding_most_of_it():
    t = resolve_pick("1700,300 600x400", RIGHT)                      # 220 px on A, 380 px on B
    assert (t.monitor, t.region) == ("B", [1920, 300, 380, 400]) and "spanned two monitors" in t.note
    grid = RIGHT + [mon("C", 0, 1200, 1920, 1200, mid=2), mon("D", 1920, 1200, 1920, 1200, mid=3)]
    try:
        resolve_pick("1620,1000 600x400", grid)                      # a quarter on each of four: nobody's
    except SpanError as e:
        assert "one monitor" in str(e)
    else:
        raise AssertionError("an area with no monitor holding half of it must be refused")


# ------------------------------------------------------------------ the JSON result says what was cut
def args(**kw):
    base = dict(window=None, monitor=None, region=None, mode=None, duration=None, wait=False, json=True,
                no_editor=False, pick=None)
    return types.SimpleNamespace(**{**base, **kw})


def test_a_cut_region_is_named_in_the_result(monkeypatch):
    from dolly import capture
    monkeypatch.setattr(capture, "hypr_json", lambda what: RIGHT if what == "monitors" else [])
    d = headless._target(args(region="1700,300,600x400"))
    assert d["monitor"] == "B" and d["rect"] == [1920, 300, 380, 400]
    assert "recording the part on B" in d["note"]
    d = headless._target(args(region="2100,300,600x400"))            # wholly on B: nothing to say
    assert "note" not in d


def test_a_window_hanging_over_a_monitor_edge_is_named_in_the_result():
    win = {"address": "0x1", "monitor": 0, "at": [1500, 300], "size": [800, 500]}
    assert "outside A" in headless.off_monitor_note(win, RIGHT)
    assert headless.off_monitor_note({**win, "at": [200, 300]}, RIGHT) == ""
    on_b = {"address": "0x2", "monitor": 1, "at": [-2548, 38], "size": [2536, 1390]}
    assert headless.off_monitor_note(on_b, LEFT) == ""               # a monitor left of 0,0 is not "outside"
    assert headless.off_monitor_note({"monitor": 9, "at": [0, 0], "size": [10, 10]}, RIGHT) == ""


def test_the_note_reaches_the_warnings(tmp_path):
    out = headless.summary(tmp_path, {"kind": "region", "rect": [1920, 300, 380, 400], "monitor": "B",
                                      "note": "area spanned two monitors; recording the part on B"})
    assert "area spanned two monitors; recording the part on B" in out["warnings"]


# ------------------------------------------------------------------ dolly run: the clean stage
def desk(monkeypatch, workspaces, monitors, rules=()):
    data = {"workspaces": workspaces, "monitors": monitors, "workspacerules": list(rules)}
    monkeypatch.setattr(inject, "hypr_json", lambda what: data.get(what))
    return inject.Desktop()


def ws(i, monitor, windows=0):
    return {"id": i, "monitor": monitor, "windows": windows}


def test_the_stage_is_never_an_empty_workspace_on_the_other_monitor(monkeypatch):
    # you are on B (workspace 11); A is showing its empty workspace 2. Going to 2 would
    # move the take to A: it did, before this was pinned
    mons = [mon("A", 0, 0, 1920, 1200), mon("B", 1920, 0, 1920, 1200, focused=True, mid=1)]
    d = desk(monkeypatch, [ws(1, "A", 1), ws(2, "A"), ws(11, "B")], mons)
    assert d.empty_workspace() == 3                                  # not there yet: opens on B


def test_an_empty_workspace_already_on_this_monitor_is_fine(monkeypatch):
    mons = [mon("A", 0, 0, 1920, 1200, focused=True), mon("B", 1920, 0, 1920, 1200, mid=1)]
    d = desk(monkeypatch, [ws(1, "A", 2), ws(2, "A"), ws(11, "B", 1)], mons)
    assert d.empty_workspace() == 2


def test_a_workspace_bound_to_the_other_monitor_is_skipped(monkeypatch):
    mons = [mon("A", 0, 0, 1920, 1200), mon("B", 1920, 0, 1920, 1200, focused=True, mid=1)]
    rules = [{"workspaceString": "1", "monitor": "A"}, {"workspaceString": "2", "monitor": "A"},
             {"workspaceString": "3", "monitor": "B"}, {"workspaceString": "special:x"}]
    d = desk(monkeypatch, [ws(11, "B", 1)], mons, rules)
    assert d.empty_workspace() == 3


def test_one_monitor_behaves_as_before(monkeypatch):
    mons = [mon("A", 0, 0, 1920, 1200, focused=True)]
    d = desk(monkeypatch, [ws(1, "A", 1), ws(2, "A", 3)], mons)
    assert d.empty_workspace() == 3
    d = desk(monkeypatch, [ws(i, "A", 1) for i in range(1, 11)], mons)
    assert d.empty_workspace() is None                               # all ten in use: record where you are


# ------------------------------------------------------------------ dolly run: going back
def test_a_run_returns_to_its_monitor_before_its_workspace():
    """Started on B (workspace 11). By the time the stage is tidied the focus is on A; asking
    for workspace 11 then would open it on A (it did, live). The monitor comes first."""
    from dolly import script

    class Desk:
        def __init__(self):
            self.calls, self.mon, self.ws = [], "A", 3

        def monitor(self):
            return self.mon

        def go_monitor(self, name):
            self.calls.append(("monitor", name)); self.mon = name
            return True

        def workspace(self):
            return self.ws

        def go_workspace(self, wid):
            self.calls.append(("workspace", wid, "on", self.mon)); self.ws = wid
            return True

        def window(self, addr):
            return None

        def move_to(self, x, y, seconds=None):
            self.calls.append(("pointer", x, y))

    d = Desk()
    r = script.Runner.__new__(script.Runner)
    r.desk = d
    r.tidy([], {"workspace": 11, "window": None, "pointer": (2500.0, 400.0), "monitor": "B"})
    assert d.calls == [("monitor", "B"), ("workspace", 11, "on", "B"), ("pointer", 2500.0, 400.0)]
    d2 = Desk()
    d2.mon, d2.ws = "B", 11                                          # already home: nothing to do but the pointer
    r.desk = d2
    r.tidy([], {"workspace": 11, "window": None, "pointer": (1.0, 2.0), "monitor": "B"})
    assert d2.calls == [("pointer", 1.0, 2.0)]


def test_a_desk_that_knows_no_monitors_still_goes_home():
    from dolly import script

    class Old:
        calls = []

        def workspace(self):
            return 5

        def go_workspace(self, wid):
            self.calls.append(wid)

        def window(self, addr):
            return None

        def move_to(self, x, y, seconds=None):
            pass

    r = script.Runner.__new__(script.Runner)
    r.desk = Old()
    r.tidy([], {"workspace": 2, "window": None, "pointer": (0.0, 0.0), "monitor": None})
    assert Old.calls == [2]


# ------------------------------------------------------------------ a monitor unplugged mid-take
def test_a_recorder_without_its_monitor_is_not_waited_for_ten_seconds():
    """Live, 11 Nov: removing the recorded monitor ended the take cleanly (kept, marked
    "the recorded monitor was disconnected", desktop put back) but only 13 s later: the
    recorder waits for a frame that can no longer come and sat out the 10 s limit."""
    import inspect

    from dolly import capture
    src = inspect.getsource(capture.stop)
    assert 'GONE_PATIENCE if reason == "monitor-removed" else 500' in src
    assert capture.GONE_PATIENCE * 0.02 <= 2.0
    assert "monitor-removed" in capture.STOP_REASONS and capture.reason_text("monitor-removed") != "monitor-removed"

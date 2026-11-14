"""`dolly run`: the runner against a fake desktop and a fake recorder (no Hyprland, no input)."""
import json

import pytest

from dolly import script, storyboard as sbmod

CAPS = {"keys": True, "pointer": True, "click": False, "quiet": True, "problems": [],
        "click_note": "click steps need a Hyprland session", "quiet_note": ""}
BOARD = '''
title = "Two commands"
seed  = 3
[[setup]]
open = "terminal"
as   = "term"
[[step]]
say = "First, list the folder"
[[step]]
type  = "ls -la"
enter = true
[[step]]
wait = "quiet"
[[step]]
key = "ctrl+l"
[[step]]
move = "50%,50%"
in   = "term"
[[step]]
zoom = "here"
[[cleanup]]
close = "term"
'''


class Clock:
    def __init__(self):
        self.t = 100.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeDesk:
    """The shape of inject.Desktop; every call is logged with the time it happened."""

    def __init__(self, clock):
        self.clock, self.calls, self.wins, self.ws = clock, [], {}, 2
        self.fail_at = self.on_call = None

    def log(self, *call):
        self.calls.append((round(self.clock.now(), 3), *call))
        if self.on_call:
            self.on_call(call)
        if self.fail_at and self.fail_at(call):
            raise RuntimeError("the keyboard fell off")

    def type_char(self, ch):
        self.log("char", ch)

    def press(self, mods, key):
        self.log("press", "+".join([*mods, key]))

    def pointer(self):
        return (10.0, 20.0)

    def move_to(self, x, y, seconds=None):
        self.log("move", round(x), round(y))

    def click(self, button="left", address=None):
        self.log("click", button, address)

    def clients(self):
        return list(self.wins.values())

    def window(self, addr):
        return self.wins.get(addr) or ({"address": addr} if addr == "0xhome" else None)

    def find(self, sel):
        return next((w for w in self.wins.values() if sel.split(":", 1)[1] == w["class"]), None)

    def focus(self, addr):
        self.log("focus", addr)
        return True

    def mark(self):
        self.log("mark")

    def workspace(self):
        return self.ws

    def empty_workspace(self):
        return 5

    def focused(self):
        return "0xhome"

    def screen_rect(self):
        return (0, 0, 1920, 1080)

    def screen_sample(self, rect=None):
        return None

    def go_workspace(self, wid):
        self.log("workspace", wid)
        self.ws = wid
        return True

    def launch(self, command, cwd=None):
        self.log("launch", command)
        addr = f"0x{len(self.wins) + 1}"
        self.wins[addr] = {"address": addr, "class": "foot", "at": [100, 50], "size": [800, 600]}

    def close(self, addr):
        self.log("close", addr)
        self.wins.pop(addr, None)
        return True


class FakeRec:
    def __init__(self, clock, tmp):
        self.clock, self.tmp, self.on, self.started, self.stopped = clock, tmp, False, [], 0

    def active(self):
        return self.on

    def start(self, mode, pick):
        self.started.append((mode, pick))
        self.on = True
        (self.tmp / "events.raw.jsonl").write_text(json.dumps({"type": "start", "t0_mono": self.clock.now()}) + "\n")
        return self.tmp

    def stop(self):
        self.stopped += 1
        self.on = False
        self.finalize()

    def finalize(self):
        (self.tmp / "events.json").write_text(json.dumps({"video": {"duration": 9.0},
                                                          "capture": {"sync": {"a": 0.25, "b": 1.0}}}))

    def wait_finalized(self, pdir, seconds=0):
        self.finalize()
        return True


def make(tmp_path, text=BOARD, caps=CAPS):
    clock = Clock()
    desk, rec = FakeDesk(clock), FakeRec(clock, tmp_path)
    r = script.Runner(sbmod.loads(text), desk, rec, clock=clock.now, sleep=clock.sleep, caps=caps)
    return r, desk, rec, clock


def kinds(desk):
    return [c[1] for c in desk.calls]


def typed(desk):
    return "".join(c[2] for c in desk.calls if c[1] == "char")


def test_a_run_performs_the_steps_in_order_and_puts_everything_back(tmp_path):
    r, desk, rec, _ = make(tmp_path)
    res = r.run(export=False)
    assert res["ok"] and res["error"] is None and res["steps_done"] == 6 and res["project"] == str(tmp_path)
    assert rec.started == [("screen", None)] and rec.stopped == 1 and not rec.active()
    k = kinds(desk)
    assert k[:2] == ["workspace", "launch"]                       # the stage, then setup, before recording
    assert typed(desk) == "ls -la"
    assert [c[2] for c in desk.calls if c[1] == "press"] == ["enter", "ctrl+l"]
    assert ("move", 500, 350) in [c[1:] for c in desk.calls]      # 50%,50% of the 800x600 window at 100,50
    assert k.index("mark") > k.index("move")
    assert k[-4:] == ["close", "workspace", "focus", "move"]      # cleanup, then home workspace, window, pointer
    assert desk.ws == 2 and desk.calls[-2][2] == "0xhome"
    assert any("grim" in w for w in res["warnings"])              # "quiet" without a screen grabber says so


def test_typing_follows_the_plan(tmp_path):
    r, desk, _, _ = make(tmp_path)
    r.run(export=False)
    r2, desk2, _, _ = make(tmp_path)
    r2.run(export=False)
    chars = [(c[0], c[2]) for c in desk.calls if c[1] == "char"]
    assert chars == [(c[0], c[2]) for c in desk2.calls if c[1] == "char"]      # same seed, same take
    row = sbmod.plan(r.sb)["steps"][1]
    gaps = [b[0] - a[0] for a, b in zip(chars, chars[1:])]
    assert gaps == pytest.approx(row.delays[1:], abs=0.002)
    assert len({round(g, 2) for g in gaps}) > 2                   # not a metronome


def test_say_becomes_a_caption_in_video_time(tmp_path):
    r, _, _, _ = make(tmp_path)
    res = r.run(export=False)
    srt = (tmp_path / "captions.srt").read_text()
    assert "First, list the folder" in srt
    # said right after the lead-in: 0.8 s on our clock + the take's sync offset (0.25 s)
    assert srt.splitlines()[1].startswith("00:00:01,05")
    assert json.loads((tmp_path / "project.json").read_text())["title"] == "Two commands"
    assert res["seconds"] == 9.0


def test_a_failing_step_stops_the_recording_keeps_the_take_and_says_where(tmp_path):
    r, desk, rec, _ = make(tmp_path)
    desk.fail_at = lambda call: call == ("char", "-")
    res = r.run(export=True)
    assert not res["ok"] and res["outputs"] == []
    assert res["error"] == {"where": "step 2 (type)", "line": 9, "why": "the keyboard fell off"}
    assert res["project"] == str(tmp_path) and rec.stopped == 1 and not rec.active()
    assert res["steps_done"] == 1
    assert "close" in kinds(desk) and desk.ws == 2                # cleanup and the way home still happen


def test_the_time_limit_ends_the_take(tmp_path):
    r, desk, rec, clock = make(tmp_path, 'max_seconds = 3\n[[step]]\nwait = 60\n[[step]]\ntype = "never"\n')
    t0 = clock.now()
    res = r.run(export=False)
    assert not res["ok"] and "max_seconds = 3" in res["error"]["why"] and res["error"]["where"] == "step 1"
    assert rec.stopped == 1 and "char" not in kinds(desk)
    assert clock.now() - t0 < 5


def test_ctrl_c_stops_cleanly(tmp_path):
    r, desk, rec, _ = make(tmp_path)
    desk.on_call = lambda call: call == ("char", "s") and setattr(r, "interrupted", True)
    res = r.run(export=False)
    assert not res["ok"] and "interrupted" in res["error"]["why"]
    assert rec.stopped == 1 and typed(desk) == "ls"
    assert desk.ws == 2 and "close" in kinds(desk)


def test_dolly_stop_from_outside_is_an_early_clean_end(tmp_path):
    r, desk, rec, _ = make(tmp_path)
    desk.on_call = lambda call: call == ("char", " ") and setattr(rec, "on", False)
    res = r.run(export=False)
    assert res["ok"] and rec.stopped == 0                         # we do not stop it a second time
    assert any("stopped from outside" in w for w in res["warnings"])
    assert (tmp_path / "captions.srt").exists()


def test_refuses_while_another_recording_runs_and_touches_nothing(tmp_path):
    r, desk, rec, _ = make(tmp_path)
    rec.on = True
    res = r.run(export=False)
    assert not res["ok"] and "already running" in res["error"]["why"]
    assert desk.calls == [] and rec.started == [] and rec.stopped == 0


def test_click_steps_are_refused_up_front_without_a_click_tool(tmp_path):
    r, desk, rec, _ = make(tmp_path, '[[step]]\nclick = "50%,50%"\n')
    res = r.run(export=False)
    assert not res["ok"] and "click steps need" in res["error"]["why"] and desk.calls == [] and rec.started == []
    r, desk, rec, _ = make(tmp_path, '[[step]]\nclick = "50%,50%"\n', dict(CAPS, click=True))
    assert r.run(export=False)["ok"] and ("click", "left", None) in [c[1:] for c in desk.calls]
    text = '[[setup]]\nopen = "terminal"\nas = "t"\n[[step]]\nclick = "50%,50%"\nin = "t"\n'
    r, desk, rec, _ = make(tmp_path, text, dict(CAPS, click=True))
    assert r.run(export=False)["ok"]
    k = [c[1:] for c in desk.calls]                               # the named window comes forward, then gets the click
    assert k.index(("focus", "0x1")) < k.index(("move", 500, 350)) < k.index(("click", "left", "0x1"))


def test_a_window_take_records_that_window(tmp_path):
    text = ('record = "window:term"\nstage = "here"\n[[setup]]\nopen = "terminal"\nas = "term"\n'
            '[[step]]\nmove = "10,10"\n')
    r, desk, rec, _ = make(tmp_path, text)
    assert r.run(export=False, keep_open=True)["ok"]
    assert rec.started == [("window", "address:0x1")]        # the window itself, not its rectangle
    assert ("move", 110, 60) in [c[1:] for c in desk.calls]       # points measure from the recorded window
    assert "workspace" not in kinds(desk) and "close" not in kinds(desk)   # stage "here", --keep-open


def test_a_setup_failure_never_starts_the_recorder(tmp_path):
    r, _, rec, _ = make(tmp_path, '[[setup]]\nfocus = "class:nothing"\n[[step]]\nwait = 1\n')
    res = r.run(export=False)
    assert res["error"]["where"] == "setup 1 (focus)" and "no window matches" in res["error"]["why"]
    assert rec.started == [] and res["project"] is None


def test_cli_validation_errors_and_dry_run(tmp_path, capsys):
    from dolly.cli import main
    bad = tmp_path / "bad.toml"
    bad.write_text('[[step]]\ntype = "a"\nfast = true\n')
    with pytest.raises(SystemExit) as e:
        main(["run", str(bad), "--dry-run"])
    assert e.value.code == 2
    assert f"{bad}: line 1: step 1 (type): unknown option fast" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        main(["run", str(bad), "--dry-run", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert e.value.code == 2 and out["error"]["line"] == 1 and not out["ok"]
    good = tmp_path / "good.toml"
    good.write_text(BOARD)
    with pytest.raises(SystemExit) as e:
        main(["run", str(good), "--dry-run", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert e.value.code == 0 and out["ok"] and [p["action"] for p in out["plan"]][:2] == ["open", "say"]


def test_the_log_gets_what_was_typed_not_wtype_keycodes(tmp_path):
    from dolly.inject import Feed, char_code
    f = Feed(tmp_path / "feed.jsonl")
    f.tap(char_code("l")[0])
    f.tap(char_code("L")[0], ("shift",))
    f.tap(char_code("|")[0], ("shift",))
    f.click(3, 4)
    f.close()
    rows = [json.loads(x) for x in (tmp_path / "feed.jsonl").read_text().splitlines()]
    keys = [(r["code"] - 8, r["down"]) for r in rows if r["type"] == "key"]
    assert keys[:2] == [(38, True), (38, False)]                               # l
    assert keys[2:6] == [(42, True), (38, True), (38, False), (42, False)]     # shift held around L, then released
    assert keys[6:10] == [(42, True), (43, True), (43, False), (42, False)]    # | is shift + backslash
    assert rows[-1] == {"type": "click", "x": 3, "y": 4, "button": "left"}

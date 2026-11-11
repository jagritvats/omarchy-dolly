"""Storyboards (`dolly run`): parsing, validation messages, and a plan that is the same every time."""
import json
import random

import pytest

from dolly import storyboard as sb

GOOD = '''
title  = "Listing files"
record = "window:term"
aspect = ["16:9", "9:16"]

[[setup]]
open = "terminal"
as   = "term"

[[step]]
type  = "ls -la"
enter = true

[[step]]
wait = "quiet"

[[step]]
key = "ctrl+l"

[[step]]
move = "50%,40%"
in   = "term"

[[step]]
say = "That is the whole folder"

[[cleanup]]
close = "term"
'''


def err(text):
    with pytest.raises(sb.StoryboardError) as e:
        sb.loads(text)
    return e.value


def test_good_file_parses():
    s = sb.loads(GOOD)
    assert (s.title, s.record, s.aspect, s.stage) == ("Listing files", "window:term", ["16:9", "9:16"], "clean")
    assert [x.action for x in s.steps] == ["type", "wait", "key", "move", "say"]
    assert s.steps[0].opts == {"enter": True} and s.steps[0].line == 10
    assert s.setup[0].opts["as"] == "term" and s.cleanup[0].value == "term"


def test_json_is_the_same_storyboard():
    data = {"title": "Listing files", "step": [{"type": "ls", "enter": True}, {"wait": 1}]}
    s = sb.loads(json.dumps(data))
    assert [x.action for x in s.steps] == ["type", "wait"] and s.record == "screen"


@pytest.mark.parametrize("text, line, where, needle", [
    ('[[step]]\ntype = "a"\nwait = 2\n', 1, "step 1", "exactly one action"),
    ('[[step]]\nwait = 1\n\n[[step]]\nspeed = 3\n', 4, "step 2", "found: none"),
    ('[[step]]\ntype = "a"\nfast = true\n', 1, "step 1 (type)", "unknown option fast"),
    ('[[step]]\ntype = """a\nb"""\n', 1, "step 1 (type)", "one line per step"),
    ('[[step]]\nkey = "hyper+x"\n', 1, "step 1 (key)", '"hyper" is not a modifier'),
    ('[[step]]\nkey = "ctrl+banana"\n', 1, "step 1 (key)", '"banana" is not a key'),
    ('[[step]]\nwait = "forever"\n', 1, "step 1 (wait)", "number of seconds"),
    ('[[step]]\nclick = "middle of it"\n', 1, "step 1 (click)", "is a point like"),
    ('[[step]]\nfocus = "editor"\n', 1, "step 1 (focus)", 'no window named "editor"'),
    ('[[setup]]\nsay = "hi"\n[[step]]\nwait = 1\n', 1, "setup 1 (say)", "only makes sense while recording"),
    ('aspect = "21:9"\n[[step]]\nwait = 1\n', 1, "aspect", "one or more of"),
    ('colour = "red"\n[[step]]\nwait = 1\n', 1, "colour", "unknown setting"),
    ('title = "x"\nmax_seconds = 5000\n[[step]]\nwait = 1\n', 2, "max_seconds", "from 2 to 900"),
    ('record = "window:term"\n[[step]]\nwait = 1\n', 0, "record", "open it in a [[setup]] step"),
    ('title = "only a title"\n', 0, "", "add at least one [[step]]"),
    ('[[step]\nwait = 1\n', 1, "", "not valid TOML"),
])
def test_bad_files_say_where_and_what(text, line, where, needle):
    e = err(text)
    assert needle in e.msg, e.msg
    assert (e.line, e.where) == (line, where)
    if line:
        assert str(e).startswith(f"line {line}: ")


def test_selectors_need_no_name_and_names_come_from_open():
    s = sb.loads('[[step]]\nfocus = "class:foot"\n[[step]]\nopen = "foot"\nas = "t"\n[[step]]\nfocus = "t"\n')
    assert len(s.steps) == 3


def test_chords_and_points():
    assert sb.parse_chord("Ctrl+Shift+T") == (["ctrl", "shift"], "t")
    assert sb.parse_chord("super+3") == (["super"], "3")
    assert sb.parse_chord("enter") == ([], "enter")
    assert sb.parse_chord("ctrl++") == (["ctrl"], "+")
    assert sb.parse_point("50%,40%") == ((50.0, True), (40.0, True))
    assert sb.parse_point(" 640, 360 ") == ((640.0, False), (360.0, False))


def test_typing_has_a_human_rhythm():
    text = "git commit -m 'Fix the zoom'"
    d = sb.keystroke_delays(text, 11, random.Random(4))
    assert len(d) == len(text) and d[0] == 0.0
    rest = d[1:]
    assert len({round(x, 3) for x in rest}) > len(rest) * 0.8          # no metronome
    assert 0.03 < min(rest) and max(rest) <= 6 / 11 + 1e-9             # no burst, no stall
    assert 7 < len(rest) / sum(rest) < 13                              # about the asked speed
    inside = [x for x, a, b in zip(rest, text, text[1:]) if a.isalnum() and b.isalnum()]
    starts = [x for x, a in zip(rest, text) if a == " "]
    assert sum(starts) / len(starts) > sum(inside) / len(inside)       # a beat before each word


def test_plan_is_the_same_every_time_and_follows_the_seed():
    a, b = sb.plan(sb.loads(GOOD)), sb.plan(sb.loads(GOOD))
    rows = lambda p: [(r.start, r.seconds, r.delays, r.pause) for r in p["steps"]]   # noqa: E731
    assert rows(a) == rows(b) and a["recorded"] == b["recorded"]
    c = sb.plan(sb.loads("seed = 7\n" + GOOD))
    assert rows(c) != rows(a)
    assert a["steps"][0].start == sb.LEAD_IN and a["open_ended"]
    assert a["recorded"] == pytest.approx(sum(r.seconds for r in a["steps"]) + sb.LEAD_IN + sb.TAIL, abs=0.02)


def test_dry_run_text_shows_every_command_and_runs_nothing(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("validation ran a command"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("validation ran a command"))
    text = sb.describe(sb.loads('[[setup]]\nopen = "foot -e htop"\nas = "h"\n[[step]]\ntype = "q"\n'))
    assert 'open  "foot -e htop"' in text and "[runs a command]" in text
    assert "Before recording (off camera):" in text and "Recorded:" in text


def test_presets_are_checked_before_anything_runs():
    from dolly.share import presets
    assert set(sb.PRESETS) == set(presets.PRESETS)
    assert sb.loads('preset = "github"\n[[step]]\nwait = 1\n').preset == "github"
    e = err('preset = "tiktok"\n[[step]]\nwait = 1\n')
    assert (e.line, e.where) == (1, "preset") and "is not a share preset" in e.msg

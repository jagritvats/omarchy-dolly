"""What a recording keeps of the keyboard.

A take needs to know *when* text was typed (typing zooms, idle speed-ups) and *which*
shortcut was pressed (the key overlay). It must never hold what was typed: a password
entered in a terminal or a web page while recording would sit in the take's files, be
sent to the editor page, and go wherever the folder goes.

Also here: the input bridge's file of raw key codes is erased as it is read and removed
when the bridge stops.
"""
import json
import os
import threading
import time

import pytest

from dolly import events, fsutil, keymap, library
from dolly.events import Timeline
from dolly.logger import Logger, drop_bridge_file, runtime_dir

TYPED = keymap.TYPED


def test_typed_characters_lose_their_name_and_shift():
    for name in ("a", "Z", "7", "-", ";", "space"):
        assert keymap.recorded(name, []) == (TYPED, [])
        assert keymap.recorded(name, ["shift"]) == (TYPED, [])


def test_shortcuts_and_keys_that_type_nothing_keep_their_names():
    assert keymap.recorded("s", ["ctrl"]) == ("s", ["ctrl"])
    assert keymap.recorded("t", ["shift", "ctrl"]) == ("t", ["ctrl", "shift"])
    assert keymap.recorded("space", ["super"]) == ("space", ["super"])
    for name in ("Return", "Escape", "Tab", "BackSpace", "Left", "F5", "Delete"):
        assert keymap.recorded(name, []) == (name, [])
    assert keymap.recorded("Tab", ["shift"]) == ("Tab", ["shift"])
    assert keymap.recorded(TYPED, []) == (TYPED, [])          # already redacted: unchanged


def test_a_character_typed_with_altgr_is_not_a_shortcut():
    # AltGr+Q types @ on a German keyboard; a log from before 0.7.2 does not say which Alt it was
    assert keymap.recorded("q", ["alt"]) == (TYPED, [])
    assert keymap.recorded("q", ["alt", "shift"]) == (TYPED, [])
    assert keymap.recorded("q", ["alt"], left_alt=True) == ("q", ["alt"])
    assert keymap.recorded("q", ["alt", "ctrl"]) == ("q", ["alt", "ctrl"])
    assert keymap.recorded("Left", ["alt"]) == ("Left", ["alt"])              # types nothing either way
    assert keymap.stored("q", ["alt"], left_alt=True) == {"key": "q", "mods": ["alt"], "lalt": True}
    assert keymap.stored("q", ["alt"]) == {"key": TYPED, "mods": []}
    assert keymap.stored("s", ["ctrl"]) == {"key": "s", "mods": ["ctrl"]}


def test_the_logger_tells_left_alt_from_altgr(tmp_path):
    log = Logger(tmp_path)
    log.on_key(100, True)                             # Right Alt (AltGr) down
    log.on_key(16, True); log.on_key(16, False)       # Q: an @ on many keyboards
    log.on_key(100, False)
    log.on_key(56, True)                              # Left Alt down
    log.on_key(16, True); log.on_key(16, False)       # Alt+Q, a shortcut
    log.on_key(100, True); log.on_key(100, False)     # the other Alt pressed and let go meanwhile
    log.on_key(2, True); log.on_key(2, False)         # Alt+1, still held
    log.on_key(56, False)
    log.on_key(16, True)                              # a plain q
    log.f.close()
    keys = [json.loads(x) for x in (tmp_path / "events.raw.jsonl").read_text().splitlines()]
    keys = [{k: v for k, v in e.items() if k in ("key", "mods", "lalt")} for e in keys if e["type"] == "key"]
    assert keys == [{"key": TYPED, "mods": []}, {"key": "q", "mods": ["alt"], "lalt": True},
                    {"key": "1", "mods": ["alt"], "lalt": True}, {"key": TYPED, "mods": []}]
    # and the mark survives into events.json's reader, while an unmarked one is taken as typed
    doc = {"video": {"w": 64, "h": 36, "fps": 30, "duration": 5.0}, "capture": {},
           "events": [{"t": 1.0, "type": "key", **keys[1]}, {"t": 2.0, "type": "key", "key": "q", "mods": ["alt"]}]}
    assert [(k["key"], k["mods"]) for k in Timeline.from_dict(doc).keys] == [("q", ["alt"]), (TYPED, [])]


def _key_events(pdir):
    return [json.loads(line) for line in (pdir / "events.raw.jsonl").read_text().splitlines()
            if json.loads(line).get("type") == "key"]


def _press(log, code, down=True):
    log.on_key(code, down)


def test_the_logger_never_writes_what_was_typed(tmp_path):
    log = Logger(tmp_path)
    for code in (35, 18, 38, 38, 24):                 # h e l l o
        _press(log, code)
        _press(log, code, False)
    _press(log, 42)                                   # Shift down
    _press(log, 25); _press(log, 25, False)           # P
    _press(log, 42, False)
    _press(log, 28); _press(log, 28, False)           # Enter
    _press(log, 29)                                   # Ctrl down
    _press(log, 31); _press(log, 31, False)           # Ctrl+S
    _press(log, 29, False)
    log.f.close()
    keys = _key_events(tmp_path)
    assert [(k["key"], k["mods"]) for k in keys] == [(TYPED, [])] * 6 + [("Return", []), ("s", ["ctrl"])]
    text = (tmp_path / "events.raw.jsonl").read_text()
    for ch in "helo":
        assert f'"key":"{ch}"' not in text
    assert "shift" not in text


def test_nothing_is_written_while_a_password_manager_has_focus(tmp_path):
    log = Logger(tmp_path)
    log.secret_focus = True
    for code in (35, 18, 28):
        _press(log, code)
        _press(log, code, False)
    log.f.close()
    assert _key_events(tmp_path) == []


OLD_EVENTS = [
    {"t": 1.0, "type": "key", "key": "h", "mods": []},
    {"t": 1.1, "type": "key", "key": "u", "mods": []},
    {"t": 1.2, "type": "key", "key": "N", "mods": ["shift"]},
    {"t": 1.3, "type": "key", "key": "space", "mods": []},
    {"t": 1.4, "type": "key", "key": "2", "mods": []},
    {"t": 1.5, "type": "key", "key": "Return", "mods": []},
    {"t": 2.0, "type": "key", "key": "s", "mods": ["ctrl"]},
    {"t": 2.5, "type": "click", "x": 10, "y": 10, "button": "left"},
]


def _old_doc():
    return {"video": {"w": 640, "h": 360, "fps": 30, "duration": 5.0}, "capture": {},
            "events": [dict(e) for e in OLD_EVENTS]}


def test_an_older_recording_is_redacted_when_it_is_loaded():
    tl = Timeline.from_dict(_old_doc())
    assert [(k["key"], k["mods"]) for k in tl.keys] == [(TYPED, [])] * 5 + [("Return", []), ("s", ["ctrl"])]
    assert [k["t"] for k in tl.keys] == [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 2.0]      # the times stay


def _old_take(d):
    d.mkdir(parents=True)
    (d / "events.json").write_text(json.dumps(_old_doc()))
    (d / "events.json.bak").write_text(json.dumps(_old_doc()))
    raw = [{"type": "start", "ms": 0}] + [
        {"type": "key", "ms": int(e["t"] * 1000), "key": e["key"], "mods": e["mods"]}
        for e in OLD_EVENTS if e["type"] == "key"]
    (d / "events.raw.jsonl").write_text("\n".join(json.dumps(x, separators=(",", ":")) for x in raw) + "\nnot json\n")
    return d


def test_scrub_rewrites_the_files_of_an_older_recording(tmp_path):
    d = _old_take(tmp_path / "2026-11-10_10-00-00")
    old = time.time() - 86400
    os.utime(d / "events.json", (old, old))
    assert events.scrub_typed_keys(d) == 15                     # 5 typed keys in each of three files
    for name in ("events.json", "events.json.bak", "events.raw.jsonl"):
        text = (d / name).read_text()
        evs = json.loads(text)["events"] if name != "events.raw.jsonl" else \
            [json.loads(x) for x in text.splitlines() if x.startswith("{")]
        keys = [(e["key"], e["mods"]) for e in evs if e.get("type") == "key"]
        assert keys == [(TYPED, [])] * 5 + [("Return", []), ("s", ["ctrl"])], name
        assert "shift" not in text and "space" not in text, name
    doc = json.loads((d / "events.json").read_text())
    assert [e["t"] for e in doc["events"]] == [e["t"] for e in OLD_EVENTS]
    assert (d / "events.raw.jsonl").read_text().splitlines()[-1] == "not json"   # unreadable lines are kept
    assert abs((d / "events.json").stat().st_mtime - old) < 2                    # caches still match
    assert not list(d.glob("*.tmp")) and len(list(d.iterdir())) == 3            # no copy of the old text
    assert events.scrub_typed_keys(d) == 0                                       # nothing left to do


def test_the_library_is_scrubbed_once(tmp_path, monkeypatch):
    root = tmp_path / "Dolly"
    monkeypatch.setenv("DOLLY_DIR", str(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _old_take(root / "2026-11-10_10-00-00")
    _old_take(root / "2026-11-11_10-00-00")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    trashed = _old_take(tmp_path / "data" / "Trash" / "files" / "2026-11-09_10-00-00")
    (trashed / "capture.json").write_text("{}")
    other = _old_take(tmp_path / "data" / "Trash" / "files" / "not-a-recording")     # no capture.json: not ours
    before = (other / "events.json").read_text()
    r = library.scrub_typed_keys(once=True)
    assert r["recordings"] == 3 and r["keys"] == 45              # the two in the library and the trashed one
    assert '"h"' not in (trashed / "events.raw.jsonl").read_text()
    assert (other / "events.json").read_text() == before
    assert library.scrub_typed_keys(once=True) is None           # the session start asks every time
    assert library.scrub_typed_keys()["keys"] == 0               # `dolly library scrub-keys` always looks


def test_another_folder_can_be_scrubbed_and_a_failure_is_tried_again(tmp_path, monkeypatch):
    monkeypatch.setenv("DOLLY_DIR", str(tmp_path / "Dolly"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    backup = tmp_path / "backup"
    _old_take(backup / "2026-11-01_10-00-00")
    one = _old_take(tmp_path / "single")
    assert library.scrub_typed_keys(folder=backup)["keys"] == 15         # a folder of recordings
    assert library.scrub_typed_keys(folder=one)["keys"] == 15            # or one recording
    assert not library._scrub_stamp().exists()                           # says nothing about the library itself
    _old_take(tmp_path / "Dolly" / "2026-11-10_10-00-00")
    import dolly.events as ev
    real = ev.scrub_typed_keys
    monkeypatch.setattr(ev, "scrub_typed_keys", lambda d: (_ for _ in ()).throw(OSError("read-only")))
    assert library.scrub_typed_keys(once=True)["failed"] >= 1
    assert not library._scrub_stamp().exists()                           # not done: the next session start tries again
    monkeypatch.setattr(ev, "scrub_typed_keys", real)
    assert library.scrub_typed_keys(once=True)["keys"] == 15 and library._scrub_stamp().exists()


def test_the_session_start_does_not_wait_for_the_scrub(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
    monkeypatch.setattr(library, "scrub_typed_keys", lambda *a, **k: pytest.fail("ran in the foreground"))
    assert library.scrub_typed_keys_in_background() is True
    cmd, kw = started[0]
    assert cmd[1:] == ["-m", "dolly.cli", "library", "scrub-keys", "--once"] and kw["start_new_session"] is True
    library._scrub_stamp().write_text("1\n")
    assert library.scrub_typed_keys_in_background() is False and len(started) == 1      # done once: nothing started


# ------------------------------------------------------------------ the input bridge's file
def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _line(obj) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(Logger, "BRIDGE_KEEP", 0.05)
    log = Logger(tmp_path)
    path = tmp_path / "input.jsonl"
    path.write_bytes(_line({"type": "bridge", "clicks": 3, "keys": True}))
    th = threading.Thread(target=log.tail_lua, args=(path,), daemon=True)
    th.start()
    yield log, path, tmp_path
    log.stop.set()
    th.join(2)
    if not log.f.closed:
        log.f.close()


def test_what_was_read_from_the_bridge_file_is_erased(bridge):
    log, path, pdir = bridge
    with open(path, "ab", buffering=0) as w:              # the bridge appends, as Hyprland's Lua does
        w.write(_line({"type": "click", "x": 5.0, "y": 6.0, "button": "left"}))
        for code in (43, 26, 46):                          # key codes as Hyprland reports them (evdev + 8)
            w.write(_line({"type": "key", "code": code, "down": True}))
            w.write(_line({"type": "key", "code": code, "down": False}))
        size = path.stat().st_size
        got = lambda: [json.loads(x) for x in (pdir / "events.raw.jsonl").read_text().splitlines()]
        assert _wait(lambda: sum(e["type"] == "key" for e in got()) == 3)
        assert [e["type"] for e in got()] == ["click", "key", "key", "key"]
        probe = pdir / "probe"
        probe.write_bytes(b"x" * 8192)
        with open(probe, "r+b") as pf:
            if not fsutil.erase_range(pf.fileno(), 0, 4096):
                pytest.skip("this filesystem cannot punch holes; the file is still removed at stop")
        assert _wait(lambda: path.read_bytes().strip(b"\0") == b"")
        assert path.stat().st_size == size                 # same length: the writer is undisturbed
        assert b'"code"' not in path.read_bytes()
        w.write(_line({"type": "key", "code": 36, "down": True}))      # Enter, after the erase
        assert _wait(lambda: any(e.get("key") == "Return" for e in got()))


def test_a_bridge_file_emptied_by_another_logger_is_followed_from_its_start(bridge):
    log, path, pdir = bridge
    with open(path, "ab", buffering=0) as w:
        w.write(_line({"type": "click", "x": 1.0, "y": 1.0, "button": "left"}) * 40)
    got = lambda: (pdir / "events.raw.jsonl").read_text().count('"click"')
    assert _wait(lambda: got() == 40)
    os.truncate(path, 0)                                   # what a second logger does when it starts
    with open(path, "ab", buffering=0) as w:
        w.write(_line({"type": "bridge", "clicks": 3, "keys": True}))
        w.write(_line({"type": "click", "x": 2.0, "y": 2.0, "button": "right"}))
    assert _wait(lambda: got() == 41)


def test_the_bridge_file_goes_when_the_bridge_stops():
    f = runtime_dir() / "input.jsonl"
    f.write_text('{"type":"key","code":38,"down":true}\n')
    Logger.stop_lua_bridge()                               # hyprctl is a do-nothing shim in the tests
    assert not f.exists()
    drop_bridge_file()                                     # and it is fine when there is none


def test_the_bridge_file_is_private_and_not_left_behind_when_the_bridge_fails(tmp_path, monkeypatch):
    import stat
    import dolly.logger as logmod
    f = runtime_dir() / "input.jsonl"
    f.write_text("left over from an earlier take\n")
    os.chmod(f, 0o644)
    seen = {}

    def no_hyprland(lua):
        seen["mode"], seen["size"] = stat.S_IMODE(f.stat().st_mode), f.stat().st_size
        return False, "no Hyprland here"
    monkeypatch.setattr(logmod, "hypr_eval", no_hyprland)
    log = Logger(tmp_path)
    assert log.start_lua_bridge() is False
    log.f.close()
    assert seen == {"mode": 0o600, "size": 0}              # ours alone, and emptied, before the bridge loads
    assert not f.exists()

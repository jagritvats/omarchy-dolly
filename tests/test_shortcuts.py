"""Shortcuts are settings: every hotkey can be changed or switched off, a change rewrites
only Dolly's marked block, taken keys are left alone, and the menu has one entry."""
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import config as C  # noqa: E402
from dolly import integration as I  # noqa: E402

USER_BINDS = '''-- my own keys
o.bind("SUPER + Q", "Close window", "close")
o.bind("SUPER + RETURN", "Terminal", "foot")
'''
USER_MENU = '''{
  // mine
  "trigger.capture.mine": {"icon": "x", "label": "Mine", "action": "true"}
}
'''
# what Dolly 0.6.1's setup wrote (six rows on top level, a share row elsewhere)
OLD_BINDS = '''-- >>> dolly (managed by dolly-setup)
local dolly_present = io.open("DOLLY", "r")
if dolly_present then dolly_present:close()
  o.bind("SUPER + ALT + R", "Dolly: record / stop demo", "DOLLY toggle")
  o.bind("SUPER + ALT + P", "Dolly: pause / resume recording", "DOLLY pause")
  o.bind("SUPER + ALT + M", "Dolly: zoom here (while recording)", "DOLLY mark")
  o.bind("SUPER + ALT + SHIFT + R", "Dolly: save instant replay", "DOLLY replay save --last 30")
  o.bind("SUPER + ALT + N", "Dolly: mute / unmute microphone", "DOLLY mute")
  o.window({ class = "^DollyCam-(S|M|L)$" }, { float = true, pin = true, no_initial_focus = true, no_dim = true })
end
-- <<< dolly
'''
OLD_MENU_BLOCK = '''  // >>> dolly (managed by dolly-setup; remove with dolly-setup --undo)
  "trigger.capture.dolly-stop": {"icon": "s", "label": "Stop demo recording", "when": "true", "action": "DOLLY stop"},
  "trigger.capture.dolly-record": {"icon": "r", "label": "Record demo", "when": "true"},
  "trigger.capture.dolly-record.window": {"icon": "w", "label": "Window", "action": "DOLLY record --mode window"},
  "trigger.capture.dolly-library": {"icon": "l", "label": "Dolly recordings", "action": "DOLLY library --open"},
  "trigger.share.dolly": {"icon": "h", "label": "Last demo recording", "when": "true"},
  // <<< dolly
'''
LIVE_L = [{"modmask": 64 + 8, "key": "L", "description": "Lock screen", "dispatcher": "exec", "arg": "omarchy-lock"}]


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from dolly import log
    log.get()       # Dolly's logger is opened once per test run: open it before this test moves XDG_STATE_HOME
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("DOLLY_CONFIG", raising=False)
    monkeypatch.setenv("DOLLY_NO_HYPR_RELOAD", "1")
    monkeypatch.setenv("DOLLY_NO_BAR_SYNC", "1")
    (tmp_path / "run").mkdir()
    (tmp_path / ".config/hypr").mkdir(parents=True)
    (tmp_path / ".config/omarchy/extensions").mkdir(parents=True)
    (tmp_path / ".config/hypr/bindings.lua").write_text(USER_BINDS)
    (tmp_path / ".config/omarchy/extensions/omarchy-menu.jsonc").write_text(USER_MENU)
    monkeypatch.setattr(I, "live_binds", lambda timeout=2.0: [])
    monkeypatch.setattr(I, "_reload_hyprland", lambda: True)
    return tmp_path


def binds_text(home):
    return (home / ".config/hypr/bindings.lua").read_text()


def menu_rows(home):
    raw = (home / ".config/omarchy/extensions/omarchy-menu.jsonc").read_text()
    body = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("//"))
    return json.loads(re.sub(r",\s*([}\]])", r"\1", body))


def outside_block(text):
    return I.BLOCK_LINES.sub("", text)


# ---------------------------------------------------------------- combinations
def test_combinations_are_normalised_and_checked():
    assert I.normalize_combo("super+alt+r") == "SUPER + ALT + R"
    assert I.normalize_combo("Shift + Alt+SUPER + r") == "SUPER + ALT + SHIFT + R"
    assert I.normalize_combo("ctrl alt F9") == "CTRL + ALT + F9"
    assert I.normalize_combo("WIN + Print") == "SUPER + Print"
    assert I.normalize_combo("F9") == "F9"
    for off in ("", "  ", "off", "None", "disabled"):
        assert I.normalize_combo(off) == ""
    for bad in ("r", "SHIFT + R", "SUPER + ALT", "SUPER + A + B", "SUPER + $(reboot)", 'SUPER + "'):
        with pytest.raises(I.ShortcutError):
            I.normalize_combo(bad)


def test_every_shortcut_is_a_setting_with_the_old_default(home):
    cfg = C.load()
    assert cfg["shortcuts"] == {s["id"]: s["default"] for s in I.SHORTCUTS}
    assert cfg["shortcuts"]["toggle"] == "SUPER + ALT + R"
    assert "shortcuts" in C.GROUPS and C.BY_KEY["shortcuts.toggle"]["type"] == "shortcut"


def test_config_rejects_nonsense_and_two_shortcuts_on_the_same_keys(home):
    with pytest.raises(C.ConfigError, match="needs SUPER, CTRL or ALT"):
        C.set_many({"shortcuts.toggle": "r"})
    with pytest.raises(C.ConfigError, match="used twice"):
        C.set_many({"shortcuts.pause": "alt + super + R"})
    assert C.load()["shortcuts"]["pause"] == "SUPER + ALT + P"       # nothing was saved


# ---------------------------------------------------------------- the hotkey block
def test_apply_writes_the_chosen_keys_and_nothing_else(home):
    C.set_many({"shortcuts.toggle": "super+ctrl+d", "shortcuts.mark": ""})
    res = I.apply()
    text = binds_text(home)
    assert 'o.bind("SUPER + CTRL + D", "Dolly: record / stop demo"' in text
    assert "zoom here" not in text                                    # switched off
    assert 'o.bind("SUPER + ALT + P", "Dolly: pause / resume recording"' in text
    assert outside_block(text) == USER_BINDS                          # the user's lines, byte for byte
    states = {b["id"]: b["state"] for b in res["binds"]}
    assert states["toggle"] == "applied" and states["mark"] == "off"
    assert (home / ".config/hypr/bindings.lua.dolly-bak").read_text() == USER_BINDS


def test_keys_that_belong_to_something_else_are_left_alone(home, monkeypatch):
    monkeypatch.setattr(I, "live_binds", lambda timeout=2.0: LIVE_L)
    res = I.apply()
    assert "recordings library" not in binds_text(home)
    lib = next(b for b in res["binds"] if b["id"] == "library")
    assert lib["state"] == "taken" and lib["others"] == ["Lock screen"]
    row = next(r for r in I.shortcut_view() if r["id"] == "library")
    assert row["state"] == "taken" and row["others"] == ["Lock screen"]
    assert next(r for r in I.shortcut_view() if r["id"] == "toggle")["state"] == "active"


def test_a_changed_shortcut_rewrites_the_block_only_after_set_up(home):
    C.set_many({"shortcuts.pause": "SUPER + ALT + O"})               # never set up: no file is touched
    assert binds_text(home) == USER_BINDS
    assert next(r for r in I.shortcut_view() if r["id"] == "pause")["state"] == "pending"
    I.apply()
    C.set_many({"shortcuts.pause": "SUPER + ALT + U"})               # set up: the block follows the setting
    text = binds_text(home)
    assert 'o.bind("SUPER + ALT + U", "Dolly: pause / resume recording"' in text and "ALT + O" not in text
    assert outside_block(text) == USER_BINDS


def test_apply_twice_changes_nothing_the_second_time(home):
    I.apply()
    b, m = binds_text(home), (home / ".config/omarchy/extensions/omarchy-menu.jsonc").read_text()
    res = I.apply()
    assert res["changed"] == [] and res["binds_file"] == "unchanged" and res["menu"] == "unchanged"
    assert binds_text(home) == b and (home / ".config/omarchy/extensions/omarchy-menu.jsonc").read_text() == m


# ---------------------------------------------------------------- the menu
def test_the_capture_menu_has_one_dolly_entry_at_a_time(home):
    I.apply()
    rows = menu_rows(home)
    assert rows["trigger.capture.mine"]["label"] == "Mine"            # the user's own row is still there
    dolly = {k: v for k, v in rows.items() if "dolly" in k}
    top = [k for k in dolly if k.count(".") == 2]                     # trigger.capture.<row>
    assert sorted(top) == ["trigger.capture.dolly", "trigger.capture.dolly-stop"]
    assert not any(k.startswith("trigger.share.") for k in dolly)     # sharing lives inside Demo now
    from dolly.guards import GUARDS
    assert rows["trigger.capture.dolly"]["when"].endswith(GUARDS["not_recording"])       # idle: "Demo"
    assert rows["trigger.capture.dolly-stop"]["when"].endswith(GUARDS["recording"])      # recording: Stop, one step
    assert rows["trigger.capture.dolly-stop"]["action"].endswith("bin/dolly stop")
    assert rows["trigger.capture.dolly"]["label"] == "Demo" and "action" not in rows["trigger.capture.dolly"]
    inside = {k.split(".", 3)[3] for k in dolly if k.startswith("trigger.capture.dolly.")}
    assert {"window", "region", "screen", "last", "library", "settings", "share", "share.github",
            "replay-on", "replay-save", "replay-off"} <= inside
    for k, v in dolly.items():
        assert v["when"].startswith("[ -x "), k                      # gone with the plugin
        assert len(v["label"]) <= 18, (k, v["label"])                 # Omarchy's menu cuts longer labels off
        if ".dolly." in k and "record --mode" in v.get("action", ""):
            assert GUARDS["not_recording"] in v["when"], k            # search must not offer a second take


# ---------------------------------------------------------------- an update
def old_install(home, launcher):
    (home / ".config/hypr/bindings.lua").write_text(USER_BINDS + OLD_BINDS.replace("DOLLY", launcher))
    (home / ".config/omarchy/extensions/omarchy-menu.jsonc").write_text(
        USER_MENU.rstrip()[:-1].rstrip() + ",\n" + OLD_MENU_BLOCK.replace("DOLLY", launcher) + "}\n")


def test_an_update_refreshes_what_set_up_wrote_and_keeps_the_keys(home):
    launcher = str(I.launcher())
    old_install(home, launcher)
    before = I.BIND_LINE.findall(binds_text(home))
    res = I.auto_apply()                                              # no stamp: Set up was run by an older version
    assert res is not None and "menu" in res["changed"] and "link" in res["changed"]
    assert I.BIND_LINE.findall(binds_text(home))[:len(before)] == before          # same keys, same names
    assert outside_block(binds_text(home)) == USER_BINDS
    rows = menu_rows(home)
    assert "trigger.capture.dolly" in rows and "trigger.capture.dolly-record" not in rows
    assert "trigger.share.dolly" not in rows and rows["trigger.capture.mine"]["label"] == "Mine"
    assert os.readlink(home / ".local/bin/dolly") == launcher
    assert I.stamp()["version"] == I.version() and I.stamp()["set_up"] is True
    assert I.auto_apply() is None                                     # the same version again: nothing


def test_an_update_does_nothing_for_someone_who_never_ran_set_up(home):
    assert I.auto_apply() is None
    assert binds_text(home) == USER_BINDS
    assert (home / ".config/omarchy/extensions/omarchy-menu.jsonc").read_text() == USER_MENU
    assert not (home / ".local/bin/dolly").exists()
    assert I.stamp()["version"] == I.version() and I.stamp()["set_up"] is False     # asked the files once


def test_undo_removes_it_all_and_an_update_does_not_bring_it_back(home):
    I.apply()
    res = I.undo()
    assert set(res["changed"]) == {"menu", "binds", "link"}
    assert binds_text(home) == USER_BINDS and "dolly" not in json.dumps(menu_rows(home))
    assert not (home / ".local/bin/dolly").is_symlink()
    (I.stamp_file()).write_text(json.dumps({"version": "0.0.1", "set_up": False}))        # then a new version arrives
    assert I.auto_apply() is None and binds_text(home) == USER_BINDS


def test_a_dolly_command_that_is_not_ours_survives_apply_and_undo(home):
    mine = home / ".local/bin/dolly"
    mine.parent.mkdir(parents=True)
    mine.write_text("#!/bin/sh" + chr(10))
    assert I.apply()["link"]["state"] == "foreign"
    I.undo()
    assert mine.read_text() == "#!/bin/sh" + chr(10) and not mine.is_symlink()


def test_summary_reads_like_a_sentence():
    res = {"changed": ["menu", "binds", "link"], "binds": [{"state": "applied"}] * 5 + [{"state": "taken"}]}
    assert I.summary(res) == "refreshed the menu entry, 5 hotkeys and the dolly command"
    assert I.summary({"changed": ["binds"], "binds": [{"state": "applied"}]}) == "refreshed 1 hotkey"
    assert I.summary(None) == "nothing to refresh"


def test_a_source_checkout_never_touches_the_installed_copys_entries(home):
    """The entries point at the installed plugin; this copy lives somewhere else (a git
    checkout, a test run). Nothing automatic may repoint them or stamp them as refreshed."""
    old_install(home, "/somewhere/else/plugins/io.github.jagritvats.dolly/bin/dolly")
    b, m = binds_text(home), (home / ".config/omarchy/extensions/omarchy-menu.jsonc").read_text()
    assert I.auto_apply() is None
    C.set_many({"shortcuts.pause": "SUPER + ALT + U"})                # saved, but the block is not this copy's
    assert binds_text(home) == b and (home / ".config/omarchy/extensions/omarchy-menu.jsonc").read_text() == m
    assert I.stamp() == {}                                            # the installed copy still has its update to do
    assert not (home / ".local/bin/dolly").exists()

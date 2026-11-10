"""Omarchy integration status: hotkeys Dolly added, live conflicts, first-run detection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import integration as I  # noqa: E402

BLOCK = """-- >>> dolly (managed by dolly-setup)
local dolly_present = io.open("/p/bin/dolly", "r")
if dolly_present then dolly_present:close()
  o.bind("SUPER + ALT + R", "Dolly: record / stop demo", "/p/bin/dolly toggle")
  o.bind("SUPER + ALT + SHIFT + R", "Dolly: save instant replay", "/p/bin/dolly replay save --last 30")
  o.bind("SUPER + ALT + L", "Dolly: recordings library", "/p/bin/dolly library --open")
end
-- <<< dolly
"""


def _home(tmp_path, monkeypatch, bindings=None, menu=None):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/hypr").mkdir(parents=True)
    (tmp_path / ".config/omarchy/extensions").mkdir(parents=True)
    if bindings is not None:
        (tmp_path / ".config/hypr/bindings.lua").write_text("o.bind('SUPER + Q', 'x', 'y')\n" + bindings)
    if menu is not None:
        (tmp_path / ".config/omarchy/extensions/omarchy-menu.jsonc").write_text(menu)


def test_parse_combo():
    assert I.parse_combo("SUPER + ALT + SHIFT + R") == (64 + 8 + 1, "r")
    assert I.parse_combo("SUPER + ALT + L") == (72, "l")


def test_fresh_install_is_not_set_up(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, bindings="", menu="{\n}\n")
    st = I.status(check_live=False)
    assert st["set_up"] is False and st["binds"] == [] and st["menu"] is False


def test_conflict_after_an_omarchy_update(tmp_path, monkeypatch):
    """Omarchy 4.5 took Super+Alt+L for 'Cycle workspace layout back'."""
    _home(tmp_path, monkeypatch, bindings=BLOCK, menu='{\n  // >>> dolly\n  "a": {},\n  // <<< dolly\n}\n')
    live = [{"modmask": 72, "key": "L", "description": "Cycle workspace layout back", "arg": ""},
            {"modmask": 72, "key": "L", "description": "Dolly: recordings library", "arg": "/p/bin/dolly library"},
            {"modmask": 72, "key": "R", "description": "Dolly: record / stop demo", "arg": "/p/bin/dolly toggle"}]
    monkeypatch.setattr(I, "live_binds", lambda timeout=2.0: live)
    st = I.status()
    assert st["set_up"] and st["menu"] and len(st["binds"]) == 3
    assert st["conflicts"] == [{"combo": "SUPER + ALT + L", "dolly": "Dolly: recordings library",
                                "others": ["Cycle workspace layout back"]}]


def test_no_hyprctl_means_no_conflicts_reported(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, bindings=BLOCK, menu="{\n}\n")
    monkeypatch.setattr(I, "live_binds", lambda timeout=2.0: None)
    assert I.status()["conflicts"] == []

"""dolly.hypr: Lua dispatchers on Hyprland 0.56, legacy fallback on older Hyprland."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import hypr  # noqa: E402


class _R:
    def __init__(self, out="ok", rc=0):
        self.stdout, self.stderr, self.returncode = out, "", rc


def _fake(answers, calls):
    def run(args, **kw):
        calls.append(args)
        return answers(args)
    return run


def test_lua_first_on_new_hyprland(monkeypatch):
    calls = []
    monkeypatch.setattr(hypr, "_MODE", None)
    monkeypatch.setattr(subprocess, "run", _fake(lambda a: _R("ok"), calls))
    assert hypr.pin_on("0xabc")
    assert calls == [["hyprctl", "dispatch", 'hl.dsp.window.pin({ action = "on", window = "address:0xabc" })']]
    assert hypr._MODE == "lua"


def test_legacy_fallback_when_lua_is_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(hypr, "_MODE", None)

    def answers(a):
        return _R("Invalid dispatcher") if a[2].startswith("hl.") else _R("ok")
    monkeypatch.setattr(subprocess, "run", _fake(answers, calls))
    assert hypr.resize_exact("address:0x1", 264, 264)
    assert calls[-1] == ["hyprctl", "dispatch", "resizewindowpixel", "exact 264 264,address:0x1"]
    assert hypr._MODE == "legacy"
    calls.clear()
    assert hypr.move_exact("0x1", 10, 20)                # learned: straight to legacy
    assert calls == [["hyprctl", "dispatch", "movewindowpixel", "exact 10 20,address:0x1"]]


def test_old_syntax_error_from_0_56_is_a_failure(monkeypatch):
    """Hyprland 0.56 answers the old form with a Lua parse error on stdout (rc 0)."""
    calls = []
    monkeypatch.setattr(hypr, "_MODE", "legacy")
    err = "error: [string \"return hl.dispatch(pin address:0x0)\"]:1: ')' expected near 'address'"
    monkeypatch.setattr(subprocess, "run", _fake(lambda a: _R(err), calls))
    assert not hypr.pin_on("0x0")


def test_values_are_quoted_as_lua_strings(monkeypatch):
    calls = []
    monkeypatch.setattr(hypr, "_MODE", None)
    monkeypatch.setattr(subprocess, "run", _fake(lambda a: _R("ok"), calls))
    hypr.set_prop("0x1", "active_border_color", 'rgb(7aa2f7)"); os.exit(')
    assert '"rgb(7aa2f7)\\"); os.exit("' in calls[0][2]          # stays inside the string literal


def test_raise_top_uses_alter_zorder(monkeypatch):
    calls = []
    monkeypatch.setattr(hypr, "_MODE", None)
    monkeypatch.setattr(subprocess, "run", _fake(lambda a: _R("ok"), calls))
    assert hypr.raise_top("0x5")
    assert calls == [["hyprctl", "dispatch", 'hl.dsp.window.alter_zorder({ mode = "top", window = "address:0x5" })']]

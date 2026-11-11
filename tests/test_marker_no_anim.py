"""The sync marker layer gets a no-animation rule: Lua first, the old keyword as a fallback."""
from dolly import hypr


def test_lua_rule_is_tried_first(monkeypatch):
    calls = []
    monkeypatch.setattr(hypr, "_run", lambda args, timeout=2.0: (calls.append(args) or True, "ok"))
    assert hypr.marker_no_anim()
    assert len(calls) == 1 and calls[0][0] == "eval"
    assert "hl.layer_rule" in calls[0][1] and "dolly-sync" in calls[0][1] and "no_anim = true" in calls[0][1]
    assert "__dolly_sync_rule" in calls[0][1]          # set once per config load, not once per take


def test_old_config_format_gets_the_keyword_once(monkeypatch, tmp_path):
    calls = []

    def run(args, timeout=2.0):
        calls.append(args)
        return (args[0] != "eval", "unknown request" if args[0] == "eval" else "ok")
    monkeypatch.setattr(hypr, "_run", run)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc")
    assert hypr.marker_no_anim() and hypr.marker_no_anim()
    assert [c[0] for c in calls] == ["eval", "keyword", "eval"]
    assert calls[1] == ["keyword", "layerrule", "noanim, dolly-sync"]

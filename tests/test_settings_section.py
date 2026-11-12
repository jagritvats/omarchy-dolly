"""`dolly settings --section shortcuts`: the panel's Shortcuts button lands on that group."""
from pathlib import Path

from dolly.editor import server

ROOT = Path(__file__).resolve().parents[1]


class Ed(server.Editor):
    def __init__(self):                 # only what navigate() and nav_state() touch
        import threading
        self.view, self.proj, self.nav_seq, self.section = "library", None, 0, None
        self.nav_cv, self.page_seen = threading.Condition(), 0.0


def test_navigate_carries_the_section_for_settings_only():
    ed = Ed()
    st = ed.navigate("settings", section="shortcuts")
    assert st["view"] == "settings" and st["section"] == "shortcuts" and st["seq"] == 1
    assert ed.navigate("library", section="shortcuts")["section"] is None
    assert ed.navigate("settings")["section"] is None


def test_the_page_and_the_panel_use_it():
    page = (ROOT / "dolly" / "editor" / "static" / "index.html").read_text()
    assert "function gotoSection(g)" in page and "gotoSection(st.section)" in page
    panel = (ROOT / "RecordPanel.qml").read_text()
    assert '["settings", "--section", "shortcuts"]' in panel and 'tr.t("panel.shortcuts")' in panel
    cli = (ROOT / "dolly" / "cli.py").read_text()
    assert '"--section"' in cli and "section=a.section" in cli


def test_open_asks_the_shell_for_the_bar_in_front():
    bar = (ROOT / "BarWidget.qml").read_text()
    assert "function open(): void { root.openInFront() }" in bar
    assert '"shell", "summon", "io.github.jagritvats.dolly"' in bar
    assert 'if (text.trim() !== "ok") root.open()' in bar          # no shell answer: open here, as before

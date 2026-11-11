"""The bar widget and the panel start no process to learn the state.

The widget watches Dolly's runtime files; this keeps the field names it reads in step
with the code that writes them, and makes sure no polling timer comes back."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAR = (ROOT / "BarWidget.qml").read_text()
PANEL = (ROOT / "RecordPanel.qml").read_text()


def commands(qml: str) -> list[str]:
    """Every `command: [root.dolly, ...]` a QML file can start, as the argument text."""
    return [m.strip() for m in re.findall(r"command:\s*\[root\.dolly,?\s*([^\]]*)\]", qml)]


def test_the_widget_starts_one_command_and_only_when_the_shell_loads_it():
    assert commands(BAR) == ['"shell-start"']
    assert "startProc.running = true" in BAR and BAR.count("startProc.running = true") == 1
    assert "Component.onCompleted: { startProc.running = true" in BAR
    for gone in ("pollFast", "pollSlow", "statusProc", '"status"'):
        assert gone not in BAR, gone


def test_no_timer_in_the_widget_starts_a_process():
    timers = re.findall(r"Timer\s*\{\n(.*?)\n  \}", BAR, re.S) + re.findall(r"Timer \{ ([^\n]*)\}", BAR)
    assert len(timers) >= 6
    for body in timers:
        assert "running = true" not in body and "execDetached" not in body and "run(" not in body, body


def test_the_liveness_check_only_runs_while_something_is_shown():
    m = re.search(r"id: aliveCheck(.*?)onTriggered", BAR, re.S)
    assert m and "running: root.st !== null || root.ph !== null || root.rp !== null || root.jobInfo !== null" in m.group(1)


def test_the_files_the_widget_watches_are_the_ones_dolly_writes():
    from dolly import capture, replay
    assert capture.STATE == "state.json" and '"/state.json"' in BAR
    assert replay.state_file().name == "replay.json" and '"/replay.json"' in BAR
    health = (ROOT / "dolly/health.py").read_text()
    assert 'runtime_dir() / "phase.json"' in health and '"/phase.json"' in BAR
    assert 'runtime_dir() / "jobs"' in health and '"/jobs"' in BAR


def test_the_fields_the_widget_reads_are_written():
    src = {n: (ROOT / "dolly" / n).read_text() for n in ("capture.py", "health.py", "replay.py")}
    derive = BAR[BAR.index("function derive()"):BAR.index("function reloadAll()")]
    reads = {"capture.py": set(re.findall(r"\bst\.([a-z_]+)", derive)) | {"recorder_pid"},
             "health.py": set(re.findall(r"\bph\.([a-z_]+)", derive)) | set(re.findall(r"\bj\.([a-z_]+)", derive)) | {"pid"},
             "replay.py": set(re.findall(r"\brp\.([a-z_]+)", derive))}
    assert reads["capture.py"] >= {"preroll", "recorder_pid", "paused_at", "paused_total", "started", "muted", "backend"}
    assert reads["health.py"] >= {"phase", "left", "progress", "kind", "pid"}
    assert reads["replay.py"] >= {"recorder_pid", "seconds"}
    for name, fields in reads.items():
        for f in fields:
            where = src[name] if not (name == "health.py" and f == "left") else src["capture.py"]
            assert f'"{f}"' in where or f"{f}=" in where, f"{f} is read by the bar but {name} does not write it"


def test_the_panel_fills_from_one_command():
    cmds = commands(PANEL)
    assert '"panel", "--json"' in cmds
    data = [c for c in cmds if "meter" not in c]                 # the level meters stream while the panel is open
    assert data == ['"panel", "--json"'], data
    assert "function refreshAll() { if (!panelProc.running) panelProc.running = true }" in PANEL

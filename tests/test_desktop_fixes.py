"""Fixes that came from running Dolly on real desktops (Omarchy 4.0.4 and 4.5):
which Python the launcher picks, the application launcher entry, the window a window take
follows when two windows share a size, terminals started with --app-id, and what an empty
trim says."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import capture  # noqa: E402
from dolly import desktop_entry as D  # noqa: E402
from dolly import integration as I  # noqa: E402
from dolly import project as projmod  # noqa: E402

DOLLY = str(ROOT / "bin" / "dolly")
SYSTEM_PY = "/usr/bin/python3"
NL = chr(10)


# ---------------------------------------------------------------- which Python
def _no_cairo_python(folder: Path, name: str = "python3") -> Path:
    """A Python that runs and is new enough but cannot import cairo: what `python3` is on
    a PATH managed by mise or pyenv, or in an environment made without system packages."""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text("#!/bin/sh" + NL
                 + 'if [ "$1" = -c ]; then case "$2" in *cairo*) exit 1 ;; esac; fi' + NL
                 + f'exec "{sys.executable}" "$@"' + NL)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _picked(tmp_path, path_first: Path | None = None, **env_over) -> str:
    env = dict(os.environ, HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path / "data"))
    env.pop("DOLLY_VENV", None)
    if path_first:
        env["PATH"] = f"{path_first}{os.pathsep}{env['PATH']}"
    env.update(env_over)
    r = subprocess.run([DOLLY, "--print-python"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _system_python_draws() -> bool:
    return os.access(SYSTEM_PY, os.X_OK) and subprocess.run(
        [SYSTEM_PY, "-c", "import cairo"], capture_output=True).returncode == 0


needs_system_cairo = pytest.mark.skipif(not _system_python_draws(), reason="needs /usr/bin/python3 with python-cairo")


@needs_system_cairo
def test_a_path_python_without_cairo_loses_to_the_system_python(tmp_path):
    fake = _no_cairo_python(tmp_path / "mise")
    assert _picked(tmp_path, path_first=fake.parent) == SYSTEM_PY


def _environment(tmp_path, *flags) -> Path:
    venv = tmp_path / "data/dolly/venv"
    r = subprocess.run([SYSTEM_PY, "-m", "venv", "--without-pip", *flags, str(venv)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"cannot make a Python environment: {r.stderr.strip()[-200:]}")
    return venv


@needs_system_cairo
def test_a_private_environment_that_sees_the_system_comes_first(tmp_path):
    venv = _environment(tmp_path, "--system-site-packages")           # as docs/captions.md says
    assert _picked(tmp_path) == str(venv / "bin/python")


@needs_system_cairo
def test_a_closed_private_environment_is_not_even_tried(tmp_path):
    venv = _environment(tmp_path)                                      # what Set up made up to 0.7.3
    (next((venv / "lib").glob("python*/site-packages")) / "sitecustomize.py").write_text(
        "import os" + NL + "open(os.environ['DOLLY_TEST_ENV_USED'], 'w').close()" + NL)
    used = tmp_path / "used"
    picked = _picked(tmp_path, DOLLY_TEST_ENV_USED=str(used))
    assert picked != str(venv / "bin/python") and not used.exists()
    assert subprocess.run([picked, "-c", "import cairo"], capture_output=True).returncode == 0


@needs_system_cairo
def test_an_open_environment_that_cannot_draw_gives_way(tmp_path):
    """include-system-site-packages = true, but made from a Python that has no cairo (mise)."""
    _no_cairo_python(tmp_path / "data/dolly/venv/bin", "python")
    (tmp_path / "data/dolly/venv/pyvenv.cfg").write_text("include-system-site-packages = true" + NL)
    picked = _picked(tmp_path)
    assert picked != str(tmp_path / "data/dolly/venv/bin/python")
    assert subprocess.run([picked, "-c", "import cairo"], capture_output=True).returncode == 0


def test_dolly_venv_is_used_as_given(tmp_path):
    fake = _no_cairo_python(tmp_path / "mine/bin", "python")           # even one that cannot draw
    assert _picked(tmp_path, DOLLY_VENV=str(tmp_path / "mine")) == str(fake)


def test_without_any_python_that_draws_the_first_that_runs_is_used(tmp_path):
    """Recording, the plain export and doctor run without cairo, so the launcher must not
    give up. (Only checkable where the system Python cannot draw either.)"""
    if _system_python_draws():
        pytest.skip("the system Python can draw here")
    fake = _no_cairo_python(tmp_path / "mise")
    assert _picked(tmp_path, path_first=fake.parent) == str(fake)


# ---------------------------------------------------------------- the launcher entry
@pytest.fixture()
def data_home(tmp_path, monkeypatch):
    from dolly import log
    log.get()       # opened once per test run: open it before this test moves HOME
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(D, "_refresh", lambda: None)
    return tmp_path


def test_the_launcher_entry_is_named_and_opens_the_panel(data_home):
    launcher = "/home/me/.config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly"
    assert D.apply(launcher)["state"] == "written"
    text = D.path().read_text()
    lines = text.splitlines()
    assert D.path() == data_home / "data/applications/io.github.jagritvats.dolly.desktop"
    assert "Name=Dolly Recorder" in lines and "GenericName=Screen Recorder" in lines
    assert f'Exec="{launcher}" launch' in lines                        # the panel; never a take
    assert not any(ln.startswith("Exec=") and (" record" in ln or " toggle" in ln) for ln in lines)
    assert f'Exec="{launcher}" library --open' in lines and f'Exec="{launcher}" settings' in lines
    assert D.MARKER in lines and D.status(launcher)["state"] == "current"
    assert D.apply(launcher)["state"] == "unchanged"                   # again: nothing is written
    check = shutil.which("desktop-file-validate")
    if check:
        r = subprocess.run([check, str(D.path())], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_a_path_with_special_characters_stays_one_argument(data_home):
    launcher = '/home/me/my plug"ins/100%/bin/dolly'
    D.apply(launcher)
    line = next(ln for ln in D.path().read_text().splitlines() if ln.startswith("Exec=") and ln.endswith(" launch"))
    assert line == 'Exec="/home/me/my plug\\\\"ins/100%%/bin/dolly" launch'
    assert D.ours(launcher) and D.status(launcher)["state"] == "current"


def test_an_entry_that_is_not_ours_is_never_replaced_or_removed(data_home):
    D.path().parent.mkdir(parents=True)
    D.path().write_text("[Desktop Entry]" + NL + "Name=Mine" + NL)
    assert D.apply("/p/bin/dolly")["state"] == "foreign"
    assert D.undo("/p/bin/dolly") is False
    assert D.path().read_text() == "[Desktop Entry]" + NL + "Name=Mine" + NL


def test_the_entry_of_another_copy_is_replaced_but_not_removed_by_undo(data_home):
    D.apply("/old/bin/dolly")
    assert D.status("/new/bin/dolly")["state"] == "other-copy"
    assert D.undo("/new/bin/dolly") is False and D.path().exists()     # undo of this copy leaves the other's
    assert D.apply("/new/bin/dolly")["state"] == "written" and D.ours("/new/bin/dolly")
    assert D.undo("/new/bin/dolly") is True and not D.path().exists()


def test_set_up_adds_the_entry_and_undo_removes_it(data_home, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(data_home / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(data_home / ".config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(data_home / "run"))
    monkeypatch.delenv("DOLLY_CONFIG", raising=False)
    monkeypatch.setenv("DOLLY_NO_HYPR_RELOAD", "1")
    monkeypatch.setenv("DOLLY_NO_BAR_SYNC", "1")
    (data_home / "run").mkdir()
    (data_home / ".config/hypr").mkdir(parents=True)
    (data_home / ".config/omarchy/extensions").mkdir(parents=True)
    (data_home / ".config/hypr/bindings.lua").write_text("-- mine" + NL)
    (data_home / ".config/omarchy/extensions/omarchy-menu.jsonc").write_text(
        "{" + NL + '  "trigger.capture.mine": {"icon": "x", "label": "Mine", "action": "true"}' + NL + "}" + NL)
    monkeypatch.setattr(I, "live_binds", lambda timeout=2.0: [])
    monkeypatch.setattr(I, "_reload_hyprland", lambda: True)
    I.apply()
    assert D.ours(str(I.launcher())) and "Name=Dolly Recorder" in D.path().read_text()
    assert I.stamp().get("integration_revision") == I.INTEGRATION_REVISION
    I.undo()
    assert not D.path().exists()


# ---------------------------------------------------------------- the window a take follows
def _client(addr, ws, at=(12, 38), size=(1896, 1150), **more):
    return {"address": addr, "workspace": {"id": ws}, "at": list(at), "size": list(size), "mapped": True,
            "hidden": False, "pinned": False, **more}


MON_ON_3 = [{"name": "A", "activeWorkspace": {"id": 3}, "specialWorkspace": {"id": 0}}]


def test_a_pick_takes_the_window_on_the_visible_workspace():
    """Two tiled windows of one size on workspaces 2 and 3 have the same geometry. 0.7.3 took
    the first in Hyprland's list, which could be the one nobody can see."""
    a, b = _client("0xa", 2, focusHistoryID=0), _client("0xb", 3, focusHistoryID=1)
    assert capture.window_at((12, 38, 1896, 1150), clients=[a, b], mons=MON_ON_3)["address"] == "0xb"
    assert capture.window_at((12, 38, 1896, 1150), clients=[b, a], mons=MON_ON_3)["address"] == "0xb"


def test_hidden_and_unmapped_windows_are_not_picked_and_pinned_ones_are():
    gone = _client("0xa", 3, mapped=False)
    grouped = _client("0xc", 3, hidden=True)
    pinned = _client("0xp", 2, pinned=True, at=(100, 100), size=(400, 300))
    shown = _client("0xb", 3)
    clients = [gone, grouped, pinned, shown]
    assert capture.window_at((12, 38, 1896, 1150), clients=clients, mons=MON_ON_3)["address"] == "0xb"
    assert capture.window_at((100, 100, 400, 300), clients=clients, mons=MON_ON_3)["address"] == "0xp"


def test_a_window_on_an_open_special_workspace_can_be_picked():
    mons = [{"name": "A", "activeWorkspace": {"id": 1}, "specialWorkspace": {"id": -98}}]
    scratch = _client("0xs", -98, at=(200, 150), size=(1500, 800))
    assert capture.window_at((200, 150, 1500, 800), clients=[_client("0xa", 2), scratch], mons=mons)["address"] == "0xs"


def test_a_terminal_started_with_its_own_app_id_is_still_a_terminal(monkeypatch):
    clients = [{"class": "my-demo-term", "pid": 4242}, {"class": "firefox", "pid": 4243},
               {"class": "no-pid"}, {"class": "gone", "pid": 4244}]
    exe = {4242: "/usr/bin/foot", 4243: "/usr/lib/firefox/firefox"}

    def readlink(p):
        pid = int(str(p).split("/")[2])
        if pid not in exe:
            raise FileNotFoundError(p)
        return exe[pid]

    monkeypatch.setattr(capture, "hypr_json", lambda what: clients if what == "clients" else [])
    monkeypatch.setattr(capture.os, "readlink", readlink)
    got = capture.terminal_classes()
    assert "my-demo-term" in got and set(capture.TERMINALS) <= set(got)
    assert "firefox" not in got and "no-pid" not in got and "gone" not in got


# ---------------------------------------------------------------- an empty trim
@pytest.fixture(scope="module")
def take(tmp_path_factory):
    from dolly.ff import ffmpeg_bin
    d = tmp_path_factory.mktemp("take")
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x200:rate=30:duration=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "raw.mp4")], check=True)
    (d / "capture.json").write_text(json.dumps({"backend": "gsr", "region": [0, 0, 320, 200], "scale": 1.0,
                                                "cursor_baked": False}))
    lines = [{"type": "start", "ms": 0}, {"type": "move", "ms": 500, "x": 100, "y": 100}]
    (d / "events.raw.jsonl").write_text(NL.join(json.dumps(x) for x in lines) + NL)
    capture.finalize(d)
    return d


@pytest.mark.parametrize("trim", [[2.0, 2.0], [2.5, 1.0]])
def test_an_empty_or_reversed_trim_is_refused_in_words(take, trim):
    from dolly import plain, render
    with pytest.raises(projmod.EmptyTrim, match="no video remains in the selected trim"):
        render.plan_timing(take, t_range=trim)
    data = json.loads((take / "project.json").read_text())
    data["trim"] = trim
    (take / "project.json").write_text(json.dumps(data))
    try:
        with pytest.raises(projmod.EmptyTrim, match="no video remains in the selected trim"):
            plain.export(take)
    finally:
        data["trim"] = None
        (take / "project.json").write_text(json.dumps(data))


def test_the_message_carries_no_error_type(monkeypatch):
    from dolly import cli, headless
    e = projmod.EmptyTrim("no video remains in the selected trim (2–2 s); adjust the trim")
    assert cli._known_error(e) == str(e)                               # the terminal and the toast

    def run():
        raise e

    out = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", out)
    with pytest.raises(SystemExit):
        headless.export_json(run, object())                            # --json
    got = json.loads(out.getvalue())
    assert got["ok"] is False and got["error"] == "export-failed" and got["message"] == str(e)

    def other():
        raise ValueError("something else")

    out2 = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", out2)
    with pytest.raises(SystemExit):
        headless.export_json(other, object())
    assert json.loads(out2.getvalue())["message"] == "ValueError: something else"   # unexpected ones keep their type

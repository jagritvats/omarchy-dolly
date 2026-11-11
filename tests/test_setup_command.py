"""Set up links ~/.local/bin/dolly to the plugin's launcher; --undo removes only that link."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._pathutil import path_without  # noqa: E402

SETUP = str(ROOT / "bin" / "dolly-setup")
MINE = "#!/bin/sh" + chr(10) + "echo mine" + chr(10)


def run(tmp_path, *args):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / ".config"))
    env["PATH"] = path_without("hyprctl", tmp_path)
    r = subprocess.run([SETUP, *args], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_setup_links_the_command_and_undo_removes_it(tmp_path):
    link = tmp_path / ".local/bin/dolly"
    assert "command: dolly is on your PATH" in run(tmp_path)
    assert link.is_symlink() and os.readlink(link) == str(ROOT / "bin" / "dolly")
    assert "command: dolly is on your PATH" in run(tmp_path)          # again: nothing changes
    assert "command: removed" in run(tmp_path, "--undo")
    assert not link.exists() and not link.is_symlink()


def test_a_command_that_is_not_ours_is_left_alone(tmp_path):
    link = tmp_path / ".local/bin/dolly"
    link.parent.mkdir(parents=True)
    link.write_text(MINE)
    assert "left alone" in run(tmp_path)
    assert link.read_text() == MINE and not link.is_symlink()
    run(tmp_path, "--undo")
    assert link.exists()


def test_a_link_left_by_a_removed_copy_is_replaced(tmp_path):
    link = tmp_path / ".local/bin/dolly"
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / ".config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly")   # dangling
    run(tmp_path)
    assert os.readlink(link) == str(ROOT / "bin" / "dolly")

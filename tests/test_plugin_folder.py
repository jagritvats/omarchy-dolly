"""Running Dolly must not write into the plugin folder: Omarchy reloads a plugin when
its folder changes, and __pycache__ written there reset the bar widget over and over
(VM, Nov 10)."""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _snapshot(d: Path):
    return sorted(str(p.relative_to(d)) for p in d.rglob("*"))


def test_running_dolly_writes_nothing_into_the_plugin_folder(tmp_path):
    plugin = tmp_path / "plugins" / "io.github.jagritvats.dolly"
    shutil.copytree(ROOT, plugin, ignore=shutil.ignore_patterns(
        "__pycache__", ".git", "scenarios-out", "demo-out", "tests", "harness", "*.pyc"))
    before = _snapshot(plugin)
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    env.pop("PYTHONPYCACHEPREFIX", None)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    for args in (["doctor", "--json"], ["deps", "--json"], ["config", "get"], ["library", "--json", "--recent", "5"],
                 ["status"], ["--help"]):
        subprocess.run([str(plugin / "bin" / "dolly"), *args], env=env, capture_output=True, timeout=120)
    subprocess.run(["bash", str(plugin / "bin" / "dolly-setup")], env=env, capture_output=True, timeout=120,
                   stdin=subprocess.DEVNULL)
    after = _snapshot(plugin)
    assert after == before, sorted(set(after) - set(before))[:10]
    assert (tmp_path / "home" / ".cache" / "dolly" / "pycache").is_dir()

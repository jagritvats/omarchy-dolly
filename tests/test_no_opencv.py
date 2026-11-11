"""Dolly runs without OpenCV. Until 0.7.0 `python-opencv` was a required package (72 MiB to
download with what it depends on); now NumPy and Pillow do its work (dolly/imgops.py).

- nothing under dolly/ mentions it, and nothing in bin/ or the harness imports it;
- the test session cannot import it either (tests/no_cv2/cv2.py is found first, see
  conftest.py), so a test that passes here passes on a machine where it is not installed.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ONLY_HERE = ROOT / "harness" / "bench" / "make_imgops_reference.py"      # remakes the frozen reference; needs OpenCV


def imports_of(path: Path) -> set:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


def test_nothing_under_dolly_mentions_opencv():
    hits = [str(p.relative_to(ROOT)) for p in sorted((ROOT / "dolly").rglob("*"))
            if p.is_file() and p.suffix in (".py", ".html", ".js", ".css", ".json", ".lua")
            and "cv2" in p.read_text(errors="replace")]
    assert hits == []


def test_no_script_or_harness_module_imports_opencv():
    files = [p for d in ("dolly", "harness", "tests") for p in sorted((ROOT / d).rglob("*.py"))]
    files = [p for p in files if p not in (ONLY_HERE, Path(__file__).resolve()) and "no_cv2" not in p.parts]
    bad = [str(p.relative_to(ROOT)) for p in files if "cv2" in imports_of(p)]
    assert bad == []
    for p in sorted((ROOT / "bin").iterdir()):
        assert "opencv" not in p.read_text(errors="replace").lower(), p.name


def test_packages_dolly_asks_for():
    from dolly import deps
    assert [pkg for _, pkg in deps.PY_PACKAGES] == ["python-numpy", "python-pillow"]


@pytest.mark.skipif(os.environ.get("DOLLY_TEST_NO_CV2", "1") != "1", reason="OpenCV is not hidden in this run")
def test_opencv_cannot_be_imported_in_the_test_session():
    with pytest.raises(ImportError):
        import cv2  # noqa: F401
    r = subprocess.run([sys.executable, "-c", "import cv2"], capture_output=True, text=True)
    assert r.returncode != 0 and "ImportError" in r.stderr


@pytest.mark.skipif(os.environ.get("DOLLY_TEST_NO_CV2", "1") != "1", reason="OpenCV is not hidden in this run")
def test_every_module_imports_without_opencv():
    code = ("import importlib, pkgutil, dolly\n"
            "names = [m.name for m in pkgutil.walk_packages(dolly.__path__, 'dolly.') if not m.name.endswith('__main__')]\n"
            "for n in names:\n"
            "    importlib.import_module(n)\n"
            "print(len(names))\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "tests" / "no_cv2"), str(ROOT)])))
    assert r.returncode == 0, r.stderr[-800:]
    assert int(r.stdout.strip().splitlines()[-1]) > 20

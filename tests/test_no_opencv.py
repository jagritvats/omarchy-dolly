"""Dolly runs on what a stock Omarchy has: no OpenCV, no NumPy, no Pillow. `python-opencv`
was a required package until 0.7.0, `python-numpy` and `python-pillow` until 0.7.3. Now
cairo draws (dolly/gfx.py), plain lists do the sums (dolly/num.py) and ffmpeg reads and
writes the files.

- nothing under dolly/, bin/, tests/ or harness/ imports one of the three. The imports are
  parsed, so a comment or a message that names a package is not an import;
- the test session cannot import them either (tests/no_cv2/ and tests/no_numpy/ are found
  first, see conftest.py), so a test that passes here passes on a machine without them.
"""
import ast
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HIDDEN = ("cv2", "numpy", "PIL")
_BY_NAME = ("importorskip", "import_module", "__import__")
hidden_cv2 = pytest.mark.skipif(os.environ.get("DOLLY_TEST_NO_CV2", "1") != "1", reason="OpenCV is not hidden in this run")
hidden_numpy = pytest.mark.skipif(os.environ.get("DOLLY_TEST_NO_NUMPY", "1") != "1",
                                  reason="NumPy and Pillow are not hidden in this run")


def imports_of(path: Path) -> set:
    """The top-level packages a Python file imports: by a statement, or by name with
    importorskip("x"), import_module("x") or __import__("x")."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str) \
                and getattr(node.func, "attr", getattr(node.func, "id", "")) in _BY_NAME:
            names.add(node.args[0].value.split(".")[0])
    return names


def test_imports_are_found_however_they_are_written(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("import os\nimport numpy as np\nfrom PIL import Image\n\ndef g():\n    import cv2.dnn\n"
                 "# import scipy\ns = 'import pandas'\n")
    assert imports_of(f) == {"os", "numpy", "PIL", "cv2"}
    f.write_text("import importlib, pytest\nnp = pytest.importorskip('numpy')\n"
                 "im = importlib.import_module('PIL.Image')\ncv = __import__('cv2')\nfrom . import numpy\n")
    assert imports_of(f) == {"importlib", "pytest", "numpy", "PIL", "cv2"}


def test_nothing_imports_opencv_numpy_or_pillow():
    found = {}
    for d in ("dolly", "tests", "harness"):
        for p in sorted((ROOT / d).rglob("*.py")):
            hit = imports_of(p) & set(HIDDEN)
            if hit:
                found[p.relative_to(ROOT).as_posix()] = sorted(hit)
    assert found == {}
    # bin/ holds shell scripts: no Python in them imports one either
    pat = re.compile(r"\b(?:import|from)\s+(?:" + "|".join(HIDDEN) + r")\b")
    for p in sorted((ROOT / "bin").iterdir()):
        text = p.read_text(errors="replace")
        assert not pat.search(text), p.name
        assert "opencv" not in text.lower(), p.name


def test_only_gfx_imports_cairo_or_gi():
    """One drawing layer: dolly/gfx.py is the only module that imports cairo or gi, also
    inside a function, so everything else keeps working on a machine without them. The
    one other place is the test that asks whether this machine has cairo at all."""
    allowed = {"dolly/gfx.py": {"cairo", "gi"}, "tests/test_standard_install.py": {"cairo"}}
    found = {}
    for d in ("dolly", "tests", "harness"):
        for p in sorted((ROOT / d).rglob("*.py")):
            hit = imports_of(p) & {"cairo", "gi"}
            if hit:
                found[p.relative_to(ROOT).as_posix()] = hit
    assert found == allowed


def test_nothing_under_dolly_mentions_opencv():
    hits = [str(p.relative_to(ROOT)) for p in sorted((ROOT / "dolly").rglob("*"))
            if p.is_file() and p.suffix in (".py", ".html", ".js", ".css", ".json", ".lua")
            and "cv2" in p.read_text(errors="replace")]
    assert hits == []


def test_the_old_image_module_and_its_reference_are_gone():
    for rel in ("dolly/imgops.py", "tests/test_imgops.py", "tests/data/imgops_reference.npz",
                "harness/bench/make_imgops_reference.py"):
        assert not (ROOT / rel).exists(), rel
    users = [p.relative_to(ROOT).as_posix() for d in ("dolly", "tests") for p in sorted((ROOT / d).rglob("*.py"))
             if p != Path(__file__).resolve() and "imgops" in p.read_text(errors="replace")]
    assert users == []


def test_the_only_python_package_dolly_names_is_cairo():
    from dolly import deps
    assert deps.DRAW_PACKAGE == "python-cairo" and not hasattr(deps, "PY_PACKAGES")
    assert deps.python_missing() == []                       # this test run, like a stock Omarchy, has cairo


def _cannot_import(names):
    for name in names:
        with pytest.raises(ImportError):
            importlib.import_module(name)
        r = subprocess.run([sys.executable, "-c", f"import {name}"], capture_output=True, text=True)
        assert r.returncode != 0 and "ImportError" in r.stderr, name


@hidden_cv2
def test_opencv_cannot_be_imported_in_the_test_session():
    _cannot_import(["cv2"])


@hidden_numpy
def test_numpy_and_pillow_cannot_be_imported_in_the_test_session():
    _cannot_import(["numpy", "PIL", "PIL.Image"])


def test_all_three_are_hidden_unless_switched_off():
    src = (ROOT / "tests" / "conftest.py").read_text()
    assert 'os.environ.get("DOLLY_TEST_NO_CV2", "1") == "1"' in src
    assert 'os.environ.get("DOLLY_TEST_NO_NUMPY", "1") == "1"' in src
    make = (ROOT / "Makefile").read_text()
    assert "DOLLY_TEST_NO_CV2=1 DOLLY_TEST_NO_NUMPY=1 $(PY) -m pytest" in make
    for folder, name in (("no_cv2", "cv2.py"), ("no_numpy", "numpy.py"), ("no_numpy", "PIL.py")):
        assert "raise ImportError" in (ROOT / "tests" / folder / name).read_text()


def test_every_module_imports_without_the_three():
    code = ("import importlib, pkgutil, dolly\n"
            "names = [m.name for m in pkgutil.walk_packages(dolly.__path__, 'dolly.') if not m.name.endswith('__main__')]\n"
            "for n in names:\n"
            "    importlib.import_module(n)\n"
            "print(len(names))\n")
    hide = [str(ROOT / "tests" / "no_cv2"), str(ROOT / "tests" / "no_numpy")]
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, PYTHONPATH=os.pathsep.join([*hide, str(ROOT)])))
    assert r.returncode == 0, r.stderr[-800:]
    assert int(r.stdout.strip().splitlines()[-1]) > 20

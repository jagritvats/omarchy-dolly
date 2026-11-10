"""`dolly share export <preset>` (no --copy/--upload) used to crash after exporting:
a loop variable named `t` in cmd_share shadowed the i18n function t()."""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_no_cli_function_rebinds_the_i18n_t():
    tree = ast.parse((ROOT / "dolly/cli.py").read_text())
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        uses_t = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "t"
                     for n in ast.walk(fn))
        rebinds = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "t"
                   and isinstance(n.ctx, ast.Store)]
        if uses_t and rebinds:
            bad.append((fn.name, rebinds))
    assert not bad, f"these functions call t() and also assign t (UnboundLocalError): {bad}"

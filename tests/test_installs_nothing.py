"""Dolly installs nothing and elevates nothing.

It never runs a package manager, pip, sudo or pkexec, and it starts no system service.
Where something is missing it names the package and leaves the installing to the user
(Omarchy menu > Install > Package; see dolly/deps.py).

- The Omarchy plugin marketplace scans a plugin's text for such commands. The scan's
  published rules are written out again here and run over the same files: the only
  capability left must be "installer", which every file named like a setup script gets
  (bin/dolly-setup). This checks lines the marketplace may skip (comments), so it is the
  stricter of the two.
- Nothing in dolly/ or bin/ builds a command line with one of those programs.
"""
import ast
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ------------------------------------------------------------------ the scan's scope
EXCLUDED_DIRS = {".github", "coverage", "docs", "fixtures", "node_modules", "spec", "specs", "test", "tests"}
SCANNED_EXT = {".bash", ".cjs", ".desktop", ".fish", ".js", ".lua", ".mjs", ".pl", ".py", ".qml", ".rb",
               ".service", ".sh", ".sudoers", ".toml", ".yaml", ".yml", ".zsh"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".mp4",
              ".mkv", ".webm", ".mp3", ".wav", ".pdf", ".zip", ".gz", ".npz"}
SETUP_LIKE = re.compile(r"(?:install|installer|setup|uninstall)", re.I)
INSTALLER = re.compile(r"(?:^|[-_])(install|installer|setup|uninstall)(?:[-_.]|$)", re.I)

# ------------------------------------------------------------------ the scan's capability rules
PRIVILEGE = re.compile(r"\b(?:sudo|pkexec)\b", re.I)
PACKAGE_MANAGER = [re.compile(p, re.I) for p in (
    r"\bomarchy\s+pkg\s+(?:add|drop|remove|update)\b",
    r"\b(?:pacman|paru|yay|apt|apt-get|dnf|zypper|apk)\s+(?:-[A-Za-z]*[SRU]|install|remove|upgrade|add|del)\b",
    r"(?:^|[\s/'\"])(?:pip|pip3|pipx)[\"']?\s+install\b",
    r"\bpython[23]?(?:\.[0-9]+)?\s+-m\s+pip\s+install\b",
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:install|add)\b",
    r"\bcargo\s+install\b",
    r"\bgo\s+install\b",
    r"\bgem\s+install\b",
    r"\bbrew\s+(?:install|uninstall|upgrade)\b",
)]
SERVICE = re.compile(r"\bsystemctl\b|\bsystemd-run\b", re.I)
SUDOERS = re.compile(r"/etc/sudoers(?:\.d)?(?:/|\b)|\bvisudo\b|\bSUDOERS(?:_FILE)?\s*=", re.I)
REMOTE_BUILD = re.compile(r"\b(?:git\s+(?:clone|fetch|pull)|curl|wget)\b", re.I)
# remote-build counts only a fetch of the plugin's own repository (commandUsesOnlySubmissionRepository)
OWN_REPO = re.compile(r"github\.com[/:]jagritvats/", re.I)


def in_scope(rel: str) -> bool:
    """The marketplace's isSecurityScanPath()."""
    parts = rel.lower().split("/")
    if "/" not in rel and re.match(r"^readme(?:\.[^/]+)?$", rel, re.I):
        return True
    if any(p in EXCLUDED_DIRS for p in parts[:-1]):
        return False
    base = parts[-1]
    dot = base.rfind(".")
    ext = base[dot:] if dot > 0 else ""
    if ext in SCANNED_EXT:
        return True
    if ext in BINARY_EXT:
        return False
    if parts[0] in ("bin", "scripts"):
        return dot < 0
    return bool(SETUP_LIKE.search(base))


def repo_files() -> list[str]:
    """Every file a clone of the plugin would hold (tracked and new, not ignored)."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, check=True).stdout
        names = [n for n in out.decode().split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    return sorted(n.replace(os.sep, "/") for n in names if (ROOT / n).is_file())


def is_executable_binary(path: Path) -> bool:
    if not path.stat().st_mode & stat.S_IXUSR:
        return False
    head = path.read_bytes()[:4096]
    return head[:4] in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf") or head[:2] == b"MZ" or b"\0" in head


def capabilities() -> dict[str, list[str]]:
    """{capability: ["path:line: text", ...]} over the scan's scope."""
    found: dict[str, list[str]] = {}

    def add(cap, where):
        found.setdefault(cap, []).append(where)
    for rel in repo_files():
        path = ROOT / rel
        if is_executable_binary(path):
            add("bundled-executable-binary", rel)
            continue
        if not in_scope(rel):
            continue
        if INSTALLER.search(rel.split("/")[-1]):
            add("installer", rel)
        if rel.endswith(".service"):
            add("service-management", rel)
        if re.search(r"(?:^|/)(?:sudoers(?:\.d)?(?:/|$)|[^/]+\.sudoers$)", rel, re.I):
            add("sudoers-modification", rel)
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            where = f"{rel}:{n}: {line.strip()[:120]}"
            if PRIVILEGE.search(line):
                add("privilege", where)
            if any(p.search(line) for p in PACKAGE_MANAGER):
                add("package-manager", where)
            if SERVICE.search(line):
                add("service-management", where)
            if SUDOERS.search(line):
                add("sudoers-modification", where)
            if REMOTE_BUILD.search(line) and OWN_REPO.search(line):
                add("remote-build", where)
    return found


def test_the_scan_finds_only_the_setup_script():
    found = capabilities()
    assert set(found) == {"installer"}, {k: v[:5] for k, v in found.items() if k != "installer"}
    assert found["installer"] == ["bin/dolly-setup"]


def test_the_scope_is_the_marketplace_scope():
    assert in_scope("README.md") and in_scope("bin/dolly") and in_scope("bin/dolly-setup")
    assert in_scope("dolly/deps.py") and in_scope("RecordPanel.qml") and in_scope("hypr/dolly-input.lua")
    assert not in_scope("tests/test_deps.py") and not in_scope("docs/captions.md") and not in_scope("AGENTS.md")
    assert not in_scope(".github/workflows/tests.yml") and not in_scope("dolly/editor/static/index.html")
    assert in_scope("harness/bench/run_export.py") and not in_scope("harness/bench/README.md")
    assert PRIVILEGE.search("sudo pacman -S x") and any(p.search("pacman -S x") for p in PACKAGE_MANAGER)
    assert any(p.search("omarchy pkg add x") for p in PACKAGE_MANAGER) and SERVICE.search("systemctl --user")
    assert any(p.search('"pip" install x') for p in PACKAGE_MANAGER)


# ------------------------------------------------------------------ no command line with an installer
FORBIDDEN = {"pacman", "paru", "yay", "pip", "pip3", "pipx", "sudo", "pkexec", "omarchy-pkg-add", "systemctl",
             "systemd-run"}
_STRIP = "\"'`()[]{},;:."


def _words(text: str) -> list[str]:
    return [w.strip(_STRIP).split("/")[-1].lower() for w in text.split()]


def _bad(text: str) -> bool:
    w = _words(text)
    return bool(FORBIDDEN & set(w)) or any(a == "omarchy" and b == "pkg" for a, b in zip(w, w[1:]))


def test_nothing_in_dolly_or_bin_names_an_installer():
    """Every string in dolly/ (commands, messages and docstrings alike) and every line of
    the launchers: none names a package manager, pip, sudo, pkexec or a service manager."""
    bad = []
    for path in sorted((ROOT / "dolly").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _bad(node.value):
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value.strip()[:100]!r}")
    for path in sorted(p for p in (ROOT / "bin").iterdir() if p.is_file()):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if _bad(line):
                bad.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    for path in (ROOT / "RecordPanel.qml", ROOT / "BarWidget.qml"):
        if "--install" in path.read_text():
            bad.append(f"{path.name}: --install")
    assert not bad, "\n".join(bad)
    assert _bad('["sudo", "pacman", "-S"]') and _bad("omarchy pkg add x") and _bad("/usr/bin/pip")
    assert not _bad("python-cairo (Omarchy menu > Install > Package)")


def test_dolly_has_no_install_command():
    from dolly import deps
    assert not hasattr(deps, "install") and not hasattr(deps, "install_command")
    r = subprocess.run([str(ROOT / "bin" / "dolly"), "deps", "--install"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "unrecognized arguments: --install" in r.stderr

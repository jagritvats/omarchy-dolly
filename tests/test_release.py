"""Release hygiene: the things a version bump can forget."""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "manifest.json").read_text())


def test_manifest_has_what_omarchy_and_the_marketplace_need():
    for key in ("schemaVersion", "id", "name", "version", "author", "description", "kinds", "entryPoints"):
        assert MANIFEST.get(key), key
    assert not MANIFEST["id"].startswith("omarchy."), "reserved namespace"
    assert re.fullmatch(r"\d+\.\d+\.\d+", MANIFEST["version"]), MANIFEST["version"]
    for kind, entry in MANIFEST["entryPoints"].items():
        assert (ROOT / entry).is_file(), f"{kind} -> {entry}"


def test_changelog_starts_with_the_manifest_version():
    first = re.search(r"^## (\d+\.\d+\.\d+)", (ROOT / "CHANGELOG.md").read_text(), re.M)
    assert first and first.group(1) == MANIFEST["version"], "add a CHANGELOG entry for this version"


def test_docs_use_the_manifest_id():
    """Install, update and uninstall commands in the README must name the real plugin id."""
    readme = (ROOT / "README.md").read_text()
    ids = set(re.findall(r"plugins/([\w.-]+)/bin", readme)) | set(re.findall(r"plugin (?:remove|update) ([\w.-]+)", readme))
    assert ids == {MANIFEST["id"]}, ids


def test_no_symlinks_are_tracked():
    """Omarchy's validator rejects a plugin that contains a symlink."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return
    if out.returncode != 0:
        return                      # not a git checkout (e.g. an unpacked archive)
    links = [ln.split("\t", 1)[1] for ln in out.stdout.splitlines() if ln.startswith("120000")]
    assert not links, links


def test_readme_images_exist():
    for rel in re.findall(r"\]\(((?:docs/|preview)[^)]+)\)", (ROOT / "README.md").read_text()):
        assert (ROOT / rel).is_file(), rel

"""Share links stay in the share history (0600). The library index, which the bar panel
reads, is rewritten whenever the panel opens: it used to carry a copy of every link and was
readable by other accounts under the usual umask of 022. A presigned link to a private
bucket is all it takes to download the export."""
import json
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_settings import home, make_project  # noqa: E402,F401

LINK = "https://bucket.s3.example.com/2026/11/take.mp4?X-Amz-Signature=0123abcd&X-Amz-Expires=604800"
GONE = "https://bucket.s3.example.com/2026/11/old.mp4?X-Amz-Signature=ffff"


def shared_project(root: Path) -> Path:
    p = make_project(root, name="2026-11-09_11-30-00")
    (p / "shares.json").write_text(json.dumps([
        {"action": "upload", "url": LINK, "time": 2},
        {"action": "upload", "url": GONE, "time": 1, "deleted": True},
        {"action": "copy", "time": 3}, "not an entry"]))
    return p


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_index_has_no_links_and_is_private(home, monkeypatch):  # noqa: F811
    from dolly import fsutil, library
    monkeypatch.setattr(fsutil, "_UMASK", 0o022)
    p = shared_project(home / "Videos" / "Dolly")
    idx = library.index()
    e = idx["items"][0]
    assert e["shared"] and "links" not in e
    cache = library.cache_path()
    text = cache.read_text()
    assert "X-Amz-Signature" not in text and "s3.example.com" not in text
    assert mode(cache) == 0o600
    assert library.links(p) == [LINK]                       # still there for whoever owns the recording
    library.index()                                         # a second scan reuses the entry: still none
    assert "X-Amz-Signature" not in cache.read_text() and mode(cache) == 0o600


def test_other_files_keep_the_umask(tmp_path, monkeypatch):
    from dolly import fsutil
    monkeypatch.setattr(fsutil, "_UMASK", 0o022)
    fsutil.atomic_write_json(tmp_path / "a.json", {"a": 1})
    fsutil.atomic_write_json(tmp_path / "b.json", {"a": 1}, private=True)
    assert mode(tmp_path / "a.json") == 0o644 and mode(tmp_path / "b.json") == 0o600


def test_links_of_a_folder_without_history_or_with_a_damaged_one(tmp_path):
    from dolly import library
    assert library.links(tmp_path) == []
    (tmp_path / "shares.json").write_text("{not json")
    assert library.links(tmp_path) == []
    (tmp_path / "shares.json").write_text(json.dumps({"url": LINK}))
    assert library.links(tmp_path) == []
    (tmp_path / "shares.json").write_text(json.dumps([{"url": 5}, {"url": ""}, {"url": LINK}]))
    assert library.links(tmp_path) == [LINK]


def test_an_index_from_0_7_2_is_cleaned_at_session_start(home):  # noqa: F811
    from dolly import library
    cache = library.cache_path()
    cache.parent.mkdir(parents=True)
    old = {"version": 2, "root": "/x", "items": [
        {"dir": "/x/a", "id": "a", "title": "A", "shared": True, "links": [LINK]},
        {"dir": "/x/b", "id": "b", "title": "B", "shared": False, "links": []}]}
    cache.write_text(json.dumps(old))
    cache.chmod(0o644)
    assert library.drop_cached_links() is True
    new = json.loads(cache.read_text())
    assert mode(cache) == 0o600 and new["version"] == library.INDEX_VERSION
    assert [e["id"] for e in new["items"]] == ["a", "b"] and all("links" not in e for e in new["items"])
    assert new["items"][0]["shared"] is True                # the panel's "shared" tag is kept
    assert library.drop_cached_links() is False             # nothing left to do
    cache.chmod(0o644)                                      # a current index someone made readable
    assert library.drop_cached_links() is True and mode(cache) == 0o600


def test_no_index_or_a_damaged_one_at_session_start(home):  # noqa: F811
    from dolly import library
    assert library.drop_cached_links() is False
    cache = library.cache_path()
    cache.parent.mkdir(parents=True)
    cache.write_text('{"version": 2, "items": [{"links": ["' + LINK + '"]')      # cut off mid-write
    assert library.drop_cached_links() is True and not cache.exists()
    cache.write_text(json.dumps(["x"]))
    assert library.drop_cached_links() is True and not cache.exists()


def test_cli_still_lists_links(home, monkeypatch, capsys):  # noqa: F811
    from dolly import cli, library
    shared_project(home / "Videos" / "Dolly")
    monkeypatch.setattr(sys, "argv", ["dolly", "library", "--no-posters"])
    cli.main()
    out = capsys.readouterr().out
    assert f"link: {LINK}" in out and GONE not in out
    monkeypatch.setattr(sys, "argv", ["dolly", "library", "--json", "--no-posters"])
    cli.main()
    doc = json.loads(capsys.readouterr().out)
    assert doc["items"][0]["links"] == [LINK]               # scripts reading --json keep the field
    assert "X-Amz-Signature" not in library.cache_path().read_text()

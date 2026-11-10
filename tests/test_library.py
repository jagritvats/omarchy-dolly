"""Recordings library: cached index, posters, flags, rename, trash + undo, raw cleanup,
and the editor's Library/Settings API (project-less editor)."""
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_settings import home, make_project  # noqa: E402,F401


def test_index_flags_posters_rename_trash_undo(home):  # noqa: F811
    from dolly import library
    root = home / "Videos" / "Dolly"
    a = make_project(root, name="2026-11-08_09-00-00")
    b = make_project(root, name="2026-11-09_11-30-00")
    (b / "exports").mkdir()
    (b / "exports" / "b-1200-9x16.mp4").write_bytes(b"x" * 10)
    (b / "exports" / "b-1200.mp4").write_bytes(b"x" * 10)
    (b / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    (b / "shares.json").write_text(json.dumps([{"action": "upload", "url": "https://x.example/v", "time": 1}]))
    c = make_project(root, name="2026-11-09_12-00-00")
    meta = json.loads((c / "capture.json").read_text())
    meta["stop_reason"] = "recovered"
    (c / "capture.json").write_text(json.dumps(meta))
    idx = library.index()
    ids = [e["id"] for e in idx["items"]]
    assert ids[0] == "2026-11-09_12-00-00" and set(ids) == {a.name, b.name, c.name}
    eb = next(e for e in idx["items"] if e["id"] == b.name)
    assert eb["exported"] and eb["vertical"] and eb["captions"] and eb["shared"] and eb["links"] == ["https://x.example/v"]
    assert next(e for e in idx["items"] if e["id"] == c.name)["recovered"]
    assert all(Path(e["thumb"]).exists() for e in idx["items"])          # cached poster frames
    cached = json.loads(library.cache_path().read_text())
    assert [e["id"] for e in cached["items"]] == ids                     # what the bar panel reads
    # rename: title in the project, folder unchanged
    library.rename(a, "  Onboarding   flow ")
    assert next(e for e in library.index()["items"] if e["id"] == a.name)["title"] == "Onboarding flow"
    assert library.find("Onboarding flow") == a
    # trash with undo (freedesktop trash layout)
    library.trash([a, c])
    assert not a.exists() and (home / "data" / "Trash" / "info" / f"{a.name}.trashinfo").exists()
    assert {e["id"] for e in library.index()["items"]} == {b.name}
    assert library.undo()["restored"] == 2 and a.exists() and c.exists()
    with pytest.raises(ValueError):
        library.trash([home])                                           # never outside the library
    # cleanup of raw files of exported recordings
    dry = library.cleanup(dry_run=True)
    assert dry["recordings"] == 1 and (b / "raw.mkv").exists()
    r = library.cleanup()
    assert r["recordings"] == 1 and not (b / "raw.mkv").exists() and (a / "raw.mkv").exists()
    eb = next(e for e in library.index()["items"] if e["id"] == b.name)
    assert not eb["raw"] and eb["exported"]


def test_cli_library_json_is_instant_from_cache(home):  # noqa: F811
    import subprocess
    import os
    make_project(home / "Videos" / "Dolly")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-m", "dolly.cli", "library", "--json", "--recent", "6"], env=env,
                       capture_output=True, text=True, check=True)
    assert len(json.loads(r.stdout)["items"]) == 1
    r = subprocess.run([sys.executable, "-m", "dolly.cli", "library", "--json", "--cached"], env=env,
                       capture_output=True, text=True, check=True)
    assert json.loads(r.stdout)["items"][0]["thumb"]


def test_editor_library_and_settings_api(home):  # noqa: F811
    from http.server import ThreadingHTTPServer
    from dolly.editor.server import Editor, make_handler
    p = make_project(home / "Videos" / "Dolly")
    ed = Editor(None, view="library")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ed))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path + ("&" if "?" in path else "?") + "t=" + ed.token,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    try:
        assert call("/api/app")[1]["view"] == "library"
        assert call("/api/project")[0] == 409                           # nothing open yet
        st, lib = call("/api/library")
        assert st == 200 and lib["items"][0]["dir"] == str(p)
        assert call("/api/library/rename", {"dir": str(p), "title": "Demo A"})[1]["item"]["title"] == "Demo A"
        assert call("/api/library/open", {"dir": str(home)})[0] == 500    # outside the library: refused
        assert call("/api/library/open", {"dir": str(p)})[1]["ok"]
        assert call("/api/project")[1]["title"] == "Demo A"
        st, s = call("/api/settings")
        assert st == 200 and "capture.mode" in [x["key"] for x in s["schema"]] and s["hotkeys"]
        st, s = call("/api/settings", {"updates": {"look.radius": 25}})
        assert st == 200 and s["values"]["look"]["radius"] == 25
        assert call("/api/settings", {"updates": {"look.radius": "abc"}})[0] == 400
        st, r = call("/api/library/trash", {"dirs": [str(p)]})
        assert r["trashed"] == 1 and ed.proj is None
        assert call("/api/library/undo", {})[1]["restored"] == 1
    finally:
        httpd.shutdown()


def test_index_order_does_not_depend_on_directory_order(home, monkeypatch):  # noqa: F811
    # same "started" in every capture.json: ext4 on Arch listed them oldest-first (VM, Nov 10)
    from dolly import library
    root = home / "Videos" / "Dolly"
    names = ["2026-11-08_09-00-00", "2026-11-09_12-00-00", "2026-11-09_11-30-00"]
    for n in names:
        make_project(root, name=n)
    real = Path.iterdir
    for order in (sorted, lambda xs: sorted(xs, reverse=True)):
        monkeypatch.setattr(Path, "iterdir", lambda self, o=order: iter(o(list(real(self)))))
        ids = [e["id"] for e in library.index(refresh=True)["items"]]
        assert ids == sorted(names, reverse=True)

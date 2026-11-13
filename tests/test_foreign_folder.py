"""A recording folder made by someone else (unpacked from an archive, synced, cloned) is
data from them: every file in it, and every name. These are the ways such a folder reached
files outside itself, or took the library down, up to 0.7.2."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_settings import home, make_project  # noqa: E402,F401


def lib(home):  # noqa: F811
    return home / "Videos" / "Dolly"


def outside(home, name="notes.txt", text="mine\n"):  # noqa: F811
    p = home / "Documents" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ------------------------------------------------------------------ symbolic links
def test_library_scan_does_not_write_through_a_link(home):  # noqa: F811
    from dolly import library, project
    good = make_project(lib(home), name="2026-11-09_10-00-00")
    bad = make_project(lib(home), name="2026-11-09_11-00-00")
    target = outside(home)
    (bad / "thumb.jpg").unlink(missing_ok=True)
    (bad / "thumb.tmp.jpg").symlink_to("../../../Documents/notes.txt")      # where the poster is written first
    idx = library.index()
    assert target.read_text() == "mine\n"
    assert [e["id"] for e in idx["items"]] == [good.name]
    assert idx["skipped"] == [{"dir": str(bad), "id": bad.name, "reason": "symbolic links inside"}]
    with pytest.raises(project.UnsafeFolder, match="thumb.tmp.jpg"):
        project.Project(bad)
    assert project.latest_project() == good                                # `dolly edit` without a folder


def test_links_one_folder_down_are_seen_and_the_folder_itself_may_be_a_link(home):  # noqa: F811
    from dolly import project
    p = make_project(lib(home), name="2026-11-09_10-00-00")
    elsewhere = home / "disk2"
    elsewhere.mkdir()
    (lib(home) / "on-disk2").symlink_to(elsewhere)                         # the user's own doing: fine
    assert project.Project(lib(home) / "on-disk2").dir.name == "on-disk2"
    (p / "exports" / ".cache").mkdir(parents=True)
    (p / "exports" / ".cache" / "master-1.mp4").symlink_to(outside(home))
    (p / "tracks").symlink_to(home / "Documents")
    assert project.links_in(p) == ["exports/.cache/master-1.mp4", "tracks"]
    with pytest.raises(project.UnsafeFolder):
        project.Project(p)


def test_share_history_is_not_written_through_a_link(home, monkeypatch):  # noqa: F811
    from dolly import share
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    p = make_project(lib(home), name="2026-11-09_10-00-00")
    target = outside(home, "other.json", '{"keep": 1}')
    (p / "shares.tmp").symlink_to(target)                                  # the old temp name
    share.record(p, {"action": "copy-file", "file": "x", "target": "clipboard"})
    assert target.read_text() == '{"keep": 1}'
    assert share.history(p)[-1]["action"] == "copy-file"
    assert (p / "shares.json").stat().st_mode & 0o777 == 0o600


def test_recover_does_not_append_through_a_link(home):  # noqa: F811
    from dolly import capture
    p = make_project(lib(home), name="2026-11-09_10-00-00")
    (p / "events.json").unlink()                                           # an unfinished take
    target = outside(home, "startup.txt", "#!/bin/sh\n")
    (p / "recorder.log").unlink(missing_ok=True)
    (p / "recorder.log").symlink_to(target)
    capture.recover_all()
    assert target.read_text() == "#!/bin/sh\n" and not (p / "events.json").exists()


def test_cleanup_does_not_trash_what_a_link_points_at(home):  # noqa: F811
    from dolly import library
    p = make_project(lib(home), name="2026-11-09_10-00-00")
    (p / "exports").mkdir()
    (p / "exports" / "a.mp4").write_bytes(b"x" * 10)
    taxes = home / "Documents" / "taxes"
    taxes.mkdir(parents=True)
    (taxes / "2026.pdf").write_text("x")
    (p / "webcam.mkv").symlink_to("../../../Documents/taxes")
    library.cleanup(0)
    assert (taxes / "2026.pdf").exists() and (p / "raw.mkv").exists()      # the folder is left alone
    assert library.trash_raw(p) > 0                                        # called on it anyway:
    assert (taxes / "2026.pdf").exists() and (p / "webcam.mkv").is_symlink() and not (p / "raw.mkv").exists()
    moved = library._trash_one(p / "webcam.mkv")                           # a link is trashed as a link
    assert Path(moved["trashed"]).is_symlink() and (taxes / "2026.pdf").exists()


# ------------------------------------------------------------------ remote delete
class Spy:
    kind, name, deleted = "s3", "r2", []

    def delete(self, ref):
        Spy.deleted.append(ref)


def test_delete_remote_only_for_uploads_made_here(home, monkeypatch):  # noqa: F811
    from dolly import share
    from dolly.share import config, targets
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.setattr(config, "load", lambda: {"targets": {"r2": {"type": "s3"}}})
    monkeypatch.setattr(targets, "make", lambda name, t: Spy())
    Spy.deleted.clear()
    p = make_project(lib(home), name="2026-11-09_10-00-00")
    mine = share.record(p, {"action": "upload", "target": "r2", "url": "https://x.example/a.mp4",
                            "remote_ref": {"bucket": "b", "key": "2026/11/a.mp4"}})
    items = json.loads((p / "shares.json").read_text())
    items.append({"id": "feedf00d", "action": "upload", "target": "r2", "url": "https://x.example/b",
                  "remote_ref": {"bucket": "b", "key": "backups/everything.tar"}})           # came with the folder
    items.append({**mine, "id": mine["id"] + "x"})
    (p / "shares.json").write_text(json.dumps(items))
    with pytest.raises(targets.UploadError, match="not made from this computer"):
        share.delete_remote(p, "feedf00d")
    forged = json.loads((p / "shares.json").read_text())                   # the right id, another object
    forged[0]["remote_ref"] = {"bucket": "b", "key": "backups/everything.tar"}
    (p / "shares.json").write_text(json.dumps(forged))
    with pytest.raises(targets.UploadError):
        share.delete_remote(p, mine["id"])
    assert Spy.deleted == []
    forged[0]["remote_ref"] = mine["remote_ref"]
    (p / "shares.json").write_text(json.dumps(forged))
    assert share.delete_remote(p, mine["id"])["deleted"]
    assert Spy.deleted == [{"bucket": "b", "key": "2026/11/a.mp4"}]


def test_targets_refuse_a_ref_outside_their_own_place(monkeypatch):
    from dolly.share import targets
    ran = []
    t = targets.RcloneTarget("gdrive", {"remote": "gdrive:demos/"})
    monkeypatch.setattr(t, "_run", lambda *a, **k: ran.append(a))
    for path in ("/home/me/.ssh/id_ed25519", "other:secret/file", "--config=/x", "gdrive:demos/../x", None):
        with pytest.raises(targets.UploadError):
            t.delete({"path": path})
    assert ran == []
    t.delete({"path": "gdrive:demos/2026/11/a.mp4"})
    assert ran == [("deletefile", "gdrive:demos/2026/11/a.mp4")]
    s3 = targets.S3Target.__new__(targets.S3Target)
    s3.bucket = "demos"
    for ref in ({"bucket": "other", "key": "k"}, {"bucket": "demos"}, {"bucket": "demos", "key": 5}):
        with pytest.raises(targets.UploadError):
            s3.delete(ref)
    assert targets._header_name('a"b\r\nX: y\\.mp4') == "a_b__X: y_.mp4"


# ------------------------------------------------------------------ files of the wrong shape
@pytest.mark.parametrize("name,content", [
    ("shares.json", "5"), ("shares.json", '{"a": 1}'), ("capture.json", '{"started": "yesterday"}'),
    ("capture.json", "[1, 2]"), ("capture.json", '{"started": 1e300, "stop_reason": 5, "mode": [], "monitor": {}}'),
    ("project.json", '{"title": 7}'), ("project.json", "[]"), ("events.json", '{"video": {"duration": [1]}}'),
    ("events.json", '{"video": {"duration": 1e300}}')])
def test_one_odd_folder_does_not_take_the_library_down(home, name, content):  # noqa: F811
    from dolly import library
    good = make_project(lib(home), name="2026-11-09_10-00-00")
    odd = make_project(lib(home), name="2026-11-09_11-00-00")
    (odd / name).write_text(content)
    idx = library.index(posters=False)
    assert {e["id"] for e in idx["items"]} == {good.name, odd.name} and idx["skipped"] == []
    json.dumps(idx)
    e = next(x for x in idx["items"] if x["id"] == odd.name)
    assert isinstance(e["title"], str) and isinstance(e["created"], (int, float)) and e["duration"] < 1e7


def test_a_folder_that_cannot_be_read_is_skipped_and_named(home, monkeypatch):  # noqa: F811
    from dolly import library
    good = make_project(lib(home), name="2026-11-09_10-00-00")
    odd = make_project(lib(home), name="2026-11-09_11-00-00")
    real = library.entry

    def entry(d, **kw):
        if Path(d).name == odd.name:
            raise RuntimeError("something this code did not expect")
        return real(d, **kw)
    monkeypatch.setattr(library, "entry", entry)
    idx = library.index(posters=False)
    assert [e["id"] for e in idx["items"]] == [good.name]
    assert idx["skipped"] == [{"dir": str(odd), "id": odd.name, "reason": "unreadable (RuntimeError)"}]


def test_sizes_no_recording_has_are_refused():
    from dolly.events import Timeline
    ok = {"video": {"w": 1920, "h": 1080, "fps": 60, "duration": 12.5}}
    assert Timeline.from_dict(ok).w == 1920
    for k, v in (("w", 10 ** 9), ("h", 0), ("fps", 1e9), ("duration", 1e12), ("duration", -1)):
        with pytest.raises(ValueError):
            Timeline.from_dict({"video": {**ok["video"], k: v}})


# ------------------------------------------------------------------ audio and background
def test_audio_tracks_are_mka_files_only():
    from dolly.audio import clean_tracks
    got = clean_tracks([{"track": "mic", "file": "tracks/mic.mka", "offset": 0},
                        {"track": "system", "file": "tracks/list.m3u8", "offset": 0},
                        {"track": "extra", "file": "tracks/x.mka.txt", "offset": 0}])
    assert [t["file"] for t in got] == ["tracks/mic.mka"]


def test_export_checks_the_background_picture_again(home, tmp_path_factory, monkeypatch):  # noqa: F811
    """project.json names the picture. The editor checks it when it is picked; a project from
    someone else never went through the editor."""
    from PIL import Image

    from dolly.render import build_background, make_layout
    elsewhere = tmp_path_factory.mktemp("not-home") / "red.png"
    Image.new("RGB", (320, 180), (220, 20, 20)).save(elsewhere)
    inside = home / "Pictures" / "red.png"
    inside.parent.mkdir()
    Image.new("RGB", (320, 180), (220, 20, 20)).save(inside)
    monkeypatch.delenv("DOLLY_IMAGE_ROOTS", raising=False)

    def corner(path):
        style = {"background": {"type": "wallpaper", "path": str(path), "blur": 0}, "padding": 0.06, "shadow": 0,
                 "radius": 0, "output": {"width": 640, "height": 360}}
        img = build_background(make_layout(640, 400, style), style)
        b, g, r = (int(x) for x in img[2, 2])
        return r > 180 and g < 60 and b < 60
    assert corner(inside)
    assert not corner(elsewhere)                    # outside the home folder: not opened, a gradient instead
    assert not corner(home / "Pictures" / "gone.png")
    os.rename(inside, inside.with_suffix(".txt"))
    assert not corner(inside.with_suffix(".txt"))   # not an image by name

"""What Dolly writes outside recording folders is for this user: with the usual umask of 022
its state, cache and runtime-fallback folders were readable by every account, and a private
share link went onto the notifier's command line."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_settings import home, make_project  # noqa: E402,F401

PRIVATE = "https://bucket.s3.example.com/2026/11/take.mp4?X-Amz-Signature=0123abcd&X-Amz-Expires=604800"


def mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


@pytest.fixture()
def umask022():
    old = os.umask(0o022)
    yield
    os.umask(old)


def test_state_cache_and_trash_folders_are_private(home, umask022):  # noqa: F811
    from dolly import guards, library, log
    log._LOG = None
    state = guards.state_dir()
    assert mode(state) == 0o700
    p = make_project(home / "Videos" / "Dolly", name="2026-11-09_10-00-00")
    library.index(posters=False)
    assert mode(library.cache_path().parent) == 0o700
    library.trash([p])
    trash = home / "data" / "Trash"
    assert [mode(d) for d in (trash, trash / "files", trash / "info")] == [0o700] * 3
    assert mode(library._undo_file()) == 0o600


def test_a_state_folder_an_older_version_made_is_tightened(home, umask022):  # noqa: F811
    from dolly import guards
    d = home / "state" / "dolly"
    d.mkdir(parents=True)
    assert mode(d) == 0o755
    guards.state_dir()
    assert mode(d) == 0o700


def test_runtime_folder_without_xdg_runtime_dir(home, umask022, monkeypatch):  # noqa: F811
    from dolly import guards, logger
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.delenv("XDG_STATE_HOME")
    d = home / ".local" / "state" / "dolly"
    d.mkdir(parents=True)                           # made first by the log, with the umask
    assert logger.runtime_dir() == d and mode(d) == 0o700
    assert guards.state_dir() == d and mode(d) == 0o700


def test_runtime_folder_of_another_account_is_refused(home, monkeypatch):  # noqa: F811
    from dolly import logger
    logger.runtime_dir()
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)             # as if someone else made it
    with pytest.raises(PermissionError, match="XDG_RUNTIME_DIR"):
        logger.runtime_dir()


def test_runtime_folder_that_is_a_link(home, monkeypatch):  # noqa: F811
    from dolly import logger
    real = home / "elsewhere"
    real.mkdir()
    (home / "run" / "dolly").symlink_to(real)       # our own link to our own folder: used
    assert logger.runtime_dir() == home / "run" / "dolly"


def test_old_hyprland_socket_in_tmp_must_be_ours(tmp_path, monkeypatch):
    from dolly import logger
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "sig")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    s = tmp_path / "hypr" / "sig" / ".socket.sock"
    s.parent.mkdir(parents=True)
    s.write_text("")
    assert logger.hypr_socket() == str(s)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    assert logger.hypr_socket() is None


def test_rewriting_a_file_keeps_its_mode(tmp_path, umask022):
    from dolly import fsutil
    f = tmp_path / "bindings.lua"
    f.write_text("old")
    f.chmod(0o600)
    fsutil.atomic_write_text(f, "new", backup=True)
    assert f.read_text() == "new" and mode(f) == 0o600 and mode(fsutil.backup_path(f)) == 0o600
    g = tmp_path / "new.json"
    fsutil.atomic_write_json(g, {})
    assert mode(g) == 0o644                          # a new file: the umask, as before


def test_a_picked_background_is_copied_for_this_user_only(home, umask022):  # noqa: F811
    import io

    from PIL import Image

    from dolly import images
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), (10, 20, 30)).save(buf, "PNG")
    got = images.save_upload(buf.getvalue(), "wall.png")
    assert mode(got["path"]) == 0o600 and mode(Path(got["path"]).parent) == 0o700


def test_report_is_a_new_private_file_and_hides_link_queries(home, umask022, monkeypatch, tmp_path):  # noqa: F811
    from dolly import report
    monkeypatch.setattr(report, "build", lambda: "report\n")
    shared = tmp_path / "shared"
    shared.mkdir()
    p = report.write(shared)
    assert mode(p) == 0o600 and p.read_text() == "report\n"
    victim = tmp_path / "victim.txt"
    victim.write_text("mine")
    monkeypatch.setattr(report.time, "strftime", lambda fmt: "20261114-120000")
    (shared / "dolly-report-20261114-120000.txt").symlink_to(victim)        # a name someone could guess
    monkeypatch.chdir(home)
    p = report.write(shared)
    assert victim.read_text() == "mine" and p.parent == home
    text = report.redact(f"upload failed for {PRIVATE} (403)\nsee https://example.org/docs#x")
    assert "X-Amz-Signature" not in text and "0123abcd" not in text
    assert "https://bucket.s3.example.com/2026/11/take.mp4?<redacted>" in text and "https://example.org/docs#x" in text


# ------------------------------------------------------------------ the link and command lines
def run_share_upload(home, monkeypatch, entry):  # noqa: F811
    from dolly import capture, cli, share
    p = make_project(home / "Videos" / "Dolly", name="2026-11-09_10-00-00")
    (p / "exports").mkdir()
    f = p / "exports" / "take-1200.mp4"
    f.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(share, "upload", lambda *a, **k: entry)
    monkeypatch.setattr(capture, "notify", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "argv", ["dolly", "share", "upload", str(f)])
    cli.main()
    return calls


def test_a_private_link_is_not_handed_to_the_notifier(home, monkeypatch, capsys):  # noqa: F811
    calls = run_share_upload(home, monkeypatch, {"url": PRIVATE, "public": False, "copied": True})
    assert len(calls) == 1 and "take-1200.mp4" in calls[0]
    assert not any("X-Amz" in str(x) or "s3.example.com" in str(x) for x in calls[0])
    assert PRIVATE in capsys.readouterr().out       # still printed for the person who ran it


def test_a_public_link_keeps_its_open_button(home, monkeypatch):  # noqa: F811
    url = "https://demos.example.dev/2026/11/take.mp4"
    calls = run_share_upload(home, monkeypatch, {"url": url, "public": True, "copied": True})
    assert url in calls[0] and any(isinstance(x, tuple) and "xdg-open" in x for x in calls[0])


def test_upload_is_recorded_when_the_clipboard_fails(home, monkeypatch):  # noqa: F811
    """wl-copy installed but failing raised after the file was already up: no history entry,
    so no link and nothing to delete it with."""
    from dolly import share
    from dolly.share import clipboard, targets

    class T:
        kind, name = "s3", "r2"

        def upload(self, path):
            return {"url": "https://demos.example.dev/a.mp4", "public": True, "deletable": True,
                    "remote_ref": {"bucket": "b", "key": "a.mp4"}}

    def broken(text):
        raise subprocess.CalledProcessError(1, ["wl-copy"])
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.setattr(targets, "get_target", lambda name: T())
    monkeypatch.setattr(clipboard, "copy_text", broken)
    p = make_project(home / "Videos" / "Dolly", name="2026-11-09_10-00-00")
    e = share.upload(p, p / "raw.mkv")
    assert e["copied"] is False and e["url"] and share.history(p)[-1]["id"] == e["id"]
    assert json.loads((home / "state" / "dolly" / "shares.jsonl").read_text().splitlines()[-1])["id"] == e["id"]

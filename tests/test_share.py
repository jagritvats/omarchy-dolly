"""Share flow: size targeting, clipboard, config safety, S3 (moto), rclone, Litterbox (fake host)."""
import datetime as dt
import http.server
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly.render import ffmpeg_bin  # noqa: E402
from dolly.share import clipboard, config, encode, presets, targets  # noqa: E402
import dolly.share as share  # noqa: E402

MB = 1_000_000


@pytest.fixture(scope="module")
def busy_clip(tmp_path_factory):
    """A hard-to-compress 8 s 1080p clip with audio (noise + motion) so caps actually bind."""
    d = tmp_path_factory.mktemp("clip")
    f = d / "busy.mp4"
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=30:duration=6,noise=alls=40:allf=t",
                    "-f", "lavfi", "-i", "sine=f=440:duration=6", "-c:v", "libx264", "-crf", "14",
                    "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(f)], check=True)
    return f


def test_presets_are_complete():
    for p in presets.all_presets():
        assert p["format"] in ("mp4", "gif") and p["width"] % 2 == 0 and p["height"] % 2 == 0
    assert presets.get("github")["cap_mb"] == 10 and presets.get("discord")["cap_mb"] == 20
    assert presets.get("shorts")["aspect"] == "9:16"


def test_ladder_steps_down_when_bitrate_too_low():
    lad = encode.plan_ladder(1920, 1080, 60, 120, 5 * MB, 96)
    first_ok = next(c for c in lad if c[4] >= encode.BPP_FLOOR)
    assert first_ok[0] < 1920 or first_ok[2] < 60     # 5 MB for 2 min of 1080p60 can't stay full size


def test_two_pass_hits_cap(busy_clip, tmp_path):
    out = tmp_path / "capped.mp4"
    cap = int(2.0 * MB)
    rep = encode.target_size_mp4(busy_clip, out, cap, 1280, 720, 30, log=lambda m: None)
    size = out.stat().st_size
    assert rep["method"] == "two-pass"
    assert size <= cap
    assert size >= cap * 0.75                          # uses the budget, not wildly under
    info = encode.media_info(out)
    assert info["audio"] and abs(info["fps"] - 30) < 0.01


def test_crf_kept_when_it_already_fits(busy_clip, tmp_path):
    out = tmp_path / "small.mp4"
    rep = encode.target_size_mp4(busy_clip, out, 500 * MB, 640, 360, 30, log=lambda m: None)
    assert rep["method"] == "crf"


def test_gif_shrinks_until_under_cap(tmp_path):
    src = tmp_path / "clean.mp4"
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6",
                    "-c:v", "libx264", "-crf", "16", "-preset", "ultrafast", str(src)], check=True)
    full = tmp_path / "full.gif"
    encode.gif_encode(src, full, 800, 15)
    cap = int(full.stat().st_size * 0.5)
    out = tmp_path / "x.gif"
    rep = encode.gif_under_cap(src, out, cap, 800, 15, log=lambda m: None)
    assert out.stat().st_size <= cap and rep["attempts"] >= 2


def test_estimate_caps_and_duration_warning():
    e = encode.estimate(presets.get("github"), 600)
    assert e["bytes"] <= 10 * MB and e["fits"] is None
    assert encode.estimate(presets.get("x"), 200)["over_duration"]
    assert encode.estimate(presets.get("slack"), 30)["fits"] is True


# ------------------------------------------------------------------ clipboard
@pytest.fixture()
def fake_wlcopy(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "wlcopy.log"
    (bindir / "wl-copy").write_text(f"#!/usr/bin/env bash\necho \"ARGS:$*\" >> {log}\ncat >> {log}\necho >> {log}\n")
    (bindir / "wl-copy").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return log


def test_copy_file_as_uri_list(fake_wlcopy, tmp_path):
    f = tmp_path / "my demo.mp4"
    f.write_bytes(b"x")
    clipboard.copy_file(f)
    txt = fake_wlcopy.read_text()
    assert "ARGS:--type text/uri-list" in txt
    assert "file://" + str(f.resolve()).replace(" ", "%20") in txt


def test_copy_without_wlcopy(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(clipboard.ClipboardUnavailable):
        clipboard.copy_text("x")


# ------------------------------------------------------------------ config safety
def write_cfg(tmp_path, monkeypatch, obj, mode=0o600):
    p = tmp_path / "share.json"
    p.write_text(json.dumps(obj))
    p.chmod(mode)
    monkeypatch.setenv("DOLLY_SHARE_CONFIG", str(p))
    return p


def test_default_is_local_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DOLLY_SHARE_CONFIG", str(tmp_path / "missing.json"))
    assert config.load()["targets"] == {}
    with pytest.raises(config.ConfigError, match="local-only"):
        targets.get_target()


def test_inline_secrets_need_0600(tmp_path, monkeypatch):
    t = {"targets": {"s3": {"type": "s3", "bucket": "b", "access_key": "AK", "secret_key": "SK"}}}
    write_cfg(tmp_path, monkeypatch, t, 0o644)
    with pytest.raises(config.ConfigError, match="chmod 600"):
        config.load()
    write_cfg(tmp_path, monkeypatch, t, 0o600)
    assert "s3" in config.load()["targets"]


def test_public_host_needs_explicit_ack(tmp_path, monkeypatch):
    write_cfg(tmp_path, monkeypatch, {"targets": {"tmp": {"type": "litterbox"}}})
    with pytest.raises(config.ConfigError, match="public"):
        config.load()


def test_template_is_0600_and_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DOLLY_SHARE_CONFIG", str(tmp_path / "d" / "share.json"))
    p = config.init_template()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert config.load()["targets"] == {}          # examples ship disabled


def test_credentials_from_env(monkeypatch):
    monkeypatch.setenv("MY_AK", "ak1")
    monkeypatch.setenv("MY_SK", "sk1")
    assert config.resolve_s3_credentials({"access_key_env": "MY_AK", "secret_key_env": "MY_SK"})[:2] == ("ak1", "sk1")


# ------------------------------------------------------------------ SigV4
def test_presign_matches_botocore():
    botocore = pytest.importorskip("botocore")
    import botocore.auth
    import botocore.awsrequest
    import botocore.credentials
    now = dt.datetime(2026, 11, 9, 12, 0, 0, tzinfo=dt.timezone.utc)
    ours = targets.presign("PUT", "http://127.0.0.1:9000", "us-east-1", "demos", "dolly/2026/a b.mp4",
                           "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG", 3600, "path", now=now)
    creds = botocore.credentials.Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG")
    req = botocore.awsrequest.AWSRequest(method="PUT", url="http://127.0.0.1:9000/demos/dolly/2026/a%20b.mp4")
    auth = botocore.auth.S3SigV4QueryAuth(creds, "s3", "us-east-1", expires=3600)
    orig = botocore.auth.datetime.datetime

    class Frozen(orig):
        @classmethod
        def utcnow(cls):
            return now.replace(tzinfo=None)

        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)
    botocore.auth.datetime.datetime = Frozen
    try:
        auth.add_auth(req)
    finally:
        botocore.auth.datetime.datetime = orig
    sig_ours = ours.split("X-Amz-Signature=")[1]
    sig_boto = req.url.split("X-Amz-Signature=")[1].split("&")[0]
    assert sig_ours == sig_boto


# ------------------------------------------------------------------ S3 against moto
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def moto():
    srv_mod = pytest.importorskip("moto.server")
    port = free_port()
    srv = srv_mod.ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    srv.start()
    url = f"http://127.0.0.1:{port}"
    req = urllib.request.Request(targets.presign("PUT", url, "us-east-1", "demos", "", "test", "test", 600, "path"),
                                 method="PUT", data=b"")
    urllib.request.urlopen(req).read()  # create bucket
    yield url
    srv.stop()


def test_s3_upload_presigned_get_and_delete(moto, tmp_path, monkeypatch):
    monkeypatch.setenv("T_AK", "test")
    monkeypatch.setenv("T_SK", "test")
    f = tmp_path / "demo.mp4"
    f.write_bytes(os.urandom(200_000))
    t = targets.S3Target("moto", {"type": "s3", "endpoint": moto, "bucket": "demos", "region": "us-east-1",
                                  "prefix": "dolly/", "access_key_env": "T_AK", "secret_key_env": "T_SK",
                                  "presign_days": 1})
    res = t.upload(f)
    assert res["url"] and not res["public"] and res["expires_at"]
    assert res["remote_ref"]["key"].startswith("dolly/") and res["remote_ref"]["key"].endswith(".mp4")
    assert urllib.request.urlopen(res["url"]).read() == f.read_bytes()
    t.delete(res["remote_ref"])
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(res["url"])


def test_s3_public_base_url(moto, tmp_path, monkeypatch):
    monkeypatch.setenv("T_AK", "test")
    monkeypatch.setenv("T_SK", "test")
    f = tmp_path / "pub.mp4"
    f.write_bytes(b"hello")
    t = targets.S3Target("r2", {"type": "s3", "endpoint": moto, "bucket": "demos", "access_key_env": "T_AK",
                                "secret_key_env": "T_SK", "public_base_url": "https://demos.example.dev/"})
    res = t.upload(f)
    assert res["url"].startswith("https://demos.example.dev/") and res["public"] and res["expires_at"] is None


def test_share_upload_records_history_and_delete(moto, tmp_path, monkeypatch, fake_wlcopy):
    monkeypatch.setenv("T_AK", "test")
    monkeypatch.setenv("T_SK", "test")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    write_cfg(tmp_path, monkeypatch, {"default": "moto", "targets": {"moto": {
        "type": "s3", "endpoint": moto, "bucket": "demos", "access_key_env": "T_AK", "secret_key_env": "T_SK"}}})
    proj = tmp_path / "proj"
    (proj / "exports").mkdir(parents=True)
    f = proj / "exports" / "x.mp4"
    f.write_bytes(b"data")
    e = share.upload(proj, f)
    assert e["copied"] and e["url"] in fake_wlcopy.read_text()          # link put on the clipboard
    assert share.history(proj)[-1]["id"] == e["id"]
    share.delete_remote(proj, e["id"])
    assert share.history(proj)[-1]["deleted"]
    assert (tmp_path / "state" / "dolly" / "shares.jsonl").exists()


# ------------------------------------------------------------------ rclone
RCLONE = shutil.which("rclone") or (str(Path.home() / ".local/bin/rclone") if (Path.home() / ".local/bin/rclone").exists() else None)


@pytest.mark.skipif(not RCLONE, reason="rclone not installed")
def test_rclone_s3_remote_link_and_delete(moto, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{Path(RCLONE).parent}:{os.environ['PATH']}")
    for k, v in {"TYPE": "s3", "PROVIDER": "Other", "ENDPOINT": moto, "ACCESS_KEY_ID": "test",
                 "SECRET_ACCESS_KEY": "test", "FORCE_PATH_STYLE": "true", "REGION": "us-east-1"}.items():
        monkeypatch.setenv(f"RCLONE_CONFIG_MOTOR_{k}", v)
    f = tmp_path / "r.mp4"
    f.write_bytes(os.urandom(50_000))
    t = targets.RcloneTarget("motor", {"type": "rclone", "remote": "motor:demos/rc", "link_expire": "1h"})
    res = t.upload(f)
    assert res["url"] and res["url"].startswith("http")
    assert urllib.request.urlopen(res["url"]).read() == f.read_bytes()
    t.delete(res["remote_ref"])
    out = subprocess.run([RCLONE, "lsf", "motor:demos/rc", "-R"], capture_output=True, text=True).stdout
    assert f.stem not in out


@pytest.mark.skipif(not RCLONE, reason="rclone not installed")
def test_rclone_remote_without_links(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{Path(RCLONE).parent}:{os.environ['PATH']}")
    monkeypatch.setenv("RCLONE_CONFIG_LOC_TYPE", "local")
    f = tmp_path / "l.mp4"
    f.write_bytes(b"abc")
    t = targets.RcloneTarget("loc", {"type": "rclone", "remote": f"loc:{tmp_path}/remote"})
    res = t.upload(f)
    assert res["url"] is None and "cannot make links" in res["note"]
    assert any((tmp_path / "remote").rglob("l-*.mp4"))


# ------------------------------------------------------------------ Litterbox (fake host)
def test_litterbox_upload_against_fake_server(tmp_path):
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers["Content-Length"])
            body = self.rfile.read(n)
            got["body"] = body
            got["ctype"] = self.headers["Content-Type"]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"https://litter.catbox.moe/abc123.mp4")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    f = tmp_path / "t.mp4"
    f.write_bytes(b"VIDEO")
    t = targets.LitterboxTarget("tmp", {"type": "litterbox", "time": "1h", "public": True,
                                        "endpoint": f"http://127.0.0.1:{srv.server_address[1]}/api.php"})
    res = t.upload(f)
    srv.shutdown()
    assert res["url"].endswith("abc123.mp4") and res["public"] and not res["deletable"]
    assert b'name="reqtype"\r\n\r\nfileupload' in got["body"] and b'name="time"\r\n\r\n1h' in got["body"]
    assert b"VIDEO" in got["body"] and got["ctype"].startswith("multipart/form-data")
    with pytest.raises(targets.UploadError):
        t.delete({})

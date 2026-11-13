"""Who may talk to the editor's server.

The editor listens on 127.0.0.1, which every account on the machine can reach, and it reads
recordings and changes the library and settings. Three things are checked here:

- a connection from another local account is refused, also with the right token;
- the browser is not started with the token on its command line (any local user can read
  command lines): it gets an address with a code that works once;
- a web page in the user's own browser cannot post to the editor, and the editor page runs
  no script but its own.
"""
import http.client
import json
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_pipeline import proj  # noqa: E402,F401
from tests.test_settings import home, make_project  # noqa: E402,F401
from dolly.editor import server as S  # noqa: E402


@pytest.fixture()
def editor(proj):
    ed = S.Editor(proj)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(ed))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield ed, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def get(port, path, headers=None, method="GET", body=None):
    """(status, headers, body) without following redirects."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


# ------------------------------------------------------------------ the other end's user id
TABLE = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:8AE4 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 4170 1 0000000000000000 100 0 0 10 0
   1: 0100007F:C350 0100007F:8AE4 01 00000000:00000000 00:00000000 00000000  1001        0 4183 1 0000000000000000 20 4 30 10 -1
   2: 0100007F:8AE4 0100007F:C350 01 00000000:00000000 00:00000000 00000000  1000        0 4184 1 0000000000000000 20 4 30 10 -1
   3: 0100007F:C351 0100007F:8AE4 06 00000000:00000000 03:000016E7 00000000     0        0 0 3 0000000000000000
"""


def test_peer_uid_reads_the_socket_table(tmp_path):
    if sys.byteorder != "little":
        pytest.skip("the sample table is from a little-endian machine")
    t = tmp_path / "tcp"
    t.write_text(TABLE)
    server, client = ("127.0.0.1", 0x8AE4), ("127.0.0.1", 0xC350)
    assert S.peer_uid(client, server, str(t)) == 1001              # the client's socket, not ours
    assert S.peer_uid(("127.0.0.1", 50001), server, str(t)) == S.GONE
    assert S.peer_uid(("127.0.0.1", 0xC351), server, str(t)) == S.GONE      # closed: listed as uid 0
    assert S.peer_uid(client, server, str(tmp_path / "missing")) is None
    assert S.peer_uid(("not an address", 1), server, str(t)) is None


def test_peer_uid_of_a_real_connection():
    import socket
    if not os.access("/proc/net/tcp", os.R_OK):
        pytest.skip("no /proc/net/tcp here")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.create_connection(srv.getsockname())
    conn, addr = srv.accept()
    try:
        assert S.peer_uid(addr, conn.getsockname()) == os.getuid()
        cli.close()                                     # what is left of it is not an open connection
        assert S.peer_uid(addr, conn.getsockname()) == S.GONE
        conn.close()
        assert S.peer_uid(addr, srv.getsockname()) == S.GONE
    finally:
        srv.close()


def test_another_users_connection_is_refused_even_with_the_token(editor, monkeypatch):
    ed, port = editor
    assert get(port, f"/api/app?t={ed.token}")[0] == 200
    monkeypatch.setattr(S, "peer_uid", lambda c, s: os.getuid() + 1)
    assert get(port, f"/api/app?t={ed.token}")[0] == 403
    assert get(port, f"/?t={ed.token}")[0] == 403
    assert get(port, "/api/quit", {"X-Dolly-Token": ed.token}, "POST", b"{}")[0] == 403
    assert get(port, "/static/fonts/x")[0] == 403                # nothing at all, not even a 404
    monkeypatch.setattr(S, "peer_uid", lambda c, s: S.GONE)      # sent and hung up: not served either
    assert get(port, "/api/settings", {"X-Dolly-Token": ed.token}, "POST", b'{"updates":{}}')[0] == 403
    monkeypatch.setenv("DOLLY_EDITOR_PEER_CHECK", "0")           # switched off by the user: the token guards
    assert get(port, f"/api/app?t={ed.token}")[0] == 200 and get(port, "/api/app?t=wrong")[0] == 403
    monkeypatch.delenv("DOLLY_EDITOR_PEER_CHECK")
    assert get(port, f"/api/app?t={ed.token}")[0] == 403
    monkeypatch.setattr(S, "peer_uid", lambda c, s: None)        # no socket table: the token still guards
    assert get(port, f"/api/app?t={ed.token}")[0] == 200
    assert get(port, "/api/app?t=wrong")[0] == 403


def test_a_real_second_account_is_refused(editor):
    """Run as root (CI containers): a process of user `nobody` with the right token."""
    if os.geteuid() != 0 or not os.access("/proc/net/tcp", os.R_OK):
        pytest.skip("needs root to become another user")
    ed, port = editor
    code = ("import os,sys,urllib.request,urllib.error\n"
            "os.setgroups([]); os.setgid(65534); os.setuid(65534)\n"
            "try:\n"
            "    print(urllib.request.urlopen(sys.argv[1], timeout=10).status)\n"
            "except urllib.error.HTTPError as e:\n"
            "    print(e.code)\n")
    url = f"http://127.0.0.1:{port}/api/project?t={ed.token}"
    out = subprocess.run([sys.executable, "-I", "-c", code, url], capture_output=True, text=True, timeout=30,
                         env={"PATH": os.environ.get("PATH", "/usr/bin")}, cwd="/")
    assert out.stdout.strip() == "403", (out.stdout, out.stderr)
    assert urllib.request.urlopen(url, timeout=10).status == 200           # the same address, as ourselves


# ------------------------------------------------------------------ other web pages
def test_only_this_server_as_host_and_origin(editor):
    ed, port = editor
    tok = {"X-Dolly-Token": ed.token, "Content-Type": "application/json"}
    here = f"http://127.0.0.1:{port}"
    # a host name someone pointed at 127.0.0.1
    assert get(port, f"/api/app?t={ed.token}", {"Host": f"evil.example:{port}"})[0] == 403
    assert get(port, f"/api/app?t={ed.token}", {"Host": f"localhost:{port}"})[0] == 200
    # a page on another site (or another local port) posting with a token it got hold of
    for origin in ("https://evil.example", f"http://127.0.0.1:{port + 1}", "null"):
        st = get(port, "/api/navigate", {**tok, "Origin": origin}, "POST", b'{"view":"settings"}')[0]
        assert st == 403, origin
    assert ed.view == "edit"
    assert get(port, "/api/navigate", {**tok, "Origin": here}, "POST", b'{"view":"settings"}')[0] == 200
    assert ed.view == "settings"
    assert get(port, "/api/navigate", tok, "POST", b'{"view":"library"}')[0] == 200     # `dolly`: no Origin


# ------------------------------------------------------------------ the browser's command line
def test_a_launch_code_opens_the_editor_once(editor):
    ed, port = editor
    code = ed.launch_code()
    assert code != ed.token and ed.token not in code
    st, h, _ = get(port, f"/?k={code}")
    assert st == 302 and h["Location"] == f"/?t={ed.token}" and h["Cache-Control"] == "no-store"
    st, _, body = get(port, f"/?k={code}")                         # read from a command line and tried again
    assert st == 403 and b"already been used" in body and ed.token.encode() not in body
    assert get(port, "/?k=never-given-out")[0] == 403
    assert get(port, f"/api/app?k={ed.launch_code()}")[0] == 403   # a code is for the page only


def test_a_launch_code_runs_out(editor, monkeypatch):
    ed, port = editor
    code = ed.launch_code()
    real = time.time
    monkeypatch.setattr(S.time, "time", lambda: real() + S.LAUNCH_CODE_SECONDS + 1)
    assert get(port, f"/?k={code}")[0] == 403


def test_a_new_launch_code_needs_the_token(editor):
    ed, port = editor
    assert get(port, "/api/launch-code", {}, "POST", b"{}")[0] == 403
    st, _, body = get(port, "/api/launch-code", {"X-Dolly-Token": ed.token}, "POST", b"{}")
    assert st == 200 and get(port, f"/?k={json.loads(body)['code']}")[0] == 302


def test_the_browser_is_never_started_with_the_token(home, monkeypatch):
    launched = []
    monkeypatch.setattr(S, "launch", lambda url: launched.append(url))
    monkeypatch.setattr(S, "editor_windows", lambda: [])
    monkeypatch.delenv("DOLLY_EDITOR_NEW", raising=False)
    pdir = make_project(home / "Videos" / "Dolly")
    (pdir / ".editor-url").write_text("http://127.0.0.1:1/?t=left-by-an-older-version")
    t = threading.Thread(target=S.serve, args=(pdir,), daemon=True)
    t.start()
    reg = S._registry_file()
    for _ in range(200):
        if reg.exists() and launched:
            break
        time.sleep(0.05)
    first = json.loads(reg.read_text())
    try:
        assert stat.S_IMODE(reg.stat().st_mode) == 0o600
        assert not (pdir / ".editor-url").exists()                 # no address with the token on disk
        S.serve(None, view="settings")                             # a second call: a window for the same editor
        assert len(launched) == 2
        port = first["port"]
        for url in launched:
            assert first["token"] not in url and url.startswith(f"http://127.0.0.1:{port}/?k=")
        assert launched[0].endswith("#edit") and launched[1].endswith("#settings")
        for url in launched:                                       # and each of them does open the editor
            st, h, _ = get(port, url.split(str(port), 1)[1].split("#")[0])
            assert st == 302 and h["Location"] == f"/?t={first['token']}"
    finally:
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{first['port']}/api/quit?t={first['token']}", data=b"{}", method="POST"), timeout=5)
        t.join(5)


def test_an_editor_from_before_the_update_is_replaced_not_given_the_token(home, monkeypatch):
    """An editor started by the old version can still be running when the plugin is updated.
    It has no launch codes. `dolly edit` must not fall back to starting the browser with the
    token: it asks the old editor to exit and starts a new one."""
    from http.server import BaseHTTPRequestHandler
    seen = []

    class Old(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._send(200, {}) if self.path.startswith("/api/app") else self._send(404, {})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            seen.append(self.path)
            if self.path == "/api/navigate":
                return self._send(200, {"view": "library", "page": False})
            self._send(200, {"ok": True}) if self.path == "/api/quit" else self._send(404, {})

        def log_message(self, *a):
            pass
    old = ThreadingHTTPServer(("127.0.0.1", 0), Old)
    threading.Thread(target=old.serve_forever, daemon=True).start()
    reg = S._registry_file()
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"pid": os.getpid(), "port": old.server_address[1], "token": "old-token"}))
    launched = []
    monkeypatch.setattr(S, "launch", lambda url: launched.append(url))
    monkeypatch.setattr(S, "editor_windows", lambda: [])
    monkeypatch.delenv("DOLLY_EDITOR_NEW", raising=False)
    t = threading.Thread(target=S.serve, args=(None,), kwargs={"view": "library"}, daemon=True)
    t.start()
    try:
        for _ in range(200):
            if launched:
                break
            time.sleep(0.05)
        new = json.loads(reg.read_text())
        assert seen == ["/api/navigate", "/api/launch-code", "/api/quit"]
        assert new["port"] != old.server_address[1] and new["token"] != "old-token"
        assert len(launched) == 1 and launched[0].startswith(f"http://127.0.0.1:{new['port']}/?k=")
        assert "old-token" not in launched[0] and new["token"] not in launched[0]
    finally:
        old.shutdown()
        old.server_close()
        new = json.loads(reg.read_text())
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{new['port']}/api/quit?t={new['token']}", data=b"{}", method="POST"), timeout=5)
        t.join(5)


# ------------------------------------------------------------------ what the page may run
def test_the_page_runs_only_its_own_script(editor):
    import re
    ed, port = editor
    st, h, body = get(port, f"/?t={ed.token}")
    csp = h["Content-Security-Policy"]
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]{16,})'", csp).group(1)
    html = body.decode()
    assert st == 200 and html.count("<script") == 1 and f'<script nonce="{nonce}">' in html
    assert "default-src 'none'" in csp and "unsafe-eval" not in csp and "connect-src 'self'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert h["Referrer-Policy"] == "no-referrer" and h["Cache-Control"] == "no-store"
    assert get(port, f"/?t={ed.token}")[1]["Content-Security-Policy"] != csp        # a new nonce each time


def test_files_from_a_recording_folder_are_shown_not_run(editor):
    ed, port = editor
    (ed.proj.exports).mkdir(exist_ok=True)
    (ed.proj.exports / "page.html").write_text("<script>document.title='ran'</script>")
    (ed.proj.exports / "pic.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>')
    try:
        for name in ("page.html", "pic.svg"):
            st, h, _ = get(port, f"/media/export/{name}?t={ed.token}")
            assert st == 200 and h["Content-Security-Policy"] == "sandbox", name
            assert h["X-Content-Type-Options"] == "nosniff"
        st, h, _ = get(port, f"/media/raw.mp4?t={ed.token}", {"Range": "bytes=0-99"})
        assert st == 206 and h["Content-Type"] == "video/mp4" and h["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" not in h                # a video opened in a tab still plays
    finally:
        (ed.proj.exports / "page.html").unlink()
        (ed.proj.exports / "pic.svg").unlink()

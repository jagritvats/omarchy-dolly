"""Editor and library fixes from the VirtualBox test pass (Nov 10):
one editor window (navigation instead of a new window per call), your own background
image (safe paths, upload, Settings default), cursor options with a baked-in cursor,
and one size format everywhere ("0 MB" for small recordings)."""
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_settings import home, make_project  # noqa: E402,F401
from dolly.units import human_size  # noqa: E402

SIZES = [0, 1, 499, 512, 412_000, 999_400, 999_999, 1_000_000, 4_700_000, 9_949_999, 9_950_000,
         10_000_000, 41_400_000, 999_499_999, 999_500_000, 1_300_000_000]


def test_human_size_rule():
    got = [human_size(b) for b in SIZES]
    assert got == ["0 KB", "1 KB", "1 KB", "1 KB", "412 KB", "999 KB", "1.0 MB", "1.0 MB", "4.7 MB", "9.9 MB",
                   "10 MB", "10 MB", "41 MB", "999 MB", "1.0 GB", "1.3 GB"], got
    assert human_size(400_000) == "400 KB"        # was "0 MB" in the library, "1 MB" in the panel


def test_editor_js_size_format_matches_python():
    """The editor's fmtSize is the same rule (checked by running it in node when available)."""
    import glob
    import os
    # mise's shim needs the real HOME, which the test isolation replaces: try real installs too
    ub = os.environ.get("PYTHONUSERBASE") or ""
    cands = [shutil.which("node")] + sorted(glob.glob(os.path.join(ub, "share/mise/installs/node/*/bin/node")))
    node = next((c for c in cands if c and subprocess.run([c, "-e", "0"], capture_output=True).returncode == 0), None)
    if not node:
        pytest.skip("node not installed")
    html = (ROOT / "dolly/editor/static/index.html").read_text()
    m = re.search(r"const fmtSize = (n => \{.*?\});\n", html, re.S)
    assert m, "fmtSize not found in index.html"
    js = f"const f={m.group(1)};console.log(JSON.stringify({json.dumps(SIZES)}.map(f)))"
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=20).stdout
    assert json.loads(out) == [human_size(b) for b in SIZES]
    assert "Math.round(b/1e6)+' MB'" not in html.split("const fmtGB")[1][:80]    # no integer-MB formatter left


def test_library_json_has_size_text(home):
    from dolly import library
    root = home / "Videos" / "Dolly"
    make_project(root)
    idx = library.index()
    e = idx["items"][0]
    assert e["size_text"] == human_size(e["bytes"]) and not e["size_text"].startswith("0 ")
    assert idx["total_text"] == human_size(idx["total_bytes"])
    assert "free_text" in idx and "cleanable_text" in idx


# ------------------------------------------------------------------ background images

def _png(path: Path, w=64, h=36, color=(30, 120, 200)):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), color).save(path)
    return path


def test_image_check_accepts_a_picture_in_home(home):
    from dolly import images
    p = _png(home / "Pictures" / "wall.png")
    assert images.check(str(p)) == p.resolve()
    assert images.check("~/Pictures/wall.png") == p.resolve()          # ~ expands
    assert images.info(p)["width"] == 64


@pytest.mark.parametrize("bad", ["", "Pictures/wall.png", "/etc/passwd", "/etc/hostname", "~/../../etc/passwd",
                                 "~/Pictures/missing.png", "~/notes.txt", "~/fake.png", "~/link.png", "/x\0y.png"])
def test_image_check_rejects(home, bad):
    from dolly import images
    _png(home / "Pictures" / "wall.png")
    (home / "notes.txt").write_text("hello")
    (home / "fake.png").write_text("not really a png")
    outside = home.parent / f"outside-{home.name}.png"
    _png(outside)
    (home / "link.png").symlink_to(outside)          # a link out of home is judged by its target
    try:
        with pytest.raises(images.ImageError):
            images.check(bad)
    finally:
        outside.unlink(missing_ok=True)


def test_upload_is_verified_and_content_addressed(home):
    from dolly import images
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (40, 20), (200, 10, 10)).save(buf, "PNG")
    a = images.save_upload(buf.getvalue(), "My Wall.PNG")
    b = images.save_upload(buf.getvalue(), "again.png")
    assert a["path"] == b["path"] and Path(a["path"]).parent == images.uploads_dir()
    with pytest.raises(images.ImageError):
        images.save_upload(b"<html>not an image</html>", "x.png")
    with pytest.raises(images.ImageError):
        images.save_upload(buf.getvalue(), "x.svg")
    assert not list(images.uploads_dir().glob("*.part"))
    names = [x["path"] for x in images.candidates()["uploaded"]]
    assert a["path"] in names


def test_config_background_image_round_trip(home):
    from dolly import config as cfgmod
    p = _png(home / "Pictures" / "wall.png")
    cfgmod.set_many({"look.background_image": "~/Pictures/wall.png", "look.background": "image",
                     "look.background_blur": 6})
    cfg = cfgmod.load()
    assert cfg["look"]["background_image"] == str(p.resolve()) and cfg["look"]["background"] == "image"
    st = cfgmod.style_defaults()
    assert st["background"] == {"type": "wallpaper", "path": str(p.resolve()), "blur": 6.0}
    with pytest.raises(cfgmod.ConfigError) as e:
        cfgmod.set_many({"look.background_image": "/etc/passwd"})
    assert "home folder" in str(e.value) or "not an image" in str(e.value)
    p.unlink()                                    # moved away later: a gradient, never a crash
    assert cfgmod.style_defaults()["background"]["type"] == "gradient"


def test_export_uses_the_custom_image(home):
    """The renderer draws the picture (cover-fit) behind the recording."""
    from dolly.render import build_background, make_layout
    p = _png(home / "Pictures" / "red.png", 320, 180, (220, 20, 20))
    style = {"background": {"type": "wallpaper", "path": str(p), "blur": 0}, "padding": 0.06, "shadow": 0,
             "radius": 0, "output": {"width": 640, "height": 360}}
    lay = make_layout(640, 400, style)
    img = build_background(lay, style, colors={"background": "#000000", "accent": "#7aa2f7"})
    b, g, r = img[2, 2]                           # a corner: background, not the screen
    assert r > 180 and g < 60 and b < 60, img[2, 2]


# ------------------------------------------------------------------ the editor server

@pytest.fixture()
def editor(home):
    from http.server import ThreadingHTTPServer
    from dolly.editor.server import Editor, make_handler
    pdir = make_project(home / "Videos" / "Dolly")
    ed = Editor(pdir)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ed))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, raw=None, timeout=10):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(base + path + ("&" if "?" in path else "?") + "t=" + ed.token, data=data,
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ctype = r.headers.get("Content-Type", "")
                return r.status, (json.loads(r.read()) if "json" in ctype else r.read())
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, json.loads(b)
            except ValueError:
                return e.code, b
    yield ed, call, pdir
    httpd.shutdown()


def test_image_endpoints_refuse_paths_outside(editor, home):
    ed, call, _ = editor
    p = _png(home / "Pictures" / "ok.png")
    code, body = call("/api/images/check", {"path": "/etc/passwd"})
    assert code == 400 and "error" in body
    code, _ = call("/media/image?path=/etc/passwd")
    assert code == 404
    code, body = call("/media/image?path=" + urllib.request.quote(str(p)))
    assert code == 200 and body[:8] == b"\x89PNG\r\n\x1a\n"
    code, body = call("/api/images/upload?name=x.png", raw=b"GIF89a junk")
    assert code == 400
    code, body = call("/api/images")
    assert code == 200 and any(x["path"] == str(p.resolve()) for x in body["pictures"])


def test_save_refuses_an_unchecked_background_path(editor, home):
    ed, call, pdir = editor
    code, body = call("/api/save", {"style": {**ed.proj.load()["style"],
                                              "background": {"type": "wallpaper", "path": "/etc/passwd"}}})
    assert code == 500 and "background image" in body["error"]
    p = _png(home / "Pictures" / "ok.png")
    code, _ = call("/api/save", {"style": {**ed.proj.load()["style"],
                                          "background": {"type": "wallpaper", "path": str(p), "index": 3}}})
    assert code == 200
    bg = ed.proj.load()["style"]["background"]
    assert bg["path"] == str(p.resolve()) and "index" not in bg


def test_navigation_long_poll(editor, home):
    ed, call, pdir = editor
    code, app = call("/api/app")
    seq = app["nav_seq"]
    got = {}

    def poll():
        got["r"] = call(f"/api/nav?seq={seq}&timeout=10", timeout=15)
    t = threading.Thread(target=poll)
    t.start()
    time.sleep(0.3)
    code, st = call("/api/navigate", {"view": "settings"})
    t.join(5)
    assert code == 200 and st["view"] == "settings"
    assert got["r"][0] == 200 and got["r"][1]["seq"] == seq + 1 and got["r"][1]["view"] == "settings"
    assert got["r"][1]["page"] is True                          # the page is polling: a window shows us
    code, st = call("/api/navigate", {"view": "nonsense"})
    assert code == 400


def test_serve_reuses_the_running_editor(home, monkeypatch):
    """A second `dolly library --open` / `settings` / `edit` navigates the running editor and
    returns: no second server, no second window (before: one window per call)."""
    from dolly.editor import server as S
    launched = []
    monkeypatch.setattr(S, "launch", lambda url: launched.append(url))
    monkeypatch.setattr(S, "editor_windows", lambda: [])
    monkeypatch.delenv("DOLLY_EDITOR_NEW", raising=False)
    pdir = make_project(home / "Videos" / "Dolly")
    t = threading.Thread(target=S.serve, args=(None,), kwargs={"view": "library"}, daemon=True)
    t.start()
    reg = S._registry_file()
    for _ in range(100):
        if reg.exists():
            break
        time.sleep(0.05)
    first = json.loads(reg.read_text())
    assert len(launched) == 1

    done = threading.Event()
    t2 = threading.Thread(target=lambda: (S.serve(None, view="settings"), done.set()), daemon=True)
    t2.start()
    assert done.wait(10), "second call must return (it attaches) instead of serving again"
    assert json.loads(reg.read_text()) == first                 # still the first editor
    # the page never polled (no window): one window for the same server, not a new server
    assert len(launched) == 2 and launched[1].startswith(f"http://127.0.0.1:{first['port']}/")
    assert launched[1].endswith("#settings")

    S.serve(pdir, view="edit")                                   # open a recording in it
    st = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{first['port']}/api/app?t={first['token']}", timeout=5).read())
    assert st["view"] == "edit" and st["project"] == pdir.name
    urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{first['port']}/api/quit?t={first['token']}", data=b"{}", method="POST"), timeout=5)
    t.join(5)
    assert not reg.exists()                                       # the editor removed its entry on exit


def test_stale_registry_starts_a_fresh_editor(home):
    from dolly.editor import server as S
    reg = S._registry_file()
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"pid": 1, "port": 9, "token": "x"}))       # nothing listens on port 9
    assert S._attach("library", None) is None and not reg.exists()


def test_focus_window_uses_lua_dispatch_first(monkeypatch):
    from dolly.editor import server as S
    calls = []

    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""
    monkeypatch.setattr(S.shutil if hasattr(S, "shutil") else __import__("shutil"), "which", lambda c: "/usr/bin/" + c)

    def run(cmd, **kw):
        calls.append(cmd)
        return R(0, "ok") if "hl.dsp.focus" in cmd[-1] else R(0, "error: legacy")
    monkeypatch.setattr(S.subprocess, "run", run)
    assert S.focus_window("0xabc") is True
    assert calls[0][-1] == 'hl.dsp.focus({ window = "address:0xabc" })' and len(calls) == 1


def test_motion_blur_is_off_when_the_cursor_is_baked_in():
    """Motion blur only streaks the re-drawn cursor; with wf-recorder's real cursor it did
    nothing visible but disabled frame reuse."""
    src = (ROOT / "dolly/render.py").read_text()
    assert 'self.motion_blur = bool(cs.get("motion_blur", False)) and self.draw_cursor' in src
    html = (ROOT / "dolly/editor/static/index.html").read_text()
    assert "BAKED_OFF=['cursor.enabled','cursor.motion_blur']" in html and 'id="bakedNote"' in html


def test_settings_payload_says_whether_capture_bakes_the_cursor(home, monkeypatch):
    from dolly.editor import server as S
    monkeypatch.setattr(S, "capture_bakes_cursor", lambda: True)
    ed = S.Editor(None, view="settings")
    assert ed.settings_payload()["capture_bakes_cursor"] is True

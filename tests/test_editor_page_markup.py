"""The editor page shows what is in a recording folder and a share config as text.

Names and labels in project.json, share targets, share history (which holds what an upload
host answered) end up in the page. None of it may become markup: script in the editor page
can read every recording and change the library and settings. Checked in a real browser:
the hostile values are shown literally, nothing runs, and the page's content policy stops an
event-handler attribute even if one did get in.

Needs playwright and Chromium (skipped without them).
"""
import json
import shutil
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_pipeline import proj  # noqa: E402,F401

pw = pytest.importorskip("playwright.sync_api")

BOOM = "<img src=x onerror=\"window.__ran=(window.__ran||0)+1\">"


@pytest.fixture()
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(args=["--no-sandbox"])
        except Exception as e:                                  # no browser installed
            pytest.skip(f"Chromium not available: {str(e).splitlines()[0]}")
        yield b
        b.close()


@pytest.fixture()
def hostile(proj, tmp_path, monkeypatch):
    """A copy of the test recording whose every free-text value carries markup."""
    from dolly.editor import server as S
    pdir = tmp_path / "take"
    shutil.copytree(proj, pdir)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cfg = tmp_path / "share.json"
    cfg.write_text(json.dumps({"targets": {f"t{BOOM}": {"type": "litterbox", "public": True}}, "default": f"t{BOOM}"}))
    cfg.chmod(0o600)
    monkeypatch.setenv("DOLLY_SHARE_CONFIG", str(cfg))
    ed = S.Editor(pdir)
    data = ed.proj.load()
    data["segments"] = [{"start": 0.2, "end": 1.2, "zoom": 1.8, "cx": 0.5, "cy": 0.5, "source": "auto",
                         "reason": f"click{BOOM}", "follow": True}]
    data["speed"] = [{"start": 1.5, "end": 2.2, "speed": 2, "reason": f"idle{BOOM}", "id": "s1"}]
    data["privacy"] = [{"id": f'p1"><b id=pwn>{BOOM}', "start": 0.1, "end": 0.9, "rect": [0.1, 0.1, 0.3, 0.2],
                        "mode": "blur", "source": "secret", "label": f"token{BOOM}"}]
    ed.proj.save(data)
    (pdir / "shares.json").write_text(json.dumps([
        {"id": f'ab"><b id=pwn2>{BOOM}', "time": 1762900000, "action": f"upload{BOOM}", "preset": f"gh{BOOM}",
         "target": f"tmp{BOOM}", "public": True, "deletable": True, "url": f"http://l.example/{BOOM}"},
        {"id": f'cd"><b id=pwn2>{BOOM}', "time": 1762900060, "action": "upload", "target": "tmp", "deletable": True,
         "url": f"http://l.example/{BOOM}"},
        None, "not an entry"]))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(ed))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield ed, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def open_page(browser, url):
    pg = browser.new_page(viewport={"width": 1500, "height": 950})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
    pg.goto(url)
    pg.wait_for_selector("#zlane .it", timeout=30000)
    return pg, errors


def test_hostile_values_are_shown_as_text_and_nothing_runs(browser, hostile):
    ed, base = hostile
    pg, errors = open_page(browser, f"{base}/?t={ed.token}#edit")
    pg.wait_for_selector("#plane .it")
    pg.click("#plane .it")                                       # the selection panel of the flagged box
    pg.click("#shareTop")
    pg.wait_for_selector("#shHist div", timeout=60000)
    pg.wait_for_selector("#shTarget option", state="attached")
    pg.wait_for_timeout(500)
    got = pg.evaluate("""() => ({
        ran: window.__ran || 0,
        injected: document.querySelectorAll('img[src="x"], #pwn, #pwn2').length,
        lanes: [...document.querySelectorAll('#zlane .it, #slane .it, #plane .it')].map(e => e.textContent).join(' | '),
        priv: document.querySelector('#privList').textContent,
        hist: document.querySelector('#shHist').textContent,
        target: document.querySelector('#shTarget').value,
        rm: document.querySelector('#shHist [data-rm]').dataset.rm })""")
    assert got["ran"] == 0 and got["injected"] == 0, got
    for where in ("lanes", "priv", "hist"):
        assert "<img src=x onerror=" in got[where], (where, got[where])        # literally, as text
    assert got["lanes"].count("<img") == 3 and got["hist"].count("<img") >= 4
    assert got["target"].startswith("t<img") and got["rm"].startswith('cd"><b id=pwn2>')   # values intact
    assert not [e for e in errors if "favicon" not in e], errors


def test_the_content_policy_stops_a_handler_that_did_get_in(browser, hostile):
    ed, base = hostile
    pg, errors = open_page(browser, f"{base}/?t={ed.token}#edit")
    assert not errors, errors                                    # the page itself runs under its policy
    pg.evaluate("""() => { document.querySelector('#privList').innerHTML =
        '<img src=x onerror="window.__ran=1"><a id=j href="javascript:window.__ran=2">x</a>';
        const s = document.createElement('script'); s.textContent = 'window.__ran=3'; document.body.append(s);
        document.querySelector('#j').click() }""")
    pg.wait_for_timeout(600)
    assert pg.evaluate("window.__ran || 0") == 0
    assert pg.evaluate("typeof TOKEN") == "string"               # and its own script did run


def test_a_launch_address_opens_the_page_at_its_view(browser, hostile):
    """What the browser is started with: /?k=<code>#view. One hop later it is the usual page,
    the #view kept; the same address a second time (read off the command line) is refused."""
    ed, base = hostile
    start = f"{base}/?k={ed.launch_code()}#edit"
    pg, errors = open_page(browser, start)
    assert pg.url == f"{base}/?t={ed.token}#edit" and not errors
    pg2 = browser.new_page()
    r = pg2.goto(start)
    assert r.status == 403 and "already been used" in pg2.content() and ed.token not in pg2.content()

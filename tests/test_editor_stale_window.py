"""A window left over from an editor that has exited, loaded against a newer editor, gets a
page that says so instead of the bare "Error response" (seen on a desktop, Nov 10)."""
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_pipeline import proj  # noqa: E402,F401


def test_stale_window_is_told_and_the_api_stays_closed(proj):
    from http.server import ThreadingHTTPServer
    from dolly.editor.server import Editor, make_handler
    ed = Editor(proj)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ed))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/?t=a-token-from-an-earlier-editor")
        body = e.value.read().decode()
        assert e.value.code == 403 and "earlier session" in body and "dolly edit" in body
        assert "Error response" not in body and ed.token not in body
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/project?t=a-token-from-an-earlier-editor")
        assert e.value.code == 403 and "earlier session" not in e.value.read().decode()
        assert urllib.request.urlopen(f"{base}/?t={ed.token}").status == 200
    finally:
        httpd.shutdown()

"""A standard install: `omarchy plugin add ... --enable`, and nothing else installed.

Three machines, each through the real launcher (bin/dolly) in child processes:

- stock Omarchy: python-cairo and python-gobject are there, NumPy, Pillow and OpenCV are
  not (hidden as in every test run). Record, stop, the zoom plan, the styled export (the
  compositor) and the editor all work;
- python-cairo removed (a Python without site packages): Dolly still records, makes the
  zoom plan and gives the plain export; the editor and the styled export say that
  python-cairo is missing and where to install it;
- python-gobject (Pango) removed: text falls back to cairo's simple font and the styled
  export still works.

Dolly installs nothing in any of them (tests/test_installs_nothing.py).
"""
import json
import os
import random
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import analysis, capture, deps, gfx, plain, sync  # noqa: E402
from tests.desktop_stubs import GSR, HYPRCTL  # noqa: E402  (stub hyprctl + recorder)

CAIRO = "python-cairo"
HOW = "Omarchy menu > Install > Package"
needs_ffmpeg = pytest.mark.skipif(not (__import__("shutil").which("ffmpeg") and __import__("shutil").which("ffprobe")),
                                  reason="needs ffmpeg + ffprobe")


def _has_cairo() -> bool:
    try:
        import cairo  # noqa: F401
        return True
    except ImportError:
        return False


needs_cairo = pytest.mark.skipif(not _has_cairo(), reason="needs python-cairo (a stock Omarchy has it)")


# ------------------------------------------------------------------ the three machines
@pytest.fixture(scope="module")
def bare_venv(tmp_path_factory):
    """A Python without site packages: no cairo, no gi, no numpy, PIL or cv2."""
    venv = tmp_path_factory.mktemp("bare") / "venv"
    r = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"cannot make a Python without site packages: {r.stderr.strip()[-200:]}")
    probe = "import importlib.util as u, sys; sys.exit(any(u.find_spec(m) for m in ('cairo', 'gi', 'numpy', 'PIL', 'cv2')))"
    assert subprocess.run([str(venv / "bin" / "python"), "-c", probe], env={**os.environ, "PYTHONPATH": ""}).returncode == 0
    return venv


@pytest.fixture(scope="module")
def system_python(tmp_path_factory):
    """The machine's own Python as bin/dolly's DOLLY_VENV: a folder whose bin/python is it."""
    d = tmp_path_factory.mktemp("system")
    (d / "bin").mkdir()
    (d / "bin" / "python").symlink_to(sys.executable)
    return d


def _env(base: Path, venv: Path, hide: tuple = (), open_editor: bool = False) -> dict:
    """bin/dolly's environment: stub desktop, private folders, `venv` as its Python, the
    modules in `hide` made unimportable (as NumPy and Pillow already are in this run)."""
    bindir = base / "bin"
    bindir.mkdir()
    for name, body in (("hyprctl", HYPRCTL), ("gpu-screen-recorder", GSR)):
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    path = [str(ROOT)]
    if hide:
        stubs = base / "hidden"
        stubs.mkdir()
        for mod in hide:
            (stubs / f"{mod}.py").write_text(f"raise ImportError('{mod} is hidden in this test')\n")
        path.append(str(stubs))
    path += [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]     # tests/no_cv2, tests/no_numpy
    (base / "run").mkdir()
    (base / "run").chmod(0o700)
    e = dict(os.environ)
    e.update({"PATH": f"{bindir}:{e['PATH']}", "DOLLY_DIR": str(base / "videos"), "XDG_RUNTIME_DIR": str(base / "run"),
              "XDG_STATE_HOME": str(base / "state"), "XDG_CACHE_HOME": str(base / "cache"),
              "XDG_CONFIG_HOME": str(base / "config"), "HOME": str(base), "PYTHONPATH": os.pathsep.join(path),
              "DOLLY_VENV": str(venv), "DOLLY_OPEN_EDITOR": "1" if open_editor else "0", "DOLLY_EDITOR_NEW": "1"})
    for k in ("HYPRLAND_INSTANCE_SIGNATURE", "DOLLY_BACKEND", "DOLLY_SKIP_DEPS", "WAYLAND_DISPLAY"):
        e.pop(k, None)                    # DOLLY_BACKEND would skip the check this is about
    return e


def run(env, *args, check=True, timeout=180):
    return subprocess.run([str(ROOT / "bin" / "dolly"), *args], env=env, capture_output=True, text=True,
                          check=check, timeout=timeout)


def py(env, code: str) -> str:
    """Run Python code with the Python and the module path bin/dolly would use."""
    exe = Path(env["DOLLY_VENV"]) / "bin" / "python"
    return subprocess.run([str(exe), "-c", code], env=env, capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()


SHIM_CALL = re.compile(r"^(omarchy-[a-z-]+|notify-send|makoctl|hyprctl|uwsm|systemctl|loginctl|gdbus|wl-copy|wl-paste|"
                       r"xdg-open|nautilus|mpv|grim|slurp)( |$)")


def cards(since: int = 0) -> list[str]:
    """What Dolly sent to the notification shim after offset `since`, one string per card
    (a card's text can hold line breaks, so a call can span lines of the shim log)."""
    log = Path(os.environ["DOLLY_TEST_SHIM_LOG"])
    text = log.read_bytes()[since:].decode("utf-8", "replace") if log.exists() else ""
    calls: list[str] = []
    for ln in text.splitlines():
        if SHIM_CALL.match(ln) or not calls:
            calls.append(ln)
        else:
            calls[-1] += " " + ln
    return [c for c in calls if c.startswith("omarchy-notification-send")]


def cards_soon(since: int, containing: str, wait: float = 5.0) -> list[str]:
    """The cards with that text; a card is sent by a detached process, so it can land a
    moment after the command that asked for it has returned."""
    end = time.time() + wait
    while True:
        got = [c for c in cards(since) if containing in c]
        if got or time.time() > end:
            return got
        time.sleep(0.1)


def mark() -> int:
    log = Path(os.environ["DOLLY_TEST_SHIM_LOG"])
    return log.stat().st_size if log.exists() else 0


def video(path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    p = json.loads(out)
    v = next(s for s in p["streams"] if s["codec_type"] == "video")
    return {"w": v["width"], "h": v["height"], "duration": float(p["format"]["duration"])}


def colours(path) -> tuple[str, str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=pix_fmt,color_range", "-of", "json", str(path)], capture_output=True, text=True,
                         check=True).stdout
    s = json.loads(out)["streams"][0]
    return s["pix_fmt"], s.get("color_range", "")


def patch_of(path, t: float, x: int, y: int, size: int = 16) -> list[tuple[int, int, int]]:
    """The pixels of a small square of the frame at time `t`."""
    buf = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-vf", f"crop={size}:{size}:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    assert len(buf) == size * size * 3
    return [(buf[i], buf[i + 1], buf[i + 2]) for i in range(0, len(buf), 3)]



def record(env) -> tuple[Path, list[str]]:
    """Record about 2.5 s of the stub desktop and stop: (the take's folder, the cards Stop sent)."""
    r = run(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "30")
    pdir = Path(r.stdout.strip().splitlines()[-1])
    time.sleep(2.5)
    at = mark()
    run(env, "stop")
    time.sleep(1.0)                       # an editor, had one been started, would have said so by now
    return pdir, cards(at)


def planned(pdir: Path, wait: float = 60.0) -> bool:
    """The zoom plan is made (the background job Stop started has finished)."""
    end = time.time() + wait
    while time.time() < end:
        if analysis.pending(pdir) is None and not analysis.running(pdir):
            return isinstance(json.loads((pdir / "project.json").read_text())["segments"], list)
        time.sleep(0.3)
    return False


def get(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, "", b""


# ------------------------------------------------------------------ (a) stock Omarchy
@pytest.fixture(scope="module")
def stock(tmp_path_factory, system_python):
    env = _env(tmp_path_factory.mktemp("stock"), system_python)
    pdir, sent = record(env)
    return env, pdir, sent


@needs_ffmpeg
@needs_cairo
def test_stock_records_plans_exports_styled_and_edits(stock):
    env, pdir, sent = stock
    assert py(env, "from dolly import deps, gfx; print(gfx.available(), deps.python_missing())") == "True []"
    hidden = ("import importlib\n"
              "for m in ('numpy', 'PIL', 'cv2'):\n"
              "    try:\n"
              "        importlib.import_module(m)\n"
              "        print(m)\n"
              "    except ImportError:\n"
              "        pass\n")
    assert py(env, hidden) == ""                                                  # the stock machine has none of them
    ev = json.loads((pdir / "events.json").read_text())
    assert ev["video"]["duration"] > 1.5 and any(e["type"] == "clients" for e in ev["events"])
    done = [c for c in sent if "Demo recorded" in c]
    assert len(done) == 1 and f"edit {pdir}" in done[0] and f"export {pdir}" in done[0]
    assert "python-cairo" not in done[0] and "--install" not in done[0]
    assert planned(pdir), "the zoom plan was not made"
    log = (pdir / "recorder.log").read_text()
    assert "Traceback" not in log and "No module named" not in log
    j = json.loads(run(env, "export", str(pdir), "--json", "--quality", "draft").stdout)
    assert j["ok"] and j["engine"] == "compositor" and j["format"] == "mp4" and Path(j["output"]).exists()
    got = video(j["output"])
    assert got["duration"] > 1.0 and got["w"] > 0
    rep = json.loads(run(env, "doctor", "--json", check=False).stdout)
    rows = {c["name"]: c for c in rep["checks"]}
    assert rows["python: cairo"]["ok"] is True and rep["missing_packages"]["packages"] == []
    assert not any(n.startswith(("python: numpy", "python: PIL")) for n in rows)


@needs_ffmpeg
@needs_cairo
def test_stock_editor_starts_and_serves_a_frame(stock):
    env, pdir, _ = stock
    p = subprocess.Popen([str(ROOT / "bin" / "dolly"), "edit", str(pdir), "--no-browser"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        url = p.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:"), (url, p.stderr.read()[-500:] if p.poll() is not None else "")
        base, token = url.split("/?t=")[0], url.split("?t=")[1].split("#")[0]
        status, _, body = get(f"{base}/api/plan?t={token}")
        assert status == 200 and isinstance(json.loads(body).get("segments"), list)
        status, ctype, body = get(f"{base}/media/thumb?t={token}&dir={pdir}")
        assert status == 200 and ctype == "image/jpeg" and body[:2] == b"\xff\xd8" and len(body) > 1000
    finally:
        p.terminate()
        p.wait(timeout=20)


@needs_ffmpeg
@needs_cairo
def test_the_private_environment_of_0_7_3_is_passed_over(stock, tmp_path):
    """Set up offered one until 0.7.3 (`python3 -m venv`, NumPy and Pillow in it). It cannot
    see the system's cairo, so the launcher runs the system Python: the editor and the
    styled export are still there after the update, and doctor names the folder. Remade
    with the system's packages in view (docs/captions.md), it is used again."""
    _, pdir, _ = stock
    env = _env(tmp_path, tmp_path / "not-set")
    del env["DOLLY_VENV"]
    env.update({"XDG_DATA_HOME": str(tmp_path / "share"), "DOLLY_DIR": str(pdir.parent),
                "DOLLY_TEST_ENV_USED": str(tmp_path / "used")})
    old = tmp_path / "share" / "dolly" / "venv"
    r = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(old)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"cannot make a Python environment: {r.stderr.strip()[-200:]}")
    cfg, closed = old / "pyvenv.cfg", "include-system-site-packages = false"
    assert closed in cfg.read_text()
    # only a Python that runs inside the environment leaves this file
    (next((old / "lib").glob("python*/site-packages")) / "sitecustomize.py").write_text(
        "import os\nopen(os.environ['DOLLY_TEST_ENV_USED'], 'w').close()\n")

    def doctor_rows():
        return {c["name"]: c for c in json.loads(run(env, "doctor", "--json", check=False).stdout)["checks"]}
    out = tmp_path / "old-env.mp4"
    j = json.loads(run(env, "export", str(pdir), "--json", "--quality", "draft", "-o", str(out)).stdout)
    assert j["ok"] and j["engine"] == "compositor" and out.exists() and video(out)["duration"] > 1.0
    rows = doctor_rows()
    row = rows["private Python environment"]
    assert row["level"] == "warn" and str(old) in row["detail"] and "docs/captions.md" in row["detail"]
    assert rows["python: cairo"]["ok"] is True and not (tmp_path / "used").exists()
    cfg.write_text(cfg.read_text().replace(closed, "include-system-site-packages = true"))
    rows = doctor_rows()
    assert "private Python environment" not in rows and rows["python: cairo"]["ok"] is True
    assert (tmp_path / "used").exists()
    (tmp_path / "used").unlink()
    env["DOLLY_VENV"] = str(old)                    # named outright, an environment is taken as it is
    cfg.write_text(cfg.read_text().replace("include-system-site-packages = true", closed))
    rows = doctor_rows()
    assert "private Python environment" not in rows and rows["python: cairo"]["ok"] is False
    assert (tmp_path / "used").exists()


# ------------------------------------------------------------------ (c) Pango removed
@needs_ffmpeg
@needs_cairo
def test_without_pango_text_falls_back_and_the_styled_export_works(stock, tmp_path, system_python):
    _, pdir, _ = stock
    env = _env(tmp_path, system_python, hide=("gi",))
    env["DOLLY_DIR"] = str(pdir.parent)
    code = ("from dolly import deps, gfx\n"
            "img = gfx.new(200, 60)\n"
            "gfx.draw_text(img, 'Ctrl+S', 10, 10, gfx.font('sans', 24), gfx.rgb('#ffffff'))\n"
            "print(gfx.text_engine(), deps.text_layout_missing(), deps.python_missing(), gfx.mean(img)[0] > 0)")
    assert py(env, code) == "cairo True [] True"
    out = tmp_path / "nogi.mp4"
    j = json.loads(run(env, "export", str(pdir), "--json", "--quality", "draft", "-o", str(out)).stdout)
    assert j["ok"] and j["engine"] == "compositor" and out.exists() and video(out)["duration"] > 1.0
    rows = {c["name"]: c for c in json.loads(run(env, "doctor", "--json", check=False).stdout)["checks"]}
    assert rows["python: Pango (text)"]["ok"] is False and rows["python: Pango (text)"]["level"] == "warn"
    assert json.loads(run(env, "deps", "--json").stdout)["packages"] == []          # nothing is missing for real


# ------------------------------------------------------------------ (b) python-cairo removed
@pytest.fixture(scope="module")
def take(tmp_path_factory, bare_venv):
    """One take recorded and stopped on that machine: (env, its folder, the cards Stop sent)."""
    env = _env(tmp_path_factory.mktemp("nocairo"), bare_venv, open_editor=True)
    pdir, sent = record(env)
    return env, pdir, sent


@needs_ffmpeg
def test_every_module_imports_without_cairo(bare_venv):
    code = ("import importlib, json, pkgutil, dolly" + chr(10)
            + "bad = {}" + chr(10)
            + "for m in pkgutil.walk_packages(dolly.__path__, 'dolly.'):" + chr(10)
            + "    try:" + chr(10)
            + "        importlib.import_module(m.name)" + chr(10)
            + "    except ImportError as e:" + chr(10)
            + "        bad[m.name] = str(e)" + chr(10)
            + "print(json.dumps(bad))" + chr(10))
    r = subprocess.run([str(bare_venv / "bin" / "python"), "-c", code], env={**os.environ, "PYTHONPATH": str(ROOT)},
                       capture_output=True, text=True, check=True)
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {}


@needs_ffmpeg
def test_without_cairo_a_recording_is_made_and_planned(take):
    env, pdir, sent = take
    assert json.loads(run(env, "status", "--json", check=False).stdout)["phase"] == "idle"
    ev = json.loads((pdir / "events.json").read_text())
    assert ev["video"]["duration"] > 1.5 and any(e["type"] == "clients" for e in ev["events"])
    assert planned(pdir), "the zoom plan needs no cairo, so it is made here too"
    log = (pdir / "recorder.log").read_text()
    assert "Traceback" not in log and "No module named" not in log
    done = [c for c in sent if "Demo recorded" in c]
    assert len(done) == 1
    assert f"export {pdir}" in done[0] and f"edit {pdir}" not in done[0] and "--install" not in done[0]
    assert f"Zoom and the editor need python-cairo ({HOW})" in done[0]
    assert not any("needs python-cairo" in c for c in sent)                       # nobody tried to open the editor
    assert not any("could not" in c.lower() or "failed" in c.lower() for c in sent)


@needs_ffmpeg
def test_without_cairo_the_export_is_the_plain_recording(take):
    env, pdir, _ = take
    at = mark()
    r = run(env, "export", str(pdir))
    out = Path(r.stdout.strip().splitlines()[-1])
    assert out.suffix == ".mp4" and out.parent == pdir / "exports" and "plain export" in r.stderr
    assert f"python-cairo (install it with {HOW})" in r.stderr
    ev = json.loads((pdir / "events.json").read_text())
    proj = json.loads((pdir / "project.json").read_text())
    t0, t1 = proj.get("trim") or [0.0, ev["video"]["duration"]]
    got = video(out)
    assert (got["w"], got["h"]) == (ev["video"]["w"], ev["video"]["h"])
    assert abs(got["duration"] - (t1 - t0)) < 0.25
    sent = cards_soon(at, "Exported as recorded")
    assert len(sent) == 1 and "--install" not in sent[0] and f"edit {pdir}" not in sent[0] and HOW in sent[0]
    assert not list((pdir / "exports").glob(".*.part.mp4"))
    assert (pdir / "raw.mkv").exists()                                           # never traded for a plain export
    j = json.loads(run(env, "export", str(pdir), "--json").stdout)
    assert j["ok"] and j["engine"] == "plain" and Path(j["output"]).exists() and j["format"] == "mp4"


@needs_ffmpeg
def test_without_cairo_what_draws_says_what_is_missing(take):
    env, pdir, _ = take
    for args in (("edit", str(pdir)), ("settings",), ("library", "--open")):
        at = mark()
        r = run(env, *args, check=False)
        assert r.returncode == 1 and f"the editor needs python-cairo (install it with {HOW})" in r.stderr, args
        assert "Traceback" not in r.stderr
        sent = cards_soon(at, "The editor needs python-cairo")
        assert len(sent) == 1 and HOW in sent[0] and "--action" not in sent[0] and len(cards(at)) == 1
    for args, needle in ((("share", "export", "github", str(pdir)), f"this needs python-cairo (install it with {HOW})"),
                         (("run", "--check"), f"a scripted demo needs python-cairo (install it with {HOW})"),
                         (("run", str(ROOT / "examples" / "terminal-demo.toml")), "a scripted demo needs python-cairo")):
        r = run(env, *args, check=False)
        assert r.returncode == 1 and needle in r.stderr and "Traceback" not in r.stderr, (args, r.stderr[-300:])
        assert "unexpected error" not in r.stderr
    at = mark()
    r = run(env, "export", str(pdir), "--aspect", "9:16", check=False)
    assert r.returncode == 1 and "9:16" in r.stderr and f"python-cairo (install it with {HOW})" in r.stderr
    assert cards_soon(at, "A 9:16 export needs python-cairo")
    assert len(list((pdir / "exports").glob("*9x16*"))) == 0


@needs_ffmpeg
def test_without_cairo_doctor_deps_and_the_panel_say_what_is_held_back(take):
    env, _, _ = take
    rep = json.loads(run(env, "doctor", "--json", check=False).stdout)
    rows = {c["name"]: c for c in rep["checks"]}
    assert rows["python: cairo"]["level"] == "warn" and rows["python: cairo"]["ok"] is False
    assert HOW in rows["python: cairo"]["detail"] and rows["share targets"]["level"] == "ok"
    mp = rep["missing_packages"]
    assert mp["packages"] == [CAIRO] and mp["blocking"] == []
    assert rep["paths"]["render"].startswith("plain export only")
    r = run(env, "deps")
    assert r.stdout.startswith("python-cairo ") and HOW in r.stdout
    panel = json.loads(run(env, "panel").stdout)
    assert panel["deps"]["packages"] == [CAIRO] and panel["deps"]["blocking"] == []
    assert not {"deps", "recent", "config"} & set(panel.get("errors") or {}) and len(panel["recent"]) >= 1


# ------------------------------------------------------------------ markers, boxes and the cover
GREY, FRAME = 0x80, (640, 400)
SECRET = [200, 120, 240, 160]            # x, y, w, h of a password manager's window


def _marked_take(base: Path) -> Path:
    """6 s of grey at 30 fps with the sync marker at 0.5 s and at 5 s, a logger that started
    0.4 s after the first frame, and a password manager on screen the whole time. Full-range
    video, as wf-recorder writes it."""
    pdir = base / "2026-11-14_10-00-00"
    pdir.mkdir(parents=True)
    en = "between(n,15,19)+between(n,150,154)"
    box = (f"drawbox=x=0:y=0:w=40:h=40:color=0xff00ff:t=fill:enable='{en}',"
           f"drawbox=x=10:y=10:w=20:h=20:color=0x00ff00:t=fill:enable='{en}'")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x808080:s={FRAME[0]}x{FRAME[1]}:r=30",
                    "-frames:v", "180", "-vf", box + ",scale=out_range=pc", "-c:v", "libx264", "-preset", "ultrafast",
                    "-qp", "0", "-pix_fmt", "yuv444p", "-color_range", "pc", "-f", "matroska", str(pdir / "raw.mkv")],
                   check=True)
    (pdir / "capture.json").write_text(json.dumps({"backend": "gsr", "region": [0, 0, *FRAME], "scale": 1.0,
                                                   "mode": "screen", "fps": 30, "clock_mono": 1000.0}))
    win = {"address": "0xb1", "class": "org.keepassxc.KeePassXC", "title": "Passwords", "rect": SECRET,
           "floating": True, "fullscreen": False}
    lines = [{"ms": 0, "type": "start", "t0_mono": 1000.0 + sync.FIRST_FRAME_LATENCY + 0.4},
             {"ms": 0, "type": "info", "click_source": "lua"},
             {"ms": 100, "type": "sync", "kind": "start"},
             {"ms": 150, "type": "clients", "focus": "0xb1", "windows": [win]},
             {"ms": 2000, "type": "click", "x": 500, "y": 300},
             {"ms": 4000, "type": "clients", "focus": "0xb1", "windows": [win, {**win, "address": "0xc2", "class": "foot",
                                                                                "rect": [20, 300, 100, 60]}]},
             {"ms": 4600, "type": "sync", "kind": "stop"}]
    (pdir / "events.raw.jsonl").write_text("".join(json.dumps(e) + chr(10) for e in lines))
    return pdir


@needs_ffmpeg
def test_markers_boxes_and_the_cover_without_cairo(tmp_path, bare_venv):
    base = tmp_path / "m"
    base.mkdir()
    env = _env(base, bare_venv)
    pdir = _marked_take(tmp_path / "videos")
    run(env, "finalize", str(pdir))
    ev = json.loads((pdir / "events.json").read_text())
    fit = ev["capture"]["sync"]
    assert fit["method"] == "markers2" and abs(fit["markers_video"][0] - 0.5) < 0.04 and abs(fit["markers_video"][1] - 5.0) < 0.04
    proj = json.loads((pdir / "project.json").read_text())
    t0, t1 = proj["trim"]
    assert 0.6 < t0 < 0.9 and 4.7 < t1 <= 5.0                                   # the marker frames are outside the range
    (box,) = [b for b in proj["privacy"] if b["source"] == "auto"]
    x, y, w, h = box["rect"]
    assert (x, y) == (SECRET[0] - 6, SECRET[1] - 6) and (w, h) == (SECRET[2] + 12, SECRET[3] + 12)
    assert isinstance(proj["segments"], list) and analysis.pending(pdir) is None      # planned without cairo
    out = Path(run(env, "export", str(pdir), "-q").stdout.strip().splitlines()[-1])
    assert abs(video(out)["duration"] - (t1 - t0)) < 0.2
    assert colours(pdir / "raw.mkv")[1] == "pc"
    fmt, rng = colours(out)
    assert fmt == "yuv420p" and rng in ("tv", "")              # limited range (said or implied), never "pc"
    fill = tuple(int(plain.FILL[i:i + 2], 16) for i in (2, 4, 6))
    for t in (0.0, 2.0, t1 - t0 - 0.1):
        inside = patch_of(out, t, SECRET[0] + 60, SECRET[1] + 60)
        assert all(max(abs(p[k] - fill[k]) for k in range(3)) <= 6 for p in inside), (t, inside[:3])
        outside = patch_of(out, t, 500, 40)
        assert all(max(abs(c - GREY) for c in p) <= 6 for p in outside), (t, outside[:3])
        corner = patch_of(out, t, 12, 12)                                        # where the marker was: none in the export
        assert all(max(abs(c - GREY) for c in p) <= 6 for p in corner), (t, corner[:3])


# ------------------------------------------------------------------ the pieces
# What the array version of the marker test (dolly/sync.py until 0.7.3, NumPy) answered for
# the 500 crops of _marker_crops(), one bit a crop, the first crop the highest bit. Worked
# out once with NumPy on the 0.7.4 base; the crops are made by the standard library, so
# they are the same on every machine.
ARRAY_VERSION_SAID = ("600488350c44a0050058f2b002004224a02020206d805549001110810baaa0051d4190a39ce04052220"
                      "04d820201498c00120000000000000000000000000")


def _marker_crops():
    """400 magenta squares with a green middle, in every fifth more of it covered by one
    random colour, then 100 crops of noise."""
    P, rnd, crops = sync.PROBE, random.Random(7), []
    for k in range(400):
        edge = (rnd.randrange(100, 256), rnd.randrange(0, 120), rnd.randrange(100, 256))        # magenta-ish, some not
        mid = (rnd.randrange(0, 120), rnd.randrange(100, 256), rnd.randrange(0, 120))
        cover = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        px = [mid if P // 2 - 1 <= x <= P // 2 and P // 2 - 1 <= y <= P // 2 else edge
              for y in range(P) for x in range(P)]
        px = [cover if rnd.random() < (k % 5) * 0.08 else p for p in px]                          # more and more of it covered
        crops.append(bytes(c for p in px for c in p))
    return crops + [rnd.randbytes(sync.FRAME_BYTES) for _ in range(100)]


def test_the_marker_test_gives_what_the_array_version_gave():
    P = sync.PROBE

    def by_the_rule(px):                         # the rule spelled out pixel by pixel: rows, then columns
        at = lambda y, x: tuple(px[(y * P + x) * 3:(y * P + x) * 3 + 3])  # noqa: E731
        edge = ([at(0, x) for x in range(P)] + [at(P - 1, x) for x in range(P)]
                + [at(y, 0) for y in range(P)] + [at(y, P - 1) for y in range(P)])
        mag = [r - g > 70 and b - g > 70 and abs(r - b) < 70 and r > 110 for r, g, b in edge]
        mid = [at(y, x) for y in (P // 2 - 1, P // 2) for x in (P // 2 - 1, P // 2)]
        grn = [g - max(r, b) > 70 and g > 110 for r, g, b in mid]
        return sum(mag) / len(mag) > 0.7 and sum(grn) / len(grn) > 0.7
    crops = _marker_crops()
    want = [by_the_rule(c) for c in crops]
    assert 60 < sum(want) < 440                                                  # both answers are well represented
    assert "%x" % int("".join("1" if w else "0" for w in want), 2) == ARRAY_VERSION_SAID
    assert [sync.is_marker(c) for c in crops] == want                            # bytes, as the decoder gives
    assert [sync.is_marker(bytearray(c)) for c in crops] == want
    joined = b"".join(crops)
    assert [sync.is_marker(v) for v in sync._split_frames(joined)] == want


def test_cover_filters():
    box = {"start": 2.0, "end": 4.0, "rect": [10.4, 20.6, 100, 50], "enabled": True, "mode": "blur"}
    (f,) = plain.cover_filters([box], 1.0, 6.0, 640, 400)
    assert f == f"drawbox=x=10:y=21:w=100:h=50:color={plain.FILL}:t=fill:enable='between(t,0.950,3.050)'"
    assert plain.cover_filters([{**box, "enabled": False}], 1.0, 6.0, 640, 400) == []
    assert plain.cover_filters([box], 4.5, 6.0, 640, 400) == []                 # over before the range starts
    assert plain.cover_filters([box], 0.0, 1.5, 640, 400) == []                 # starts after it ends
    (f,) = plain.cover_filters([{**box, "rect": [600, 380, 100, 50]}], 0.0, 6.0, 640, 400)
    assert "x=600:y=380:w=40:h=20" in f                                          # clipped to the picture
    assert plain.cover_filters([{**box, "rect": [700, 0, 50, 50]}], 0.0, 6.0, 640, 400) == []
    assert plain.cover_filters(None, 0.0, 6.0, 640, 400) == []
    for broken in ({"start": 1, "end": 2}, {"start": "x", "end": 2, "rect": [0, 0, 9, 9]},
                   {"start": 1, "end": 2, "rect": [0, 0, "9; rm", 9]}, "box"):
        with pytest.raises(RuntimeError, match="cannot be read"):                # never export past a box it cannot place
            plain.cover_filters([broken], 0.0, 6.0, 640, 400)


def test_only_what_recording_needs_holds_a_recording_back(monkeypatch):
    monkeypatch.setattr(gfx, "available", lambda: False)
    monkeypatch.setattr(deps.shutil, "which", lambda c: f"/usr/bin/{c}")
    m = deps.missing()
    assert m["packages"] == [CAIRO] and m["blocking"] == [] and deps.python_missing() == [CAIRO]
    monkeypatch.setattr(deps.shutil, "which", lambda c: None if c == "wf-recorder" else f"/usr/bin/{c}")
    monkeypatch.setattr(deps, "gsr_unusable", lambda: "unknown gpu vendor")
    m = deps.missing()
    assert m["packages"] == [CAIRO, "wf-recorder"] and m["blocking"] == ["wf-recorder"]
    monkeypatch.setattr(deps.shutil, "which", lambda c: None if c == "ffmpeg" else f"/usr/bin/{c}")
    assert deps.missing()["blocking"] == ["ffmpeg"]


class _Past(Exception):
    pass


def _start(monkeypatch, need):
    from dolly import project as projmod
    monkeypatch.delenv("DOLLY_BACKEND", raising=False)
    monkeypatch.delenv("DOLLY_SKIP_DEPS", raising=False)
    monkeypatch.setattr(deps, "missing", lambda capture=True: need)
    monkeypatch.setattr(projmod, "videos_root", lambda: (_ for _ in ()).throw(_Past()))
    sent = []
    monkeypatch.setattr(capture, "notify", lambda *a, **k: sent.append(a))
    return sent


def test_the_start_check_lets_a_recording_through_without_cairo(monkeypatch):
    sent = _start(monkeypatch, {"packages": [CAIRO], "why": {}, "blocking": []})
    with pytest.raises(_Past):
        capture.start(None)
    assert sent == []


def test_the_start_check_still_stops_for_a_missing_recorder(monkeypatch):
    sent = _start(monkeypatch, {"packages": [CAIRO, "wf-recorder"], "blocking": ["wf-recorder"],
                                "why": {"wf-recorder": "gpu-screen-recorder cannot capture here (unknown gpu vendor)"}})
    with pytest.raises(SystemExit) as e:
        capture.start(None)
    assert f"missing packages: wf-recorder (install it with {HOW})" in str(e.value)
    (card,) = sent
    assert card[1].index("wf-recorder") < card[1].index(CAIRO)                  # what holds it back comes first
    assert HOW in card[1]



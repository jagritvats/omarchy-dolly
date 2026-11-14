"""Stop sends its card when the file is safe; the zoom plan is worked out afterwards
(dolly.analysis). Nothing that uses the plan may see a half-analysed take, the plan is
the same as a synchronous finalize's, and a job that died is finished by whoever needs
the take next."""
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dolly import analysis, capture, gfx  # noqa: E402
from dolly import project as projmod  # noqa: E402

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                reason="needs ffmpeg + ffprobe")


def make_raw(pdir: Path, n=120, fps=30, W=640, H=400):
    """A click at 1 s, typing at 1.5-3.4 s, then idle (as tests/test_settings.py)."""
    pdir.mkdir(parents=True)

    def frame(i):
        img = gfx.new(W, H)
        gfx.fill_rect(img, 0, 0, W, H, gfx.rgb((30, 30, 30)))
        t = i / fps
        if t > 1.1:
            gfx.fill_rect(img, 150, 100, 300, 120, gfx.rgb((100, 90, 90)))
            gfx.fill_rect(img, 160, 110, int(min(280, max(0, (t - 1.5) * 140))), 8, gfx.rgb((230, 230, 230)))
        return img
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr0", "-s", f"{W}x{H}",
                          "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                          str(pdir / "raw.mkv")], stdin=subprocess.PIPE)
    for i in range(n):
        p.stdin.write(gfx.data(frame(i)))
    p.stdin.close()
    p.wait()
    from dolly.monitors import monitor_target
    tgt = monitor_target({"name": "DP-1", "width": W, "height": H, "scale": 1.0, "x": 0, "y": 0})
    (pdir / "capture.json").write_text(json.dumps({"backend": "gsr", "region": tgt.region, "scale": 1.0, "fps": fps,
                                                   "target_info": tgt.to_meta(), "mode": "screen", "started": 1.7e9}))
    ev = [{"ms": 0, "type": "info", "click_source": "lua"},
          {"ms": 900, "type": "move", "x": 290, "y": 150}, {"ms": 1000, "type": "click", "x": 300, "y": 160}]
    ev += [{"ms": 1500 + 100 * k, "type": "key", "key": "a", "mods": []} for k in range(20)]
    (pdir / "events.raw.jsonl").write_text("".join(json.dumps(e) + chr(10) for e in ev))
    return pdir


@pytest.fixture(scope="module")
def raw_take(tmp_path_factory):
    return make_raw(tmp_path_factory.mktemp("deferred") / "2026-11-11_10-00-00")


def copy_of(raw_take: Path, to: Path) -> Path:
    shutil.copytree(raw_take, to)
    return to


def project(pdir) -> dict:
    return json.loads((Path(pdir) / "project.json").read_text())


def test_the_fast_half_leaves_the_plan_pending(raw_take, tmp_path):
    pdir = copy_of(raw_take, tmp_path / "t")
    doc = capture.finalize(pdir, plan=False)
    assert doc["video"]["duration"] > 3 and (pdir / "events.json").exists()
    assert analysis.pending(pdir) is not None and not analysis.running(pdir)
    p = project(pdir)
    assert p["segments"] is None            # null = "plan it": never an empty plan someone could export
    assert "style" in p and "zoom_config" in p
    assert not (pdir / "aoi-activity-v9.json").exists()       # the slow analysis has not run


def test_the_deferred_plan_is_the_synchronous_plan(raw_take, tmp_path):
    a = copy_of(raw_take, tmp_path / "sync")
    b = copy_of(raw_take, tmp_path / "later")
    capture.finalize(a)                      # as before: everything at once
    capture.finalize(b, plan=False)
    assert analysis.analyse(b) is True
    pa, pb = project(a), project(b)
    assert pa["segments"] and pa["segments"] == pb["segments"]
    assert {k: v for k, v in pa.items() if k != "style"} == {k: v for k, v in pb.items() if k != "style"}
    assert json.loads((a / "events.json").read_text()) == json.loads((b / "events.json").read_text())
    assert analysis.pending(a) is None and analysis.pending(b) is None
    assert analysis.analyse(b) is False      # nothing left to do


def test_ensure_finishes_a_plan_nobody_is_working_on(raw_take, tmp_path):
    """The background job died (crash, logout): whoever needs the take does the work."""
    pdir = copy_of(raw_take, tmp_path / "t")
    capture.finalize(pdir, plan=False)
    said = []
    analysis.ensure(pdir, say=said.append)
    assert analysis.pending(pdir) is None and project(pdir)["segments"] and said
    analysis.ensure(pdir, say=lambda m: pytest.fail("nothing to wait for"))


def test_ensure_waits_for_the_job_that_is_running(raw_take, tmp_path):
    pdir = copy_of(raw_take, tmp_path / "t")
    capture.finalize(pdir, plan=False)
    started, order = threading.Event(), []

    def job():
        with analysis._locked(pdir):
            started.set()
            time.sleep(0.4)
            analysis.plan(pdir)
            order.append("job done")
    th = threading.Thread(target=job)
    th.start()
    assert started.wait(5)
    assert analysis.running(pdir)
    analysis.ensure(pdir)                    # blocks on the lock, then finds the work done
    order.append("ensure returned")
    th.join()
    assert order == ["job done", "ensure returned"] and analysis.pending(pdir) is None


def test_an_edit_made_while_the_plan_is_coming_is_kept(raw_take, tmp_path):
    from dolly.editor.server import Editor
    pdir = copy_of(raw_take, tmp_path / "t")
    capture.finalize(pdir, plan=False)
    ed = Editor(pdir)
    first = ed.project_payload()
    assert first["analysis"] == "pending" and first["segments"] == []
    assert ed.plan_payload() == {"analysis": "pending", "preview": "ready"}
    # the page saves what it holds: "no zooms", no speed-ups, and a trim the user just dragged
    ed.save({"segments": [], "speed": [], "trim": [0.5, 3.0]})
    assert project(pdir)["segments"] is None and project(pdir)["trim"] == [0.5, 3.0]
    analysis.analyse(pdir)
    got = ed.plan_payload()
    assert got["analysis"] == "ready" and got["segments"] and got["segments"] == project(pdir)["segments"]
    assert project(pdir)["trim"] == [0.5, 3.0]               # the edit survived the plan's arrival
    # from now on the page's own segments are saved again
    ed.save({"segments": got["segments"][:1]})
    assert len(project(pdir)["segments"]) == 1
    assert ed.project_payload()["analysis"] == "ready"


def test_the_preview_path_does_not_start_its_own_analysis(raw_take, tmp_path, monkeypatch):
    from dolly.editor import server
    pdir = copy_of(raw_take, tmp_path / "t")
    capture.finalize(pdir, plan=False)
    ed = server.Editor(pdir)
    monkeypatch.setattr(ed, "aoi", lambda *a, **k: pytest.fail("the editor analysed the screen itself"))
    path = ed.path_payload(30.0)
    assert path["geom"]["W"] == 640
    assert not (pdir / "aoi-activity-v9.json").exists()


def test_recover_finishes_interrupted_plans(raw_take, tmp_path, monkeypatch):
    root = tmp_path / "Videos"
    pdir = copy_of(raw_take, root / "2026-11-11_10-00-00")
    capture.finalize(pdir, plan=False)
    monkeypatch.setattr(projmod, "videos_root", lambda: root)
    assert analysis.unfinished(root) == [pdir]
    out = capture.recover_all()
    assert pdir in out and analysis.pending(pdir) is None and project(pdir)["segments"]
    assert analysis.unfinished(root) == []


def test_export_and_the_json_commands_wait_for_the_plan(monkeypatch, tmp_path):
    """They call analysis.ensure() before touching the take."""
    from dolly import headless
    calls = []
    monkeypatch.setattr(analysis, "ensure", lambda p, say=None: calls.append(Path(p)))
    pdir = tmp_path / "take"
    pdir.mkdir()
    (pdir / "events.json").write_text("{}")
    headless._wait_finalized(pdir, timeout=2)
    assert calls == [pdir]
    import inspect
    from dolly import cli, script, share
    for fn in (cli.cmd_export, cli.cmd_auto, cli.cmd_speed, cli.cmd_scrub, share.render_master,
               script.Runner.after_take):
        assert "analysis.ensure(" in inspect.getsource(fn), fn.__name__
    from dolly.editor import server
    for fn in (server.Editor.start_export, server.Editor.auto, server.Editor.speed_detect, server.Editor.scrub):
        assert "analysis.ensure(" in inspect.getsource(fn), fn.__name__


def test_the_json_commands_ask_for_the_plan_at_once():
    import inspect
    from dolly import headless
    src = inspect.getsource(headless)
    assert "capture.stop(None, defer=False)" in src           # record --for / --wait
    assert "capture.stop(a, defer=False)" in inspect.getsource(headless.stop_json)
    assert "capture.stop(None)" not in src and "capture.stop(a)" not in src


def test_the_background_job_is_a_detached_low_priority_process(monkeypatch, tmp_path):
    got = {}

    def popen(cmd, **kw):
        got["cmd"], got["kw"] = cmd, kw
    monkeypatch.setattr(analysis.subprocess, "Popen", popen)
    assert analysis.spawn(tmp_path) is True
    assert got["cmd"][1:] == ["-m", "dolly.analysis", str(tmp_path)] and got["kw"]["start_new_session"] is True
    assert str(Path(analysis.__file__).resolve().parents[1]) in got["kw"]["env"]["PYTHONPATH"]


def test_the_job_itself_finishes_the_take(raw_take, tmp_path, monkeypatch):
    monkeypatch.setattr(analysis.os, "nice", lambda n: 0)        # do not slow the rest of the test run
    pdir = copy_of(raw_take, tmp_path / "t")
    capture.finalize(pdir, plan=False)
    assert analysis.main([str(pdir)]) == 0
    assert analysis.pending(pdir) is None and project(pdir)["segments"]
    assert analysis.main([str(pdir)]) == 0   # again: nothing to do, still fine


def test_the_editor_does_not_wait_for_its_preview(raw_take, tmp_path):
    """The preview proxy is encoded in the background: the take is shown at once and the
    page is told when the video can be loaded. Never half a preview on disk."""
    from dolly.editor import server
    pdir = copy_of(raw_take, tmp_path / "t")
    capture.finalize(pdir)
    ed = server.Editor(pdir)
    assert not (pdir / "preview.mp4").exists()
    t = time.time()
    ed.start_preview()
    assert time.time() - t < 1.0                 # it only starts the work
    first = ed.project_payload()
    assert first["preview"] == "pending" or (pdir / "preview.mp4").exists()
    end = time.time() + 60
    while ed.preview_pending() and time.time() < end:
        assert not (pdir / "preview.mp4").exists() or not ed.preview_pending()
        time.sleep(0.05)
    assert (pdir / "preview.mp4").stat().st_size > 0 and not list(pdir.glob("preview.*.part.mp4"))
    assert ed.plan_payload()["preview"] == "ready" and ed.project_payload()["preview"] == "ready"
    ed.start_preview()                           # already there and newer than the recording: nothing to do
    assert ed.preview_pending() is False

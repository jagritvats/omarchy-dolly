"""The countdown is pre-roll: the recorder and the event logger run through it, so the take
starts the instant it ends ("go") and nothing pressed right after it is lost. Measured in
a VM on Nov 10: a 3 s countdown took 6.7 s and a key pressed in the gap was not in the take."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dolly import capture  # noqa: E402
from tests.test_system import dolly, env, status  # noqa: E402,F401  (stub hyprctl + recorder)


def _alive(pid) -> bool:
    from dolly.health import pid_alive
    return pid_alive(pid)


def _counting(env, countdown="3"):
    p = subprocess.Popen([sys.executable, "-m", "dolly.cli", "record", "--mode", "screen", "--countdown", countdown,
                          "--fps", "30"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    seen = None
    for _ in range(100):
        s = status(env)
        if s["phase"] == "countdown":
            seen = s
            break
        time.sleep(0.05)
    assert seen, "the countdown never showed"
    return p, seen


def test_countdown_is_recorded_and_the_take_starts_at_go(env):
    p, seen = _counting(env, "2")
    assert not seen["recording"] and seen["countdown"] in (1, 2)
    st = json.loads((Path(env["XDG_RUNTIME_DIR"]) / "dolly" / "state.json").read_text())
    assert st["preroll"] and _alive(st["recorder_pid"]) and _alive(st["logger_pid"])     # both already running
    assert dolly(env, "status", check=False).stdout.strip().startswith("countdown ")
    out, _err = p.communicate(timeout=30)
    assert p.returncode == 0
    pdir = Path(out.strip().splitlines()[-1])
    meta = json.loads((pdir / "capture.json").read_text())
    first = json.loads((pdir / "events.raw.jsonl").read_text().splitlines()[0])
    assert meta["countdown"] == 2 and first["type"] == "start"
    assert first["t0_mono"] < meta["go_mono"] - 1.0          # input was being logged through the countdown
    s = status(env)
    assert s["recording"] and s["phase"] == "recording" and s["elapsed"] < 2.0      # the clock starts at go
    time.sleep(1.5)
    dolly(env, "stop")
    ev = json.loads((pdir / "events.json").read_text())
    go = ev["capture"]["go"]
    assert go and go > 1.2 and ev["video"]["duration"] > go + 1.0    # the countdown is in the file...
    trim = json.loads((pdir / "project.json").read_text())["trim"]
    assert trim[0] == pytest.approx(go, abs=0.02)                     # ...and not in the clip


def test_no_countdown_no_go(env):
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "30")
    pdir = Path(r.stdout.strip().splitlines()[-1])
    time.sleep(1.5)
    dolly(env, "stop")
    assert json.loads((pdir / "events.json").read_text())["capture"]["go"] is None
    assert json.loads((pdir / "capture.json").read_text())["countdown"] == 0


@pytest.mark.parametrize("how", ["cancel", "toggle", "stop"])
def test_ending_the_countdown_leaves_nothing_behind(env, how):
    p, _seen = _counting(env)
    st = json.loads((Path(env["XDG_RUNTIME_DIR"]) / "dolly" / "state.json").read_text())
    r = dolly(env, how)
    assert "countdown cancelled" in r.stdout
    p.wait(timeout=15)
    for _ in range(40):
        if not _alive(st["recorder_pid"]) and not _alive(st["logger_pid"]):
            break
        time.sleep(0.1)
    assert not _alive(st["recorder_pid"]) and not _alive(st["logger_pid"])
    assert not (Path(env["XDG_RUNTIME_DIR"]) / "dolly" / "state.json").exists()
    s = status(env)
    assert s["phase"] == "idle" and not s["recording"]
    vids = Path(env["DOLLY_DIR"])
    assert not vids.exists() or not any(vids.iterdir())


def test_recorder_dying_in_the_countdown_is_reported(env):
    p, _seen = _counting(env)
    st = json.loads((Path(env["XDG_RUNTIME_DIR"]) / "dolly" / "state.json").read_text())
    os.kill(st["recorder_pid"], 9)
    p.wait(timeout=15)
    assert p.returncode == 1
    for _ in range(40):
        if not _alive(st["logger_pid"]):
            break
        time.sleep(0.1)
    assert not _alive(st["logger_pid"]) and status(env)["phase"] == "idle"
    left = [f.name for d in Path(env["DOLLY_DIR"]).iterdir() for f in d.iterdir()]
    assert left == ["recorder.log"]                       # nothing `dolly recover` would pick up


def _take(tmp_path, meta, lines, seconds=6):
    from dolly.render import ffmpeg_bin
    pdir = tmp_path / "2026-11-11_10-00-00"
    pdir.mkdir()
    subprocess.run([ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x200:rate=30",
                    "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    str(pdir / "raw.mkv")], check=True)
    (pdir / "capture.json").write_text(json.dumps({"backend": "gsr", "region": [0, 0, 320, 200], "scale": 1.0,
                                                   "mode": "screen", "fps": 30, **meta}))
    (pdir / "events.raw.jsonl").write_text("".join(json.dumps(e) + chr(10) for e in lines))
    return pdir


def test_input_during_the_countdown_is_not_part_of_the_take(tmp_path):
    lines = [{"type": "start", "ms": 0, "t0_mono": 1000.2},
             {"type": "key", "ms": 1500, "key": "Return", "mods": []},                  # during the countdown
             {"type": "click", "ms": 2000, "x": 50, "y": 50, "button": "left"},
             {"type": "move", "ms": 2100, "x": 60, "y": 60},
             {"type": "key", "ms": 3100, "key": "Tab", "mods": []},                     # 0.1 s after go
             {"type": "click", "ms": 4000, "x": 80, "y": 80, "button": "left"}]
    pdir = _take(tmp_path, {"countdown": 3, "clock_mono": 1000.0, "go_mono": 1003.2}, lines)
    doc = capture.finalize(pdir, aoi=False)
    go = doc["capture"]["go"]
    assert go == pytest.approx(3.04, abs=0.01)                # clock fit: 0.04 + (1003.2 - 1000.2)
    ev = doc["events"]
    assert [e["key"] for e in ev if e["type"] == "key"] == ["Tab"]
    assert [round(e["t"], 1) for e in ev if e["type"] == "click"] == [4.0]
    assert any(e["type"] == "move" and e["t"] < go for e in ev)                         # the pointer path is kept
    proj = json.loads((pdir / "project.json").read_text())
    assert proj["trim"][0] == pytest.approx(go, abs=0.001)
    assert all(s["start"] >= go - 1e-6 for s in proj["segments"])


def test_no_zoom_reaches_back_into_the_countdown():
    from dolly.zoom import Segment
    segs = [Segment(0.5, 2.0, 1.8), Segment(2.5, 5.0, 1.8), Segment(6.0, 8.0, 2.0)]
    out = capture._from_go(segs, 3.0)
    assert [(s.start, s.end) for s in out] == [(3.0, 5.0), (6.0, 8.0)]
    assert capture._from_go(segs, None) is segs


class FakeRecorder:
    def __init__(self, dies_after=None):
        self.t0, self.dies_after = time.monotonic(), dies_after

    def poll(self):
        return 1 if self.dies_after is not None and time.monotonic() - self.t0 >= self.dies_after else None


def _timed(fn):
    t = time.monotonic()
    return fn(), time.monotonic() - t


def test_recorder_is_up_when_its_file_has_bytes(tmp_path):
    raw, log = tmp_path / "raw.mkv", tmp_path / "recorder.log"
    raw.write_bytes(b"x" * 64)
    log.write_text("")
    ok, took = _timed(lambda: capture._recorder_up(FakeRecorder(), raw, log, 0, "gsr"))
    assert ok and took < 0.2


def test_wf_recorder_on_a_still_screen_does_not_cost_three_seconds(tmp_path):
    """It writes nothing until the screen changes; having picked its region it is listening."""
    raw, log = tmp_path / "raw.mkv", tmp_path / "recorder.log"
    raw.write_bytes(b"")
    old = "=== earlier attempt" + chr(10) + "selected region 0,0 1x1" + chr(10)
    log.write_text(old + "$ wf-recorder -o X" + chr(10) + "selected region 0,0 1920x1200" + chr(10))
    ok, took = _timed(lambda: capture._recorder_up(FakeRecorder(), raw, log, len(old), "wf"))
    assert ok and 0.25 <= took < 1.0
    log.write_text(old + "$ wf-recorder -o X" + chr(10))             # this launch has said nothing yet
    ok, took = _timed(lambda: capture._recorder_up(FakeRecorder(), raw, log, len(old), "wf", limit=0.6))
    assert ok and took >= 0.6                                         # alive, so given the benefit of the doubt


def test_other_recorders_still_have_to_write(tmp_path):
    raw, log = tmp_path / "raw.mkv", tmp_path / "recorder.log"
    log.write_text("selected region 0,0 1920x1200" + chr(10))
    ok, took = _timed(lambda: capture._recorder_up(FakeRecorder(), raw, log, 0, "gsr", limit=0.5))
    assert ok and took >= 0.5


def test_a_dead_recorder_is_not_up(tmp_path):
    raw, log = tmp_path / "raw.mkv", tmp_path / "recorder.log"
    log.write_text("")
    ok, took = _timed(lambda: capture._recorder_up(FakeRecorder(dies_after=0.1), raw, log, 0, "wf"))
    assert not ok and took < 0.5


def test_known_unusable_gpu_recorder_is_not_tried(monkeypatch):
    monkeypatch.delenv("DOLLY_BACKEND", raising=False)
    monkeypatch.setattr(capture, "_have", lambda c: True)
    monkeypatch.setattr(capture, "_gsr_known_unusable", lambda: True)
    assert capture.backend_order("auto") == ["wf"]
    monkeypatch.setattr(capture, "_gsr_known_unusable", lambda: False)
    assert capture.backend_order("auto") == ["gsr", "gsr-cpu", "wf"]
    monkeypatch.setattr(capture, "_have", lambda c: c == "gpu-screen-recorder")
    monkeypatch.setattr(capture, "_gsr_known_unusable", lambda: True)
    assert capture.backend_order("auto") == ["gsr", "gsr-cpu"]        # nothing to fall back to: still try
    assert capture.backend_order("gsr") == ["gsr"]                     # an explicit choice is kept


def test_the_note_is_only_read_never_probed(monkeypatch, tmp_path):
    from dolly import deps
    gsr = tmp_path / "gpu-screen-recorder"
    gsr.write_text("#!/bin/sh" + chr(10) + "exit 1" + chr(10))
    gsr.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(deps.subprocess, "run", lambda *a, **k: pytest.fail("a recording start must not probe"))
    assert capture._gsr_known_unusable() is False                      # no note yet
    assert deps.note_gsr_failure("gsr error: unknown gpu vendor: VMware, Inc.")
    assert capture._gsr_known_unusable() is True
    gsr.write_text("#!/bin/sh" + chr(10) + "exit 0" + chr(10))        # reinstalled or updated: ask again
    os.utime(gsr, (1, 1))
    assert capture._gsr_known_unusable() is False


def test_countdown_lasts_n_seconds(monkeypatch):
    from dolly import health
    phases, called = [], []
    monkeypatch.setattr(capture, "notify", lambda *a, **k: time.sleep(0.08))      # a slow notification
    monkeypatch.setattr(health, "set_phase", lambda p, **k: phases.append((p, k.get("left"))))
    t = time.monotonic()
    capture.countdown(2, alive=lambda: True, last_call=lambda: called.append(time.monotonic() - t))
    took = time.monotonic() - t
    assert 1.95 <= took < 2.3 and phases == [("countdown", 2), ("countdown", 1)]
    assert len(called) == 1 and 1.4 <= called[0] <= 1.7              # half a second before the end


def test_countdown_cards_never_outlive_it(monkeypatch):
    """The shell keeps a card longer than it is asked to; each one is taken down by name."""
    from dolly import health
    shown, gone = [], []
    monkeypatch.setattr(capture, "notify", lambda title, *a, **k: shown.append(title))
    monkeypatch.setattr(capture, "_dismiss_toast", lambda s: gone.append((s, time.monotonic())) if s else None)
    monkeypatch.setattr(health, "set_phase", lambda p, **k: None)
    capture.countdown(2)
    end = time.monotonic()
    assert shown == [capture.t("notify.countdown", 2), capture.t("notify.countdown", 1)]
    assert [g[0] for g in gone] == shown and gone[-1][1] <= end - 0.25       # the last one before "go"


def test_countdown_stops_when_the_recorder_dies(monkeypatch):
    from dolly import health
    monkeypatch.setattr(capture, "notify", lambda *a, **k: None)
    monkeypatch.setattr(health, "set_phase", lambda p, **k: None)
    t = time.monotonic()
    with pytest.raises(capture.RecorderGone):
        capture.countdown(3, alive=lambda: time.monotonic() - t < 0.3)
    assert time.monotonic() - t < 1.0

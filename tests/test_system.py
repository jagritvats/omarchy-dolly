"""System robustness with stubbed desktop tools (round 3).

A working gpu-screen-recorder stub (ffmpeg testsrc -> raw.mkv) and a hyprctl
stub with monitors/clients. Covers: phases + status, crash-safe MKV,
time-to-first-frame logging, the watchdog stopping cleanly when the recorder
dies, the disk-space guard, `dolly recover` for an interrupted recording,
cancelling during the countdown with the same hotkey, and sync-marker
detection with clock drift.
"""
import json
import math
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.desktop_stubs import GSR, HYPRCTL  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("hyprctl", HYPRCTL), ("gpu-screen-recorder", GSR)):
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    e = dict(os.environ)
    e.update({"PATH": f"{bindir}:{e['PATH']}", "DOLLY_DIR": str(tmp_path / "videos"),
              "XDG_RUNTIME_DIR": str(tmp_path / "run"), "DOLLY_OPEN_EDITOR": "0", "PYTHONPATH": str(ROOT),
              "HOME": str(tmp_path), "DOLLY_BACKEND": "gsr"})
    e.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
    (tmp_path / "run").mkdir()
    return e


def dolly(env, *args, check=True, timeout=120):
    return subprocess.run([sys.executable, "-m", "dolly.cli", *args], env=env, capture_output=True, text=True,
                          check=check, timeout=timeout)


def status(env):
    return json.loads(dolly(env, "status", "--json", check=False).stdout)


def test_record_phases_mkv_ttff_and_clean_stop(env):
    assert status(env)["phase"] == "idle"
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "30")
    pdir = Path(r.stdout.strip().splitlines()[-1])
    s = status(env)
    assert s["recording"] and s["phase"] == "recording"
    time.sleep(2.5)
    dolly(env, "stop")
    assert status(env)["phase"] == "idle"
    assert (pdir / "raw.mkv").exists() and not (pdir / "raw.mp4").exists()     # crash-safe container
    meta = json.loads((pdir / "capture.json").read_text())
    assert meta["container"] == "mkv" and meta["ttff_ms"] < 3000 and meta["recorder_pid"] > 0
    ev = json.loads((pdir / "events.json").read_text())
    assert ev["video"]["duration"] > 1.5
    assert any(e["type"] == "clients" for e in ev["events"])                   # window state for the AOI
    assert ev["capture"]["cursor_size"] == 24 and ev["capture"]["sync"]["method"] in ("none", "clock")   # stub draws no marker


def test_watchdog_stops_cleanly_when_recorder_dies(env):
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "30")
    pdir = Path(r.stdout.strip().splitlines()[-1])
    time.sleep(2.0)
    pid = json.loads((pdir / "capture.json").read_text())["recorder_pid"]
    os.kill(pid, signal.SIGKILL)                     # e.g. GPU reset, OOM
    for _ in range(60):
        if (pdir / "events.json").exists() and status(env)["phase"] == "idle":
            break
        time.sleep(0.25)
    meta = json.loads((pdir / "capture.json").read_text())
    assert meta.get("stop_reason") == "recorder-died"
    ev = json.loads((pdir / "events.json").read_text())
    assert ev["video"]["duration"] > 1.0                                        # partial MKV is usable
    raw = [json.loads(l) for l in (pdir / "events.raw.jsonl").read_text().splitlines() if l.strip()]
    assert any(e["type"] == "warn" and e.get("reason") == "recorder-died" for e in raw)


def test_stop_from_outside_is_a_clean_stop(env):
    """Omarchy's screen-recording key SIGINTs every recorder when it has no pid file of
    its own: the recorder closes its file, Dolly finalizes it as a normal recording."""
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "30")
    pdir = Path(r.stdout.strip().splitlines()[-1])
    time.sleep(2.0)
    pid = json.loads((pdir / "capture.json").read_text())["recorder_pid"]
    os.kill(pid, signal.SIGINT)
    for _ in range(60):
        if (pdir / "events.json").exists() and status(env)["phase"] == "idle":
            break
        time.sleep(0.25)
    assert json.loads((pdir / "capture.json").read_text()).get("stop_reason") == "stopped-outside"
    # a normal recording: not partial / recovered in the library, nothing for `dolly recover`
    lib = json.loads(dolly(env, "library", "--json").stdout)
    e = next(x for x in lib["items"] if x["dir"] == str(pdir))
    assert not e["partial"] and not e["recovered"]
    assert "recovered" not in dolly(env, "recover").stdout


def test_refuses_to_start_next_to_another_recorder(env, tmp_path):
    import shutil
    other = tmp_path / "other"
    other.mkdir()
    fake = other / "gpu-screen-recorder"           # e.g. Omarchy's own recording (Alt+Print)
    shutil.copy(shutil.which("sleep"), fake)
    p = subprocess.Popen([str(fake), "30"])
    try:
        r = dolly(env, "record", "--mode", "screen", "--countdown", "0", check=False)
        assert r.returncode != 0 and "already recording" in r.stderr
        assert not Path(env["DOLLY_DIR"]).exists() or not any(Path(env["DOLLY_DIR"]).iterdir())
        r = dolly(env, "replay", "start", check=False)
        assert r.returncode != 0 and "already recording" in r.stderr
    finally:
        p.kill()
        p.wait()
    # Omarchy still starting (its picker is open): also refused
    (Path(env["XDG_RUNTIME_DIR"]) / "omarchy-screenrecord-starting").write_text(str(os.getpid()))
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", check=False)
    assert r.returncode != 0 and "Omarchy" in r.stderr


def test_disk_guard_refuses_to_start(env):
    env = dict(env, DOLLY_MIN_FREE_GB="100000")
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", check=False)
    assert r.returncode != 0 and "dolly doctor" in (r.stderr + r.stdout)
    assert not Path(env["DOLLY_DIR"]).exists() or not any(Path(env["DOLLY_DIR"]).iterdir())


def test_recover_interrupted_recording(env, tmp_path):
    from dolly.render import ffmpeg_bin
    pdir = Path(env["DOLLY_DIR"]) / "2026-11-09_10-00-00"
    pdir.mkdir(parents=True)
    # a recorder killed mid-write: Matroska without its cues/duration
    p = subprocess.Popen([ffmpeg_bin(), "-v", "error", "-y", "-re", "-f", "lavfi", "-i", "testsrc2=size=640x400:rate=30",
                          "-c:v", "libx264", "-preset", "ultrafast", "-g", "15", "-f", "matroska", str(pdir / "raw.mkv")])
    time.sleep(2.5)
    p.kill()
    p.wait()
    (pdir / "capture.json").write_text(json.dumps({"backend": "gsr", "region": [0, 0, 640, 400], "scale": 1.0,
                                                   "mode": "screen", "fps": 30}))
    (pdir / "events.raw.jsonl").write_text(json.dumps({"type": "move", "ms": 100, "x": 100, "y": 100}) + "\n"
                                           + '{"type": "click", "ms": 900, "x": 120')          # torn last line
    run = Path(env["XDG_RUNTIME_DIR"]) / "dolly"
    run.mkdir(parents=True, exist_ok=True)
    (run / "state.json").write_text(json.dumps({"dir": str(pdir), "recorder_pid": 999999, "logger_pid": 999998}))
    out = dolly(env, "recover")
    ev = json.loads((pdir / "events.json").read_text())
    assert ev["video"]["duration"] > 1.0 and ev["video"]["w"] == 640
    assert json.loads((pdir / "capture.json").read_text())["stop_reason"] == "recovered"
    assert not (run / "state.json").exists()


def test_same_hotkey_cancels_countdown(env):
    p = subprocess.Popen([sys.executable, "-m", "dolly.cli", "record", "--mode", "screen", "--countdown", "3"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(40):
        if status(env)["phase"] == "countdown":
            break
        time.sleep(0.1)
    assert status(env)["phase"] == "countdown" and status(env)["countdown"] in (1, 2, 3)
    dolly(env, "toggle")                       # Super+Alt+R again
    p.wait(timeout=10)
    assert status(env)["phase"] == "idle" and not status(env)["recording"]
    vids = Path(env["DOLLY_DIR"])
    assert not vids.exists() or not any(vids.iterdir())          # nothing half-made left behind


def test_sync_markers_offset_and_drift():
    from dolly import sync
    fps = 60.0
    n = int(120 * fps)
    a_true, b_true = -0.27, 1 / 1.003                     # logger clock: 0.27 s late, 0.3 % fast
    log_marks = [0.6, 118.0]
    frames = [bytes(sync.FRAME_BYTES)] * n                 # black, as the decoder gives them: rows of r, g, b
    marker = bytearray(bytes((255, 0, 255)) * (sync.PROBE * sync.PROBE))
    for y in (3, 4):
        marker[(y * sync.PROBE + 3) * 3:(y * sync.PROBE + 5) * 3] = bytes((0, 255, 0)) * 2
    shown = []
    for lm in log_marks:
        tv = a_true + b_true * lm                          # when the overlay really appeared
        i0 = math.ceil(tv * fps)
        shown.append(tv)
        for i in range(i0, i0 + 2):
            frames[i] = bytes(marker)
    runs = sync.detect_markers(None, 1.0, fps, frames=frames)
    assert len(runs) == 2
    fit = sync.fit(log_marks, runs, fps)
    assert fit.method == "markers2"
    for t_log in (118 * i / 49 for i in range(50)):
        assert abs(fit.map(t_log) - (a_true + b_true * t_log)) < 1.0 / fps    # < 1 frame everywhere
    trim = sync.marker_trim(runs, fps, n / fps)
    assert trim[0] > runs[0][1] and trim[1] < runs[1][0]                    # markers trimmed out


def test_glassy_solid_windows_restored_on_stop_and_after_crash(env, tmp_path):
    tog = tmp_path / ".local/state/omarchy/toggles/hypr"
    tog.mkdir(parents=True)
    (tog / "window-glassy.lua").write_text("-- glassy on")
    ledger = Path(env["XDG_RUNTIME_DIR"]) / "dolly" / "solid.json"
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "30")
    pdir = Path(r.stdout.strip().splitlines()[-1])
    assert json.loads((pdir / "capture.json").read_text())["solid_windows"] is True
    assert json.loads(ledger.read_text()) == {"0xa1": "false"}       # forced opaque while recording
    time.sleep(1.0)
    dolly(env, "stop")
    assert not ledger.exists()                                        # put back on stop
    assert (tog / "window-glassy.lua").exists()                       # the user's mode never changed
    ledger.write_text(json.dumps({"0xa1": "false"}))                  # a recording that crashed mid-way
    dolly(env, "recover")
    assert not ledger.exists()
    dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "30", "--solid", "off")
    assert not ledger.exists()                                        # setting off: windows untouched
    dolly(env, "cancel")


def test_finished_job_is_not_resurrected_by_a_late_progress(tmp_path, monkeypatch):
    """The bar showed 'rendering 100 %' forever: export reported its final progress after
    job.done() and rewrote the job file."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    from dolly import health
    j = health.Job("exporting", "p", "out.mp4")
    assert health.jobs()
    j.done()
    j.update(1.0, force=True)
    assert health.jobs() == []

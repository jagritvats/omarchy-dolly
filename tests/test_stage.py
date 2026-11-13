"""The hidden stage (beta): a second Hyprland with its own pointer and keyboard.

Everything here runs against fakes: a fake `Hyprland` that only makes the instance folder a
real one makes, a fake `hyprctl`, `systemd-run`, `systemctl`, `pactl` and `grim`. The real
thing is exercised by hand in a VM (see the README's "Hidden stage (beta)").
"""
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dolly import capture, headless, health, hypr, logger, stage, sync  # noqa: E402

# what a stage test must leave as it found it (test_and_the_next_test_starts_on_the_real_desktop)
ENV_BEFORE = {k: os.environ.get(k) for k in ("DOLLY_STAGE", "HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY", "DISPLAY",
                                             "PULSE_SINK", "PIPEWIRE_NODE", "DOLLY_OPEN_EDITOR")}

FAKE_HYPRLAND = """#!/bin/bash
# a second Hyprland, as far as Dolly can tell: an instance folder, a lock file, a socket
if [ "$1" = "--verify-config" ]; then echo "config ok"; exit 0; fi
sig="fake_$$"
d="$XDG_RUNTIME_DIR/hypr/$sig"
mkdir -p "$d"
if [ "$FAKE_HYPR_FAIL" = "1" ]; then exit 1; fi          # dies before it writes its lock
n=2; while [ -e "$XDG_RUNTIME_DIR/wayland-$n" ]; do n=$((n+1)); done
touch "$XDG_RUNTIME_DIR/wayland-$n" "$XDG_RUNTIME_DIR/wayland-$n.lock" "$d/.socket.sock"
printf '%s\\nwayland-%s\\n' "$$" "$n" > "$d/hyprland.lock"
trap 'exit 0' TERM
while :; do sleep 0.2; done
"""

FAKE_HYPRCTL = """#!/bin/bash
sig=""; json=0
while [ $# -gt 0 ]; do
  case "$1" in
    --instance) sig="$2"; shift 2 ;;
    -j) json=1; shift ;;
    *) break ;;
  esac
done
[ -z "$sig" ] && sig="$HYPRLAND_INSTANCE_SIGNATURE"
d="$XDG_RUNTIME_DIR/hypr/$sig"
[ -d "$d" ] || { echo "no such instance"; exit 1; }
echo "$*" >> "$XDG_RUNTIME_DIR/hyprctl-calls.log"
case "$1" in
  version) echo "Hyprland fake" ;;
  output) touch "$d/output-$4"; echo ok ;;
  monitors) if [ -e "$d/output-STAGE" ]; then echo '[{"name": "STAGE", "width": '"${FAKE_W:-1920}"', "height": '"${FAKE_H:-1080}"', "x": 0, "y": 0, "scale": 1, "focused": true}]'; else echo '[]'; fi ;;
  clients) echo '[]' ;;
  cursorpos) echo '{"x": 400, "y": 300}' ;;
  layers) if [ -e "$d/layers" ]; then cat "$d/layers"; else echo '{}'; fi ;;
  dispatch)
    case "$2" in
      *"exit()"*) kill "$(sed -n 1p "$d/hyprland.lock")" 2>/dev/null ;;
      *quickshell*) [ "$FAKE_NO_LAYER" = "1" ] || echo '{"STAGE": {"levels": {"0": [{"namespace": "dolly-stage-wallpaper"}]}}}' > "$d/layers" ;;
    esac
    echo ok ;;
  *) echo ok ;;
esac
"""

FAKE_SYSTEMD_RUN = """#!/bin/bash
# the memory limit is systemd's business; the fake only runs the command
while [ $# -gt 0 ]; do
  case "$1" in
    -p) echo "$2" >> "$XDG_RUNTIME_DIR/limits.log"; shift 2 ;;
    --*) shift ;;
    *) break ;;
  esac
done
exec "$@"
"""

FAKE_PACTL = """#!/bin/bash
f="$XDG_RUNTIME_DIR/pactl-modules"
case "$1" in
  load-module) echo "41\tmodule-null-sink\t$3" >> "$f"; echo 41 ;;
  unload-module) : > "$f" ;;
  list) cat "$f" 2>/dev/null ;;
esac
"""

FAKE_GRIM = """#!/bin/bash
out="${@: -1}"
if [ "$out" = "-" ]; then      # a small look at the stage: one colour all over (a picture's, unless told otherwise)
  exec python3 -c "
import os, sys
rgb = bytes(int(v) for v in os.environ.get('FAKE_GRIM_RGB', '200,60,90').split(','))
sys.stdout.buffer.write(b'P6\\n192 108\\n255\\n' + rgb * (192 * 108))"
fi
exec python3 -c "
from PIL import Image
import os, sys
Image.new('RGB', (int(os.environ.get('FAKE_W', 1920)), int(os.environ.get('FAKE_H', 1080))), (26, 27, 38)).save(sys.argv[1])" "$out"
"""

FAKE_WF_RECORDER = """#!/usr/bin/env python3
# Stands in for wf-recorder: two seconds of "stage" in which the pointer is in the corner for
# a moment, then it waits to be stopped. Python, not a shell script: a shell cannot trap a
# signal that was ignored when it started, and the stop signal is ignored in everything a
# background job starts (the whole suite run with nohup ... &): the stand-in never stopped
# there, where the real recorder, which sets its own handler, does.
import os, signal, subprocess, sys, time
if os.environ.get("FAKE_WF_BROKEN") == "1":
    sys.stderr.write("no screencopy here\\n")
    sys.exit(1)
args = sys.argv[1:]
out = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "-f"), None)
if not out:
    sys.exit(0)
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda *_: os._exit(0))
box = "drawbox=x=0:y=0:w=12:h=12:color=white:t=fill:enable='between(t,0.8,1.1)'"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1e1e28:s=320x180:r=20:d=1.8",
                "-vf", box, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out])
while True:
    time.sleep(0.1)
"""

FAKE_WTYPE = """#!/bin/bash
echo "$*" >> "$XDG_RUNTIME_DIR/wtype.log"
"""


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """A runtime dir with one real session in it, and fakes of every program a stage starts."""
    rt = tmp_path / "run"
    real = rt / "hypr" / "realsession"
    real.mkdir(parents=True)
    (real / ".socket.sock").touch()
    (real / "hyprland.lock").write_text(f"{os.getpid()}\nwayland-1\n")
    (rt / "wayland-1").touch()
    (rt / "wayland-1.lock").touch()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, text in (("Hyprland", FAKE_HYPRLAND), ("hyprctl", FAKE_HYPRCTL), ("systemd-run", FAKE_SYSTEMD_RUN),
                       ("pactl", FAKE_PACTL), ("grim", FAKE_GRIM), ("systemctl", "#!/bin/bash\nexit 0\n"),
                       ("wf-recorder", FAKE_WF_RECORDER), ("wtype", FAKE_WTYPE),
                       ("quickshell", "#!/bin/bash\nexit 0\n")):
        p = bin_ / name
        p.write_text(text)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setenv("OMARCHY_PATH", str(tmp_path / "no-omarchy"))
    # the theme's colour unless a test asks for the desktop picture
    monkeypatch.setenv("DOLLY_STAGE_BACKGROUND", "color")
    for k in ("DOLLY_STAGE", "HYPRLAND_INSTANCE_SIGNATURE", "DISPLAY", "PULSE_SINK", "PIPEWIRE_NODE", "DOLLY_OPEN_EDITOR",
              "FAKE_HYPR_FAIL", "FAKE_NO_LAYER", "FAKE_GRIM_RGB", "FAKE_WF_BROKEN", "DOLLY_REPORT_STAGE"):
        # Set, then removed: a variable that was not there is otherwise not put back, and what
        # stage.enter() sets (DOLLY_STAGE=1 ...) would stay for every test that runs after
        # this file. It did: 13 tests of the real desktop then ran "on a stage" and failed.
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    # tests must never touch a stage that is really running on this machine
    monkeypatch.setattr(stage, "MARK", f"DOLLY_STAGE_TEST_{os.getpid()}")
    yield rt
    for i in stage.instances(rt):
        if i["stage"] and i["alive"]:
            try:
                os.kill(i["pid"], signal.SIGKILL)
            except OSError:
                pass


def sigs(rt):
    return sorted(p.name for p in (rt / "hypr").iterdir())


# ------------------------------------------------------------------ start, use, stop
def test_start_gives_an_invisible_screen_and_stop_leaves_nothing(desk):
    inf = stage.start("1280x720")
    assert inf["size"] == [1920, 1080] or inf["size"] == [1280, 720]     # what the compositor reports wins
    assert inf["wl"] == "wayland-2" and inf["real"] == {"sig": "realsession", "wl": "wayland-1"}
    assert stage.info()["sig"] == inf["sig"] and stage.state_file().exists()
    conf = (stage.base_dir() / "hyprland.lua").read_text()
    assert 'mode = "1280x720@60"' in conf and f'output = "{stage.NESTED_OUTPUT}", disabled = true' in conf
    calls = (desk / "hyprctl-calls.log").read_text()
    assert "output create headless STAGE" in calls
    limits = (desk / "limits.log").read_text().split()
    assert "MemoryMax=1500M" in limits and "MemorySwapMax=0" in limits
    st = stage.status()
    assert st["running"] and st["env"]["WAYLAND_DISPLAY"] == "wayland-2" and st["env"]["DOLLY_STAGE"] == "1"
    assert st["sound"] == stage.SINK and st["env"]["PULSE_SINK"] == stage.SINK
    # the session Dolly would pick by itself is still the real one
    assert stage.real_session()["sig"] == "realsession"
    r = stage.stop()
    assert r["was_running"] and stage.info() is None and not stage.state_file().exists()
    assert sigs(desk) == ["realsession"]
    assert not (desk / "wayland-2").exists() and (desk / "wayland-1").exists()
    assert (desk / "pactl-modules").read_text() == "" and stage.sweep_report() == []


def test_a_second_start_is_refused_and_another_size_needs_a_stop(desk):
    stage.start()
    with pytest.raises(stage.StageError, match="already running"):
        stage.start()
    with pytest.raises(stage.StageError, match="stop it first"):
        stage.ensure("1080x1920")
    inf, started = stage.ensure()
    assert not started and inf["sig"] == stage.info()["sig"]
    stage.stop()


def test_a_killed_stage_is_cleaned_up_by_the_next_start(desk):
    inf = stage.start()
    os.kill(inf["pid"], signal.SIGKILL)
    for _ in range(40):
        if not stage._alive(inf["pid"]):
            break
        time.sleep(0.05)
    assert stage.info() is None
    assert any("dead stage" in s for s in stage.sweep_report())
    again = stage.start()                       # sweeps the dead one first
    assert again["sig"] != inf["sig"] and inf["sig"] not in sigs(desk)
    stage.stop()
    assert sigs(desk) == ["realsession"] and stage.sweep_report() == []


def test_a_start_that_fails_leaves_no_folder_behind(desk, monkeypatch):
    monkeypatch.setenv("FAKE_HYPR_FAIL", "1")
    with pytest.raises(stage.StageError, match="did not start"):
        stage.start()
    assert sigs(desk) == ["realsession"] and stage.info() is None
    assert (desk / "pactl-modules").read_text() == ""


def test_stage_needs_the_real_session(desk):
    import shutil
    shutil.rmtree(desk / "hypr" / "realsession")
    with pytest.raises(stage.StageError, match="desktop session"):
        stage.start()


def quiet_sound(monkeypatch, result=(True, "heard")):
    """The self test's sound step without a sound server."""
    monkeypatch.setattr(stage, "_sound_begin", lambda inf: None)
    monkeypatch.setattr(stage, "_sound_end", lambda snd, inf: result)


def test_doctor_check_says_what_worked_step_by_step(desk, monkeypatch):
    quiet_sound(monkeypatch)
    res = stage.selftest()
    assert res["ok"] and res["started"] and res["frame"] == [1920, 1080], res
    assert [s["name"] for s in res["steps"]] == ["start", "frame", "recording", "sync mark", "sound", "stop"]
    assert all(s["ok"] is True for s in res["steps"]), res["steps"]
    assert res["recording"]["w"] == 320 and res["mark"]["found"] == 1
    assert stage.info() is None and sigs(desk) == ["realsession"]
    assert not list(stage.base_dir().glob("selftest*"))
    text = "\n".join(stage.selftest_lines(res))
    assert "ok      start: 1920x1080" in text and "sync mark: found in the recording" in text
    assert text.splitlines()[-1].startswith("hidden stage: ok")
    assert "memory limit 1500 MB" in text and "the theme's colour" in text


def test_doctor_check_names_the_step_that_failed(desk, monkeypatch):
    quiet_sound(monkeypatch, (False, "a program on the stage was NOT heard on the stage's private output"))
    monkeypatch.setenv("FAKE_WF_BROKEN", "1")
    res = stage.selftest()
    by = {s["name"]: s for s in res["steps"]}
    assert not res["ok"] and by["start"]["ok"] is True and by["frame"]["ok"] is True
    assert by["recording"]["ok"] is False and "no screencopy here" in by["recording"]["detail"]
    assert by["sync mark"]["ok"] is None                      # nothing to look in: not tried, not "failed"
    assert by["sound"]["ok"] is False and by["stop"]["ok"] is True
    assert len(res["problems"]) == 2 and stage.info() is None and sigs(desk) == ["realsession"]
    lines = stage.selftest_lines(res)
    assert any(l.startswith("  FAILED  recording:") for l in lines)
    assert any(l.startswith("  skipped sync mark:") for l in lines)
    assert lines[-1].startswith("hidden stage: NOT working")


def test_doctor_check_on_a_running_stage_leaves_it_running(desk, monkeypatch):
    quiet_sound(monkeypatch)
    inf = stage.start()
    res = stage.selftest()
    assert res["ok"] and not res["started"] and res["steps"][-1]["ok"] is None
    assert stage.info()["sig"] == inf["sig"]
    stage.stop()


def test_the_report_has_a_stage_section(desk, monkeypatch):
    from dolly import report
    quiet_sound(monkeypatch)
    plain = "\n".join(report._stage())
    assert "self test: not run" in plain and "settings: " in plain and '"running": false' in plain
    full = "\n".join(report._stage(selftest=True))
    assert "sync mark: found in the recording" in full and "hidden stage: ok" in full
    assert "-- stage.log (" in full and "=== " in full            # each start of a stage is in it
    monkeypatch.setenv("DOLLY_REPORT_STAGE", "0")
    assert "self test: not run" in "\n".join(report._stage(selftest=True))
    assert stage.info() is None


def tone(*hz, seconds=2.0, rate=16000):
    if not hz:
        return b"\0" * int(rate * seconds) * 2
    t = np.arange(int(rate * seconds)) / rate
    return sum(3000 * np.sin(2 * np.pi * f * t) for f in hz).astype(np.int16).tobytes()


def test_tones_are_told_apart():
    assert stage.tones_in(tone(660, 1000), 16000, stage.SELFTEST_TONES) == {"PulseAudio": True, "PipeWire": True}
    assert stage.tones_in(tone(660), 16000, stage.SELFTEST_TONES) == {"PulseAudio": True, "PipeWire": False}
    assert stage.tones_in(tone(), 16000, stage.SELFTEST_TONES) == {"PulseAudio": False, "PipeWire": False}
    assert stage.tones_in(b"\1\0" * 100, 16000, stage.SELFTEST_TONES) == {"PulseAudio": False, "PipeWire": False}


def test_a_tone_that_starts_and_stops_is_not_taken_for_its_neighbour():
    """Seen on 11 Nov: 660 Hz and 1000 Hz were played for 2.5 s each, and 440 Hz was "heard" too."""
    rate = 16000
    burst = np.zeros(rate * 3, np.int16)
    t = np.arange(int(rate * 1.3)) / rate
    burst[rate // 2: rate // 2 + len(t)] = (4096 * np.sin(2 * np.pi * 660 * t)).astype(np.int16)
    heard = stage.tones_in(burst.tobytes(), rate, {"440": 440, "660": 660, "1000": 1000})
    assert heard == {"440": False, "660": True, "1000": False}


def test_sound_that_does_not_reach_the_stages_output_is_a_failure(desk):
    f = stage.base_dir() / "selftest-sound.raw"
    done = subprocess.Popen(["true"])
    done.wait()
    f.write_bytes(tone(660))
    ok, detail = stage._sound_end({"proc": done, "file": f, "played": ["PulseAudio", "PipeWire"]}, {"sink": stage.SINK})
    assert ok is False and "PipeWire" in detail and "speakers" in detail and not f.exists()
    f.write_bytes(tone(660, 1000))
    ok, detail = stage._sound_end({"proc": done, "file": f, "played": ["PulseAudio", "PipeWire"]}, {"sink": stage.SINK})
    assert ok is True and "private output" in detail
    assert stage._sound_end(None, {"sink": None})[0] is False            # no private output at all
    assert stage._sound_end(None, {"sink": stage.SINK})[0] is None       # could not be checked


def test_shot_and_exec_need_a_stage(desk, tmp_path):
    with pytest.raises(stage.StageError):
        stage.shot(tmp_path / "x.png")
    with pytest.raises(stage.StageError):
        stage.exec_on(["foot"])
    stage.start()
    p = stage.shot(tmp_path / "x.png")
    assert p.exists() and stage.exec_on(["foot"])
    assert 'exec_cmd("foot")' in (desk / "hyprctl-calls.log").read_text()
    stage.stop()


def test_a_command_that_started_the_stage_stops_it(desk):
    s = stage.Session("1920x1080")
    assert s.started and os.environ["DOLLY_STAGE"] == "1" and os.environ["WAYLAND_DISPLAY"] == "wayland-2"
    assert os.environ["HYPRLAND_INSTANCE_SIGNATURE"] == s.inf["sig"] and os.environ["DOLLY_OPEN_EDITOR"] == "0"
    assert logger.runtime_dir() == stage.take_dir()
    os.environ.pop("DOLLY_STAGE")
    s.end()
    assert stage.info() is None


def test_a_running_take_keeps_the_stage(desk):
    s = stage.Session()
    os.environ.pop("DOLLY_STAGE")
    stage.take_dir().mkdir(parents=True, exist_ok=True)
    (stage.take_dir() / "state.json").write_text(json.dumps({"recorder_pid": os.getpid()}))
    s.end()
    assert stage.info() is not None             # `dolly record --stage` returned while it records
    (stage.take_dir() / "state.json").unlink()
    stage.stop()


# ------------------------------------------------------------------ never a stage by accident
def test_the_session_found_without_environment_is_the_real_one(desk):
    inf = stage.start()
    env = {"XDG_RUNTIME_DIR": str(desk)}
    headless.adopt_session_env(env)
    assert env["HYPRLAND_INSTANCE_SIGNATURE"] == "realsession" and env["WAYLAND_DISPLAY"] == "wayland-1"
    # the old rule ("the newest folder, the last socket") would have named the stage
    newest = max((desk / "hypr").iterdir(), key=lambda p: p.stat().st_mtime).name
    assert newest == inf["sig"]
    stage.stop()


def test_a_dead_stages_leftovers_are_not_a_session(desk):
    d = desk / "hypr" / "old_stage"
    d.mkdir()
    (d / ".socket.sock").touch()
    (d / "hyprland.lock").write_text("999999\nwayland-7\n")
    (desk / "wayland-7").touch()
    stage._remember("old_stage", "wayland-7")
    env = {"XDG_RUNTIME_DIR": str(desk)}
    headless.adopt_session_env(env)
    assert env["HYPRLAND_INSTANCE_SIGNATURE"] == "realsession" and env["WAYLAND_DISPLAY"] == "wayland-1"
    assert stage.sweep()                        # and the next sweep removes them
    assert sigs(desk) == ["realsession"] and not (desk / "wayland-7").exists()


def test_sweep_leaves_a_session_it_does_not_know_alone(desk):
    other = desk / "hypr" / "someone_elses"
    other.mkdir()
    (other / ".socket.sock").touch()
    (other / "hyprland.lock").write_text("999998\nwayland-5\n")
    stage.sweep()
    assert "someone_elses" in sigs(desk)


# ------------------------------------------------------------------ one state per desktop
def test_nothing_a_stage_test_sets_outlives_it(desk):
    """`desk` takes back whatever a test (through stage.enter) puts in the environment."""
    stage.start()
    stage.enter()
    assert os.environ["DOLLY_STAGE"] == "1" and os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
    stage.stop()


def test_and_the_next_test_starts_on_the_real_desktop():
    now = {k: os.environ.get(k) for k in ENV_BEFORE}
    assert now == ENV_BEFORE and not logger.on_stage()


def test_a_stage_take_has_its_own_state_folder(desk, monkeypatch):
    real_dir = logger.runtime_dir()
    monkeypatch.setenv("DOLLY_STAGE", "1")
    assert logger.runtime_dir() == real_dir / "stage" / "rt" and capture.state_path().parent == stage.take_dir()
    assert hypr._repeat_file().name == "key-repeat-stage"
    monkeypatch.delenv("DOLLY_STAGE")
    assert hypr._repeat_file().name == "key-repeat"


def test_each_desktops_recorder_is_dollys_own(desk, monkeypatch):
    base = desk / "dolly"
    (base / "stage" / "rt").mkdir(parents=True)
    (base / "state.json").write_text(json.dumps({"recorder_pid": 111}))
    (base / "stage" / "rt" / "state.json").write_text(json.dumps({"recorder_pid": 222}))
    assert {111, 222} <= health.own_recorder_pids()
    monkeypatch.setenv("DOLLY_STAGE", "1")
    assert {111, 222} <= health.own_recorder_pids()


def test_a_take_starting_on_the_other_desktop_is_not_another_recorder(desk, monkeypatch):
    """Seen once in ten simultaneous starts: the desktop take was refused because the stage
    take's recorder was running and not yet in its state file."""
    stage.start()                                               # its socket is wayland-2
    on_stage = subprocess.Popen(["sleep", "30"], env=dict(os.environ, WAYLAND_DISPLAY="wayland-2"))
    on_desktop = subprocess.Popen(["sleep", "30"], env=dict(os.environ, WAYLAND_DISPLAY="wayland-1"))
    elsewhere = subprocess.Popen(["sleep", "30"], env=dict(os.environ, WAYLAND_DISPLAY="wayland-9"))
    monkeypatch.setattr(health, "_proc_exe", lambda pid, root="/proc": "wf-recorder")
    try:
        monkeypatch.setattr(health, "recorder_pids", lambda: [on_stage.pid])
        assert health.other_recorder() is None                  # seen from the desktop: the stage's own
        monkeypatch.setattr(health, "recorder_pids", lambda: [on_desktop.pid])
        assert health.other_recorder() == f"wf-recorder (pid {on_desktop.pid})"      # on this desktop: a real conflict
        monkeypatch.setattr(health, "recorder_pids", lambda: [elsewhere.pid])
        assert health.other_recorder() == f"wf-recorder (pid {elsewhere.pid})"       # not a stage: not ours to wave through
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-2")       # and seen from the stage
        monkeypatch.setattr(health, "recorder_pids", lambda: [on_desktop.pid])
        assert health.other_recorder() is None
        monkeypatch.setattr(health, "recorder_pids", lambda: [on_stage.pid])
        assert health.other_recorder() == f"wf-recorder (pid {on_stage.pid})"
    finally:
        for proc in (on_stage, on_desktop, elsewhere):
            proc.kill()
            proc.wait()
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
        stage.stop()


def test_a_stage_take_does_not_touch_the_real_desktop(desk, monkeypatch):
    started = []
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *a, **k: started.append(a[0]))
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: started.append(a[0]))
    monkeypatch.setenv("DOLLY_STAGE", "1")
    capture.notify("Demo recorded", "0:10")
    capture.shell_refresh()
    assert capture._toast_count() == 0 and capture._dismiss_own_toasts() is False
    assert started == []
    assert health._shell("notifications", "isDnd") is None and health.dnd_begin() is None
    assert health.awake_begin() is None and not health.stay_awake_file().exists()
    assert capture.backend_order("auto") == ["wf"]


def test_healing_on_a_stage_leaves_the_real_desktops_changes(desk, monkeypatch):
    health.ledger_set("dnd_prev", False)
    f = health.stay_awake_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"{health.AWAKE_PREFIX}1:1\n")
    monkeypatch.setenv("DOLLY_STAGE", "1")
    assert health.heal() == [] and health.leftovers() == []
    assert health.ledger().get("dnd_prev") is False and f.exists()


# ------------------------------------------------------------------ programs
def test_browsers_get_their_own_profile_and_stay_inside_the_limit(desk):
    c = stage.wrap_command("uwsm-app -- chromium https://example.com")
    assert c.startswith("chromium --user-data-dir=") and "stage-profiles/chromium" in c and c.endswith("https://example.com")
    assert "--no-first-run" in c and "uwsm-app" not in c
    assert stage.wrap_command("chromium --user-data-dir=/x a") == "chromium --user-data-dir=/x a"
    f = stage.wrap_command("firefox about:blank")
    assert "--profile" in f and "stage-profiles/firefox" in f and "--new-instance" in f
    assert stage.wrap_command("env A=1 brave x").startswith("env A=1 brave --user-data-dir=")
    assert stage.wrap_command("uwsm-app -- xdg-terminal-exec") == "xdg-terminal-exec"
    assert stage.wrap_command("foot -e htop") == "foot -e htop"
    assert "--user-data-dir=" in stage.wrap_command("omarchy-launch-browser about:blank")


def test_launching_on_a_stage_goes_through_the_wrapper(desk, monkeypatch):
    from dolly import inject
    sent = []
    monkeypatch.setattr(inject, "_dispatch", lambda lua, timeout=1.0: sent.append(lua) or True)
    monkeypatch.setenv("DOLLY_STAGE", "1")
    inject.Desktop().launch("uwsm-app -- chromium")
    assert "uwsm-app" not in sent[0] and "user-data-dir" in sent[0]
    monkeypatch.delenv("DOLLY_STAGE")
    inject.Desktop().launch("uwsm-app -- chromium")
    assert "uwsm-app -- chromium" in sent[1] and "user-data-dir" not in sent[1]


# ------------------------------------------------------------------ small things
def test_sizes():
    assert stage.parse_size("1080x1920") == (1080, 1920) and stage.parse_size(" 1281 X 721 ") == (1280, 720)
    for bad in ("", "big", "10x10", "99999x100", "1920"):
        with pytest.raises(stage.StageError):
            stage.parse_size(bad)


def test_the_config_has_the_look_and_none_of_the_session(desk, tmp_path, monkeypatch):
    plain = stage.config_text(1920, 1080)
    assert "autostart" not in plain and "bindings" not in plain and "hyprland.lua" not in plain.split("\n", 2)[2]
    assert "disable_watchdog_warning = true" in plain and "xwayland = { enabled = false }" in plain
    assert f'hl.env("{stage.MARK}", "1")' in plain
    omarchy = tmp_path / "omarchy"
    (omarchy / "default" / "hypr").mkdir(parents=True)
    (omarchy / "default" / "hypr" / "bootstrap.lua").touch()
    monkeypatch.setenv("OMARCHY_PATH", str(omarchy))
    look = stage.config_text(1920, 1080)
    assert 'require("default.hypr.looknfeel")' in look and "default.hypr.autostart" not in look
    assert "default.hypr.bindings" not in look and "pcall(function()" in look


def test_limits_come_from_the_environment_or_settings(desk, monkeypatch):
    assert stage.settings()["memory_mb"] == 1500 and stage.settings()["max_minutes"] == 30
    monkeypatch.setenv("DOLLY_STAGE_MEMORY_MB", "800")
    monkeypatch.setenv("DOLLY_STAGE_MAX_MINUTES", "5")
    assert stage.settings()["memory_mb"] == 800 and stage.settings()["max_minutes"] == 5
    cmd = stage.limit_cmd(["wf-recorder", "-o", "STAGE"], 700)
    assert cmd[0] == "systemd-run" and "MemoryMax=700M" in cmd and cmd[-3:] == ["wf-recorder", "-o", "STAGE"]
    monkeypatch.setattr(stage.shutil, "which", lambda name: None)
    assert stage.limit_cmd(["x"], 700) == ["x"]


def test_stage_flag_is_a_headless_flag():
    assert "stage" in headless.FLAGS


def test_status_command_without_a_stage(desk, capsys):
    from dolly import cli
    with pytest.raises(SystemExit) as e:
        cli._main(["stage", "status", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert e.value.code == 0 and out == {"running": False, "leftovers": []}


# ------------------------------------------------------------------ the pointer as the sync marker
def frames_with(n, on=(), value=180, step_at=None):
    """n probe frames of a dark corner; `on`: frame indexes with the pointer in the corner;
    `step_at`: from this frame on the corner stays different (a window opened)."""
    f = np.full((n, sync.PROBE, sync.PROBE, 3), 30, np.uint8)
    for i in on:
        f[i, :2, :2] = value
    if step_at is not None:
        f[step_at:, :3, :3] = 90
    return f


def test_pointer_marks_are_found_and_nothing_else(desk):
    pts = [i / 60 for i in range(120)]
    runs = sync.pointer_runs(frames_with(120, on=range(30, 41)), pts)
    assert runs == [(pts[30], pts[40])]
    assert sync.pointer_runs(frames_with(120, step_at=50), pts) == []            # a change that stays
    assert sync.pointer_runs(frames_with(120, on=[70]), pts) == []               # one frame: too short
    assert sync.pointer_runs(frames_with(120, on=range(20, 119)), pts) == []     # too long to be a mark
    deep = frames_with(120)
    deep[40:52, 4:6, 4:6] = 200                                                    # text deeper in the corner
    assert sync.pointer_runs(deep, pts) == []
    late = frames_with(120, on=range(110, 120))
    assert sync.pointer_runs(late, pts) == [] and sync.pointer_open_run(late, pts) == (pts[110], pts[119])


def test_fit_and_trim_from_pointer_marks():
    start, stop = (0.50, 0.66), (20.50, 20.66)
    f = sync.fit_pointer([0.30, 20.30], {"start": start, "stop": stop}, 60)
    assert f.method == "markers2" and abs(f.map(0.30) - (0.50 - 0.5 / 60)) < 1e-6
    only = sync.fit_pointer([0.30, 20.30], {"start": start, "stop": None}, 60)
    assert only.method == "markers1" and abs(only.a - (0.50 - 0.5 / 60 - 0.30)) < 1e-6
    end = sync.fit_pointer([0.30, 20.30], {"start": None, "stop": stop}, 60)
    assert end.method == "markers1" and abs(end.a - (20.50 - 0.5 / 60 - 20.30)) < 1e-6
    assert sync.fit_pointer([0.3], {"start": None, "stop": None}, 60, 0.2).method == "clock"
    assert sync.fit_pointer([0.3], {"start": None, "stop": None}, 60).method == "none"
    t0, t1 = sync.pointer_trim({"start": start, "stop": stop}, 60, 22.0)
    assert 0.86 < t0 < 0.92 and 20.25 < t1 < 20.30            # the marks and the repaints around them are left out
    assert sync.pointer_trim({"start": None, "stop": stop}, 60, 22.0)[0] == 0.0
    assert sync.pointer_trim({"start": None, "stop": None}, 60, 22.0) is None


CORNER = f"x = {stage.MARK_AT[0]}, y = {stage.MARK_AT[1]}"
WIGGLE = f"x = {stage.MARK_WIGGLE[0]}, y = {stage.MARK_WIGGLE[1]}"


def test_the_pointer_mark_goes_to_the_corner_and_back(desk, monkeypatch):
    moves = []
    monkeypatch.setattr(stage, "pointer", lambda: (640.0, 400.0))
    monkeypatch.setattr(stage, "_socket_dispatch", lambda lua: moves.append(lua) or True)
    m = stage.pointer_mark(0.1)
    assert m and abs(m["t"] - time.monotonic()) < 3 and m["rtt"] >= 0
    first = next(i for i, mv in enumerate(moves) if CORNER in mv)
    last = max(i for i, mv in enumerate(moves) if CORNER in mv)
    # the stage repaints before the jump (a frame to compare with), while the pointer is shown
    # in the corner (a busy recorder still gets a frame with it) and after it has gone back
    assert first >= 2 and all("y = 400" in mv for mv in moves[:first]) and "x = 641, y = 400" in moves[0]
    assert any(WIGGLE in mv for mv in moves[first:last])
    assert "x = 640, y = 400" in moves[last + 1] and len(moves) > last + 3 and "x = 640, y = 400" in moves[-1]
    assert not stage._home_file().exists()
    monkeypatch.setattr(stage, "pointer", lambda: None)
    assert stage.pointer_mark(0.05) is None


def test_the_stop_mark_stays_until_the_recorder_has_gone(desk, monkeypatch):
    moves = []
    monkeypatch.setattr(stage, "pointer", lambda: (640.0, 400.0))
    monkeypatch.setattr(stage, "_socket_dispatch", lambda lua: moves.append(lua) or True)
    m = stage.pointer_mark(0.1, stay=True)
    assert m and CORNER in moves[-1]                            # still there: nothing takes it back yet
    assert json.loads(stage._home_file().read_text()) == {"x": 640, "y": 400}
    moves.clear()
    assert stage.pointer_home() and "x = 640, y = 400" in moves[-1] and not stage._home_file().exists()
    assert stage.pointer_home() is False                        # nothing to take back
    # a stop mark nobody tidied up after (a take that was cancelled): the next mark starts from home
    stage.pointer_mark(0.05, stay=True)
    moves.clear()
    stage.pointer_mark(0.05)
    assert "x = 640, y = 400" in moves[0] and not stage._home_file().exists()


def test_the_stop_signal_waits_until_the_recorder_is_idle():
    """wf-recorder drops the frames it has not encoded yet when it is told to stop; on a busy
    machine that was the whole stop mark."""
    idle = subprocess.Popen(["sleep", "5"])
    busy = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    try:
        said = []
        waited = stage.recorder_drained(idle.pid, least=0.3, most=2.0, log=said.append)
        assert 0.3 <= waited < 1.2 and "processor share" in said[0]
        assert stage.recorder_drained(busy.pid, least=0.2, most=0.8) >= 0.8       # never idle: the longest wait
    finally:
        idle.kill()
        busy.kill()
        idle.wait()
        busy.wait()
    t0 = time.monotonic()
    stage.recorder_drained(busy.pid, least=0.3, most=2.0)                         # gone: the shortest wait
    assert 0.25 <= time.monotonic() - t0 < 1.0
    assert stage._cpu_ticks(os.getpid()) >= 0 and stage._cpu_ticks(-1) is None


def test_a_stage_that_is_still_gets_a_longer_wait(desk, monkeypatch):
    """After a still stretch the recorder has that whole stretch to encode before it gets to
    the stop mark; on a stage where things move it is never far behind."""
    assert stage.is_still() is None                             # no stage: nothing to look at
    stage.start()
    assert stage.is_still(gap=0.01) is True                     # the fake stage never changes
    assert [stage.drain_limit(t) for t in (2, 10, 500)] == pytest.approx([6.6, 17.0, 60.0])
    monkeypatch.setattr(stage, "is_still", lambda gap=0.3: False)
    assert stage.drain_limit(500) == 3.0
    monkeypatch.setattr(stage, "is_still", lambda gap=0.3: None)
    assert stage.drain_limit(10) == 17.0                        # cannot tell: the careful wait
    stage.stop()


def test_a_still_stage_is_repainted_until_its_recorder_has_a_frame(desk, tmp_path, monkeypatch):
    class Running:
        def poll(self):
            return None
    nudges = []
    monkeypatch.setattr(capture, "_nudge_screen", lambda: nudges.append(1))
    log = tmp_path / "recorder.log"
    log.write_text("")
    assert capture._recorder_up(Running(), tmp_path / "raw.mkv", log, 0, "wf", limit=0.5)
    assert nudges == []                                         # the desktop: something always moves there
    monkeypatch.setenv("DOLLY_STAGE", "1")
    assert capture._recorder_up(Running(), tmp_path / "raw.mkv", log, 0, "wf", limit=0.5)
    assert len(nudges) >= 2


def test_the_start_mark_is_found_when_the_file_began_later_than_the_clock_says(tmp_path):
    """A still stage: the recorder's first frame waited for the first change, so the start
    mark is at 0.5 s where the clock expects it at 2.5 s."""
    raw = tmp_path / "raw.mkv"
    box = "drawbox=x=0:y=0:w=12:h=12:color=white:t=fill:enable='between(t,0.5,0.8)+gte(t,3.0)'"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1e1e28:s=320x180:r=30:d=4.5",
                    "-vf", box, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(raw)], check=True)
    said = []
    found = sync.detect_pointer_marks(raw, 1.0, 30, "ffmpeg", [0.3, 2.8], 2.2, 4.5, log=said.append)
    assert found["start"] and abs(found["start"][0] - 0.5) < 0.05, (found, said)
    assert found["stop"] and abs(found["stop"][0] - 3.0) < 0.05, (found, said)
    fit = sync.fit_pointer([0.3, 2.8], found, 30, 2.2)
    assert fit.method == "markers2" and abs(fit.map(0.3) - (0.5 - 0.5 / 30)) < 0.04
    # a visit long after the estimate is not the start mark
    late = sync.detect_pointer_marks(raw, 1.0, 30, "ffmpeg", [0.3], -3.0, 4.5)
    assert late["start"] is None


def test_the_stop_mark_is_found_when_the_file_runs_on_long_after_it(tmp_path):
    """The stop waits for a recorder that is behind, and the file can run on for many
    seconds after the stop mark: more than the end of a file that is ever read."""
    raw = tmp_path / "raw.mkv"
    box = "drawbox=x=0:y=0:w=12:h=12:color=white:t=fill:enable='between(t,0.5,0.8)+gte(t,5.0)'"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1e1e28:s=320x180:r=10:d=40",
                    "-vf", box, "-c:v", "libx264", "-preset", "ultrafast", "-g", "20", "-pix_fmt", "yuv420p", str(raw)],
                   check=True)
    said = []
    found = sync.detect_pointer_marks(raw, 1.0, 10, "ffmpeg", [0.3, 4.8], 0.2, 40.0, log=said.append)
    assert found["start"] and abs(found["start"][0] - 0.5) < 0.11, (found, said)
    assert found["stop"] and abs(found["stop"][0] - 5.0) < 0.11, (found, said)       # 35 s before the file ends
    assert sync.fit_pointer([0.3, 4.8], found, 10, 0.2).method == "markers2"


def test_a_stage_take_with_one_mark_says_the_recorder_fell_behind(tmp_path):
    (tmp_path / "capture.json").write_text(json.dumps({"stage": True}))
    (tmp_path / "events.json").write_text(json.dumps({"video": {}, "capture": {"sync": {"method": "markers1"}}}))
    assert any("fell behind" in w for w in headless.summary(tmp_path)["warnings"])
    (tmp_path / "events.json").write_text(json.dumps({"video": {}, "capture": {"sync": {"method": "markers2"}}}))
    assert headless.summary(tmp_path)["warnings"] == []
    (tmp_path / "capture.json").write_text("{}")                # the desktop: one flash of two is not news there
    (tmp_path / "events.json").write_text(json.dumps({"video": {}, "capture": {"sync": {"method": "markers1"}}}))
    assert headless.summary(tmp_path)["warnings"] == []


def test_a_stage_only_stops_programs_of_its_own_session(desk, tmp_path):
    """Its socket's name says little (the second compositor of anyone is "wayland-2"): these
    tests once stopped the recorder of a stage that was really running."""
    env = dict(os.environ, **{stage.MARK: "1"})
    ours = subprocess.Popen(["sleep", "30"], env=env)
    theirs = subprocess.Popen(["sleep", "30"], env=dict(env, XDG_RUNTIME_DIR=str(tmp_path / "another-session")))
    try:
        stage._kill_marked("wayland-2", "wayland-1")
        ours.wait(timeout=5)
        assert ours.poll() is not None and theirs.poll() is None
    finally:
        for proc in (ours, theirs):
            proc.kill()
            proc.wait()


def test_stopping_a_stage_take_takes_the_pointer_home(desk, monkeypatch):
    home = []
    monkeypatch.setenv("DOLLY_STAGE", "1")
    monkeypatch.setattr(stage, "pointer_home", lambda: home.append(1) or True)
    capture._stage_pointer_home()
    assert home == [1]
    monkeypatch.delenv("DOLLY_STAGE")
    capture._stage_pointer_home()
    assert home == [1]                                          # the real desktop's pointer is never moved


def test_a_mark_that_wiggles_is_one_mark(desk):
    pts = [i / 60 for i in range(240)]
    f = frames_with(240)
    for i in range(60, 80):                                     # the start mark: a pixel to and fro while shown
        f[i, :2, :2] = 180 if i % 2 else 140
    for i in range(200, 240):                                   # the stop mark: stays until the file ends
        f[i, :2, :2] = 180 if i % 2 else 140
    assert sync.pointer_runs(f, pts) == [(pts[60], pts[79])]
    assert sync.pointer_open_runs(f, pts) == [(pts[200], pts[239])]
    assert sync.pointer_open_run(f, pts) == (pts[200], pts[239])
    # a pointer that left again did not stay (what stays from then on is the empty corner)
    assert [r[0] for r in sync.pointer_open_runs(frames_with(240, on=range(60, 80)), pts)] == [pts[80]]


def test_a_pointer_whose_look_changes_while_shown_is_still_there(desk):
    """Seen on 11 Nov: near a window's corner the pointer was the resize pointer, plain in
    one position and almost invisible a pixel further out. Half the frames of a mark differed
    from the empty corner by 58 grey levels, half by 10, and every mark was read late."""
    pts = [i / 60 for i in range(300)]
    f = frames_with(300)
    looks = [58, 58, 58, 10, 56, 56, 56, 10, 10, 10, 59, 59, 11, 11, 11, 58, 58, 10]
    for k, v in enumerate(looks):
        f[35 + k, :2, :2] = 30 + v                              # the start mark, frame by frame as recorded
    for i in range(240, 300):
        f[i, :2, :2] = 30 + (60 if (i // 3) % 2 else 10)        # the stop mark, to the end of the file
    assert sync.pointer_runs(f, pts) == [(pts[35], pts[35 + len(looks) - 1])]
    assert sync.pointer_open_runs(f, pts) == [(pts[240], pts[299])]


def test_the_stage_never_shows_the_resize_pointer(desk):
    conf = stage.config_text(1920, 1080)
    assert "resize_on_border = false" in conf and "hover_icon_on_border = false" in conf
    assert conf.index("resize_on_border = false") > conf.index("hl.monitor(")      # after Omarchy's look, which sets it


def test_a_stay_seen_at_a_low_frame_rate_is_not_a_row_of_visits(desk):
    """A recorder on a busy machine gets a frame every 80 ms: two frames of the waiting
    pointer would pass for a short visit each."""
    n = 60
    pts = [i * 0.08 for i in range(n)]
    f = frames_with(n)
    for i in range(40, n):
        f[i, :2, :2] = 180 if i % 2 else 140
    stays = sync.pointer_open_runs(f, pts)
    visits = [r for r in sync.pointer_runs(f, pts) if not any(s[0] <= r[0] for s in stays)]
    assert stays == [(pts[40], pts[-1])] and visits == []


def test_a_busy_machines_late_frame_does_not_lose_the_start_mark(desk):
    pts = [i / 60 for i in range(200)]
    assert sync.pointer_runs(frames_with(200, on=range(30, 110)), pts) == [(pts[30], pts[109])]     # 1.3 s
    assert sync.pointer_trim({"start": (0.5, 0.8), "stop": (20.5, 22.0)}, 60, 22.0) == \
        (round(0.8 + 2 / 60 + sync.POINTER_AFTER_S, 4), round(20.5 - 1 / 60 - sync.POINTER_AFTER_S, 4))


# ------------------------------------------------------------------ the picture behind the windows
def picture(tmp_path, size=(400, 300)):
    """A desktop picture where Omarchy keeps the link to the current one: blue on the left, red on the right."""
    from PIL import Image
    src = tmp_path / "themes" / "1-two-halves.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, (200, 60, 90))
    im.paste((20, 120, 220), (0, 0, size[0] // 2, size[1]))
    im.save(src)
    link = Path(os.environ["XDG_STATE_HOME"]) / "omarchy" / "current" / "background"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(src)
    return src


def test_the_desktop_picture_is_cut_to_fill_the_stage(desk, tmp_path):
    from PIL import Image
    assert stage.wallpaper_source() is None
    src = picture(tmp_path)
    assert stage.wallpaper_source() == src
    tall = stage.prepare_wallpaper(src, 320, 568)               # a wide picture on a tall stage
    with Image.open(tall) as im:
        assert im.size == (320, 568)
        rgb = im.convert("RGB")
        left, right = rgb.getpixel((20, 284)), rgb.getpixel((300, 284))
    assert left[2] > 150 > left[0] and right[0] > 150 > right[2]      # the middle of the picture, both halves in it
    assert stage.prepare_wallpaper(src, 320, 568) == tall              # kept: the next start only opens a file
    wide = stage.prepare_wallpaper(src, 640, 360)
    with Image.open(wide) as im:
        assert im.size == (640, 360) and wide != tall
    bad = tmp_path / "bad.png"
    bad.write_text("not a picture")
    assert stage.prepare_wallpaper(bad, 320, 240) is None and stage.prepare_wallpaper(tmp_path / "gone.png", 320, 240) is None
    assert not list(stage.wallpaper_cache().glob("*.tmp.png"))


def test_a_stage_shows_the_desktop_picture(desk, tmp_path, monkeypatch):
    monkeypatch.setenv("DOLLY_STAGE_BACKGROUND", "wallpaper")
    picture(tmp_path)
    inf = stage.start()
    assert inf["background"] == "wallpaper" and inf["background_note"] == ""
    assert stage.status()["background"] == "wallpaper"
    calls = (desk / "hyprctl-calls.log").read_text()
    started = next(l for l in calls.splitlines() if "quickshell" in l)
    assert "QT_QUICK_BACKEND=software" in started and str(stage.base_dir() / "wallpaper") in started
    qml = (stage.base_dir() / "wallpaper" / "shell.qml").read_text()
    assert f'namespace: "{stage.WALLPAPER_NS}"' in qml and "WlrLayer.Background" in qml
    assert "Image.PreserveAspectCrop" in qml and 'source: "file://' in qml and f'color: "#{stage.theme_background()}"' in qml
    cut = list(stage.wallpaper_cache().glob("*.png"))
    assert len(cut) == 1 and cut[0].name in qml
    stage.stop()
    assert stage.sweep_report() == []


def test_a_stage_falls_back_to_the_theme_colour(desk, tmp_path, monkeypatch):
    monkeypatch.setenv("DOLLY_STAGE_BACKGROUND", "wallpaper")
    quick = stage.wallpaper_start
    monkeypatch.setattr(stage, "wallpaper_start", lambda inf: quick(inf, wait=0.5))

    def started():
        inf = stage.start()
        stage.stop()
        return inf["background"], inf["background_note"]
    assert started() == ("color", "no desktop picture was found")
    src = picture(tmp_path)
    monkeypatch.setenv("FAKE_NO_LAYER", "1")                    # the picture's program never shows a window
    assert started() == ("color", "the picture's program did not come up")
    monkeypatch.delenv("FAKE_NO_LAYER")
    bg = stage.theme_background()                               # its window is there, the picture is not in it
    monkeypatch.setenv("FAKE_GRIM_RGB", ",".join(str(int(bg[i:i + 2], 16)) for i in (0, 2, 4)))
    assert started() == ("color", "the picture was not drawn in time")
    monkeypatch.delenv("FAKE_GRIM_RGB")
    src.write_text("not a picture")
    kind, why = started()
    assert kind == "color" and "could not be read" in why
    which = stage.shutil.which
    monkeypatch.setattr(stage.shutil, "which", lambda n, *a, **k: None if n in ("quickshell", "qs") else which(n, *a, **k))
    assert started() == ("color", "Quickshell is not installed")
    assert sigs(desk) == ["realsession"]


def test_the_background_is_a_setting(desk, monkeypatch):
    from dolly import config
    monkeypatch.delenv("DOLLY_STAGE_BACKGROUND")
    assert stage.settings()["background"] == "wallpaper"
    entry = next(e for e in config.SCHEMA if e["key"] == "stage.background")
    assert entry["default"] == "wallpaper" and entry["options"] == ["wallpaper", "color"]
    monkeypatch.setenv("DOLLY_STAGE_BACKGROUND", "colour")
    assert stage.settings()["background"] == "color"
    assert stage.background_choice(" Colour ") == "color" and stage.background_choice("picture") is None
    with pytest.raises(stage.StageError, match="wallpaper or colour"):
        stage.start(background="picture")
    assert stage.info() is None
    monkeypatch.setenv("DOLLY_STAGE_BACKGROUND", "wallpaper")
    inf = stage.start(background="colour")                      # asked for on the command line: no picture is looked for
    assert inf["background"] == "color" and inf["background_note"] == ""
    stage.stop()


# ------------------------------------------------------------------ sound, microphone, camera
def test_the_stages_programs_play_into_its_own_output(desk):
    inf = stage.start()
    env = stage._environ_of(inf["pid"])         # the compositor's environment: what every program it starts gets
    assert env.get("PULSE_SINK") == stage.SINK and env.get("PIPEWIRE_NODE") == stage.SINK
    assert stage.env_for(inf)["PULSE_SINK"] == stage.SINK and stage.env_for(inf)["PIPEWIRE_NODE"] == stage.SINK
    assert stage.system_audio_device() == f"{stage.SINK}.monitor"     # what "system audio" means for a stage take
    stage.stop()
    assert stage.system_audio_device() is None


def record_args(**given):
    import argparse
    base = dict(mode=None, countdown=None, fps=None, audio=None, camera=None, mic=None, desktop_audio=None,
                webcam=None, no_keys=None, solid=None, no_dnd=None)
    return argparse.Namespace(**{**base, **given})


def test_a_stage_take_records_no_microphone_or_camera_unless_asked(desk, monkeypatch):
    from dolly import cli, config
    settings = {"mode": "screen", "countdown": 3, "fps": 60, "audio": "both", "camera": "live", "camera_device": "",
                "keys": True, "solid": "auto", "dnd": True, "mic_device": "default", "system_device": "default"}
    monkeypatch.setattr(config, "record_defaults", lambda cfg=None: dict(settings))
    a = cli.apply_record_defaults(record_args())
    assert a.audio == "both" and a.camera == "live" and a.mic                  # the desktop: what Settings say
    monkeypatch.setenv("DOLLY_STAGE", "1")
    a = cli.apply_record_defaults(record_args())
    assert a.audio == "none" and a.camera == "off" and not a.mic and not a.desktop_audio and not a.webcam
    a = cli.apply_record_defaults(record_args(audio="system"))
    assert a.audio == "system" and a.desktop_audio and not a.mic and a.camera == "off"
    a = cli.apply_record_defaults(record_args(audio="mic", camera="edit"))    # asked for: the person's own microphone
    assert a.audio == "mic" and a.mic and a.camera == "edit" and a.webcam


def test_the_camera_bubble_on_a_stage_is_drawn_with_plain_pictures(desk, monkeypatch):
    """mpv's GPU output could not show a window on the stage ("invalid arguments for
    wl_surface.attach"); and a bubble that is never placed would be a window as large as the stage."""
    from dolly import camera

    class Mpv:
        stopped = False

        def poll(self):
            return None

        def terminate(self):
            Mpv.stopped = True

        def wait(self, timeout=None):
            return 0
    started = []
    monkeypatch.setattr(capture, "_have", lambda cmd: True)
    monkeypatch.setattr(capture.subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or Mpv())
    monkeypatch.setattr(camera, "place_bubble", lambda *a, **k: True)
    monkeypatch.setattr(capture.time, "sleep", lambda s: None)
    region = [0, 0, 1920, 1080]
    assert capture.start_bubble(None, region, live=True) is not None
    assert "--vo=wlshm" not in started[-1]                      # the desktop: as before
    monkeypatch.setenv("DOLLY_STAGE", "1")
    assert capture.start_bubble(None, region, live=True) is not None
    assert "--vo=wlshm" in started[-1] and started[-1][0] == "mpv"
    monkeypatch.setattr(camera, "place_bubble", lambda *a, **k: False)      # its window never comes
    assert capture.start_bubble(None, region, live=True) is None and Mpv.stopped


def test_two_takes_started_in_the_same_second_get_a_folder_each(tmp_path):
    a = capture.new_take_folder(tmp_path / "Dolly", "2026-11-11_21-32-41")
    b = capture.new_take_folder(tmp_path / "Dolly", "2026-11-11_21-32-41")
    c = capture.new_take_folder(tmp_path / "Dolly", "2026-11-11_21-32-41")
    assert [x.name for x in (a, b, c)] == ["2026-11-11_21-32-41", "2026-11-11_21-32-41-2", "2026-11-11_21-32-41-3"]
    assert all(x.is_dir() for x in (a, b, c))


# ------------------------------------------------------------------ keys by name
def raw_events(pdir):
    return [json.loads(l) for l in (pdir / "events.raw.jsonl").read_text().splitlines() if l.strip()]


def test_a_key_from_another_tool_is_a_key_without_a_name(desk, tmp_path, monkeypatch):
    from dolly import keymap, render
    monkeypatch.setenv("DOLLY_STAGE", "1")
    lg = logger.Logger(tmp_path)
    assert lg.stage
    lg.on_unnamed_key(True)                     # wtype's "l": code 9, "Escape" on a real keyboard
    lg.on_unnamed_key(False)
    lg.raw_hold_until = time.monotonic() + 5    # `dolly stage type` is typing: the codes are its own
    lg.on_unnamed_key(True)
    lg.on_key(38, True)                         # ... and it says which key it was (an "l": typed text,
    keys = [e for e in raw_events(tmp_path) if e["type"] == "key"]          # kept as a time only)
    assert [k["key"] for k in keys] == [keymap.UNNAMED, keymap.TYPED]
    assert keymap.UNNAMED not in render.SHOW_ALONE and keymap.UNNAMED not in keymap.NAMES.values()


def test_what_stage_commands_type_is_logged_by_name_and_once(desk, tmp_path, monkeypatch):
    import threading

    from dolly import inject
    monkeypatch.setenv("DOLLY_STAGE", "1")
    lg = logger.Logger(tmp_path)
    lg.feed_on = True
    feed = inject.Feed()
    assert feed.path == stage.take_dir() / inject.FEED_NAME
    th = threading.Thread(target=lg.tail_harness, args=(feed.path,), daemon=True)
    th.start()
    time.sleep(0.1)
    feed.hold(5)
    feed.tap(38, ("ctrl",))
    feed.click(10, 20)
    for _ in range(100):
        if any(e["type"] == "click" for e in raw_events(tmp_path)):
            break
        time.sleep(0.02)
    assert lg.raw_hold_until > time.monotonic() + 3
    lg.on_unnamed_key(True)                                                  # the same key, as Hyprland reports it
    lg.on_click({"x": 10.0, "y": 20.0, "button": "left"}, fed=False)         # the same click, as Hyprland saw it
    lg.on_click({"x": 300.0, "y": 200.0, "button": "left"}, fed=False)       # another click
    feed.hold(0)
    for _ in range(100):
        if lg.raw_hold_until < time.monotonic():
            break
        time.sleep(0.02)
    lg.on_unnamed_key(True)                                                  # typed by something else, afterwards
    lg.stop.set()
    ev = raw_events(tmp_path)
    assert [(e["key"], e["mods"]) for e in ev if e["type"] == "key"] == [("l", ["ctrl"]), ("?", [])]
    assert [(e["x"], e["y"]) for e in ev if e["type"] == "click"] == [(10, 20), (300.0, 200.0)]


def feed_lines():
    from dolly import inject
    return [json.loads(l) for l in (stage.take_dir() / inject.FEED_NAME).read_text().splitlines()]


def test_stage_type_and_key_type_it_and_say_what_they_typed(desk):
    with pytest.raises(stage.StageError, match="no stage is running"):
        stage.do_type("ls")
    stage.start()
    r = stage.do_type("ls", speed=0, enter_key=True)
    assert r["ok"] and r["typed"] == 2 and r["enter"]
    assert (desk / "wtype.log").read_text().splitlines() == ["-- l", "-- s", "-k Return"]
    lines = feed_lines()
    assert lines[0]["type"] == "hold" and lines[-1]["type"] == "hold" and lines[-1]["until"] < lines[0]["until"]
    downs = [l["code"] - 8 for l in lines if l["type"] == "key" and l["down"]]
    assert downs == [38, 31, 28]                                # l, s, Return: the codes of a real keyboard
    stage.do_key(["ctrl+l", "enter"])
    assert (desk / "wtype.log").read_text().splitlines()[-2:] == ["-M ctrl -k l -m ctrl", "-k Return"]
    keys = [(l["code"] - 8, l["down"]) for l in feed_lines() if l["type"] == "key"][-6:]
    assert keys == [(29, True), (38, True), (38, False), (29, False), (28, True), (28, False)]
    with pytest.raises(stage.StageError, match="not a modifier"):
        stage.do_key(["hyper+x"])
    with pytest.raises(stage.StageError, match="which key"):
        stage.do_key([])
    with pytest.raises(stage.StageError, match="nothing to type"):
        stage.do_type("")
    stage.stop()


def test_stage_move_and_click_know_the_stage(desk):
    stage.start()
    r = stage.do_move(100, 50, instant=True)
    assert r == {"ok": True, "x": 100, "y": 50}
    assert "cursor.move({ x = 100, y = 50 })" in (desk / "hyprctl-calls.log").read_text()
    with pytest.raises(stage.StageError, match="not on the stage"):
        stage.do_move(5000, 50)
    with pytest.raises(stage.StageError, match="two numbers"):
        stage.do_click(10, None)
    with pytest.raises(stage.StageError, match="no window under the pointer"):
        stage.do_click()                                        # the fake stage has no windows
    stage.stop()


def test_the_stage_commands_answer_in_json(desk, capsys):
    from dolly import cli
    with pytest.raises(SystemExit) as e:
        cli._main(["stage", "type", "ls", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert e.value.code == 1 and out["ok"] is False and "no stage is running" in out["error"]
    stage.start()
    with pytest.raises(SystemExit) as e:
        cli._main(["stage", "key", "ctrl+l", "--json"])
    assert e.value.code == 0 and json.loads(capsys.readouterr().out) == {"ok": True, "keys": ["ctrl+l"]}
    with pytest.raises(SystemExit) as e:
        cli._main(["stage", "start", "--background", "colour", "--json"])
    assert e.value.code == 1 and "already running" in json.loads(capsys.readouterr().out)["error"]
    stage.stop()


def test_a_stage_take_says_so_and_warns_when_its_mark_was_not_found(tmp_path):
    (tmp_path / "capture.json").write_text(json.dumps({"stage": True}))
    (tmp_path / "events.json").write_text(json.dumps({"video": {}, "capture": {"sync": {"method": "clock"}}}))
    s = headless.summary(tmp_path)
    assert s["stage"] is True and any("pointer mark" in w for w in s["warnings"])
    (tmp_path / "capture.json").write_text("{}")
    assert "stage" not in headless.summary(tmp_path)

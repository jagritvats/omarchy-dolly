"""record -> stop -> finalize with stubbed system tools.

Simulates a Windows-hosted VM: gpu-screen-recorder exits immediately (no
usable GPU encoder), wf-recorder works (stub records ffmpeg testsrc), hyprctl
answers monitors/cursorpos but `eval` fails (no Lua bridge), and there is no
readable /dev/input -> click source "none" -> dwell-based zoom.
Verifies the fallback order, CFR output, cursor_baked handling and status.
"""
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

HYPRCTL = r'''#!/usr/bin/env bash
case "$*" in
  *monitors*) echo '[{"name":"Virtual-1","focused":true,"x":0,"y":0,"width":1280,"height":800,"scale":1.0,"transform":0}]';;
  *cursorpos*) python3 -c "import time,math;t=time.time()%6;print('{\"x\":%d,\"y\":%d}'%(300+150*math.floor(t/1.5),300+60*math.floor(t/1.5)))";;
  *activewindow*) echo '{"at":[0,0],"size":[1280,800],"title":"stub","class":"stub"}';;
  eval*) echo "error: lua disabled in stub"; exit 1;;
  *) echo "{}";;
esac
'''
GSR = '#!/usr/bin/env bash\necho "gsr: no supported GPU found" >&2\nexit 1\n'
WF = r'''#!/usr/bin/env bash
out=""; geo="0,0 1280x800"; fps=60
while [[ $# -gt 0 ]]; do case "$1" in -f) out="$2"; shift;; -g) geo="$2"; shift;; -o) shift;; -r) fps="$2"; shift;; esac; shift; done
size="${geo#* }"
exec ffmpeg -v error -y -f lavfi -i "testsrc2=size=${size}:rate=${fps}" -c:v libx264 -preset ultrafast -pix_fmt yuv420p "$out"
'''


@pytest.fixture()
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("hyprctl", HYPRCTL), ("gpu-screen-recorder", GSR), ("wf-recorder", WF)):
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    e = dict(os.environ)
    e.update({"PATH": f"{bindir}:{e['PATH']}", "DOLLY_DIR": str(tmp_path / "videos"), "XDG_RUNTIME_DIR": str(tmp_path / "run"),
              "DOLLY_OPEN_EDITOR": "0", "PYTHONPATH": str(ROOT), "HOME": str(tmp_path)})
    e.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
    (tmp_path / "run").mkdir()
    return e


def dolly(env, *args, check=True):
    return subprocess.run([sys.executable, "-m", "dolly.cli", *args], env=env, capture_output=True, text=True,
                          check=check, timeout=120)


def test_vm_fallback_record_stop(env):
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", "--fps", "30")
    pdir = Path(r.stdout.strip().splitlines()[-1])
    st = dolly(env, "status", "--json", check=False)
    s = json.loads(st.stdout)
    assert st.returncode == 0 and s["recording"] and s["backend"] == "wf"
    time.sleep(3.2)
    dolly(env, "stop")
    assert dolly(env, "status", check=False).returncode == 1
    log = (pdir / "recorder.log").read_text()
    assert "backend=gsr" in log and "backend=wf" in log          # tried GPU path first, fell back
    meta = json.loads((pdir / "capture.json").read_text())
    assert meta["backend"] == "wf" and meta["cursor_baked"] is True
    ev = json.loads((pdir / "events.json").read_text())
    assert ev["video"]["w"] == 1280 and ev["video"]["fps"] == 30
    assert ev["capture"]["click_source"] == "none"
    assert sum(1 for e in ev["events"] if e["type"] == "move") >= 2
    proj = json.loads((pdir / "project.json").read_text())
    assert proj["style"]["cursor"]["enabled"] is False          # cursor already in the pixels
    assert proj["zoom_config"].get("use_dwell") is True
    # export works on the fallback recording
    out = dolly(env, "export", str(pdir), "--quality", "fast", "--width", "640", "--fps", "30").stdout.strip()
    assert Path(out).exists()


def test_forced_backend_env(env):
    env = dict(env, DOLLY_BACKEND="gsr-cpu")
    r = dolly(env, "record", "--mode", "screen", "--countdown", "0", check=False)
    assert r.returncode != 0                                       # gsr stub fails, no fallback when forced
    vids = list((Path(env["DOLLY_DIR"])).glob("*/recorder.log"))
    assert vids and "backend=gsr-cpu" in vids[0].read_text() and "backend=wf" not in vids[0].read_text()

"""Real-desktop smoke check; not a unit test. No mic, camera or uploads.

Runs in an empty workspace, starts/stops through the actual bar IPC, checks
pause/resume and restores capture settings, focus and pointer afterwards.
Run with Dolly's Python, with PYTHONPATH pointing at the installed plugin.
"""
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from dolly.inject import Desktop

OUT = Path(__file__).resolve().parents[1] / "local-verification/live-hardware"
OUT.mkdir(parents=True, exist_ok=True)
desk = Desktop()  # No Feed: use the real Lua bridge, not storyboard events.


def run(*args, timeout=30):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"{args[0]} failed: {p.stderr or p.stdout}")
    return p.stdout.strip()


def status():
    # status uses exit 1 for idle (valid JSON, not a failed command).
    p = subprocess.run(["dolly", "status", "--json"], capture_output=True, text=True, timeout=20)
    return json.loads(p.stdout)


def wait_for(predicate, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        s = status()
        if predicate(s):
            return s
        time.sleep(0.2)
    raise RuntimeError("timed out waiting for Dolly state")


home = (desk.workspace(), desk.focused(), desk.pointer())
original = json.loads(run("dolly", "config", "get"))
report = {"start": status(), "dnd_before": run("omarchy-shell", "notifications", "isDnd")}
assert not report["start"]["recording"], "Refusing to interrupt an existing recording"
term = None
try:
    ws = desk.empty_workspace()
    if ws is None:
        raise RuntimeError("no empty workspace available")
    desk.go_workspace(ws)
    time.sleep(0.4)
    desk.launch("foot --app-id=dolly-platform-check --title=Dolly-Platform-Check /bin/bash --noprofile --norc")
    for _ in range(50):
        term = desk.find("class:dolly-platform-check")
        if term:
            break
        time.sleep(0.1)
    if not term:
        raise RuntimeError("dummy terminal did not open")
    desk.focus(term["address"])
    run("wtype", "--", "printf 'Dolly Recorder: live platform check\\n'")
    run("wtype", "-k", "Return")
    run("dolly", "config", "set", "capture.mode=screen", "capture.countdown=0",
        "audio.mode=none", "camera.mode=off")
    run("omarchy-shell", "io.github.jagritvats.dolly", "record")
    report["recording"] = wait_for(lambda s: s["recording"] and s["phase"] == "recording")
    report["dnd_during"] = run("omarchy-shell", "notifications", "isDnd")
    time.sleep(1)
    desk.focus(term["address"])
    run("wtype", "-d", "45", "--", "printf 'Recording on real Hyprland, not a VM\\n'")
    run("wtype", "-k", "Return")
    desk.move_to(550, 390, seconds=0.5)
    desk.click(address=term["address"])
    run("dolly", "mark")
    run("omarchy-shell", "io.github.jagritvats.dolly", "pause")
    report["paused"] = wait_for(lambda s: s["paused"])
    time.sleep(1.5)
    run("omarchy-shell", "io.github.jagritvats.dolly", "pause")
    report["resumed"] = wait_for(lambda s: s["recording"] and not s["paused"])
    run("wtype", "--", "printf 'Pause and resume complete\\n'")
    run("wtype", "-k", "Return")
    time.sleep(2)
    run("omarchy-shell", "io.github.jagritvats.dolly", "stop")
    report["finished"] = wait_for(lambda s: not s["recording"] and s["phase"] == "idle", 90)
    report["dnd_after"] = run("omarchy-shell", "notifications", "isDnd")
    project = Path(report["finished"]["last"])
    events = json.loads((project / "events.json").read_text())
    report["project"] = str(project)
    report["capture"] = events["capture"]
    report["events"] = dict(Counter(e["type"] for e in events["events"]))
    report["export"] = json.loads(run("dolly", "export", str(project), "--json", "--width", "1280", timeout=180))
    assert report["export"]["ok"], report["export"]
    # The ordinary bar stop opens the actual editor. Capture only our clean workspace.
    time.sleep(3)
    run("grim", str(OUT / "after-bar-stop.png"))
    report["ok"] = True
except Exception as exc:
    report["ok"] = False
    report["error"] = str(exc)
finally:
    if status()["recording"]:
        run("dolly", "stop", "--json", timeout=90)
    run("dolly", "config", "set", *[
        f"{group}.{key}={original[group][key]}"
        for group, key in (("capture", "mode"), ("capture", "countdown"),
                           ("audio", "mode"), ("camera", "mode"))])
    if term and desk.window(term["address"]):
        desk.close(term["address"])
    desk.go_workspace(home[0])
    if home[1] and desk.window(home[1]):
        desk.focus(home[1])
    desk.move_to(*home[2], seconds=0.2)
    (OUT / "bar-recording-report.json").write_text(json.dumps(report, indent=2) + "\n")

print(json.dumps(report, indent=2))
sys.exit(0 if report["ok"] else 1)

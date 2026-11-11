"""Walkthrough helpers for the Omarchy shell harness (TEST HARNESS ONLY).

Drives the Omarchy shell harness (a separate checkout: $DOLLY_SHELL_HARNESS, default
~/shell-harness) through harness/dctl (so Dolly's logger
sees the clicks/keys as a labelled "harness" event source) and sets up labelled
test feeds: a 660 Hz beep pattern as the microphone (a null sink + remap source
named "Test feed: mic"), a chord as system audio, and ffmpeg's testsrc2 pattern
as the camera. Nothing here is a real camera or microphone.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

H = Path(os.environ.get("DOLLY_SHELL_HARNESS") or Path.home() / "shell-harness")
REPO = Path(__file__).resolve().parents[1]
DCTL = str(REPO / "harness" / "dctl")


def henv() -> dict:
    out = subprocess.run(["bash", "-c", f"source {H}/env.sh && env -0"], capture_output=True).stdout
    env = dict(x.split("=", 1) for x in out.decode().split("\0") if "=" in x)
    return env


ENV = henv()
RT = Path(ENV["XDG_RUNTIME_DIR"])
HOME = Path(ENV["HOME"])
DOLLY = str(HOME / ".config/omarchy/plugins/io.github.jagritvats.dolly/bin/dolly")


def sh(cmd, check=False, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), env=ENV, capture_output=True, text=True, check=check, **kw)


def bg(cmd):
    return subprocess.Popen(cmd, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            start_new_session=True)


def ctl(*args):
    return sh([DCTL, *map(str, args)])


def click(x, y, pause=0.6):
    ctl("click", x, y)
    time.sleep(pause)


def move(x, y, steps=12, dur=0.5):
    """Glide the pointer (the harness warps; small steps read as motion)."""
    cur = json.loads(sh(["hyprctl", "cursorpos", "-j"]).stdout or '{"x":0,"y":0}')
    x0, y0 = cur.get("x", 0), cur.get("y", 0)
    for i in range(1, steps + 1):
        u = i / steps
        e = u * u * (3 - 2 * u)
        ctl("move", int(x0 + (x - x0) * e), int(y0 + (y - y0) * e))
        time.sleep(dur / steps)


def glide_click(x, y, pause=0.6):
    move(x, y)
    time.sleep(0.15)
    click(x, y, pause)


def type_(text, per_char=0.0):
    ctl("type", text)


def key(*combos):
    ctl("key", *combos)


def shot(path, output="HEADLESS-1"):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    sh([str(H / "harness" / "ctl"), "screenshot", output, str(path)])
    return path


def dolly(*args):
    return sh([DOLLY, *map(str, args)])


# ---------------------------------------------------------------- test feeds
def setup_feeds():
    """Labelled test feeds + harness-only overrides for the dolly launcher."""
    pa = lambda *a: sh(["pactl", *a]).stdout.strip()  # noqa: E731
    if "dolly_test_mic" not in pa("list", "short", "sources"):
        pa("load-module", "module-null-sink", "sink_name=dolly_test_micfeed",
           "sink_properties=device.description=Test-feed-mic-input")
        pa("load-module", "module-remap-source", "master=dolly_test_micfeed.monitor", "source_name=dolly_test_mic",
           "source_properties=device.description=Test-feed:mic")
    feeds = [
        bg(["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i",
            "sine=frequency=660:sample_rate=48000,volume='if(lt(mod(t,0.9),0.45),0.8,0)':eval=frame",
            "-f", "pulse", "-device", "dolly_test_micfeed", "testmic"]),
        bg(["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i",
            "aevalsrc=0.25*sin(2*PI*220*t)+0.2*sin(2*PI*277*t)+0.2*sin(2*PI*330*t):s=48000",
            "-f", "pulse", "-device", "harness_speakers", "testsys"]),
    ]
    (RT / "dolly").mkdir(parents=True, exist_ok=True)
    (RT / "dolly" / "harness.env").write_text(
        "DOLLY_EVENTS_FROM_HARNESS=1\n"
        f"DOLLY_BROWSER={REPO}/harness/chrome-wayland\n"
        "DOLLY_WEBCAM_INPUT=lavfi:testsrc2=size=1280x720:rate=30\n")
    return feeds

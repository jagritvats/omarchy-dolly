#!/usr/bin/env python3
"""Real walkthrough of Dolly inside the Omarchy shell harness (TEST HARNESS ONLY).

Films HEADLESS-1 (1920x1200 @1.25) with an outer wf-recorder + a Dolly event logger,
while this script drives the real shell with real (virtual) pointer/keyboard input:
bar widget -> panel -> pick mode/audio/camera -> 3-2-1 -> type in foot -> stop ->
editor (Chromium inside sway) -> Library -> Settings -> re-export -> light/dark theme
-> Omarchy menu rows -> live bubble. Then Dolly finalizes and polishes the outer
recording itself (auto-zoom, chapters, idle speed-ups) into 16:9 and 9:16 exports.

Labelled test feeds: camera = ffmpeg testsrc2 pattern, mic = 660 Hz beeps, system
audio = a synthetic chord. Clicks/keys reach Dolly from the harness input driver
(harness/dctl), not from Hyprland. Usage: python3 harness/walkthrough.py OUT_DIR
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wt  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/claude/work/dolly-demo/walkthrough")
SHOTS = OUT / "screenshots"
OUTER = OUT / "outer-project" / time.strftime("%Y-%m-%d_%H-%M-%S")
CHAPTERS: list[tuple[float, str]] = []
NOTES: list[str] = []
SCALE = 1.25


def chapter(text):
    CHAPTERS.append((time.monotonic(), text))
    print(f"[{time.strftime('%T')}] chapter: {text}", flush=True)


def shot(name):
    p = wt.shot(SHOTS / f"{name}.png")
    print("  shot", name, flush=True)
    return p


def wait(s):
    time.sleep(s)


def close_panel():
    wt.sh(["omarchy-shell", "io.github.jagritvats.dolly", "close"])
    time.sleep(0.5)


def open_panel(pause=2.2):
    close_panel()
    wt.glide_click(1405, 13, pause)


# ---------------------------------------------------------------- editor (Chromium) helpers
def cdp_page():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    for _ in range(60):
        try:
            b = pw.chromium.connect_over_cdp("http://127.0.0.1:9333")
            pages = [p for c in b.contexts for p in c.pages if "127.0.0.1" in p.url]
            if pages:
                return pw, b, pages[0]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("editor window did not appear")


def editor_window():
    tree = json.loads(wt.sh(["swaymsg", "-t", "get_tree", "-r"]).stdout)
    found = []

    def walk(n):
        if n.get("name") and "Dolly" in str(n.get("name")) and n.get("pid"):
            found.append(n)
        for k in ("nodes", "floating_nodes"):
            for c in n.get(k, []):
                walk(c)
    walk(tree)
    return found[0] if found else None


def close_stray_browser():
    """omarchy-theme-set's browser hook opens a Chrome window in the harness; not part of Dolly."""
    tree = json.loads(wt.sh(["swaymsg", "-t", "get_tree", "-r"]).stdout)

    def walk(n):
        name = str(n.get("name") or "")
        if n.get("pid") and n.get("app_id") and "chrom" in str(n.get("app_id")).lower() and "Dolly" not in name:
            wt.sh(["swaymsg", f"[con_id={n['id']}] kill"])
        for k in ("nodes", "floating_nodes"):
            for c in n.get(k, []):
                walk(c)
    walk(tree)
    # and any Chrome process not started for Dolly's editor (its own --user-data-dir)
    for pid in wt.sh(["pgrep", "-f", "chrome"]).stdout.split():
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace")
        except OSError:
            continue
        try:      # only browsers started inside the harness session, never anyone else's
            envb = open(f"/proc/{pid}/environ", "rb").read().decode(errors="replace")
        except OSError:
            continue
        in_harness = f"XDG_RUNTIME_DIR={wt.ENV['XDG_RUNTIME_DIR']}" in envb.split("\0")
        if in_harness and "dolly-chrome" not in cmd and ("chrome" in cmd.split("\0")[0]):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass


def el_click(page, selector, pause=0.8, glide=True):
    """Real pointer click on a DOM element of the editor (CDP only measures where it is)."""
    box = page.locator(selector).first.bounding_box()
    win = editor_window()
    if not box or not win:
        raise RuntimeError(f"cannot locate {selector}")
    r = win["rect"]
    x = r["x"] + box["x"] + box["width"] / 2
    y = r["y"] + box["y"] + box["height"] / 2
    (wt.glide_click if glide else wt.click)(int(x), int(y), pause)


def side_scroll(page, selector):
    """Scroll the editor's side panel (never the page) so `selector` is near the top."""
    page.evaluate("s => { const e = document.querySelector(s), p = e.closest('.side') || e.closest('#settingsView');"
                  " if (p) p.scrollTop += e.getBoundingClientRect().top - p.getBoundingClientRect().top - 20;"
                  " window.scrollTo(0, 0); document.documentElement.scrollLeft = 0; document.body.scrollLeft = 0 }", selector)
    time.sleep(0.6)


def el_point(page, selector, fx=0.5, fy=0.5):
    box = page.locator(selector).first.bounding_box()
    r = editor_window()["rect"]
    return int(r["x"] + box["x"] + box["width"] * fx), int(r["y"] + box["y"] + box["height"] * fy)


# ---------------------------------------------------------------- outer capture
def start_outer():
    OUTER.mkdir(parents=True, exist_ok=True)
    raw = OUTER / "raw.mkv"
    rec = subprocess.Popen(["wf-recorder", "-o", "HEADLESS-1", "-r", "30", "-c", "libx264", "-p", "preset=ultrafast",
                            "-p", "crf=20", "-x", "yuv420p", "-f", str(raw)], env=wt.ENV,
                           stdout=open(OUTER / "recorder.log", "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    (wt.RT / "dolly" / "ignore-recorders").write_text(f"{rec.pid}\n")   # Dolly may record next to the film camera
    for _ in range(100):
        if raw.exists() and raw.stat().st_size > 0:
            break
        time.sleep(0.05)
    meta = {"backend": "wf", "region": [0, 0, 1536, 960], "scale": SCALE, "monitor": "HEADLESS-1", "fps": 30,
            "target_info": {"kind": "monitor", "monitor": "HEADLESS-1", "region": [0, 0, 1536, 960], "scale": SCALE,
                            "exact": [1536.0, 960.0], "physical": [1920, 1200]},
            "cursor_baked": True, "mode": "screen", "started": time.time(), "t0_mono": time.monotonic(),
            "clock_wall": time.time(), "clock_mono": time.monotonic(), "recorder_pid": rec.pid, "cursor_size": 24,
            "container": "mkv", "note": "walkthrough film camera (outer wf-recorder); events from harness/dctl"}
    (OUTER / "capture.json").write_text(json.dumps(meta, indent=2))
    env = dict(wt.ENV, DOLLY_EVENTS_FROM_HARNESS="1", PYTHONPATH=str(wt.REPO))
    lg = subprocess.Popen([sys.executable, "-m", "dolly.cli", "log-events", str(OUTER)], env=env,
                          stdout=open(OUTER / "logger.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    return rec, lg


def stop_outer(rec, lg):
    os.kill(lg.pid, signal.SIGTERM)
    lg.wait(15)
    rec.send_signal(signal.SIGINT)
    rec.wait(30)
    (wt.RT / "dolly" / "ignore-recorders").unlink(missing_ok=True)


# ---------------------------------------------------------------- the walkthrough
def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    # ---- preparation (not part of the story)
    wt.sh("pkill -f 'dolly-chrome' ; pkill -x foot ; pkill -x mpv ; true")
    wt.sh(["omarchy-shell", "notifications", "dismissAll"])
    wt.sh(["omarchy-theme-set", "tokyo-night"])
    wt.setup_feeds()
    wt.dolly("config", "set", "audio.mic_device=dolly_test_mic", "look.follow_theme=true", "look.background=theme-glow",
             "look.cursor_size=1.5", "capture.countdown=3", "capture.fps=30", "camera.size=M",
             "camera.corner=bottom-right", "zoom.idle_speedup=false")
    # sway stand-in for Dolly's Hyprland window rule (DollyCam: float, pin, no focus, corner)
    wt.sh(["swaymsg", 'for_window [app_id="^DollyCam"] floating enable, sticky enable, '
                      'resize set 260 260, move position 1236 626'])
    seeds = {"2026-11-09_18-30-22": "Onboarding flow (first take)", "2026-11-09_18-32-58": "Deploy script, terminal",
             "2026-11-09_18-33-43": "Theme switcher demo", "2026-11-09_18-36-35": "Bug repro: settings dialog",
             "2026-11-09_18-39-29": "Region on the 1.6667x output", "2026-11-09_18-40-10": "Release notes, region"}
    for d, title in seeds.items():
        if (wt.HOME / "Videos/Dolly" / d).exists():
            wt.dolly("library", "rename", str(wt.HOME / "Videos/Dolly" / d), title)
    wt.dolly("library", "--json")
    # the theme switch above fires omarchy-theme-set's browser hook a few seconds later
    for _ in range(3):
        time.sleep(2.5)
        close_stray_browser()
    wt.ctl("move", 760, 480)
    time.sleep(2)

    rec, lg = start_outer()
    time.sleep(1.5)
    try:
        story()
    finally:
        time.sleep(1.0)
        stop_outer(rec, lg)
    polish()


def story():
    chapter("Dolly lives in the Omarchy bar")
    wt.move(1300, 200, dur=0.8)
    wt.move(1405, 13, dur=0.7)
    wait(1.6)
    shot("01-bar-widget-tooltip")
    close_panel()
    wt.click(1405, 13, 2.2)
    shot("02-panel-recent")

    chapter("Pick what to record: screen, mic + system, camera")
    wt.glide_click(1222, 142, 0.8)
    wt.glide_click(1398, 218, 2.2)
    shot("03-panel-audio-meters")
    wt.glide_click(1278, 357, 2.0)
    shot("04-panel-camera")

    chapter("Start: 3-2-1 with a camera preview")
    wt.glide_click(1341, 542, 1.2)
    shot("05-countdown-camera-preview")
    wait(3.0)

    t0 = time.time()
    while time.time() - t0 < 15 and json.loads(wt.dolly("status", "--json").stdout or "{}").get("phase") != "recording":
        time.sleep(0.3)
    chapter("Type in a terminal on the 1.25x output")
    wt.ctl("term")
    wait(2.2)
    wt.ctl("click", 600, 400)
    wait(0.4)
    wt.type_("cd ~/Videos/Dolly && ls | tail -3")
    wt.key("Return")
    wait(1.2)
    wt.type_("printf 'step %s\\n' build test ship")
    wt.key("Return")
    wait(1.2)
    wt.key("ctrl+l")
    wait(0.5)
    wt.type_("git --version")
    wt.key("Return")
    wait(1.5)
    shot("06-recording-bar-timer")

    chapter("Right-click the timer: pause, mute mic, stop")
    wt.move(1405, 13)
    wait(0.3)
    wt.ctl("click", 1405, 13, "right")
    wait(1.8)
    shot("07-recording-controls")
    close_panel()
    wt.sh(["swaymsg", "[app_id=foot] kill"])
    wait(1.0)

    chapter("Stop with the same button: the editor opens")
    wt.glide_click(1405, 13, 0.5)
    t0 = time.time()
    while time.time() - t0 < 60 and not editor_window():
        time.sleep(0.5)
    pw, b, page = cdp_page()
    page.wait_for_selector("#tlwrap .it", timeout=90000)
    wait(2.0)
    win = editor_window()
    wt.sh(["swaymsg", f'[con_id={win["id"]}] focus'])
    wait(0.8)
    shot("08-editor-timeline-zooms")

    chapter("Editor: auto zooms, camera bubble, separate audio tracks")
    el_click(page, "#play", 3.5)
    el_click(page, "#play", 0.8)
    side_scroll(page, "#camGrp")
    el_click(page, '#camShape .opt[data-v="rounded"]', 1.0)
    el_click(page, '#camSizes .opt[data-v="0.30"]', 1.0)
    # drag the bubble to the top-left of the preview
    if page.evaluate("P && P.has_webcam && S.style.webcam.enabled"):
        g = page.evaluate("(()=>{const r=cv.getBoundingClientRect(), g=camGeom(vid.currentTime);"
                          "return [r.x+(g.x+g.w/2)/cv.width*r.width, r.y+(g.y+g.h/2)/cv.height*r.height, r.x, r.y]})()")
        wr = editor_window()["rect"]
        sx, sy = wr["x"] + g[0], wr["y"] + g[1]
        wt.move(int(sx), int(sy), dur=0.4)
        wt.ctl("press")
        wt.move(int(wr["x"] + g[2] + 140), int(wr["y"] + g[3] + 110), dur=0.9)
        wt.ctl("release")
        wait(1.0)
    side_scroll(page, "#audioGrp")
    shot("09-editor-camera-audio")

    chapter("Library: find previous recordings")
    el_click(page, '#nav .tab[data-v="library"]', 2.5)
    shot("10-library")
    el_click(page, "#lq", 0.3)
    wt.type_("deploy")
    wait(1.5)
    shot("11-library-search")
    page.fill("#lq", "")
    page.dispatch_event("#lq", "input")
    wait(0.8)

    chapter("Settings: own look, bigger cursor")
    el_click(page, '#nav .tab[data-v="settings"]', 2.0)
    el_click(page, '#snav div[data-g="look"]', 1.2)
    el_click(page, "#s_look_follow_theme", 0.8)
    page.select_option("#s_look_background", "theme-aurora")
    page.dispatch_event("#s_look_background", "change")
    wait(0.6)
    x, y = el_point(page, "#s_look_cursor_size", 0.82, 0.5)
    wt.glide_click(x, y, 1.2)
    shot("12-settings-look")

    chapter("Apply it to this recording and re-export")
    el_click(page, "#tabEdit", 2.5)
    side_scroll(page, "#bgs")
    el_click(page, "#useSettingsLook", 2.5)
    shot("13-editor-settings-look-applied")
    el_click(page, "#exportTop", 1.0)
    t0 = time.time()
    while time.time() - t0 < 240:
        txt = page.inner_text("#progtxt")
        if txt.startswith("Done") or txt.startswith("Failed"):
            break
        time.sleep(1.0)
    NOTES.append(f"re-export: {page.inner_text('#progtxt')} in {time.time() - t0:.0f} s")
    shot("14-editor-exported")

    chapter("Follows the Omarchy theme, light and dark")
    wt.sh(["omarchy-theme-set", "catppuccin-latte"])
    wait(4.0)
    close_stray_browser()
    wait(2.0)
    close_stray_browser()
    wait(1.0)
    shot("15-light-theme-editor")
    el_click(page, '#nav .tab[data-v="library"]', 2.0)
    shot("15b-light-theme-library")
    el_click(page, '#nav .tab[data-v="settings"]', 2.0)
    shot("15c-light-theme-settings")
    el_click(page, "#tabEdit", 2.5)
    close_stray_browser()
    open_panel()
    shot("16-light-theme-panel")
    close_panel()
    wt.sh(["omarchy-theme-set", "tokyo-night"])
    wait(4.0)
    close_stray_browser()
    wait(2.0)
    shot("17-dark-theme-editor")

    chapter("Omarchy menu: Trigger > Capture")
    try:
        b.close()
        pw.stop()
    except Exception:
        pass
    # Super+Space runs `omarchy-menu`; the harness has no Hyprland key binds, so call it directly
    wt.bg(["omarchy-menu", "toggle", "trigger.capture"])
    wait(2.5)
    shot("18-omarchy-menu-capture")
    wt.key("Escape")
    wait(0.6)
    wt.key("Escape")
    wait(0.6)
    close_panel()

    chapter("Live bubble: on screen, recorded as seen")
    wt.sh("pkill -f dolly-chrome; true")
    close_stray_browser()
    wait(1.0)
    open_panel()
    wt.glide_click(1405, 357, 1.5)       # Live bubble
    shot("19-panel-live-bubble")
    wt.glide_click(1341, 542, 1.0)
    wait(4.5)
    shot("20-live-bubble-recording")
    wait(2.0)
    wt.glide_click(1405, 13, 4.0)
    wt.sh("pkill -f dolly-chrome; true")
    wt.dolly("config", "set", "camera.mode=edit")
    wait(1.0)


# Seconds of film per chapter (the story runs ~4.5 min; the film is 60-90 s). Idle
# stretches go fast first; a chapter with more action than its budget plays faster
# as a whole (the export shows the "N×" badge whenever it is sped up).
BUDGET = [5, 7, 6, 7, 4, 5, 9, 6, 7, 6, 9, 3, 9]
CAVEATS = [
    "Runs in the Omarchy shell harness: headless sway + fakehypr + llvmpipe, HEADLESS-1 1920x1200 at 1.25x, not a real Hyprland session.",
    "Camera = testsrc2 pattern (DOLLY_WEBCAM_INPUT), mic = 660 Hz beeps into a remap source, system audio = a synthetic chord into a null sink.",
    "Clicks and keys reach Dolly through the harness input driver (DOLLY_EVENTS_FROM_HARNESS=1), not the Hyprland Lua bridge.",
    "The live bubble is square and placed by a sway rule: Hyprland's rounding/border/no-focus rule from dolly-setup is not exercised here.",
    "The editor runs in Chrome on Wayland inside sway (DOLLY_BROWSER); one <select> in Settings was changed through the DOM, everything else is real pointer input.",
    "The foot error line in the terminal comes from the harness theme files, not from Dolly.",
    "Recorder: wf-recorder in the harness (no gpu-screen-recorder); the outer film is recorded by a second wf-recorder and polished by Dolly itself.",
]
MAX_SPEED = 24.0


def budget_speeds(chapters, auto, t0, t1, budget=BUDGET):
    """Speed segments that fit each chapter into its budget (source seconds in, out)."""
    bounds = [t0] + [c["t"] for c in chapters[1:]] + [t1]
    segs = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
        B = budget[min(i, len(budget) - 1)]
        idle = [(max(a, s["start"]), min(b, s["end"])) for s in auto if min(b, s["end"]) > max(a, s["start"])]
        idle_len = sum(y - x for x, y in idle)
        act = (b - a) - idle_len
        if act + idle_len / MAX_SPEED <= B:
            f, k = 1.0, min(MAX_SPEED, max(2.0, idle_len / max(B - act, 0.1))) if idle_len else 1.0
        else:
            k = MAX_SPEED
            f = min(MAX_SPEED, act / max(B - idle_len / k, 0.5))
        cur = a
        for x, y in idle:
            if x > cur and f > 1.01:
                segs.append({"start": round(cur, 3), "end": round(x, 3), "speed": round(f, 2), "reason": "edit"})
            segs.append({"start": round(x, 3), "end": round(y, 3), "speed": round(k, 2), "reason": "idle"})
            cur = y
        if b > cur and f > 1.01:
            segs.append({"start": round(cur, 3), "end": round(b, 3), "speed": round(f, 2), "reason": "edit"})
    for j, s in enumerate(segs):
        s["id"], s["source"] = f"s{j + 1}", "auto"
    return segs


def polish(outer=None):
    """Dolly polishes the film: finalize, chapters, a time budget per chapter, 16:9 + 9:16 exports."""
    outer = outer or OUTER
    sys.path.insert(0, str(wt.REPO))
    os.environ.update({k: v for k, v in wt.ENV.items() if k in ("HOME", "XDG_RUNTIME_DIR", "FONTCONFIG_FILE")})
    os.environ["DOLLY_DIR"] = str(outer.parent)
    os.environ["DOLLY_NO_BAR_SYNC"] = "1"
    from dolly import capture, project as projmod
    from dolly.render import export
    from dolly.timemap import SpeedConfig, cached_activity, detect_speedups
    from dolly.events import Timeline
    if CHAPTERS:
        doc = capture.finalize(outer)
        raw = [json.loads(l) for l in (outer / "events.raw.jsonl").read_text().splitlines() if l.strip()]
        t0m = next(e["t0_mono"] for e in raw if e.get("type") == "start")
        sync = doc["capture"]["sync"]
        a, bb = sync.get("a", 0.0), sync.get("b", 1.0)
        chapters = [{"t": round(a + bb * (m - t0m), 2), "text": text} for m, text in CHAPTERS]
    else:                                   # --polish: re-run on a finished story
        old = json.loads((OUT / "notes.json").read_text())
        chapters, sync = old["chapters"], old["sync"]
        NOTES.extend(old.get("notes", []))
    proj = projmod.Project(outer)
    data = proj.load()
    data["style"]["chapters"] = chapters
    data["style"]["output"] = {**data["style"].get("output", {}), "fps": 30, "width": 1920, "height": 1080}
    data["style"]["background"] = {"type": "gradient", "preset": "theme-glow"}
    data["style"]["title"] = {"enabled": True, "text": "Dolly for Omarchy", "subtitle": "a real walkthrough in the shell harness",
                              "intro": 2.2, "outro": 2.0, "outro_text": "Recorded with test feeds",
                              "outro_subtitle": "camera: testsrc2 pattern · mic: 660 Hz beeps · system: synthetic chord · clicks/keys: harness input driver"}
    tl = Timeline.load(proj.events)
    t0, t1 = data.get("trim") or [0.0, tl.duration]
    auto = detect_speedups(tl, cached_activity(proj.dir), SpeedConfig.from_dict({"idle_min": 1.6}))
    sp = budget_speeds(chapters, auto, t0, t1)
    data["speed"] = sp
    data["style"]["speed"] = {"enabled": True}
    proj.save(data)
    (OUT / "chapters.json").write_text(json.dumps(chapters, indent=2))
    st = {}
    main_out = export(outer, OUT / "dolly-walkthrough-16x9.mp4", "mp4", "high", stats=st, settings=False)
    st2 = {}
    vert = export(outer, OUT / "dolly-walkthrough-9x16.mp4", "mp4", "high", aspect="9:16", stats=st2, settings=False)
    import shutil
    shutil.copy(outer / "raw.mkv", OUT / "walkthrough-raw.mkv")
    (OUT / "notes.json").write_text(json.dumps({"caveats": CAVEATS, "notes": NOTES, "export_16x9": st, "export_9x16": st2,
                                                "chapters": chapters, "speedups": sp,
                                                "sync": sync, "click_source": "harness"},
                                               indent=2))
    print(main_out, vert)


if __name__ == "__main__":
    if "--polish" in sys.argv:              # walkthrough.py OUT --polish OUTER_PROJECT_DIR
        polish(Path(sys.argv[sys.argv.index("--polish") + 1]))
    else:
        main()

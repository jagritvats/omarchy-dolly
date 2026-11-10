#!/usr/bin/env python3
"""Scenario suite: synthetic Omarchy sessions with ground truth.

Each scenario writes a project directory exactly like a real recording
(raw.mp4 with no cursor, events.raw.jsonl in the logger's format and clock,
capture.json), plus truth.json, then runs Dolly's real `finalize` on it
(sync-marker detection, coordinate mapping, auto zoom).

    python3 harness/scenarios.py OUT_DIR [--only NAME ...] [--fps 30]

Scenarios: terminal-typing, build-log, tiling, floating-drag, workspaces,
interruptions, dual-monitor, window-mode, long-drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from make_demo import Script  # noqa: E402
from scene import App, Scene, Terminal, Win, tiles  # noqa: E402

SHELL_HISTORY = [("~/app $ git pull", "fg"), ("Already up to date.", "dim"), ("", "fg"),
                 ("~/app $ bin/dev", "fg"), ("13:58:02 web.1  | Puma starting in single mode...", "dim"),
                 ("13:58:02 web.1  | * Listening on http://127.0.0.1:3000", "dim"), ("^C", "dim"), ("", "fg")]


class Scenario:
    name = ""
    lw, lh, scale = 1280, 800, 1.5
    origin = (0, 0)
    duration = 20.0
    drift = (0.12, 1.0)        # logger clock = offset + rate * video time
    mode = "screen"
    target = None              # window-mode target address

    def __init__(self, fps=30):
        self.fps = fps
        self.sc = Scene(self.lw, self.lh, self.scale, self.origin)
        self.sc._sync_len = 2 / fps
        self.s = Script(seed=7)
        self.extra_events: list[dict] = []    # logger-format extras (layer, workspace, monitor) at video time
        self.truth_fns: list = []             # (t0, t1, fn(t) -> dict)
        self.build()
        self.duration = max(self.duration, self.s.t + 0.6)
        self.sc.sync_marks = [(0.25, "start"), (self.duration - 0.3, "stop")]

    # helpers ------------------------------------------------------------
    def type_into(self, entry_or_list, text, wpm=80):
        out = []
        self.s.type(text, wpm=wpm, out=out)
        keys = [(k["t"], k["ch"]) for k in out]
        if hasattr(entry_or_list, "keys"):
            entry_or_list.keys.extend(keys)
        else:
            entry_or_list.extend(keys)
        return keys

    def enter(self):
        self.s.t += 0.18
        self.s.keys.append({"t": self.s.t, "key": "Return", "mods": []})
        return self.s.t

    def run_cmd(self, term: Terminal, cmd, output, wpm=85, line_dt=0.05, after=0.6, cwd=None):
        e = term.prompt(self.s.t, cwd)
        t_first = self.s.t + 0.0
        self.s.wait(0.3, drift=False)
        self.type_into(e, cmd, wpm)
        e.enter = self.enter()
        t = e.enter + 0.12
        for line in output:
            text, col = (line, "fg") if isinstance(line, str) else line
            term.out(t, text, col)
            t += line_dt
        self.s.t = max(self.s.t, t) + after
        return t_first, e.enter, t

    def typing_truth(self, win: Win, t0, t1):
        sc = self.sc

        def fn(t):
            r, lh = sc.term_line_rect(t, win)
            if r is None:
                return None
            return {"aoi": r, "kind": "typing", "typing": True, "text_rect": r, "line_h": lh}
        self.truth_fns.append((t0, t1, fn))

    def output_truth(self, win: Win, t_enter, t_end, hold=0.8):
        """After Enter: the command line plus its output (until the next prompt)."""
        sc = self.sc

        def fn(t):
            rows, first, fit, caret, (ox, oy), (px, lh, cw) = sc.term_layout(t, win)
            # find the prompt row of this command: last prompt entered before t
            idx = None
            k = -1
            for e in win.content.entries:
                if e.t > t:
                    break
                k += 1
                if e.prompt is not None and e.enter is not None and e.enter <= t_enter + 1e-6:
                    idx = k
            if idx is None:
                return None
            r0 = max(idx, first) - first
            r1 = len(rows) - first
            width = max((sum(len(tx) for tx, _ in rows[i]) for i in range(max(idx, first), len(rows))), default=1)
            X0, Y0, X1, Y1 = sc.R(win.rect_at(t))
            rect = (ox, oy + r0 * lh, min(ox + (width + 1) * cw, X1), oy + r1 * lh)
            if rect[3] - rect[1] > (Y1 - Y0) * 0.6:
                rect = (X0, Y0, X1, Y1)
            return {"aoi": rect, "kind": "output", "typing": False, "line_h": lh}
        self.truth_fns.append((t_enter + 0.1, t_end + hold, fn))

    def rect_truth(self, t0, t1, rect_fn, kind="window"):
        self.truth_fns.append((t0, t1, lambda t: (lambda r: None if r is None else {"aoi": r, "kind": kind})(rect_fn(t))))

    def click_truth(self, t, row_rect_logical, half=150):
        """A click on a wide row: the viewer must see the row around the pointer, not its far end."""
        x0, y0, w, h = row_rect_logical
        cx = self.s.x
        a, b = max(x0, cx - half), min(x0 + w, cx + half)
        self.rect_truth(t - 0.3, t + 1.0, lambda _t: self.sc.R((a, y0, b - a, h)), "click")

    def win_rect_fn(self, win: Win):
        return lambda t: self.sc.R(win.rect_at(t))

    def chord(self, mods, key):
        self.s.chord(mods, key)
        return self.s.t

    def layer(self, t, ns, rect, open_, keyboard):
        self.extra_events.append({"t": t, "type": "layer", "namespace": ns, "open": open_, "keyboard": keyboard,
                                  "rect": [rect[0] + self.origin[0], rect[1] + self.origin[1], rect[2], rect[3]]
                                  if rect else None})

    def build(self):
        raise NotImplementedError

    # truth ---------------------------------------------------------------
    def truth_at(self, t):
        d = {"t": round(t, 4), "aoi": None, "kind": None, "typing": False, "text_rect": None, "line_h": None,
             "transition": False}
        for (a, b, fn) in self.truth_fns:
            if a <= t < b:
                r = fn(t)
                if r:
                    d.update(r)
        for tt, ws, style in self.sc.ws_switch:
            if style == "instant" and abs(t - tt) < 0.5 / self.fps:
                d["transition"] = True
            if style == "slide" and tt - 0.5 / self.fps <= t < tt + 0.3:
                d["transition"] = True
        if d["transition"]:
            d["aoi"] = None
        x, y = self.cursor_at(t)
        d["cursor_in"] = bool(0 <= x <= self.lw and 0 <= y <= self.lh)
        if d["aoi"] is not None:
            d["aoi"] = [round(float(v), 1) for v in d["aoi"]]
        if d["text_rect"] is not None:
            d["text_rect"] = [round(float(v), 1) for v in d["text_rect"]]
        return d

    def cursor_at(self, t):
        mt = self._mt
        i = int(np.searchsorted(mt, t, side="right"))
        i = min(max(i - 1, 0), len(mt) - 1)
        return self.s.moves[i][1], self.s.moves[i][2]

    # output --------------------------------------------------------------
    def write(self, out: Path):
        out.mkdir(parents=True, exist_ok=True)
        sc, fps = self.sc, self.fps
        self._mt = np.array([m[0] for m in self.s.moves])
        n = int(math.floor(self.duration * fps))
        enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                                "-s", f"{sc.W}x{sc.H}", "-r", str(fps), "-i", "-", "-c:v", "libx264",
                                "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(fps),
                                str(out / "raw.mp4")], stdin=subprocess.PIPE)
        truth = []
        last_hash, last = None, None
        for i in range(n):
            t = i / fps
            img = sc.render(t)
            enc.stdin.write(img.tobytes())
            truth.append(self.truth_at(t))
            if i % 90 == 0:
                sys.stderr.write(f"\r  {self.name}: frame {i}/{n}")
        sys.stderr.write("\n")
        enc.stdin.close()
        enc.wait()
        self._write_events(out, n / fps)
        clicks = [{"t": round(c["t"], 4), "x": c["x"] * sc.s, "y": c["y"] * sc.s} for c in self.s.clicks
                  if 0 <= c["x"] <= self.lw and 0 <= c["y"] <= self.lh]
        doc = {"name": self.name, "fps": fps, "W": sc.W, "H": sc.H, "scale": sc.s, "origin": list(self.origin),
               "drift": list(self.drift), "frames": truth, "clicks": clicks, "mode": self.mode,
               "sync_marks": [m[0] for m in sc.sync_marks], "duration": n / fps}
        (out / "truth.json").write_text(json.dumps(doc))
        return out

    def _write_events(self, out: Path, duration: float):
        sc = self.sc
        off, rate = self.drift
        ox, oy = self.origin
        clock = lambda t: round((off + rate * t) * 1000.0, 2)  # noqa: E731
        lines = []
        meta = {"backend": "synthetic", "region": [ox, oy, self.lw, self.lh], "scale": sc.s, "monitor": "DP-1",
                "fps": self.fps, "cursor_baked": False, "mode": self.mode, "cursor_size": 24,
                "terminals": ["Alacritty"], "sync": "marker"}
        if self.target:
            meta["target"] = self.target
        (out / "capture.json").write_text(json.dumps(meta, indent=2))
        lines.append({"type": "start", "ms": clock(0.0), **meta})
        lines.append({"type": "info", "ms": clock(0.0), "click_source": "lua"})
        for (t, x, y) in self.s.moves:
            if t <= duration:
                lines.append({"type": "move", "ms": clock(t), "x": round(x + ox, 2), "y": round(y + oy, 2)})
        for c in self.s.clicks:
            lines.append({"type": "click", "ms": clock(c["t"]), "x": round(c["x"] + ox, 2), "y": round(c["y"] + oy, 2),
                          "button": c["button"]})
        for k in self.s.keys:
            lines.append({"type": "key", "ms": clock(k["t"]), "key": k["key"], "mods": k["mods"]})
        # clients polled at 10 Hz, written on change (as the logger does)
        prev = None
        t = 0.0
        while t < duration:
            wins, focus = sc.clients_at(t)
            key = json.dumps([wins, focus], sort_keys=True)
            if key != prev:
                lines.append({"type": "clients", "ms": clock(t), "windows": wins, "focus": focus})
                prev = key
            t += 0.1
        for tt, ws, style in sc.ws_switch:
            lines.append({"type": "workspace", "ms": clock(tt), "name": ws, "special": False})
        for a, b in sc.special:
            lines.append({"type": "workspace", "ms": clock(a), "name": "special:scratch", "special": True})
            lines.append({"type": "workspace", "ms": clock(b), "name": sc.ws_at(b), "special": False})
        for e in self.extra_events:
            e2 = dict(e)
            tt = e2.pop("t")
            lines.append({"ms": clock(tt), **e2})
        for tm, kind in sc.sync_marks:
            lines.append({"type": "sync", "ms": clock(tm), "kind": kind})
        lines.append({"type": "stop", "ms": clock(duration)})
        lines.sort(key=lambda d: d["ms"])
        with open(out / "events.raw.jsonl", "w") as f:
            for d in lines:
                f.write(json.dumps(d) + "\n")


def seeded_terminal(cwd="~/app"):
    term = Terminal(cwd)
    for text, col in SHELL_HISTORY:
        term.out(0.0, text, col)
    return term


# ====================================================================== scenarios
class TerminalTyping(Scenario):
    """Typing in a terminal with the mouse parked far away; browser spinner + bar clock as noise."""
    name = "terminal-typing"

    def build(self):
        sc, s = self.sc, self.s
        L, R = tiles(2, self.lw, self.lh)
        self.term = term = seeded_terminal()
        tw = sc.add(Win("t1", term, keys=[(0, L)], title="~/app"))
        sc.add(Win("b1", App("Docs - Rails Guides", spinner=True), keys=[(0, R)]), focus=False)
        sc.set_focus(0, "t1")
        s.x, s.y = 900, 500
        s.moves = [(0.0, s.x, s.y)]
        s.wait(0.8)
        s.move(300, 420)
        s.click()
        s.wait(0.3, drift=False)
        s.move(600, 770, speed=1.2)          # park the mouse in the far corner of the terminal
        s.wait(0.8, drift=False)
        t0, te, tend = self.run_cmd(term, "git status", [
            "On branch feature/assignees", "Your branch is ahead of 'origin/main' by 2 commits.", "",
            "Changes not staged for commit:", ("        modified:   app/models/task.rb", "red"),
            ("        modified:   app/views/tasks/_form.html.erb", "red"), ""], line_dt=0.04, after=1.2)
        self.typing_truth(tw, t0 + 0.3, te + 0.05)
        self.output_truth(tw, te, tend)
        t0, te, tend = self.run_cmd(term, "bin/rails test test/models/task_test.rb", [
            "Running 12 tests in parallel using 8 processes", "Run options: --seed 40211", "",
            ("............", "green"), "", "Finished in 0.84s, 14.3 runs/s",
            ("12 runs, 31 assertions, 0 failures, 0 errors, 0 skips", "green")], line_dt=0.2, after=1.4)
        self.typing_truth(tw, t0 + 0.3, te + 0.05)
        self.output_truth(tw, te, tend)
        t0, te, tend = self.run_cmd(term, 'git commit -am "Add task assignees"', [
            "[feature/assignees 4f1c2ab] Add task assignees", " 2 files changed, 31 insertions(+), 4 deletions(-)"],
            line_dt=0.15, after=1.5)
        self.typing_truth(tw, t0 + 0.3, te + 0.05)
        self.output_truth(tw, te, tend)
        s.wait(1.0, drift=False)


class BuildLog(Scenario):
    """A fast-scrolling build log: frame the whole window, don't chase lines."""
    name = "build-log"

    def build(self):
        sc, s = self.sc, self.s
        L, R = tiles(2, self.lw, self.lh)
        term = seeded_terminal("~/web")
        tw = sc.add(Win("t1", term, keys=[(0, L)]))
        sc.add(Win("e1", App("main.ts - editor"), keys=[(0, R)]), focus=False)
        sc.set_focus(0, "t1")
        s.x, s.y = 320, 700
        s.moves = [(0.0, s.x, s.y)]
        s.wait(1.0)
        rng = np.random.default_rng(3)
        mods = ["components/TaskList", "components/TaskForm", "lib/api", "pages/index", "pages/settings",
                "hooks/useTasks", "lib/date", "styles/app", "components/Avatar", "lib/auth"]
        out = []
        for i in range(140):
            m = mods[i % len(mods)]
            out.append((f"  transforming src/{m}.tsx ({int(rng.integers(2, 90))} kB) ...", "dim"))
        out += [("✓ 1402 modules transformed.", "green"), "dist/index.html       0.46 kB",
                "dist/assets/index.css  8.12 kB", "dist/assets/index.js  412.70 kB", ("✓ built in 5.12s", "green"), ""]
        t0, te, tend = self.run_cmd(term, "npm run build", out, line_dt=0.035, after=1.0)
        self.typing_truth(tw, t0 + 0.3, te + 0.05)
        win_inner = lambda t: sc.R(tw.rect_at(t))  # noqa: E731
        self.rect_truth(te + 0.3, tend - 0.2, win_inner, "scroll")
        # click into the terminal right on the prompt line, then type: the pointer sits on the text
        s.move(120, 771, speed=0.9)
        s.click()
        s.wait(0.3, drift=False)
        t0, te, tend = self.run_cmd(term, "npm test -- --run", [
            " RUN  v1.6.0 ~/web", (" ✓ src/lib/date.test.ts (6)", "green"), (" ✓ src/lib/api.test.ts (11)", "green"),
            "", (" Test Files  2 passed (2)", "green"), "      Tests  17 passed (17)"], line_dt=0.18, after=1.4)
        self.typing_truth(tw, t0 + 0.3, te + 0.05)
        self.output_truth(tw, te, tend)


class Tiling(Scenario):
    """Open / close / resize tiled windows."""
    name = "tiling"
    duration = 24.0

    def build(self):
        sc, s = self.sc, self.s
        T1, T2, T3 = tiles(1, self.lw, self.lh), tiles(2, self.lw, self.lh), tiles(3, self.lw, self.lh)
        app = App("Tasks")
        aw = sc.add(Win("a1", app, keys=[(0, T1[0])]))
        s.x, s.y = 700, 400
        s.moves = [(0.0, s.x, s.y)]
        s.wait(1.0)
        # click a row in the app
        s.move(520, 3 + 36 + 50 + 38 * 2 + 15)
        s.click()
        app.highlights.append((s.t, 99, 2))
        self.click_truth(s.t, (T1[0][0] + 180 + 14, 36 + 2 + 50 + 76, T1[0][2] - 180 - 28, 30))
        s.wait(1.4)
        # Super+Return: a terminal opens, layout animates to two tiles
        t_open = self.chord(["super"], "Return") + 0.05
        term = seeded_terminal()
        tw = sc.add(Win("t1", term, keys=[(t_open, T2[1])], open_t=t_open))
        aw.keys.append((t_open, T2[0]))
        self.rect_truth(t_open + 0.35, t_open + 1.4, lambda t: sc.R(tw.settled_rect(t)), "open")
        s.wait(0.9, drift=False)
        s.move(1000, 600, speed=0.8)          # focus follows the mouse into the new terminal
        t0, te, tend = self.run_cmd(term, "ls app/models", ["application_record.rb  assignee.rb  task.rb  user.rb"],
                                    line_dt=0.1, after=1.0)
        self.typing_truth(tw, t0 + 0.3, te + 0.05)
        self.output_truth(tw, te, tend)
        # a third window (browser) opens bottom-right
        t_open2 = self.chord(["super", "shift"], "b") + 0.15
        bw = sc.add(Win("b1", App("localhost:3000", spinner=True), keys=[(t_open2, T3[2])], open_t=t_open2))
        tw.keys.append((t_open2, T3[1]))
        self.rect_truth(t_open2 + 0.35, t_open2 + 1.5, lambda t: sc.R(bw.settled_rect(t)), "open")
        s.wait(1.2, drift=False)
        s.move(1000, 640)
        s.click()
        self.rect_truth(s.t - 0.3, s.t + 1.0, lambda t: sc.R(bw.rect_at(t)), "click")
        s.wait(1.4)
        # close it: layout goes back to two tiles; the terminal gets focus back
        t_close = self.chord(["super"], "w") + 0.05
        bw.close_t = t_close
        tw.keys.append((t_close, T2[1]))
        sc.set_focus(t_close, "t1")
        self.rect_truth(t_close + 0.35, t_close + 1.3, lambda t: sc.R(tw.settled_rect(t)), "close")
        s.wait(1.6)
        # drag the split between the windows to the right (resize_on_border)
        bx = T2[1][0] - 3
        s.move(bx, 420, speed=1.0)
        s.wait(0.2, drift=False)
        s.click()
        t_r0 = s.t
        steps = 24
        for k in range(1, steps + 1):
            nx = bx + 180 * k / steps
            s._move_to(nx, 420, 1.2 / steps, curve=0)
            Lr = (T2[0][0], T2[0][1], nx - 2 - T2[0][0], T2[0][3])
            Rr = (nx + 8, T2[1][1], self.lw - 10 - nx - 8, T2[1][3])
            aw.keys.append((s.t, Lr))
            tw.keys.append((s.t, Rr))
        aw.anim = tw.anim = 0.08
        self.rect_truth(t_r0, s.t + 0.6, lambda t: sc.R((10, 36, self.lw - 20, self.lh - 46)), "resize")
        s.wait(1.5)


class FloatingDrag(Scenario):
    """Super+drag a floating window across the screen, then resize it."""
    name = "floating-drag"

    def build(self):
        sc, s = self.sc, self.s
        T1 = tiles(1, self.lw, self.lh)
        sc.add(Win("t1", seeded_terminal(), keys=[(0, T1[0])]))
        r0 = (120, 140, 420, 300)
        fw = sc.add(Win("f1", App("Settings", rows=["Display", "Sound", "Network", "Power"]), floating=True,
                        keys=[(0, r0)], title="Settings"))
        fw.content.cls = "org.gnome.Settings"
        s.x, s.y = 330, 290
        s.moves = [(0.0, s.x, s.y)]
        s.wait(1.2)
        s.chord(["super"], "SUPER_L")
        s.click()
        t_d0 = s.t
        x, y = r0[0], r0[1]
        steps = 50
        sx0, sy0 = s.x, s.y
        for k in range(1, steps + 1):
            u = k / steps
            e = u ** 3 * (10 - 15 * u + 6 * u * u)
            nx, ny = sx0 + 560 * e, sy0 + 160 * e
            s._move_to(nx, ny, 1.8 / steps, curve=0)
            fw.keys.append((s.t, (x + (nx - sx0), y + (ny - sy0), 420, 300)))
        t_d1 = s.t
        s.wait(0.8, drift=False)
        # Super+right-drag: resize from the bottom-right
        s.click("right")
        rx, ry, rw, rh = fw.keys[-1][1]
        steps = 30
        sx0, sy0 = s.x, s.y
        for k in range(1, steps + 1):
            u = k / steps
            e = u * u * (3 - 2 * u)
            s._move_to(sx0 + 120 * e, sy0 + 90 * e, 1.0 / steps, curve=0)
            fw.keys.append((s.t, (rx, ry, rw + 180 * e, rh + 120 * e)))
        t_r1 = s.t
        s.wait(0.6, drift=False)
        rr = fw.keys[-1][1]
        self.rect_truth(t_d0, t_r1 + 0.6, lambda t: sc.R(fw.rect_at(t)), "window")
        s.move(rr[0] + 300, rr[1] + 140)
        s.click()
        self.click_truth(s.t, (rr[0] + 120, s.y - 18, rr[2] - 140, 36))
        s.wait(2.0)


class Workspaces(Scenario):
    """Dialog in an app (the old 09b case), instant workspace switch, special workspace, slide back."""
    name = "workspaces"
    duration = 26.0

    def build(self):
        sc, s = self.sc, self.s
        T1 = tiles(1, self.lw, self.lh)
        app = App("Tasks")
        aw = sc.add(Win("a1", app, keys=[(0, T1[0])]))
        term = seeded_terminal()
        tw = sc.add(Win("t1", term, ws="2", keys=[(0, T1[0])]), focus=False)
        sp = Terminal("~")
        spw = sc.add(Win("s1", sp, ws="special", floating=True, keys=[(0, (160, 120, 960, 560))]), focus=False)
        s.x, s.y = 640, 420
        s.moves = [(0.0, s.x, s.y)]
        s.wait(1.0)
        # "+ New task" at the top-right opens a centred dialog: the whole dialog is the subject
        s.move(1190, 64)
        s.click()
        dlg = {"t0": s.t + 0.1, "t1": 1e9, "rect": (360, 200, 520, 220), "title": "New task", "keys": []}
        app.dialogs.append(dlg)
        s.wait(0.6, drift=False)
        s.move(700, 290)
        s.click()
        s.wait(0.3, drift=False)
        self.type_into(dlg["keys"], "Record the launch demo", wpm=80)
        s.wait(0.4, drift=False)
        s.move(860, 410)
        s.click()
        dlg["t1"] = s.t + 0.1
        self.rect_truth(dlg["t0"] + 0.2, dlg["t1"], lambda t: sc.dialog_rect(t, aw), "dialog")
        s.wait(1.2)
        # Super+2: instant switch (Omarchy default: workspace animations off)
        t_sw = self.chord(["super"], "2")
        sc.ws_switch.append((t_sw, "2", "instant"))
        sc.set_focus(t_sw, "t1")
        s.wait(0.8, drift=False)
        t0, te, tend = self.run_cmd(term, "tail -n 3 log/development.log", [
            ("Started GET \"/tasks\" for 127.0.0.1", "dim"), ("Completed 200 OK in 12ms", "green"), ""],
            line_dt=0.1, after=1.2)
        self.typing_truth(tw, t0 + 0.3, te + 0.05)
        self.output_truth(tw, te, tend)
        # special workspace (scratchpad terminal slides down)
        t_sp = self.chord(["super"], "s")
        sc.special.append((t_sp, 1e9))
        spw.open_t = t_sp
        sc.set_focus(t_sp, "s1")
        s.wait(0.6, drift=False)
        t0, te, tend = self.run_cmd(sp, "uptime", [" 14:05:12 up 3 days,  2:41,  1 user,  load average: 0.42"],
                                    line_dt=0.1, after=1.0, cwd="~")
        self.typing_truth(spw, t0 + 0.3, te + 0.05)
        self.output_truth(spw, te, tend)
        t_spc = self.chord(["super"], "s")
        sc.special[-1] = (t_sp, t_spc)
        spw.close_t = t_spc + 0.2
        sc.set_focus(t_spc, "t1")
        s.wait(1.0)
        # back to 1 with a slide (a user who enabled workspace animations)
        t_sw2 = self.chord(["super"], "1")
        sc.ws_switch.append((t_sw2, "1", "slide"))
        sc.set_focus(t_sw2, "a1")
        s.wait(0.8, drift=False)
        s.move(520, 3 + 36 + 50 + 38 * 3 + 15)
        s.click()
        app.highlights.append((s.t, 99, 3))
        self.click_truth(s.t, (T1[0][0] + 180 + 14, 36 + 2 + 50 + 114, T1[0][2] - 180 - 28, 30))
        s.wait(1.6)


class Interruptions(Scenario):
    """Notifications, a context menu and the Omarchy menu over content."""
    name = "interruptions"
    duration = 24.0

    def build(self):
        sc, s = self.sc, self.s
        L, R = tiles(2, self.lw, self.lh)
        app = App("Tasks")
        aw = sc.add(Win("a1", app, keys=[(0, L)]))
        term = seeded_terminal()
        tw = sc.add(Win("t1", term, keys=[(0, R)]), focus=False)
        sc.set_focus(0, "a1")
        s.x, s.y = 400, 500
        s.moves = [(0.0, s.x, s.y)]
        s.wait(1.0)
        s.move(330, 64 + 26)
        s.click()
        dlg = {"t0": s.t + 0.1, "t1": 1e9, "rect": (40, 160, 520, 220), "title": "Rename list", "keys": []}
        app.dialogs.append(dlg)
        s.wait(0.5, drift=False)
        s.move(280, 290)
        s.click()
        s.wait(0.3, drift=False)
        # a notification lands top-right while typing: must not pull the camera
        n1 = {"t0": s.t + 0.6, "t1": s.t + 4.6, "title": "Slack · #launch", "body": "Priya: demo looks great!"}
        sc.notifs.append(n1)
        self.layer(n1["t0"], "notifications", sc.notif_rect(n1, n1["t0"] + 1), True, False)
        self.layer(n1["t1"], "notifications", None, False, False)
        self.type_into(dlg["keys"], "Launch checklist for Thursday", wpm=70)
        s.wait(0.4, drift=False)
        s.move(480, 410)
        s.click()
        dlg["t1"] = s.t + 0.1
        self.rect_truth(dlg["t0"] + 0.2, dlg["t1"], lambda t: sc.dialog_rect(t, aw), "dialog")
        s.wait(1.4)
        # right-click context menu near the pointer
        s.move(300, 330)
        s.click("right")
        pop = {"t0": s.t + 0.05, "t1": s.t + 1.6, "x": s.x + 4, "y": s.y + 4, "items": ["Edit", "Duplicate", "Move to…", "Delete"]}
        sc.popups.append(pop)
        s.wait(0.6, drift=False)
        s.move(pop["x"] + 60, pop["y"] + 12 + 30 * 1, speed=1.4)
        s.click()
        pop["t1"] = s.t + 0.05
        self.rect_truth(pop["t0"] + 0.1, pop["t1"], lambda t: sc.R(sc.popup_rect(pop)), "popup")
        s.wait(1.4)
        # Omarchy menu (Super+Space): a keyboard layer in the middle
        t_m = self.chord(["super"], "space")
        m = {"t0": t_m + 0.05, "t1": 1e9, "keys": [], "items": ["Apps", "Learn", "Trigger", "Style", "Setup",
                                                                  "Install", "Remove", "Update", "System"]}
        sc.menus.append(m)
        self.layer(m["t0"], "omarchy-menu", sc.menu_rect(), True, True)
        s.wait(0.6, drift=False)
        self.type_into(m["keys"], "trig", wpm=60)
        s.wait(0.5, drift=False)
        m["t1"] = self.enter() + 0.05
        self.layer(m["t1"], "omarchy-menu", None, False, True)
        self.rect_truth(m["t0"] + 0.15, m["t1"], lambda t: sc.R(sc.menu_rect()), "menu")
        s.wait(0.6, drift=False)
        n2 = {"t0": s.t, "t1": s.t + 3.5, "title": "Screenshot saved", "body": "~/Pictures/screenshot-1405.png"}
        sc.notifs.append(n2)
        self.layer(n2["t0"], "notifications", sc.notif_rect(n2, n2["t0"] + 1), True, False)
        self.layer(n2["t1"], "notifications", None, False, False)
        # meanwhile the user types in the terminal (focus follows the mouse)
        s.move(900, 500, speed=0.9)
        sc.set_focus(s.t, "t1")
        t0, te, tend = self.run_cmd(term, "git push", ["Everything up-to-date"], line_dt=0.1, after=1.6)
        self.typing_truth(tw, t0 + 0.3, te + 0.05)
        self.output_truth(tw, te, tend)


class DualMonitor(Scenario):
    """Recording DP-1 (2560x1600 at 1.6667, right of a 2560x1440 at 1.25 monitor).
    The pointer visits the other monitor (clicks there too); the other monitor is unplugged."""
    name = "dual-monitor"
    lw, lh, scale = 1536, 960, 5 / 3
    origin = (2048, 0)
    duration = 24.0

    def build(self):
        sc, s = self.sc, self.s
        L, R = tiles(2, self.lw, self.lh)
        app = App("Tasks")
        aw = sc.add(Win("a1", app, keys=[(0, L)]))
        term = seeded_terminal()
        tw = sc.add(Win("t1", term, keys=[(0, R)]), focus=False)
        sc.set_focus(0, "a1")
        s.x, s.y = 500, 500
        s.moves = [(0.0, s.x, s.y)]
        s.wait(1.0)
        s.move(260, 3 + 36 + 50 + 38 * 1 + 15)
        s.click()
        app.highlights.append((s.t, 99, 1))
        self.click_truth(s.t, (L[0] + 180 + 14, 36 + 2 + 50 + 38, L[2] - 180 - 28, 30))
        s.wait(1.2)
        # off to the other monitor (logical x < 0 here = x < 2048 globally)
        s.move(-600, 420, speed=0.8)
        s.wait(0.5, drift=False)
        s.click()                         # a click on the other monitor: not ours
        s.move(-900, 300)
        s.click()
        s.wait(2.5)
        s.move(1100, 520, speed=0.8)      # back, into the terminal
        sc.set_focus(s.t, "t1")
        s.wait(0.4, drift=False)
        t0, te, tend = self.run_cmd(term, "bin/rails db:migrate:status", [
            " Status   Migration ID    Migration Name", "--------------------------------------------------",
            ("   up     20261101120000  Create tasks", "green"), ("   up     20261104093000  Add assignee to tasks", "green")],
            line_dt=0.12, after=1.0)
        self.typing_truth(tw, t0 + 0.3, te + 0.05)
        self.output_truth(tw, te, tend)
        self.extra_events.append({"t": s.t, "type": "monitor", "reason": "removed", "name": "HDMI-A-1"})
        s.wait(0.5, drift=False)
        s.move(700, 600)
        s.click()
        s.wait(1.5)


class WindowMode(Scenario):
    """Window recording: the window moves, resizes, gets covered, then closes."""
    name = "window-mode"
    mode = "window"
    target = "0xf1"
    duration = 18.0

    def build(self):
        sc, s = self.sc, self.s
        T1 = tiles(1, self.lw, self.lh)
        sc.add(Win("t1", seeded_terminal(), keys=[(0, T1[0])]))
        term = Terminal("~/app")
        r0 = (140, 120, 640, 400)
        fw = sc.add(Win("f1", term, floating=True, keys=[(0, r0)]))
        s.x, s.y = 400, 300
        s.moves = [(0.0, s.x, s.y)]
        s.wait(1.0)
        t0, te, tend = self.run_cmd(term, "whoami", ["jagrit"], line_dt=0.1, after=0.8)
        self.typing_truth(fw, t0 + 0.3, te + 0.05)
        self.output_truth(fw, te, tend, hold=1.3)
        # drag it right and down
        s.click()
        sx0, sy0 = s.x, s.y
        for k in range(1, 41):
            u = k / 40
            e = u ** 3 * (10 - 15 * u + 6 * u * u)
            s._move_to(sx0 + 420 * e, sy0 + 180 * e, 1.6 / 40, curve=0)
            fw.keys.append((s.t, (r0[0] + 420 * e, r0[1] + 180 * e, 640, 400)))
        s.wait(0.6, drift=False)
        # resize bigger
        s.click("right")
        rx, ry, rw, rh = fw.keys[-1][1]
        for k in range(1, 26):
            e = (k / 25) ** 2 * (3 - 2 * k / 25)
            s._move_to(s.x + 4, s.y + 3, 1.0 / 25, curve=0)
            fw.keys.append((s.t, (rx - 160 * e, ry - 100 * e, rw + 160 * e, rh + 100 * e)))
        s.wait(0.5, drift=False)
        # a notification covers part of it
        n1 = {"t0": s.t, "t1": s.t + 3.0, "title": "Calendar", "body": "Standup in 5 minutes"}
        sc.notifs.append(n1)
        self.layer(n1["t0"], "notifications", sc.notif_rect(n1, n1["t0"] + 1), True, False)
        self.layer(n1["t1"], "notifications", None, False, False)
        t0, te, tend = self.run_cmd(term, "ls", ["Gemfile  app  bin  config  db  lib  log  public  test"],
                                    line_dt=0.1, after=1.2)
        self.typing_truth(fw, t0 + 0.3, te + 0.05)
        self.output_truth(fw, te, tend, hold=1.3)
        s.wait(1.0, drift=False)
        t_close = self.chord(["super"], "w") + 0.05
        fw.close_t = t_close
        sc.set_focus(t_close, "t1")
        self.close_t = t_close
        # the whole window, except where a typed line / its output is the subject (registered above, wins)
        self.truth_fns.insert(0, (0.0, t_close, lambda t: {"aoi": sc.R(fw.rect_at(t)), "kind": "window"}))
        s.wait(1.5)


class LongDrift(Scenario):
    """~2.5 minutes with a logger clock that runs 0.3 % fast and starts 270 ms late."""
    name = "long-drift"
    drift = (0.27, 1.003)
    duration = 150.0

    def build(self):
        sc, s = self.sc, self.s
        L, R = tiles(2, self.lw, self.lh)
        app = App("Tasks")
        aw = sc.add(Win("a1", app, keys=[(0, L)]))
        term = seeded_terminal()
        tw = sc.add(Win("t1", term, keys=[(0, R)]), focus=False)
        sc.set_focus(0, "a1")
        s.x, s.y = 400, 500
        s.moves = [(0.0, s.x, s.y)]
        s.wait(1.0)
        k = 0
        while s.t < self.duration - 12:
            row = k % 6
            s.move(300 + 20 * (k % 3), 3 + 36 + 50 + 38 * row + 15)
            s.click()
            app.highlights.append((s.t, s.t + 6, row))
            s.wait(1.5)
            s.move(900, 450 + 10 * (k % 4), speed=0.9)
            sc.set_focus(s.t, "t1")
            self.run_cmd(term, ["git status -s", "ls", "date", "make lint"][k % 4],
                         [(" M app/models/task.rb", "red"), "ok"], line_dt=0.15, after=1.0)
            s.move(300, 600, speed=0.9)
            sc.set_focus(s.t, "a1")
            s.wait(6.0)          # long idle stretches, like a real narrated demo
            k += 1


class TerminalTyping1667(TerminalTyping):
    """The terminal scenario on a single 2560x1600 monitor at 1.6667 (logical 1536x960)."""
    name = "scale-1.6667"
    lw, lh, scale = 1536, 960, 5 / 3


class TerminalTyping2(TerminalTyping):
    """The terminal scenario on a single 2560x1600 monitor at 2.0 (logical 1280x800), offset right."""
    name = "scale-2"
    lw, lh, scale = 1280, 800, 2.0
    origin = (1920, 0)


class TerminalTypingRotated(TerminalTyping):
    """A portrait monitor (1920x1200 mode, transform 1, scale 1.25: logical 960x1536) left of
    and above the main one (negative offset)."""
    name = "rotated"
    lw, lh, scale = 960, 1536, 1.25
    origin = (-960, -200)


SCENARIOS = {c.name: c for c in (TerminalTyping, BuildLog, Tiling, FloatingDrag, Workspaces, Interruptions,
                                 DualMonitor, WindowMode, LongDrift, TerminalTyping1667, TerminalTyping2,
                                 TerminalTypingRotated)}


def generate(out_root: Path, names=None, fps=30, finalize=True) -> list[Path]:
    from dolly import capture
    out = []
    for name in names or SCENARIOS:
        sc = SCENARIOS[name](fps=fps)
        d = out_root / name
        if (d / "truth.json").exists() and (d / "raw.mp4").exists():
            if finalize:
                capture.finalize(d)        # cheap (analysis is cached): picks up engine changes
            out.append(d)
            continue
        sc.write(d)
        if finalize:
            capture.finalize(d)
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--fps", type=int, default=30)
    a = ap.parse_args()
    for d in generate(Path(a.out), a.only, a.fps):
        print(d)


if __name__ == "__main__":
    main()

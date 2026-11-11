"""`dolly run`: perform a storyboard on the desktop and record it.

Order of a run: check -> go to the stage -> [[setup]] (off camera) -> record ->
[[step]]s -> stop and analyse -> captions, title, export -> [[cleanup]] -> go back to
where you were. Whatever happens after recording starts (a failed step, the time limit,
Ctrl+C, `dolly stop` from elsewhere) the recording is stopped and the take is kept.

The desktop (`inject.Desktop`) and the recorder (`Recorder`) are passed in, so the tests
run the same code against fakes.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import storyboard as sbmod
from .storyboard import LEAD_IN, TAIL, StoryboardError

ROOT = Path(__file__).resolve().parents[1]
QUIET_BELOW = 0.0015      # share of pixels that may still change (a blinking caret is ~0.0001)
QUIET_FOR = 0.7           # seconds the screen must stay still
SETTLE = 0.6              # after a window opens or takes focus


class StepFailed(Exception):
    def __init__(self, step, why: str):
        super().__init__(why)
        self.step, self.why = step, why


class Ended(Exception):
    """The take is over before the last step: "interrupted", "timeout" or "stopped"."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Recorder:
    """Starts and stops a real recording through Dolly's own commands (no picker, no countdown)."""

    def _env(self) -> dict:
        return dict(os.environ, PYTHONPATH=str(ROOT), DOLLY_EVENTS_FROM_SCRIPT="1", DOLLY_OPEN_EDITOR="0")

    def active(self) -> bool:
        from .capture import read_state
        return read_state() is not None

    def start(self, mode: str, pick: str | None) -> Path:
        cmd = [sys.executable, "-m", "dolly.cli", "record", "--mode", mode, "--countdown", "0",
               "--audio", "none", "--camera", "off"]
        if pick:
            cmd += ["--pick", pick]          # merge note: becomes --window / --region (work/agent)
        r = subprocess.run(cmd, env=self._env(), capture_output=True, text=True, timeout=90)
        last = (r.stdout.strip().splitlines() or [""])[-1]
        if r.returncode != 0 or not self.active() or not Path(last).is_dir():
            why = (r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or ["no reason given"])[-1]
            raise RuntimeError(f"the recording did not start: {why}")
        return Path(last)

    def stop(self) -> None:
        subprocess.run([sys.executable, "-m", "dolly.cli", "stop"], env=self._env(),
                       capture_output=True, text=True, timeout=900)

    def wait_finalized(self, pdir: Path, seconds: float = 180.0) -> bool:
        """After `dolly stop` from elsewhere: that process is analysing the take."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if (pdir / "events.json").exists():
                return True
            time.sleep(0.25)
        return False


class Runner:
    def __init__(self, sb, desk, rec, feed=None, clock=time.monotonic, sleep=time.sleep, say=None, caps=None):
        self.sb, self.desk, self.rec, self.feed, self.caps = sb, desk, rec, feed, caps
        self.clock, self.sleep_fn = clock, sleep
        self.say = say or (lambda msg: None)            # progress lines (stderr in the CLI)
        self.names: dict = {}                           # `as` name -> window address
        self.opened: list = []                          # addresses this run opened
        self.cues: list = []                            # (clock time, seconds, text)
        self.warnings: list = []
        self.interrupted = False
        self.recording = False
        self.deadline = None
        self.area = None                                # recorded rect in layout pixels
        self.pdir = None

    # ------------------------------------------------------------ time
    def pause(self, seconds: float):
        """Sleep in short slices so Ctrl+C, the time limit and `dolly stop` end the take at once."""
        end = self.clock() + max(0.0, seconds)
        n = 0
        while True:
            self.check(every_slice=(n % 5 == 0))
            left = end - self.clock()
            if left <= 0:
                return
            self.sleep_fn(min(0.05, left))
            n += 1

    def check(self, every_slice: bool = True):
        if self.interrupted:
            raise Ended("interrupted")
        if self.recording:
            if self.deadline is not None and self.clock() > self.deadline:
                raise Ended("timeout")
            if every_slice and not self.rec.active():
                raise Ended("stopped")

    # ------------------------------------------------------------ windows
    def address(self, ref: str, step) -> str:
        if ":" not in ref:
            addr = self.names.get(ref)
            if not addr or not self.desk.window(addr):
                raise StepFailed(step, f'the window "{ref}" is gone')
            return addr
        win = self.desk.find(ref)
        if not win:
            raise StepFailed(step, f"no window matches {ref}")
        return win["address"]

    def rect(self, ref: str | None, step) -> tuple:
        if not ref:
            return self.area or self.desk.screen_rect()
        win = self.desk.window(self.address(ref, step))
        (x, y), (w, h) = win["at"], win["size"]
        return (x, y, w, h)

    def point(self, step) -> tuple:
        (px, pp), (py, yp) = sbmod.parse_point(step.value)
        x0, y0, w, h = self.rect(step.opts.get("in"), step)
        return x0 + (w * px / 100 if pp else px), y0 + (h * py / 100 if yp else py)

    # ------------------------------------------------------------ steps
    def perform(self, row):
        st = row.step
        v, o = st.value, st.opts
        try:
            getattr(self, "do_" + st.action)(st, v, o, row)
        except (StepFailed, Ended):
            raise
        except subprocess.TimeoutExpired as e:
            raise StepFailed(st, f"{e.cmd[0]} did not answer") from None
        except subprocess.CalledProcessError as e:
            raise StepFailed(st, f"{e.cmd[0]} failed (exit {e.returncode})") from None
        except (OSError, RuntimeError) as e:
            raise StepFailed(st, str(e)) from None

    def do_open(self, st, v, o, row):
        before = {c.get("address") for c in self.desk.clients()}
        self.desk.launch(v, o.get("cwd"))
        end = self.clock() + float(o.get("timeout", 10.0))
        while True:
            new = [c for c in self.desk.clients() if c.get("address") not in before and c.get("mapped", True)]
            if new:
                break
            if self.clock() > end:
                raise StepFailed(st, f"no window appeared within {float(o.get('timeout', 10.0)):g} s")
            self.pause(0.1)
        addr = new[0]["address"]
        self.opened.append(addr)
        if "as" in o:
            self.names[o["as"]] = addr
        self.pause(SETTLE)

    def do_focus(self, st, v, o, row):
        addr = self.address(v, st)
        if not self.desk.focus(addr):
            raise StepFailed(st, f"could not focus {v}")
        self.pause(SETTLE)

    def do_close(self, st, v, o, row):
        addr = self.address(v, st)
        self.desk.close(addr)
        self.names = {k: a for k, a in self.names.items() if a != addr}
        self.opened = [a for a in self.opened if a != addr]
        self.pause(0.3)

    def do_type(self, st, v, o, row):
        for ch, d in zip(v, row.delays):
            self.pause(d)
            self.desk.type_char(ch)
        if o.get("enter"):
            self.pause(row.pause)
            self.desk.press([], "enter")

    def do_key(self, st, v, o, row):
        mods, key = sbmod.parse_chord(v)
        for i in range(int(o.get("times", 1))):
            if i:
                self.pause(0.16)
            self.desk.press(mods, key)
        self.pause(0.12)

    def do_wait(self, st, v, o, row):
        if isinstance(v, (int, float)):
            return self.pause(float(v))
        limit = float(o.get("timeout", 15.0))
        end = self.clock() + limit
        if v == "quiet":
            return self.wait_quiet(end, limit)
        sel = v.split(":", 1)[1]
        while not (self.desk.find(sel) if ":" in sel else self.names.get(sel) and self.desk.window(self.names[sel])):
            if self.clock() > end:
                raise StepFailed(st, f"{sel} did not appear within {limit:g} s")
            self.pause(0.15)
        self.pause(SETTLE)

    def wait_quiet(self, end: float, limit: float):
        from .inject import changed_fraction
        rect = self.area or self.desk.screen_rect()
        prev = self.desk.screen_sample(rect)
        if prev is None:
            self.warn('wait = "quiet" needs grim to watch the screen; waited 1.5 s instead')
            return self.pause(1.5)
        still_since = self.clock()
        while self.clock() - still_since < QUIET_FOR:
            if self.clock() > end:
                self.warn(f"the screen was still changing after {limit:g} s; carried on")
                return
            self.pause(0.2)
            cur = self.desk.screen_sample(rect)
            if changed_fraction(prev, cur) >= QUIET_BELOW:
                still_since = self.clock()
            prev = cur

    def do_move(self, st, v, o, row):
        self.desk.move_to(*self.point(st))
        self.pause(0.15)

    def do_click(self, st, v, o, row):
        addr = self.address(o["in"], st) if "in" in o else None
        if addr and self.desk.focused() != addr:
            self.desk.focus(addr)               # bring it forward: the click must land in it
            self.pause(0.25)
        self.desk.move_to(*self.point(st))
        self.pause(0.14)
        self.desk.click(o.get("button", "left"), addr)
        self.pause(0.3)

    def do_zoom(self, st, v, o, row):
        self.desk.mark()
        self.pause(0.15)

    def do_say(self, st, v, o, row):
        self.cues.append((self.clock(), float(o.get("for", sbmod.say_seconds(v))), v.strip()))

    def warn(self, msg: str):
        self.warnings.append(msg)
        self.say("note: " + msg)

    # ------------------------------------------------------------ the run
    def run(self, export: bool = True, keep_open: bool = False, progress=None) -> dict:
        sb, desk = self.sb, self.desk
        res = {"ok": False, "title": sb.title, "project": None, "outputs": [], "steps_done": 0,
               "steps": len(sb.steps), "error": None, "warnings": self.warnings}
        caps = self.caps
        if caps is None:
            from .inject import capabilities
            caps = capabilities()
        problems = list(caps["problems"])
        if not caps["click"] and any(s.action == "click" for s in [*sb.setup, *sb.steps, *sb.cleanup]):
            problems.append(caps["click_note"])
        if self.rec.active():
            problems.append("a recording is already running; stop it first (dolly stop)")
        if problems:
            res["error"] = {"where": "before starting", "line": 0, "why": "; ".join(problems)}
            return res
        plan = sbmod.plan(sb)
        home = {"workspace": desk.workspace(), "window": desk.focused(), "pointer": desk.pointer(),
                "monitor": desk.monitor() if hasattr(desk, "monitor") else None}
        old = {s: signal.signal(s, self._on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            try:
                if sb.stage == "clean":
                    ws = desk.empty_workspace()
                    if ws is None:
                        self.warn("no empty workspace for the take; recording where you are")
                    elif ws != home["workspace"]:
                        desk.go_workspace(ws)
                        self.pause(0.4)
                for row in plan["setup"]:
                    self.say(f"setup {row.step.index}: {row.step.action}")
                    self.perform(row)
                hold = getattr(desk, "hold_key_repeat", None)     # a stall must not repeat a typed key
                self.repeat_held = bool(hold and hold())
                mode, pick = self.target()
                self.pdir = self.rec.start(mode, pick)
                res["project"] = str(self.pdir)
                self.recording = True
                self.deadline = self.clock() + sb.max_seconds
                self.pause(LEAD_IN)
                for row in plan["steps"]:
                    self.say(f"step {row.step.index}/{len(sb.steps)}: {row.step.action}")
                    self.perform(row)
                    res["steps_done"] += 1
                self.pause(max(TAIL, self._cue_tail()))
                res["ok"] = True
            except StepFailed as e:
                res["error"] = {"where": e.step.where, "line": e.step.line, "why": e.why}
            except Ended as e:
                if e.reason == "stopped":       # `dolly stop`, the bar or the hotkey: an early, clean end
                    res["ok"] = True
                    self.warn(f"stopped from outside after step {res['steps_done']} of {len(sb.steps)}")
                else:
                    why = "interrupted (Ctrl+C)" if e.reason == "interrupted" else \
                        f"still running after max_seconds = {sb.max_seconds:g}"
                    where = f"step {res['steps_done'] + 1}" if self.recording else "setup"
                    res["error"] = {"where": where, "line": 0, "why": why}
            except RuntimeError as e:           # the recorder did not start
                res["error"] = {"where": "starting the recording", "line": 0, "why": str(e)}
            # ---- from here on nothing may be interrupted halfway: the take is on disk
            was_recording, self.recording = self.recording, False
            self.interrupted = False
            if was_recording:
                self.say("stopping and analysing the take")
                if self.rec.active():
                    self.rec.stop()
                elif not self.rec.wait_finalized(self.pdir):
                    self.warn("the take was stopped from outside and is not analysed yet (dolly finalize)")
                self._restore_signals(old)      # Ctrl+C during a long export ends the export
                try:
                    self.after_take(res, export and res["ok"], progress)
                except Exception as e:          # the take itself is safe; say what did not happen
                    res["ok"] = False
                    res["error"] = {"where": "after recording", "line": 0, "why": str(e)[:300]}
        finally:
            self._restore_signals(old)
            self.interrupted = False
            if getattr(self, "repeat_held", False):
                desk.restore_key_repeat()
                self.repeat_held = False
            if not keep_open:
                self.tidy(plan["cleanup"], home)
            if self.feed:
                self.feed.close()
        return res

    def _on_signal(self, *_):
        self.interrupted = True

    @staticmethod
    def _restore_signals(old: dict):
        for s, h in old.items():
            signal.signal(s, h)

    def _cue_tail(self) -> float:
        """Time the last caption still needs on screen."""
        return max((t + d - self.clock() for t, d, _ in self.cues), default=0.0)

    def target(self) -> tuple:
        """(record mode, pick) for the header's `record`, and the rect steps measure against."""
        rec = self.sb.record
        if rec == "screen":
            self.area = self.desk.screen_rect()
            return "screen", None
        kind, _, what = rec.partition(":")
        if kind == "region":
            x, y, w, h = (int(n) for n in re.split(r"[,x]", what))
        else:
            ref = what
            addr = self.names.get(ref) if ":" not in ref else (self.desk.find(ref) or {}).get("address")
            win = self.desk.window(addr) if addr else None
            if not win:
                raise RuntimeError(f"no window to record: {ref}")
            self.desk.focus(addr)
            (x, y), (w, h) = win["at"], win["size"]
        self.area = (x, y, w, h)
        return kind, f"{x},{y} {w}x{h}"

    def after_take(self, res: dict, export: bool, progress):
        """Title, captions from `say`, exports. `res["outputs"]` = [{"aspect"|"preset", "file"}]."""
        from . import library, project as projmod
        pdir, sb = self.pdir, self.sb
        proj = projmod.Project(pdir)
        if not proj.events.exists():
            raise RuntimeError(f"the take was not analysed (see {pdir}/recorder.log)")
        from . import analysis
        analysis.ensure(pdir)                # `dolly stop` sends its card first; the plan may still be running
        if sb.title:
            library.rename(pdir, sb.title)
        if self.cues:
            from .captions import write_srt
            write_srt(self.video_cues(proj), pdir / "captions.srt")
        ev = json.loads(proj.events.read_text())
        res["seconds"] = round(float(ev.get("video", {}).get("duration") or 0.0), 2)
        if not export:
            return
        slug = re.sub(r"[^a-z0-9]+", "-", sb.title.lower()).strip("-")[:40] or pdir.name
        if sb.preset:
            from .share import share_preset
            self.say(f"exporting preset {sb.preset}")
            out = share_preset(pdir, sb.preset, progress, log=lambda *_: None)
            res["outputs"].append({"preset": sb.preset, "file": str(out["file"])})
            return
        from .render import export as render
        for asp in sb.aspect:
            self.say(f"exporting {asp}")
            out = render(pdir, proj.exports / f"{slug}-{asp.replace(':', 'x')}.mp4", None, sb.quality,
                         progress=progress, aspect=asp, notify=False)
            res["outputs"].append({"aspect": asp, "file": str(out)})

    def video_cues(self, proj) -> list:
        """`say` moments (our clock) -> video time, through the take's own sync fit."""
        start = json.loads(proj.raw_events.open().readline())
        sync = json.loads(proj.events.read_text()).get("capture", {}).get("sync") or {}
        a, b = float(sync.get("a", 0.0)), float(sync.get("b", 1.0))
        cues = []
        for t, d, text in self.cues:
            v = a + b * (t - float(start["t0_mono"]))
            if cues and cues[-1]["end"] > v - 0.05:
                cues[-1]["end"] = max(cues[-1]["start"] + 0.3, v - 0.05)     # the next caption takes over
            cues.append({"start": max(0.0, v), "end": max(0.0, v) + d, "text": text})
        return cues

    def tidy(self, cleanup_rows, home: dict):
        """[[cleanup]], then back to the workspace, window and pointer the run started from.
        Each part is on its own: one failing must not stop the rest."""
        for row in cleanup_rows:
            try:
                self.perform(row)
            except (StepFailed, Ended) as e:
                self.warn(f"cleanup {row.step.index} ({row.step.action}): {getattr(e, 'why', e)}")
        # the monitor first: a workspace that no longer exists (Hyprland drops an empty one
        # you leave) is opened on whichever monitor has the focus when it is asked for
        for what in (lambda: home.get("monitor") and self.desk.monitor() != home["monitor"]
                     and self.desk.go_monitor(home["monitor"]),
                     lambda: home["workspace"] is not None and self.desk.workspace() != home["workspace"]
                     and self.desk.go_workspace(home["workspace"]),
                     lambda: home["window"] and self.desk.window(home["window"]) and self.desk.focus(home["window"]),
                     lambda: self.desk.move_to(*home["pointer"], seconds=0.0)):
            try:
                what()
            except Exception:                   # noqa: BLE001 - restoring is best effort
                pass


# ---------------------------------------------------------------- command line
def read_storyboard(path: str):
    """-> (Storyboard, source text). "-" reads stdin (JSON or TOML)."""
    if path == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise StoryboardError(f"cannot read {path}: {e.strerror or e}") from None
    return sbmod.loads(text, "json" if path.endswith(".json") else None), text


def check_report() -> dict:
    """What `dolly run --check` and `dolly doctor` say about this machine."""
    from .inject import capabilities
    c = capabilities()
    ok = not c["problems"]
    notes = [n for n in (c["click_note"], c["quiet_note"]) if n]
    return {"ok": ok, "keys": c["keys"], "pointer": c["pointer"], "click": c["click"], "quiet": c["quiet"],
            "problems": c["problems"], "notes": notes}


def cmd_run(a) -> int:
    """dolly run <file|-> [--dry-run] [--check] [--json] [--no-export] [--keep-open]"""
    def emit(obj: dict, text: str, code: int) -> int:
        print(json.dumps(obj, indent=2) if a.json else text, file=sys.stdout if a.json or code == 0 else sys.stderr)
        return code

    if a.check and not a.file:
        rep = check_report()
        lines = [f"typing and keys: {'ok (wtype)' if rep['keys'] else 'unavailable'}",
                 f"pointer travel, clicks and windows: {'ok (Hyprland)' if rep['pointer'] else 'unavailable'}",
                 f'wait = "quiet": {"ok (grim)" if rep["quiet"] else "fixed 1.5 s"}',
                 *rep["problems"], *rep["notes"]]
        return emit(rep, "\n".join(lines), 0 if rep["ok"] else 1)
    if not a.file:
        return emit({"ok": False, "error": {"why": "which storyboard? dolly run demo.toml"}},
                    "dolly run: which storyboard? (dolly run demo.toml, or - for stdin)", 2)
    try:
        sb, _ = read_storyboard(a.file)
    except StoryboardError as e:
        name = "stdin" if a.file == "-" else a.file
        return emit({"ok": False, "error": {"where": e.where, "line": e.line, "why": e.msg}},
                    f"{name}: {e}", 2)
    if a.dry_run or a.check:
        p = sbmod.plan(sb)
        rep = {"ok": True, "title": sb.title, "seconds": p["recorded"], "open_ended": p["open_ended"],
               "plan": [{"section": r.step.section, "index": r.step.index, "action": r.step.action,
                         "value": r.step.value, "start": r.start, "seconds": r.seconds, "open_ended": r.open_ended}
                        for sec in ("setup", "steps", "cleanup") for r in p[sec]]}
        text = sbmod.describe(sb)
        if a.check:
            chk = check_report()
            need_click = any(r["action"] == "click" for r in rep["plan"])
            rep["machine"] = chk
            rep["ok"] = chk["ok"] and (chk["click"] or not need_click)
            text += "\n" + ("This machine can run it." if rep["ok"] else
                            "This machine cannot run it: " + "; ".join(chk["problems"] or chk["notes"][:1]))
        return emit(rep, text, 0 if rep["ok"] else 1)
    from .inject import Desktop, Feed
    feed = Feed()
    say = (lambda m: None) if a.json else (lambda m: print(m, file=sys.stderr, flush=True))
    res = Runner(sb, Desktop(feed), Recorder(), feed, say=say).run(export=not a.no_export, keep_open=a.keep_open)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        if res["error"]:
            e = res["error"]
            print(f"dolly run: {e['where']}{' (line ' + str(e['line']) + ')' if e.get('line') else ''}: {e['why']}",
                  file=sys.stderr)
            if res["project"]:
                print(f"The take up to there is kept: {res['project']}", file=sys.stderr)
        for o in res["outputs"]:
            print(o["file"])
        if res["ok"] and not res["outputs"] and res["project"]:
            print(res["project"])
    if res["ok"]:
        return 0
    return 130 if "interrupted" in (res["error"] or {}).get("why", "") else 1

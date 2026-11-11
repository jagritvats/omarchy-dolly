"""Storyboards: a demo written as steps, performed and recorded by `dolly run`.

A storyboard is a small TOML file (or the same structure as JSON on stdin):

    title  = "Listing files"
    record = "screen"                 # "screen", "window:<name>" or "region:X,Y,WxH"
    aspect = ["16:9", "9:16"]

    [[setup]]                         # before recording, off camera
    open = "terminal"
    as   = "term"

    [[step]]
    type  = "ls -la"
    enter = true

    [[step]]
    wait = "quiet"                    # until the screen stops changing

    [[cleanup]]                       # after recording
    close = "term"

Every step table has exactly one action (open, focus, type, key, wait, move, click,
zoom, say, close) plus that action's options. This module only parses, validates and
times a storyboard: nothing here runs a command or touches the desktop
(`dolly run --dry-run` uses just this). docs/storyboard.md is the reference.
"""
from __future__ import annotations

import json
import random
import re
import zlib
from dataclasses import dataclass, field

ACTIONS = ("open", "focus", "type", "key", "wait", "move", "click", "zoom", "say", "close")
# options each action accepts (besides the action key itself)
OPTIONS = {
    "open": {"as", "cwd", "timeout"},
    "focus": set(),
    "type": {"enter", "speed"},
    "key": {"times"},
    "wait": {"timeout"},
    "move": {"in"},
    "click": {"in", "button"},
    "zoom": set(),
    "say": {"for"},
    "close": set(),
}
HEADER = {"title", "record", "aspect", "preset", "quality", "max_seconds", "speed", "seed", "stage",
          "setup", "step", "cleanup"}
ASPECTS = ("16:9", "9:16", "1:1", "4:5")
QUALITIES = ("draft", "fast", "high", "best")
# share presets by name (dolly/share/presets.py; listed here so validating needs no video packages)
PRESETS = ("github", "github-gif", "discord", "slack", "x", "linkedin", "youtube", "shorts")
MODS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "super": "super", "win": "super",
        "logo": "super", "cmd": "super"}
# named keys a `key` step may use -> (xkb keysym for wtype, evdev code for the event log)
KEYS = {"enter": ("Return", 28), "return": ("Return", 28), "tab": ("Tab", 15), "escape": ("Escape", 1),
        "esc": ("Escape", 1), "space": ("space", 57), "backspace": ("BackSpace", 14), "delete": ("Delete", 111),
        "up": ("Up", 103), "down": ("Down", 108), "left": ("Left", 105), "right": ("Right", 106),
        "home": ("Home", 102), "end": ("End", 107), "pageup": ("Prior", 104), "pagedown": ("Next", 109)}
for _i in range(1, 13):
    KEYS[f"f{_i}"] = (f"F{_i}", (58 + _i) if _i <= 10 else (76 + _i))
DEFAULT_SPEED = 11.0          # characters per second: brisk, still readable
LEAD_IN, TAIL = 0.8, 1.0      # seconds of stillness recorded before the first and after the last step


class StoryboardError(ValueError):
    """A problem in the storyboard, with where it is (`line` is 1-based, 0 = unknown)."""

    def __init__(self, msg: str, line: int = 0, where: str = ""):
        self.msg, self.line, self.where = msg, line, where
        loc = f"line {line}: " if line else ""
        super().__init__(f"{loc}{where + ': ' if where else ''}{msg}")


@dataclass
class Step:
    action: str
    value: object
    opts: dict = field(default_factory=dict)
    section: str = "step"      # setup | step | cleanup
    index: int = 0             # 1-based within its section
    line: int = 0

    @property
    def where(self) -> str:
        return f"{self.section} {self.index} ({self.action})"


@dataclass
class Storyboard:
    title: str = ""
    record: str = "screen"
    aspect: list = field(default_factory=lambda: ["16:9"])
    preset: str | None = None
    quality: str = "high"
    max_seconds: float = 120.0
    speed: float = DEFAULT_SPEED
    seed: int = 0
    stage: str = "clean"       # clean: an empty workspace for the take | here: where you are
    setup: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    cleanup: list = field(default_factory=list)


# ------------------------------------------------------------------ parsing
def _section_lines(text: str) -> dict:
    """{'step': [line of 1st [[step]], ...], ...} so errors can point at the file."""
    out = {"setup": [], "step": [], "cleanup": []}
    for i, ln in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*\[\[\s*(setup|step|cleanup)\s*\]\]", ln)
        if m:
            out[m.group(1)].append(i)
    return out


def _key_line(text: str, key: str) -> int:
    for i, ln in enumerate(text.splitlines(), 1):
        if re.match(rf"\s*{re.escape(key)}\s*=", ln):
            return i
    return 0


def loads(text: str, fmt: str | None = None) -> Storyboard:
    """Parse and validate. `fmt` is "toml", "json" or None (JSON when it starts with "{")."""
    fmt = fmt or ("json" if text.lstrip().startswith("{") else "toml")
    if fmt == "json":
        try:
            data = json.loads(text)
        except ValueError as e:
            raise StoryboardError(f"not valid JSON: {e}", getattr(e, "lineno", 0)) from None
        lines = {"setup": [], "step": [], "cleanup": []}
    else:
        import tomllib
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            m = re.search(r"line (\d+)", str(e))
            raise StoryboardError(f"not valid TOML: {e}", int(m.group(1)) if m else 0) from None
        lines = _section_lines(text)
    if not isinstance(data, dict):
        raise StoryboardError("a storyboard is a table of settings and steps")
    return build(data, lines, text if fmt == "toml" else "")


def build(data: dict, lines: dict | None = None, text: str = "") -> Storyboard:
    lines = lines or {"setup": [], "step": [], "cleanup": []}

    def bad(key, msg):
        raise StoryboardError(msg, _key_line(text, key) if text else 0, key)

    for k in data:
        if k not in HEADER:
            bad(k, f"unknown setting (settings are: {', '.join(sorted(HEADER - {'setup', 'step', 'cleanup'}))})")
    sb = Storyboard()
    sb.title = str(data.get("title", "")).strip()[:120]
    sb.record = str(data.get("record", "screen")).strip()
    if not (sb.record == "screen" or re.fullmatch(r"window:.+", sb.record)
            or re.fullmatch(r"region:\d+,\d+,\d+x\d+", sb.record)):
        bad("record", 'must be "screen", "window:<name or selector>" or "region:X,Y,WxH"')
    asp = data.get("aspect", ["16:9"])
    asp = [asp] if isinstance(asp, str) else asp
    if not isinstance(asp, list) or not asp or any(a not in ASPECTS for a in asp):
        bad("aspect", f"must be one or more of {', '.join(ASPECTS)}")
    sb.aspect = list(dict.fromkeys(asp))
    sb.preset = data.get("preset")
    if sb.preset is not None and not isinstance(sb.preset, str):
        bad("preset", "must be the name of a share preset (see `dolly share list`)")
    if sb.preset is not None:
        if sb.preset not in PRESETS:
            bad("preset", f"is not a share preset (presets: {', '.join(PRESETS)})")
    sb.quality = data.get("quality", "high")
    if sb.quality not in QUALITIES:
        bad("quality", f"must be one of {', '.join(QUALITIES)}")
    for key, lo, hi, default in (("max_seconds", 2, 900, 120.0), ("speed", 2, 40, DEFAULT_SPEED)):
        v = data.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            bad(key, f"must be a number from {lo} to {hi}")
        setattr(sb, key, float(v))
    sb.stage = data.get("stage", "clean")
    if sb.stage not in ("clean", "here"):
        bad("stage", 'must be "clean" (an empty workspace for the take) or "here"')
    seed = data.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        bad("seed", "must be a whole number")
    sb.seed = seed if seed is not None else zlib.crc32((sb.title or "dolly").encode())
    for section, attr in (("setup", "setup"), ("step", "steps"), ("cleanup", "cleanup")):
        raw = data.get(section, [])
        if not isinstance(raw, list) or any(not isinstance(s, dict) for s in raw):
            raise StoryboardError(f"write each one as a [[{section}]] table", 0, section)
        out = []
        for i, tbl in enumerate(raw, 1):
            line = lines[section][i - 1] if i - 1 < len(lines[section]) else 0
            out.append(_step(tbl, section, i, line))
        setattr(sb, attr, out)
    if not sb.steps:
        raise StoryboardError("nothing to record: add at least one [[step]]")
    _check_names(sb)
    return sb


def _step(tbl: dict, section: str, index: int, line: int) -> Step:
    where = f"{section} {index}"
    acts = [k for k in tbl if k in ACTIONS]
    if len(acts) != 1:
        found = ", ".join(acts) if acts else "none"
        raise StoryboardError(f"needs exactly one action ({', '.join(ACTIONS)}); found: {found}", line, where)
    action = acts[0]
    st = Step(action, tbl[action], {k: v for k, v in tbl.items() if k != action}, section, index, line)

    def bad(msg):
        raise StoryboardError(msg, line, st.where)

    extra = set(st.opts) - OPTIONS[action]
    if extra:
        allowed = ", ".join(sorted(OPTIONS[action])) or "none"
        bad(f"unknown option {', '.join(sorted(extra))} (options for {action}: {allowed})")
    v, o = st.value, st.opts
    if action in ("say", "zoom") and section != "step":
        bad("only makes sense while recording: move it to a [[step]]")
    if action in ("open", "focus", "close", "type", "key", "say"):
        if not isinstance(v, str) or not v.strip():
            bad("needs text in quotes")
    if action == "open":
        if "as" in o and not re.fullmatch(r"[A-Za-z][\w-]{0,30}", str(o["as"])):
            bad('"as" is a short name: letters, digits, - and _')
        if "cwd" in o and not isinstance(o["cwd"], str):
            bad('"cwd" is a folder path in quotes')
        _num(o, "timeout", 1, 120, bad)
    elif action == "type":
        if "\n" in v or "\r" in v:
            bad('one line per step: use enter = true (and another step for the next line)')
        if not isinstance(o.get("enter", False), bool):
            bad('"enter" is true or false')
        _num(o, "speed", 2, 40, bad)
    elif action == "key":
        parse_chord(v, bad)
        t = o.get("times", 1)
        if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= 50:
            bad('"times" is a whole number from 1 to 50')
    elif action == "wait":
        if isinstance(v, bool) or not (isinstance(v, (int, float)) and 0 < v <= 120
                                      or v == "quiet" or isinstance(v, str) and re.fullmatch(r"window:.+", v)):
            bad('is a number of seconds (up to 120), "quiet" (until the screen stops changing) or "window:<selector>"')
        _num(o, "timeout", 0.5, 300, bad)
    elif action in ("move", "click"):
        parse_point(v, bad)
        if "in" in o and (not isinstance(o["in"], str) or not o["in"].strip()):
            bad('"in" names a window (an "as" name or a selector)')
        if o.get("button", "left") not in ("left", "right", "middle"):
            bad('"button" is left, right or middle')
    elif action == "zoom":
        if v not in (True, "here"):
            bad('write zoom = "here" (a "zoom here" mark at the pointer)')
    elif action == "say":
        _num(o, "for", 0.5, 30, bad)
    return st


def _num(o: dict, key: str, lo: float, hi: float, bad) -> None:
    if key in o:
        v = o[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            bad(f'"{key}" is a number from {lo:g} to {hi:g}')


def _check_names(sb: Storyboard) -> None:
    """Every plain name a step refers to must have been given by an earlier `open ... as`."""
    known: set = set()
    for st in [*sb.setup, *sb.steps, *sb.cleanup]:
        refs = []
        if st.action in ("focus", "close"):
            refs.append(st.value)
        if st.action in ("move", "click") and "in" in st.opts:
            refs.append(st.opts["in"])
        if st.action == "wait" and isinstance(st.value, str) and st.value.startswith("window:"):
            refs.append(st.value.split(":", 1)[1])
        for r in refs:
            if ":" not in r and r not in known:
                names = ", ".join(sorted(known)) or "none yet"
                raise StoryboardError(f'no window named "{r}" (names so far: {names}); '
                                      'use `as` on an earlier open, or a selector like "class:foot"',
                                      st.line, st.where)
        if st.action == "open" and "as" in st.opts:
            known.add(st.opts["as"])
    if sb.record.startswith("window:"):
        r = sb.record.split(":", 1)[1]
        setup_names = {s.opts["as"] for s in sb.setup if s.action == "open" and "as" in s.opts}
        if ":" not in r and r not in setup_names:
            raise StoryboardError(f'record = "window:{r}": open it in a [[setup]] step with as = "{r}" '
                                  "(the window must exist before recording starts)", 0, "record")


def parse_chord(text: str, bad=None) -> tuple[list, str]:
    """"ctrl+shift+t" -> (["ctrl", "shift"], "t"). The last part is the key."""
    def fail(msg):
        if bad:
            bad(msg)
        raise StoryboardError(msg)
    parts = [p.strip().lower() for p in str(text).split("+")]
    if "" in parts:
        if str(text).endswith("++") or text == "+":       # "ctrl++": the key is "+"
            parts = [p for p in parts if p] + ["+"]
        else:
            fail(f'"{text}" is not a key: write it like "ctrl+l" or "enter"')
    *mods, key = parts
    for m in mods:
        if m not in MODS:
            fail(f'"{m}" is not a modifier (ctrl, alt, shift, super)')
    if key not in KEYS and len(key) != 1:
        fail(f'"{key}" is not a key: one character, or one of {", ".join(sorted(KEYS))}')
    return [MODS[m] for m in mods], key


def parse_point(text, bad=None) -> tuple:
    """"50%,40%" (of the window) or "640,360" (pixels from its top-left) -> ((value, is_percent), ...)."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)(%?)\s*,\s*(-?\d+(?:\.\d+)?)(%?)\s*", str(text))
    if not m:
        msg = 'is a point like "50%,40%" (of the window) or "640,360" (pixels from its top-left corner)'
        if bad:
            bad(msg)
        raise StoryboardError(msg)
    return (float(m.group(1)), bool(m.group(2))), (float(m.group(3)), bool(m.group(4)))


# ------------------------------------------------------------------ pacing
def keystroke_delays(text: str, speed: float, rng: random.Random) -> list[float]:
    """Seconds to wait BEFORE each character so typing reads like a person: quicker inside
    words, a beat at word starts, a longer one after punctuation, and now and then a
    short hesitation. Deterministic for a given rng."""
    base = 1.0 / max(speed, 1.0)
    out, prev = [], ""
    for i, ch in enumerate(text):
        d = base * rng.lognormvariate(0.0, 0.28)
        if i == 0:
            d = 0.0
        elif prev == " ":
            d *= 1.5                                   # finding the next word
        elif prev in ",.;:|&" or (prev in "-/=" and ch.isalnum() and rng.random() < 0.3):
            d *= 1.9
        elif ch.isalnum() and prev.isalnum():
            d *= 0.85                                  # inside a word the fingers know the way
        if ch.isupper() or ch in '~!@#$%^&*()_+{}|:"<>?':
            d += base * 0.35                           # reaching for shift
        if i and rng.random() < 0.04:
            d += rng.uniform(0.18, 0.38)               # a thought
        out.append(min(d, base * 6))
        prev = ch
    return out


def enter_pause(rng: random.Random) -> float:
    """People look at what they typed before pressing Enter."""
    return rng.uniform(0.35, 0.6)


def say_seconds(text: str) -> float:
    """Reading time for a caption: about 15 characters a second, 1.6 to 7 s."""
    return max(1.6, min(7.0, 0.8 + len(text) / 15.0))


def travel_seconds(dist_px: float) -> float:
    """Pointer travel time: quick for short hops, never a teleport, never a crawl."""
    return max(0.28, min(1.1, 0.24 + dist_px / 1700.0))


@dataclass
class Planned:
    step: Step
    start: float              # seconds from the start of its section
    seconds: float            # how long it takes (a typical time when `open_ended`)
    open_ended: bool = False  # waits for something; the step's `timeout` is the limit
    detail: str = ""
    delays: list = field(default_factory=list)   # type: seconds before each character
    pause: float = 0.0                           # type: the look before Enter


def plan(sb: Storyboard) -> dict:
    """{'setup': [Planned], 'steps': [...], 'cleanup': [...], 'recorded': seconds, 'open_ended': bool}.
    The same seed gives the same plan and the same keystroke timing in the real run."""
    rng = random.Random(sb.seed)
    out = {}
    for section, steps in (("setup", sb.setup), ("steps", sb.steps), ("cleanup", sb.cleanup)):
        t = LEAD_IN if section == "steps" else 0.0
        rows = []
        for st in steps:
            secs, open_ended, detail, delays, pause = _duration(st, sb, rng)
            rows.append(Planned(st, round(t, 3), round(secs, 3), open_ended, detail, delays, pause))
            t += secs
        out[section] = rows
        if section == "steps":
            out["recorded"] = round(t + TAIL, 2)
    out["open_ended"] = any(p.open_ended for p in out["steps"])
    return out


def _duration(st: Step, sb: Storyboard, rng: random.Random):
    v, o = st.value, st.opts
    if st.action == "type":
        delays = keystroke_delays(v, float(o.get("speed", sb.speed)), rng)
        pause = enter_pause(rng) if o.get("enter") else 0.0
        return (sum(delays) + pause, False, f"{len(v)} characters" + (" + Enter" if o.get("enter") else ""),
                delays, pause)
    if st.action == "focus":
        return 0.6, False, "", [], 0.0
    if st.action == "key":
        n = int(o.get("times", 1))
        return 0.12 + 0.16 * (n - 1), False, f"x{n}" if n > 1 else "", [], 0.0
    if st.action == "wait":
        if isinstance(v, (int, float)):
            return float(v), False, "", [], 0.0
        what = "until the screen is quiet" if v == "quiet" else f"until {v} appears"
        return 1.5, True, f"{what}, at most {float(o.get('timeout', 15.0)):g} s", [], 0.0
    if st.action == "open":
        return 1.5, True, f"until its window appears, at most {float(o.get('timeout', 10.0)):g} s", [], 0.0
    if st.action in ("move", "click"):
        return 0.9 if st.action == "move" else 1.35, False, "eased pointer travel; time depends on the distance", [], 0.0
    if st.action == "say":
        return 0.0, False, f"caption for {float(o.get('for', say_seconds(v))):.1f} s, over the next steps", [], 0.0
    return 0.3, False, "", [], 0.0


def describe(sb: Storyboard) -> str:
    """The timed plan `dolly run --dry-run` prints. Commands that would run are shown in full."""
    p = plan(sb)
    lines = [f"Storyboard: {sb.title or '(untitled)'}",
             f"  record {sb.record} · export {', '.join(sb.aspect)}"
             + (f" · preset {sb.preset}" if sb.preset else f" · quality {sb.quality}")
             + f" · stage {sb.stage} · limit {sb.max_seconds:g} s · seed {sb.seed}"]
    for section, title in (("setup", "Before recording (off camera)"), ("steps", "Recorded"),
                           ("cleanup", "After recording")):
        rows = p[section]
        if not rows:
            continue
        lines.append(f"{title}:")
        for r in rows:
            st = r.step
            val = json.dumps(st.value) if isinstance(st.value, str) else str(st.value).lower()
            dur = f"about {r.seconds:g} s" if r.open_ended else f"{r.seconds:.1f} s"
            when = f"+{r.start:5.1f}s" if section == "steps" else "       "
            run = "  [runs a command]" if st.action == "open" and st.value != "terminal" else ""
            lines.append(f"  {when}  {st.action:<5} {val}  ({dur}{'; ' + r.detail if r.detail else ''}){run}")
    more = " (the waits are estimates: they end when their condition is met)" if p["open_ended"] else ""
    lines.append(f"About {p['recorded']:.0f} s of recording{more}.")
    return "\n".join(lines)

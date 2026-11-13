"""Event timeline model.

A recording produces two files:

* ``raw.mp4``  - constant-frame-rate capture, hardware cursor hidden when possible
* ``events.json`` - the normalized timeline this module loads

``events.json`` layout (written by ``dolly finalize`` from the logger's raw
JSONL, or directly by the synthetic harness)::

    {
      "version": 1,
      "video": {"w": 2560, "h": 1440, "fps": 60, "duration": 31.2},
      "capture": {"backend": "gpu-screen-recorder", "cursor_baked": false,
                  "click_source": "evdev", "region": [x, y, w, h], "scale": 1.0},
      "events": [
        {"t": 0.016, "type": "move",  "x": 812.0, "y": 410.5},
        {"t": 1.250, "type": "click", "x": 830.0, "y": 402.0, "button": "left"},
        {"t": 2.100, "type": "key",   "key": "S", "mods": ["ctrl"]},
        {"t": 2.400, "type": "window", "rect": [x, y, w, h], "title": "..."}
      ]
    }

All coordinates are in *video pixels* and all times in seconds from the
first video frame.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from pathlib import Path
from . import fsutil, keymap

MODIFIERS = ("ctrl", "alt", "shift", "super")


@dataclass
class Timeline:
    w: int
    h: int
    fps: float
    duration: float
    moves_t: list = field(default_factory=list)
    moves_x: list = field(default_factory=list)
    moves_y: list = field(default_factory=list)
    clicks: list = field(default_factory=list)   # dicts t,x,y,button
    keys: list = field(default_factory=list)     # dicts t,key,mods
    windows: list = field(default_factory=list)  # dicts t,rect,title
    marks: list = field(default_factory=list)    # dicts t,x,y ("mark zoom here" hotkey)
    capture: dict = field(default_factory=dict)
    # window manager state (round 3): client snapshots, workspaces, layers, sync markers
    snapshots: list = field(default_factory=list)   # dicts t, focus, windows[{address,class,title,rect(x0,y0,x1,y1),floating,fullscreen}]
    workspaces: list = field(default_factory=list)  # dicts t, name, special
    layers: list = field(default_factory=list)      # dicts t, namespace, rect(x0,y0,x1,y1)|None, open, keyboard
    syncs: list = field(default_factory=list)       # dicts t, kind (start|stop|mid)
    toasts: list = field(default_factory=list)      # dicts t, count (notification toasts on screen)
    monitors: list = field(default_factory=list)    # dicts t, reason, name
    layout_changes: list = field(default_factory=list)  # derived: dicts t, address, reason (open|close|move|resize|focus|fullscreen)

    # ------------------------------------------------------------------ io
    @classmethod
    def from_dict(cls, data: dict) -> "Timeline":
        v = data["video"]
        tl = cls(int(v["w"]), int(v["h"]), float(v.get("fps", 60)), float(v["duration"]))
        if not (0 < tl.w <= 32768 and 0 < tl.h <= 32768 and 0 < tl.fps <= 1000 and 0 <= tl.duration <= 7 * 86400):
            raise ValueError("events.json: video size, rate or length out of range")
        tl.capture = dict(data.get("capture", {}))
        # names and paths from the file end up in ffmpeg's arguments at export (audio.py)
        from .audio import clean_spans, clean_tracks
        if "audio_tracks" in tl.capture:
            tl.capture["audio_tracks"] = clean_tracks(tl.capture["audio_tracks"])
        if "mic_mutes" in tl.capture:
            tl.capture["mic_mutes"] = clean_spans(tl.capture["mic_mutes"])
        evs = sorted(data.get("events", []), key=lambda e: e["t"])
        for e in evs:
            kind = e.get("type")
            if kind == "move":
                tl._add_move(e["t"], e["x"], e["y"])
            elif kind == "click":
                tl.clicks.append({"t": float(e["t"]), "x": float(e["x"]), "y": float(e["y"]),
                                  "button": e.get("button", "left")})
                tl._add_move(e["t"], e["x"], e["y"])
            elif kind == "key":
                # recordings made before 0.7.2 name typed characters in events.json; they
                # are dropped here too, so neither the editor page nor a render sees them
                key, mods = keymap.recorded(str(e["key"]), [m for m in e.get("mods", []) if m in MODIFIERS],
                                            bool(e.get("lalt")))
                tl.keys.append({"t": float(e["t"]), "key": key, "mods": mods})
            elif kind == "mark":
                tl.marks.append({"t": float(e["t"]), "x": float(e["x"]), "y": float(e["y"])})
            elif kind == "window":
                tl.windows.append({"t": float(e["t"]), "rect": e.get("rect"), "title": e.get("title", ""),
                                   "class": e.get("class", ""), "address": e.get("address", "")})
            elif kind == "clients":
                wins = []
                for w in e.get("windows", []):
                    x, y, ww, hh = w["rect"]
                    wins.append({**w, "rect": (float(x), float(y), float(x + ww), float(y + hh))})
                tl.snapshots.append({"t": float(e["t"]), "focus": e.get("focus"), "windows": wins})
            elif kind == "workspace":
                tl.workspaces.append({"t": float(e["t"]), "name": str(e.get("name", "")),
                                      "special": bool(e.get("special", False))})
            elif kind == "layer":
                r = e.get("rect")
                tl.layers.append({"t": float(e["t"]), "namespace": e.get("namespace", ""),
                                  "rect": (r[0], r[1], r[0] + r[2], r[1] + r[3]) if r else None,
                                  "open": bool(e.get("open", True)), "keyboard": bool(e.get("keyboard", False))})
            elif kind == "sync":
                tl.syncs.append({"t": float(e["t"]), "kind": e.get("kind", "start")})
            elif kind == "toasts":
                tl.toasts.append({"t": float(e["t"]), "count": int(e.get("count", 0))})
            elif kind == "monitor":
                tl.monitors.append({"t": float(e["t"]), "reason": e.get("reason", ""), "name": e.get("name", "")})
        if not tl.moves_t:
            tl._add_move(0.0, tl.w / 2, tl.h / 2)
        if not tl.snapshots and tl.windows:
            # legacy single focused-window events -> snapshots
            for w in tl.windows:
                if not w.get("rect"):
                    continue
                x, y, ww, hh = w["rect"]
                tl.snapshots.append({"t": w["t"], "focus": w.get("address") or "w",
                                     "windows": [{"address": w.get("address") or "w", "class": w.get("class", ""),
                                                  "title": w.get("title", ""),
                                                  "rect": (x, y, x + ww, y + hh)}]})
        tl.snapshots.sort(key=lambda s: s["t"])
        tl._snap_t = [s["t"] for s in tl.snapshots]
        tl.layout_changes = tl._diff_snapshots()
        return tl

    def _diff_snapshots(self) -> list:
        out = []
        prev: dict = {}
        prev_focus = None
        for s in self.snapshots:
            cur = {w["address"]: w for w in s["windows"]}
            prev_ws = {str(w.get("workspace")) for w in prev.values()}
            cur_ws = {str(w.get("workspace")) for w in cur.values()}
            for a, w in cur.items():
                p = prev.get(a)
                if p is None:
                    if prev or s is not self.snapshots[0]:
                        # a window that became visible because its workspace did is not "opened"
                        shown = w.get("workspace") is not None and str(w.get("workspace")) not in prev_ws
                        out.append({"t": s["t"], "address": a, "reason": "shown" if shown else "open"})
                    continue
                r0, r1 = p["rect"], w["rect"]
                if bool(p.get("fullscreen")) != bool(w.get("fullscreen")):
                    out.append({"t": s["t"], "address": a, "reason": "fullscreen"})
                elif abs((r0[2] - r0[0]) - (r1[2] - r1[0])) > 2 or abs((r0[3] - r0[1]) - (r1[3] - r1[1])) > 2:
                    out.append({"t": s["t"], "address": a, "reason": "resize"})
                elif abs(r0[0] - r1[0]) > 2 or abs(r0[1] - r1[1]) > 2:
                    out.append({"t": s["t"], "address": a, "reason": "move"})
            for a, w in prev.items():
                if a not in cur:
                    hidden = w.get("workspace") is not None and str(w.get("workspace")) not in cur_ws
                    out.append({"t": s["t"], "address": a, "reason": "hidden" if hidden else "close"})
            if s.get("focus") != prev_focus and prev_focus is not None:
                out.append({"t": s["t"], "address": s.get("focus"), "reason": "focus"})
            prev, prev_focus = cur, s.get("focus")
        return out

    @classmethod
    def load(cls, path: str | Path) -> "Timeline":
        return cls.from_dict(fsutil.read_json(path))   # damaged: .bak, else a clear DamagedFile

    def _add_move(self, t, x, y):
        t = float(t)
        if self.moves_t and t < self.moves_t[-1]:
            t = self.moves_t[-1]
        if self.moves_t and t == self.moves_t[-1]:
            self.moves_x[-1], self.moves_y[-1] = float(x), float(y)
            return
        self.moves_t.append(t)
        self.moves_x.append(float(x))
        self.moves_y.append(float(y))

    # ------------------------------------------------------------- queries
    def cursor_at(self, t: float) -> tuple[float, float]:
        """Raw cursor position, linearly interpolated between samples."""
        ts = self.moves_t
        i = bisect.bisect_right(ts, t)
        if i <= 0:
            return self.moves_x[0], self.moves_y[0]
        if i >= len(ts):
            return self.moves_x[-1], self.moves_y[-1]
        t0, t1 = ts[i - 1], ts[i]
        # Samples are only logged on change, so a long gap means the cursor
        # rested at the earlier position, then moved. Interpolate only across
        # the last ~50 ms of a gap so a rest does not become a slow drift.
        span = t1 - t0
        if span > 0.05:
            t0 = t1 - 0.05
            if t < t0:
                return self.moves_x[i - 1], self.moves_y[i - 1]
        u = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return (self.moves_x[i - 1] + (self.moves_x[i] - self.moves_x[i - 1]) * u,
                self.moves_y[i - 1] + (self.moves_y[i] - self.moves_y[i - 1]) * u)

    def shift(self, dt: float) -> "Timeline":
        """Shift every event by dt seconds (sync offset)."""
        if not dt:
            return self
        self.moves_t = [t + dt for t in self.moves_t]
        for coll in (self.clicks, self.keys, self.windows, self.marks, self.snapshots, self.workspaces,
                     self.layers, self.syncs, self.monitors, self.layout_changes):
            for e in coll:
                e["t"] += dt
        self._snap_t = None
        return self

    def snapshot_at(self, t: float):
        if not self.snapshots:
            return None
        ts = getattr(self, "_snap_t", None)
        if ts is None or len(ts) != len(self.snapshots):
            ts = self._snap_t = [x["t"] for x in self.snapshots]
        i = bisect.bisect_right(ts, t) - 1
        return self.snapshots[max(i, 0)]

    def keyboard_layer_at(self, t: float):
        """An open keyboard-grabbing layer (launcher, Omarchy menu) at t, if any."""
        state: dict = {}
        for l in self.layers:
            if l["t"] > t:
                break
            if l["open"] and l["keyboard"] and l["rect"]:
                state[l["namespace"]] = l
            else:
                state.pop(l["namespace"], None)
        return next(reversed(state.values()), None) if state else None

    def focused_window_at(self, t: float):
        """Focused window (rect as x0, y0, x1, y1 video px) or a keyboard layer, at t."""
        lay = self.keyboard_layer_at(t)
        if lay is not None:
            return {"address": "layer:" + lay["namespace"], "class": lay["namespace"], "rect": lay["rect"],
                    "layer": True}
        s = self.snapshot_at(t)
        if s is None:
            return None
        for w in s["windows"]:
            if w["address"] == s.get("focus"):
                return w
        return None

    def window_rect_at(self, t: float, address: str):
        s = self.snapshot_at(t)
        if s is None:
            return None
        for w in s["windows"]:
            if w["address"] == address:
                return w["rect"]
        return None

    def window_under(self, t: float, x: float, y: float):
        """Topmost window containing (x, y): floating ones first, then tiled."""
        s = self.snapshot_at(t)
        if s is None:
            return None
        ws = sorted(s["windows"], key=lambda w: (not w.get("floating"), w["address"] != s.get("focus")))
        for w in ws:
            r = w["rect"]
            if r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                return w
        return None

    def cursor_inside(self, t: float, margin: float = 0.0) -> bool:
        x, y = self.cursor_at(t)
        return -margin <= x <= self.w + margin and -margin <= y <= self.h + margin

    def last_pointer_before(self, t: float, min_move: float = 4.0) -> float:
        """Time of the last real pointer movement (> min_move px) or click at/before t."""
        ts = self.moves_t
        i = bisect.bisect_right(ts, t) - 1
        best = float("-inf")
        # walk back to the newest sample that is more than min_move away from the one 0.3 s
        # before it (a resting hand jitters by a few px; that is not pointer activity)
        j = i
        while i > 0:
            k = bisect.bisect_left(ts, ts[i] - 0.3)
            if abs(self.moves_x[i] - self.moves_x[k]) + abs(self.moves_y[i] - self.moves_y[k]) > min_move:
                best = ts[i]
                break
            i -= 1
            if j - i > 4000:
                break
        for c in reversed(self.clicks):
            if c["t"] <= t:
                best = max(best, c["t"])
                break
        return best

    def last_activity_before(self, t: float) -> float:
        """Time of the most recent move/click/key at or before t (or -inf)."""
        best = float("-inf")
        i = bisect.bisect_right(self.moves_t, t)
        if i > 0:
            best = self.moves_t[i - 1]
        for coll in (self.clicks, self.keys, self.marks):
            for e in reversed(coll):
                if e["t"] <= t:
                    best = max(best, e["t"])
                    break
        return best


# ------------------------------------------------------------------ typed text in older recordings
def scrub_typed_keys(project_dir: str | Path) -> int:
    """Recordings made before 0.7.2 name every key that was pressed, typed text included, in
    their event files. Rewrite those files so a typed character is only "a character was
    typed at this time" (keymap.recorded); shortcuts and Enter/Esc/Tab stay, so the take
    looks and plays as before. Returns how many key events changed. A file that cannot be
    read is left as it is; no backup of the old contents is kept."""
    import json
    import os
    d = Path(project_dir)
    changed = 0

    def fix(e) -> bool:
        if not isinstance(e, dict) or e.get("type") != "key" or "key" not in e:
            return False
        mods = e.get("mods") if isinstance(e.get("mods"), list) else []
        key, new_mods = keymap.recorded(str(e["key"]), [str(m) for m in mods], bool(e.get("lalt")))
        if key == e["key"] and new_mods == mods:
            return False
        e["key"], e["mods"] = key, new_mods
        return True

    def keep_times(p: Path, st) -> None:
        try:
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))     # caches compare times: the take did not change
        except OSError:
            pass

    for name in ("events.json", "events.json.bak"):
        p = d / name
        try:
            st = p.stat()
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        n = sum(fix(e) for e in doc.get("events", [])) if isinstance(doc, dict) else 0
        if n:
            fsutil.atomic_write_json(p, doc)
            keep_times(p, st)
            changed += n
    for name in ("events.raw.jsonl", "replay-events.jsonl"):
        p = d / name
        try:
            st = p.stat()
            lines = p.read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError):
            continue
        n = 0
        for i, line in enumerate(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if fix(e):
                lines[i] = json.dumps(e, separators=(",", ":"))
                n += 1
        if n:
            fsutil.atomic_write_text(p, "\n".join(lines) + "\n")
            keep_times(p, st)
            changed += n
    return changed

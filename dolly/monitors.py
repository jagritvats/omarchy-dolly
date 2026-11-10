"""Monitor geometry, fractional scaling and capture targets (pure functions).

Coordinate spaces
-----------------
* Hyprland global *logical* space: `hyprctl cursorpos`, client `at`/`size`,
  monitor `x`/`y`, slurp / `omarchy-capture-region` output.
* Monitor *physical* pixels: `width`/`height` from `hyprctl monitors -j`
  (before the transform).
* Video pixels: what the recorder wrote. For a monitor target this is the
  physical size (or gsr's `-s` cap); for a region gsr scales to physical
  itself. Dolly never assumes: finalize maps logical -> video with the real
  video size divided by the exact logical size of the target.

Scale
-----
`hyprctl monitors -j` prints the float scale with `{}` (Hyprland 0.53+: e.g.
1.6666666 or 1.3333334); older releases printed 2 decimals (1.67). Hyprland
only accepts scales on a 1/120 grid that divide the mode into whole logical
pixels (output/Monitor.cpp: "find the nearest valid"), so `effective_scale`
snaps the printed value back to that grid with the same search. Then the
logical size is an exact integer. Omarchy's `JQ_MONITOR_GEO` floors
`width/scale`, which is 1 px short at 1.3333334 (2560/1.3333334 = 1919.9999)
and 3-4 px short when the scale is printed as 1.67; `same_size` compares with
a tolerance so a `monitor:NAME` pick and Dolly's own geometry agree either way.

Transforms 1/3 (90/270) and 5/7 (flipped + 90/270) swap width and height.
Omarchy swaps only for 1/3; Dolly handles all four.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def effective_scale(m: dict) -> float:
    s = float(m.get("scale") or 1.0)
    if s <= 0:
        return 1.0
    w, h = int(m.get("width", 0)), int(m.get("height", 0))
    if not w or not h:
        return s

    def clean(sc):
        return abs(w / sc - round(w / sc)) < 1e-6 and abs(h / sc - round(h / sc)) < 1e-6

    if clean(s):
        return s
    base = round(s * 120.0)
    if clean(base / 120.0):
        return base / 120.0
    for i in range(1, 90):
        for cand in ((base + i) / 120.0, (base - i) / 120.0):
            if cand > 0 and clean(cand):
                return cand
    return s          # misc:disable_scale_checks: fractional logical pixels


def rotated(m: dict) -> bool:
    return int(m.get("transform", 0) or 0) in (1, 3, 5, 7)


def logical_rect(m: dict) -> list[int]:
    """[x, y, w, h] of the monitor in global logical space."""
    s = effective_scale(m)
    w, h = m["width"], m["height"]
    if rotated(m):
        w, h = h, w
    return [int(m.get("x", 0)), int(m.get("y", 0)), int(round(w / s)), int(round(h / s))]


def logical_size_exact(m: dict) -> tuple[float, float]:
    s = effective_scale(m)
    w, h = m["width"], m["height"]
    if rotated(m):
        w, h = h, w
    return w / s, h / s


def omarchy_geo(m: dict) -> list[int]:
    """What Omarchy's JQ_MONITOR_GEO computes (floor, 1/3-only swap); for comparisons."""
    import math
    s = float(m.get("scale") or 1.0)
    w, h = math.floor(m["width"] / s), math.floor(m["height"] / s)
    if int(m.get("transform", 0) or 0) in (1, 3):
        w, h = h, w
    return [int(m.get("x", 0)), int(m.get("y", 0)), w, h]


def same_size(a, b, tol: int = 4) -> bool:
    return all(abs(int(x) - int(y)) <= tol for x, y in zip(a, b))


def physical_size(m: dict) -> tuple[int, int]:
    """Video size of a full-monitor capture (the rotated mode)."""
    w, h = int(m["width"]), int(m["height"])
    return (h, w) if rotated(m) else (w, h)


def overlap(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = min(ax + aw, bx + bw) - max(ax, bx)
    h = min(ay + ah, by + bh) - max(ay, by)
    return max(0, w) * max(0, h)


def contains_point(r, x, y) -> bool:
    return r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]


def monitor_at(mons: list[dict], x: float, y: float) -> dict | None:
    for m in mons:
        if not m.get("disabled") and contains_point(logical_rect(m), x, y):
            return m
    return None


def monitor_for_rect(mons: list[dict], rect) -> tuple[dict | None, float]:
    """The monitor holding most of `rect` and the fraction of rect on it."""
    area = max(1, rect[2] * rect[3])
    best, frac = None, 0.0
    for m in mons:
        if m.get("disabled"):
            continue
        f = overlap(rect, logical_rect(m)) / area
        if f > frac:
            best, frac = m, f
    return best, frac


def clamp_rect(rect, bound) -> list[int]:
    x0 = max(rect[0], bound[0])
    y0 = max(rect[1], bound[1])
    x1 = min(rect[0] + rect[2], bound[0] + bound[2])
    y1 = min(rect[1] + rect[3], bound[1] + bound[3])
    return [x0, y0, max(0, x1 - x0), max(0, y1 - y0)]


def parse_geo(s: str) -> list[int] | None:
    """slurp "X,Y WxH" -> [x, y, w, h]."""
    try:
        xy, wh = s.strip().split()
        x, y = (int(v) for v in xy.split(","))
        w, h = (int(v) for v in wh.split("x"))
        return [x, y, w, h]
    except (ValueError, AttributeError):
        return None


@dataclass
class Target:
    """What to record and how to map it.

    kind      "monitor" (full output, physical pixels) or "region" (logical rect)
    monitor   the monitor the target lives on (never just the focused one)
    region    logical [x, y, w, h], even w/h for regions
    scale     effective scale of that monitor
    exact     exact logical size of the target (float; for video mapping)
    note      user-facing note (e.g. a region spanning two monitors was clamped)
    """
    kind: str
    monitor: str
    region: list[int]
    scale: float
    exact: tuple[float, float]
    transform: int = 0
    note: str = ""
    physical: tuple[int, int] = (0, 0)
    extra: dict = field(default_factory=dict)

    def gsr_args(self, max_res: str | None = None) -> list[str]:
        """Same targets as omarchy-capture-screenrecording: -w NAME (+ -s cap), or
        -w WxH+X+Y in logical coordinates (gsr scales to physical itself)."""
        if self.kind == "monitor":
            pw, ph = self.physical
            res = max_res or ("3840x2160" if pw > 3840 or ph > 2160 else "0x0")
            return ["-w", self.monitor, "-s", res]
        x, y, w, h = self.region
        return ["-w", f"{w}x{h}+{x}+{y}"]

    def wf_args(self) -> list[str]:
        if self.kind == "monitor":
            return ["-o", self.monitor]
        x, y, w, h = self.region
        return ["-g", f"{x},{y} {w}x{h}"]

    def expected_video(self) -> tuple[int, int]:
        if self.kind == "monitor":
            return self.physical
        w, h = self.region[2], self.region[3]
        return int(round(w * self.scale)), int(round(h * self.scale))

    def to_meta(self) -> dict:
        return {"kind": self.kind, "monitor": self.monitor, "region": self.region, "scale": self.scale,
                "exact": list(self.exact), "transform": self.transform, "note": self.note,
                "physical": list(self.physical), **self.extra}


class SpanError(ValueError):
    pass


def monitor_target(m: dict, note: str = "") -> Target:
    r = logical_rect(m)
    return Target("monitor", m["name"], r, effective_scale(m), logical_size_exact(m),
                  int(m.get("transform", 0) or 0), note, physical_size(m))


def resolve_pick(pick: str, mons: list[dict], min_frac: float = 0.5) -> Target:
    """Turn what `omarchy-capture-region smart --match-monitor` printed into a Target.

    * "monitor:NAME" -> that monitor, recorded whole (-w NAME / wf -o NAME).
    * "X,Y WxH" that equals a monitor (within the floor/round tolerance) -> the same.
    * a region -> the monitor that holds it (not the focused one). A region
      spanning two monitors is clamped to the monitor holding most of it (one
      output = one scale = a sharp video); with less than `min_frac` of it on
      any single monitor the pick is refused (SpanError) with a clear message.
    """
    pick = (pick or "").strip()
    live = [m for m in mons if not m.get("disabled")]
    if pick.startswith("monitor:"):
        name = pick.split(":", 1)[1]
        m = next((m for m in live if m.get("name") == name), None)
        if not m:
            raise SpanError(f"monitor {name} is not connected")
        return monitor_target(m)
    rect = parse_geo(pick)
    if not rect:
        raise SpanError(f"unexpected picker output: {pick!r}")
    for m in live:
        lr = logical_rect(m)
        if same_size(rect, lr, 4) or same_size(rect, omarchy_geo(m), 1):
            return monitor_target(m)
    m, frac = monitor_for_rect(live, rect)
    if not m or frac <= 0:
        raise SpanError("the picked area is not on any monitor")
    note = ""
    if frac < 0.999:
        if frac < min_frac:
            raise SpanError("the picked area spans monitors with no clear majority; "
                            "pick an area on one monitor")
        rect = clamp_rect(rect, logical_rect(m))
        note = f"area spanned two monitors; recording the part on {m['name']}"
    s = effective_scale(m)
    x, y, w, h = snap_even_physical(rect, logical_rect(m), s)
    if w < 16 or h < 16:
        raise SpanError("the picked area is too small")
    t = Target("region", m["name"], [x, y, w, h], s, (float(w), float(h)),
               int(m.get("transform", 0) or 0), note, (int(round(w * s)), int(round(h * s))))
    mr = logical_rect(m)
    t.extra["physical_rect"] = [int(round((x - mr[0]) * s)), int(round((y - mr[1]) * s)), t.physical[0], t.physical[1]]
    return t


def _grid(s: float, even: bool) -> int:
    """Smallest logical step q with q*s a whole (even) number of physical pixels."""
    for q in range(1, 61):
        v = q * s
        if abs(v - round(v)) < 1e-6 and (not even or round(v) % 2 == 0):
            return q
    return 2 if even else 1


def snap_even_physical(rect, mon_rect, s: float) -> list[int]:
    """Expand a logical pick so it starts on a whole physical pixel and spans an EVEN
    whole number of physical pixels (encoders round odd sizes: wf-recorder made a
    1003.33 px wide pick at 1.6667 a 1002 px video), staying on the monitor."""
    mx, my, mw, mh = mon_rect
    out = []
    for (o, length, mo, ml) in ((rect[0], rect[2], mx, mw), (rect[1], rect[3], my, mh)):
        g0, g = _grid(s, False), _grid(s, True)
        a = mo + ((o - mo) // g0) * g0                     # start: down onto the pixel grid
        end = o + length
        n = -(-(end - a) // g) * g                          # length: up to an even physical size
        if a + n > mo + ml:                                 # off the monitor: shift back, else shrink
            a = max(mo, mo + ((mo + ml - n - mo) // g0) * g0)
            if a + n > mo + ml:
                n = ((mo + ml - a) // g) * g
        out.append((a, n))
    return [out[0][0], out[1][0], out[0][1], out[1][1]]


# ------------------------------------------------------------ mapping helpers
def video_scale(region_exact: tuple[float, float], video_wh: tuple[int, int]) -> tuple[float, float]:
    """Logical -> video px factors from the real video size (handles gsr -s caps,
    even-dimension rounding and fractional scales)."""
    ew, eh = region_exact
    vw, vh = video_wh
    return (vw / ew if ew else 1.0, vh / eh if eh else 1.0)


def to_video(x: float, y: float, region, sx: float, sy: float) -> tuple[float, float]:
    return (x - region[0]) * sx, (y - region[1]) * sy

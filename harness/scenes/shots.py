#!/usr/bin/env python3
"""Print the camera plan of a take as numbers: one line per shot.

    python3 harness/scenes/shots.py TAKE_DIR [--json] [--actions] [--root OTHER_CHECKOUT]

16:9: every planned zoom shot: start, end, the zoom the camera really reaches in it (a
shot is planned at one zoom but plays wider when something bigger has to fit), the planned
zoom, the reason and what it shows.
9:16: for the same moments, the crop width in "strips" (1.00 = the full-height strip that
fills the card; above 1 the picture is letterboxed, with bands above and below) and the
part of the screen it shows; then for how long the take is letterboxed, and how widely.

The plan is recomputed with the code next to this script (planning needs no recording and
no render), so a take recorded once can be compared before and after a change:

    python3 harness/scenes/shots.py ~/Videos/Dolly/2026-11-10_21-17-44
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if "--root" in sys.argv:                 # plan with another checkout's code (before / after)
    ROOT = Path(sys.argv[sys.argv.index("--root") + 1]).expanduser().resolve()
    del sys.argv[sys.argv.index("--root"):sys.argv.index("--root") + 2]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from dolly import project as projmod  # noqa: E402
from dolly.events import Timeline  # noqa: E402
from dolly.reframe import reframe_path  # noqa: E402
from dolly.render import load_aoi  # noqa: E402
from dolly import zoom as zoommod  # noqa: E402
from dolly.zoom import CameraGeometry, ZoomConfig, auto_focus, build_segments, camera_path, extract_actions  # noqa: E402

VERTICAL = 9 / 16 * (1 - 2 * 0.06) / (1 - 2 * 0.06 * 9 / 16)   # the 9:16 card inside its padding


def plan(take: Path) -> dict:
    doc = json.loads((take / "events.json").read_text())
    tl = Timeline.from_dict(doc)
    data = projmod.Project(take).load()
    cfg = ZoomConfig.from_dict(data.get("zoom_config"))
    aoi = load_aoi(take, tl, cfg)
    segs = build_segments(tl, cfg, aoi)
    acts = extract_actions(tl, cfg, aoi if cfg.use_aoi else None)
    # a countdown recorded before the take ("go") is not part of the clip
    go = zoommod.take_start(tl) if hasattr(zoommod, "take_start") else 0.0
    segs = [s for s in segs if s.end > go]
    fps = 30.0
    crop = reframe_path(tl, segs, VERTICAL, fps, 0.0, tl.duration, cfg, aoi=aoi)
    for arr in ("cx", "cy", "cw", "ch"):                   # the clip starts at "go"
        getattr(crop, arr)[:int(go * fps)] = getattr(crop, arr)[min(int(go * fps), len(crop.cw) - 1)]
    strip = min(tl.h, tl.w / VERTICAL) * VERTICAL          # the widest crop that fills the card
    cam = camera_path(tl, segs, CameraGeometry(tl.w, tl.h, 0, 0), fps, 0.0, tl.duration, cfg, aoi=aoi)
    shots = []
    for s in segs:
        fx, fy = auto_focus(tl, s, cfg, aoi)
        zz = cam.z[int(s.start * fps):max(int(s.start * fps) + 1, int(s.end * fps))]
        # the level it settles at: the springs need the first part of a shot to get there
        played = float(np.median(zz[int(len(zz) * 0.6):])) if len(zz) else s.zoom
        i0, i1 = int(s.start * fps), max(int(s.start * fps) + 1, int(s.end * fps))
        cw = crop.cw[i0:i1]
        mid = min(len(crop.cw) - 1, (i0 + i1) // 2)
        shots.append({
            "id": s.id, "start": round(s.start, 2), "end": round(s.end, 2), "zoom": round(played, 2),
            "planned": round(s.zoom, 2),
            "reason": s.reason, "view": [round(tl.w / played), round(tl.h / played)], "centre": [round(fx), round(fy)],
            "v_strips": [round(float(cw.min()) / strip, 2), round(float(cw.max()) / strip, 2)],
            "v_crop_mid": [round(float(crop.cx[mid] - crop.cw[mid] / 2)), round(float(crop.cx[mid] + crop.cw[mid] / 2))],
        })
    def spans_of(flags):
        out, start = [], None
        for i, f in enumerate(np.append(flags, False)):
            if f and start is None:
                start = i
            elif not f and start is not None:
                if (i - start) / fps >= 0.3:
                    out.append([round(start / fps, 2), round(i / fps, 2)])
                start = None
        return out
    spans = spans_of(crop.cw >= 0.97 * tl.w)               # (nearly) the whole desktop width
    boxed = spans_of(crop.cw > 1.05 * strip)
    wide = spans_of(crop.cw > 1.4 * strip)                 # more than a long typed line may get (1.36)
    return {"take": take.name, "screen": [tl.w, tl.h], "duration": round(tl.duration, 2), "go": round(go, 2),
            "line_h": round(float(getattr(aoi, "line_h", 0) or 0), 1), "shots": shots,
            "v_full_desktop": spans, "v_full_desktop_s": round(sum(b - a for a, b in spans), 2),
            "v_letterboxed_s": round(sum(b - a for a, b in boxed), 2),
            "v_large_bands": wide, "v_large_bands_s": round(sum(b - a for a, b in wide), 2),
            "v_widest_strips": round(float(crop.cw.max()) / strip, 2),
            "actions": [{"t": round(a.t, 2), "kind": a.kind,
                         "rect": [round(v) for v in a.rect] if a.rect else None} for a in acts]}


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    p = plan(Path(args[0]).expanduser())
    if "--json" in argv:
        print(json.dumps(p))
        return 0
    print(f"{p['take']}  {p['screen'][0]}x{p['screen'][1]}  {p['duration']} s  text line {p['line_h']} px"
          + (f"  take starts at {p['go']} s" if p["go"] else ""))
    print("16:9 shots")
    for s in p["shots"]:
        print(f"  {s['id']:4s} {s['start']:6.2f}-{s['end']:6.2f}  {s['zoom']:.2f}x (planned {s['planned']:.2f})  {s['reason'] or '-':16s} "
              f"shows {s['view'][0]}x{s['view'][1]} px around {s['centre'][0]},{s['centre'][1]}")
    if not p["shots"]:
        print("  (none: the whole take stays wide)")
    print("9:16 crop (width in strips: 1.00 fills the card, more is letterboxed)")
    for s in p["shots"]:
        lo, hi = s["v_strips"]
        print(f"  {s['id']:4s} {s['start']:6.2f}-{s['end']:6.2f}  {lo:.2f}-{hi:.2f} strips  shows x {s['v_crop_mid'][0]}-{s['v_crop_mid'][1]}")
    print(f"  letterboxed for {p['v_letterboxed_s']} s, widest {p['v_widest_strips']} strips; "
          f"large bands (over 1.4 strips) for {p['v_large_bands_s']} s: {p['v_large_bands']}")
    print(f"  whole desktop for {p['v_full_desktop_s']} s: {p['v_full_desktop']}")
    if "--actions" in argv:
        print("actions")
        for a in p["actions"]:
            print(f"  {a['t']:6.2f} {a['kind']:7s} {a['rect']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

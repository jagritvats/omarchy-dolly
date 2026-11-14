"""How long the drawing layer (dolly/gfx.py) takes for what a frame is made of, on this machine.

    python3 harness/bench/gfx_bench.py [TAKE_DIR] [--size 1920x1080]

One core, best of three, in milliseconds. With a take, its frame at 1/3 of the recording is
the picture; without, a drawn one of 1920x1200. Needs cairo and ffmpeg only.
"""
import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dolly import gfx  # noqa: E402
from dolly.ff import ffmpeg_bin, probe  # noqa: E402


def ms(fn, n=5):
    best = 1e9
    for _ in range(3):
        t = time.perf_counter()
        for _ in range(n):
            fn()
        best = min(best, (time.perf_counter() - t) / n)
    return best * 1000


def source(take):
    if take:
        raw = next(p for p in (Path(take) / "raw.mkv", Path(take) / "raw.mp4") if p.exists())
        info = probe(raw)
        v = next(s for s in info["streams"] if s.get("codec_type") == "video")
        w, h, at = int(v["width"]), int(v["height"]), float(info["format"]["duration"]) / 3
        p = subprocess.Popen([ffmpeg_bin(), "-v", "error", "-ss", f"{at:.3f}", "-i", str(raw), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "bgr0", "-"], stdout=subprocess.PIPE)
        f = gfx.read_frame(p.stdout, w, h)
        p.wait()
        return f
    f = gfx.new(1920, 1200)
    c = gfx.context(f)
    c.set_source(gfx.linear_gradient(0, 0, 1920, 1200, [(0, gfx.rgb("#1a1b26")), (1, gfx.rgb("#414868"))]))
    c.paint()
    mono = gfx.font("JetBrainsMono Nerd Font, JetBrains Mono, monospace", 15)
    for y in range(40, 1180, 20):
        gfx.draw_text(c, "$ dolly export --quality high   # the quick brown fox jumps over the lazy dog 0123456789" * 2,
                      24, y, mono, gfx.rgb("#c0caf5"))
    return f


ap = argparse.ArgumentParser()
ap.add_argument("take", nargs="?")
ap.add_argument("--size", default="1920x1080")
a = ap.parse_args()
OW, OH = (int(v) for v in a.size.split("x"))
src = source(a.take)
W, H = src.get_width(), src.get_height()
bg = gfx.new(OW, OH)
gfx.fill_rect(bg, 0, 0, OW, OH, gfx.rgb("#24283b"))
gfx.add_noise(bg)
sprite = gfx.new(160, 220, alpha=True)
gfx.disc(sprite, 80, 110, 70, (1, 1, 1), 0.9)
print(f"source {W}x{H}, output {OW}x{OH}, text by {gfx.text_engine()}")
rows = [("copy of an output frame", lambda: gfx.copy(bg))]
fit = min(OW / W, OH / H)
for label, k in (("card at rest (0.87 of fit)", 0.87 * fit), ("zoomed 1.5x", 1.3 * fit), ("zoomed 2x", 1.74 * fit),
                 ("shrunk to 0.65", 0.65), ("shrunk to 0.45", 0.45)):
    tx, ty = OW / 2 - W / 2 * k + 0.37, OH / 2 - H / 2 * k + 0.61

    def card(k=k, tx=tx, ty=ty, flt=None):
        c = gfx.context(gfx.copy(bg))
        gfx.rounded_rect(c, *gfx.placed_box(W, H, k, k, tx, ty), 14 * k)
        c.clip()
        gfx.place(c, src, k, k, tx, ty, flt, gfx.PAD)
    rows.append((f"frame in a rounded card, {label} (k={k:.2f})", card))
    if k > 1:
        rows.append(('  the same with filter="sharp"', lambda card=card: card(flt="sharp")))
rows += [
    ("background moved with the camera (place, PAD)", lambda: gfx.place(gfx.copy(bg), bg, 1.2, 1.2, -100.3, -60.6, extend=gfx.PAD)),
    ("cursor sprite placed at a fraction of a pixel", lambda: gfx.place(bg, sprite, 0.4, 0.4, 700.3, 400.6)),
    ("ripple: ring and disc", lambda: (gfx.ring(bg, 900.3, 500.2, 60, 5, (1, 1, 1), 0.5), gfx.disc(bg, 900.3, 500.2, 60, (1, 1, 1), 0.1))),
    ("privacy: blur_rect 800x320, sigma 20", lambda: gfx.blur_rect(src, 300, 200, 800, 320, 20)),
    ("privacy: pixelate_rect 800x320", lambda: gfx.pixelate_rect(src, 300, 200, 800, 320, 53)),
    ("blur of the whole source, sigma 20", lambda: gfx.blur(src, 20)),
    ("blur of the whole source, sigma 3", lambda: gfx.blur(src, 3)),
    ("shadow mask of the card (soft_rect, sigma 0.03 x width)", lambda: gfx.soft_rect(OW, OH, 0.07 * OW, 0.08 * OH, 0.86 * OW, 0.84 * OH, 0.03 * OW, 14)),
    ("cross-fade of two output frames (mix)", lambda: gfx.mix(bg, bg, 0.3)),
    ("noise on an output frame", lambda: gfx.add_noise(gfx.copy(bg))),
    ("a line of text, 26 px", lambda: gfx.draw_text(bg, "Ctrl + Shift + P", 400, 300, gfx.font(None, 26, True), (1, 1, 1))),
    ("difference of two sources and threshold", lambda: gfx.threshold(gfx.difference(src, src), 12)),
    ("PNG of an output frame", lambda: gfx.save(bg, png)),
]
png = Path(tempfile.gettempdir()) / "dolly-gfx-bench.png"
for label, fn in rows:
    print(f"{ms(fn):8.2f} ms  {label}")
png.unlink(missing_ok=True)

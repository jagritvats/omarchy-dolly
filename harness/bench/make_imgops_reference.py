"""Regenerate tests/data/imgops_reference.npz: what OpenCV gives for the operations
dolly/imgops.py replaced, on small fixed pictures.

    python3 harness/bench/make_imgops_reference.py

This is the only file in the repository that needs OpenCV (python-opencv), and only to
remake the frozen file; the tests read the file and never import OpenCV. Dolly's picture was
tuned against OpenCV's until 0.7.0, so its results stay the yardstick for tests/test_imgops.py.
"""
import sys
from pathlib import Path

import cv2  # noqa: I001  (see the note above: reference generation only)
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
rng = np.random.default_rng(3)


def photo(h, w, c=3):
    """Smooth structure plus text-like hard edges: closer to a desktop than white noise."""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    base = 120 + 60 * np.sin(xs / 17.0) * np.cos(ys / 11.0)
    img = np.repeat(base[..., None], c, axis=2)
    for _ in range(60):
        x, y = int(rng.integers(0, w - 20)), int(rng.integers(0, h - 8))
        img[y:y + int(rng.integers(1, 7)), x:x + int(rng.integers(2, 20))] = rng.integers(0, 255, c)
    return np.clip(img, 0, 255).astype(np.uint8)


d = {}
src = photo(90, 144)                      # a small "screen", 16:10 like 1920x1200
d["src"] = src
mask = np.zeros((90, 144), np.float32)
mask[10:70, 20:120] = 1.0
d["mask"] = mask
card = photo(60, 96)
d["card"] = card

# resize
d["resize_linear_up"] = cv2.resize(src, (240, 150), interpolation=cv2.INTER_LINEAR)
d["resize_cubic_up"] = cv2.resize(src, (240, 150), interpolation=cv2.INTER_CUBIC)
d["resize_area_down"] = cv2.resize(src, (115, 72), interpolation=cv2.INTER_AREA)        # x0.8, like fitting the screen
d["resize_area_eighth"] = cv2.resize(src, (18, 11), interpolation=cv2.INTER_AREA)       # the privacy blur's first step
d["resize_nearest_up"] = cv2.resize(src[::8, ::8], (144, 90), interpolation=cv2.INTER_NEAREST)
d["resize_mask_linear"] = cv2.resize(mask[::4, ::4].copy(), (144, 90), interpolation=cv2.INTER_LINEAR)

# place (scale + shift)
for name, k, tx, ty, interp in (("warp_linear_shift", 1.0, 3.25, -5.5, cv2.INTER_LINEAR),
                                ("warp_linear_zoom", 1.8, -70.3, -31.7, cv2.INTER_LINEAR),
                                ("warp_cubic_zoom", 1.8, -70.3, -31.7, cv2.INTER_CUBIC)):
    M = np.float32([[k, 0, tx], [0, k, ty]])
    d[name] = cv2.warpAffine(src, M, (144, 90), flags=interp, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    d[name + "_M"] = M
# what Dolly did until 0.7.0 to show the screen smaller than recorded: shrink by area, then place
small = cv2.resize(src, (115, 72), interpolation=cv2.INTER_AREA)
M = np.float32([[144 * 0.8 / 115, 0, 14.3], [0, 90 * 0.8 / 72, 9.6]])
d["place_small_old_way"] = cv2.warpAffine(small, M, (144, 90), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
d["place_small_M"] = np.float32([[0.8, 0, 14.3], [0, 0.8, 9.6]])          # the same placement in one step
M = np.float32([[1.3, 0, -21.5], [0, 1.3, 7.25]])
d["warp_reflect"] = cv2.warpAffine(src, M, (144, 90), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
d["warp_reflect_M"] = M
M = np.float32([[0.9, 0, 6.5], [0, 0.9, 4.25]])
d["warp_mask"] = cv2.warpAffine(mask, M, (144, 90), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
d["warp_mask_M"] = M

# blur
d["blur_u8_2p5"] = cv2.GaussianBlur(src, (0, 0), 2.5)
d["blur_mask_small"] = cv2.GaussianBlur(mask, (0, 0), 1.5)
d["blur_mask_large"] = cv2.GaussianBlur(mask, (0, 0), 8.0)
rect = np.zeros((90, 144), np.float32)
cv2.rectangle(rect, (30, 20), (100, 60), 1.0, -1)
d["soft_rect"] = cv2.GaussianBlur(rect, (0, 0), 6.5)
edge = np.zeros((90, 144), np.float32)
cv2.rectangle(edge, (-5, 40), (60, 120), 1.0, -1)          # runs off two sides, like a card at the frame's edge
d["soft_rect_edge"] = cv2.GaussianBlur(edge, (0, 0), 6.5)

# composite
other = src[::-1, ::-1].copy()
d["other"] = other
soft = cv2.GaussianBlur(mask, (0, 0), 3.0)
d["soft"] = soft
d["blend"] = cv2.blendLinear(src, other, soft, 1.0 - soft)
d["mix_0p3"] = cv2.addWeighted(src, 0.7, other, 0.3, 0)

# shapes
m = np.zeros((90, 144), np.float32)
cv2.rectangle(m, (12, 9), (60, 40), 1.0, -1)
d["fill_rect"] = m
m = np.zeros((90, 144), np.float32)
cv2.circle(m, (70, 45), 20, 1.0, -1)
d["disc"] = m
sh = 4
m = np.zeros((90, 144), np.float32)
cv2.circle(m, (int(round(70.3 * 16)), int(round(44.6 * 16))), int(round(22.4 * 16)), 1.0, -1, cv2.LINE_AA, sh)
d["disc_smooth"] = m
m = np.zeros((90, 144), np.float32)
cv2.circle(m, (int(round(70.3 * 16)), int(round(44.6 * 16))), int(round(22.4 * 16)), 1.0, 3, cv2.LINE_AA, sh)
d["ring_smooth"] = m

# masks
bits = (rng.random((60, 96)) > 0.86).astype(np.uint8)
bits[20:28, 30:70] = 1
d["bits"] = bits
d["dilate_1x5_twice"] = cv2.dilate(bits, np.ones((1, 5), np.uint8), iterations=2)
d["erode_3"] = cv2.erode((bits * 255).astype(np.uint8), np.ones((3, 3), np.uint8))
d["close_5"] = cv2.morphologyEx(bits, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
n, lab, stats, _ = cv2.connectedComponentsWithStats(d["close_5"], connectivity=8)
d["regions_count"] = np.array(n)
d["regions_labels"] = lab.astype(np.int32)
d["regions_stats"] = stats.astype(np.int32)

d["gray"] = cv2.cvtColor(src, cv2.COLOR_BGR2GRAY)

out = ROOT / "tests" / "data" / "imgops_reference.npz"
out.parent.mkdir(parents=True, exist_ok=True)
np.savez_compressed(out, **d)
print(f"{out}: {len(d)} arrays, {out.stat().st_size} bytes, OpenCV {cv2.__version__}")
sys.exit(0)

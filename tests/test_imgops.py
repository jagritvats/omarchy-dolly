"""dolly/imgops.py against frozen OpenCV results (tests/data/imgops_reference.npz, made by
harness/bench/make_imgops_reference.py). Dolly's picture was tuned against OpenCV's until
0.7.0, so the bounds say how close each operation must stay; OpenCV is not needed here.

Bounds are in levels of 255 for frames and in mask units (0..1) for float planes. Measured
when the file was frozen (Pillow 13.0, NumPy 2.5): each bound has the measured value beside it.
"""
from pathlib import Path

import numpy as np
import pytest

from dolly import imgops

REF = np.load(Path(__file__).resolve().parent / "data" / "imgops_reference.npz")
SRC, MASK, OTHER, SOFT, BITS = REF["src"], REF["mask"], REF["other"], REF["soft"], REF["bits"]


def far(got, ref, rows=slice(None), cols=slice(None)):
    """(largest, mean) absolute difference from the frozen result, over a region."""
    ref = REF[ref] if isinstance(ref, str) else ref
    got = np.asarray(got)
    assert got.shape == ref.shape and got.dtype == ref.dtype, (got.shape, ref.shape, got.dtype, ref.dtype)
    d = np.abs(got[rows, cols].astype(np.float64) - ref[rows, cols].astype(np.float64))
    return float(d.max()), float(d.mean())


# ------------------------------------------------------------------ resize
def test_enlarging_bilinear_matches():
    assert far(imgops.resize(SRC, (240, 150), imgops.LINEAR), "resize_linear_up")[0] <= 1
    assert far(imgops.resize(MASK[::4, ::4].copy(), (144, 90), imgops.LINEAR), "resize_mask_linear")[0] <= 0.001


def test_enlarging_bicubic_is_close():
    # Pillow's bicubic is a little softer than OpenCV's: small on average, larger at hard edges
    mx, mean = far(imgops.resize(SRC, (240, 150), imgops.CUBIC), "resize_cubic_up")
    assert mean <= 1.0 and mx <= 20          # measured 0.46 and 14


def test_shrinking_counts_every_source_pixel():
    # against OpenCV's area averaging; bilinear widens its kernel instead, a touch smoother
    assert far(imgops.resize(SRC, (115, 72)), "resize_area_down")[1] <= 1.0             # measured 0.62
    assert far(imgops.resize(SRC, (18, 11)), "resize_area_eighth")[1] <= 4.5            # measured 2.96
    flat = np.full((90, 144, 3), 77, np.uint8)
    assert np.all(imgops.resize(flat, (115, 72)) == 77)             # a flat picture stays flat
    # no source pixel is skipped: one bright column in a dark picture always leaves a trace
    for x in range(0, 144, 7):
        dark = np.zeros((90, 144), np.uint8)
        dark[:, x] = 255
        assert imgops.resize(dark, (36, 22)).max() > 0, x


def test_nearest_makes_blocks_from_the_top_left():
    assert far(imgops.resize(SRC[::8, ::8], (144, 90), imgops.NEAREST), "resize_nearest_up") == (0.0, 0.0)


def test_same_size_is_a_copy():
    out = imgops.resize(SRC, (144, 90))
    assert out is not SRC and np.array_equal(out, SRC)


def test_float_planes_and_sprites_keep_their_type():
    sprite = np.dstack([MASK, MASK * 0.5, MASK * 0.25, MASK]).astype(np.float32)
    out = imgops.resize(sprite, (72, 45))
    assert out.shape == (45, 72, 4) and out.dtype == np.float32 and 0.0 <= out.min() and out.max() <= 1.0
    assert imgops.warp(sprite, np.float32([[1, 0, 2.5], [0, 1, 1.5]]), (150, 95)).shape == (95, 150, 4)


# ------------------------------------------------------------------ place
def test_placing_with_a_shift_matches_inside():
    out = imgops.warp(SRC, REF["warp_linear_shift_M"], (144, 90), imgops.LINEAR)
    # the picture ends at column 3.25 and row 83.5: beyond that line everything is empty
    assert np.all(out[:, :4] == 0) and np.all(out[84:] == 0)
    assert far(out, "warp_linear_shift", slice(2, 82), slice(6, 142))[0] <= 1


def test_placing_enlarged_matches():
    assert far(imgops.warp(SRC, REF["warp_linear_zoom_M"], (144, 90), imgops.LINEAR), "warp_linear_zoom")[0] <= 1
    mx, mean = far(imgops.warp(SRC, REF["warp_cubic_zoom_M"], (144, 90), imgops.CUBIC), "warp_cubic_zoom")
    assert mean <= 1.2 and mx <= 20          # measured 0.67 and 13


def test_placing_smaller_in_one_step_looks_like_shrink_then_place():
    """Until 0.7.0 the screen was shrunk by area and then placed; now one resample does both."""
    out = imgops.warp(SRC, REF["place_small_M"], (144, 90), imgops.LINEAR)
    assert np.all(out[:10] == 0) and np.all(out[:, :15] == 0) and np.all(out[81:] == 0) and np.all(out[:, 129:] == 0)
    assert out[10:81, 15:129].min() > 0
    assert far(out, "place_small_old_way", slice(13, 78), slice(18, 126))[1] <= 3.5      # measured 2.3 on hard edges


def test_placing_with_mirrored_edges_matches():
    out = imgops.warp(SRC, REF["warp_reflect_M"], (144, 90), imgops.LINEAR, border=imgops.REFLECT)
    assert far(out, "warp_reflect")[0] <= 1
    for edge in (out[0], out[-1], out[:, 0], out[:, -1]):
        assert edge.mean() > 60                       # no empty border anywhere: the picture is mirrored into it


def test_placing_a_mask_matches():
    assert far(imgops.warp(MASK, REF["warp_mask_M"], (144, 90)), "warp_mask")[0] <= 0.06      # measured 0.04


def test_placing_refuses_a_rotation():
    with pytest.raises(ValueError):
        imgops.warp(SRC, np.float32([[1, 0.2, 0], [0, 1, 0]]), (144, 90))


def test_placing_far_outside_gives_an_empty_picture():
    out = imgops.warp(SRC, np.float32([[1, 0, 500], [0, 1, 0]]), (144, 90), fill=7)
    assert out.shape == SRC.shape and np.all(out == 7)


# ------------------------------------------------------------------ blur / composite
def test_gaussian_blur_matches():
    assert far(imgops.gaussian_blur(SRC, 2.5), "blur_u8_2p5")[0] <= 1
    assert far(imgops.gaussian_blur(MASK, 1.5), "blur_mask_small")[0] <= 0.001
    assert far(imgops.gaussian_blur(MASK, 8.0), "blur_mask_large")[0] <= 0.001          # the frequency-domain path


def test_soft_rect_is_the_blurred_rectangle():
    assert far(imgops.soft_rect((90, 144), (30, 20), (100, 60), 6.5), "soft_rect")[0] <= 0.001
    assert far(imgops.soft_rect((90, 144), (-5, 40), (60, 120), 6.5), "soft_rect_edge")[0] <= 0.001


def test_blend_and_mix_match():
    assert far(imgops.blend(SRC, OTHER, SOFT), "blend")[0] <= 1
    assert far(imgops.mix(SRC, OTHER, 0.3), "mix_0p3")[0] <= 1
    assert np.array_equal(imgops.blend(SRC, OTHER, np.ones_like(SOFT)), SRC)
    assert np.array_equal(imgops.blend(SRC, OTHER, np.zeros_like(SOFT)), OTHER)
    assert np.array_equal(imgops.mix(SRC, OTHER, 0.0), SRC) and np.array_equal(imgops.mix(SRC, OTHER, 1.0), OTHER)


def test_blend_of_float_planes():
    a, b = np.full((4, 6), 1.0, np.float32), np.zeros((4, 6), np.float32)
    out = imgops.blend(a, b, np.full((4, 6), 0.25, np.float32))
    assert out.dtype == np.float32 and np.allclose(out, 0.25)


# ------------------------------------------------------------------ shapes
def blank():
    return np.zeros((90, 144), np.float32)


def test_shapes_match():
    assert far(imgops.fill_rect(blank(), (12, 9), (60, 40), 1.0), "fill_rect") == (0.0, 0.0)
    assert far(imgops.circle(blank(), (70, 45), 20, 1.0), "disc")[1] <= 0.001
    # soft-edged shapes: the same disc and ring, edge pixels shaded a little differently
    assert far(imgops.circle(blank(), (70.3, 44.6), 22.4, 1.0, smooth=True), "disc_smooth")[1] <= 0.03          # measured 0.006
    assert far(imgops.circle(blank(), (70.3, 44.6), 22.4, 1.0, thickness=3, smooth=True), "ring_smooth")[1] <= 0.04   # 0.023


def test_shapes_are_clipped_to_the_picture():
    m = np.zeros((10, 10), np.float32)
    imgops.fill_rect(m, (-5, -5), (2, 2), 1.0)
    imgops.circle(m, (30, 30), 3, 1.0)
    assert m.sum() == 9.0


# ------------------------------------------------------------------ masks
def test_mask_growing_and_shrinking_match():
    assert far(imgops.dilate(BITS, (1, 5), iterations=2), "dilate_1x5_twice") == (0.0, 0.0)
    assert far(imgops.erode((BITS * 255).astype(np.uint8), 3), "erode_3") == (0.0, 0.0)
    assert far(imgops.close_gaps(BITS, 5), "close_5") == (0.0, 0.0)


def test_regions_match():
    n, lab, stats = imgops.regions(REF["close_5"])
    assert n == int(REF["regions_count"]) and lab.dtype == np.int32 and stats.dtype == np.int32
    # the same regions, whatever their numbering
    assert len(set(zip(lab.ravel().tolist(), REF["regions_labels"].ravel().tolist()))) == n
    assert sorted(map(tuple, stats[1:].tolist())) == sorted(map(tuple, REF["regions_stats"][1:].tolist()))
    assert stats[0].tolist() == [0, 0, 96, 60, int(REF["regions_stats"][0][4])]     # row 0: the background, whole picture


def test_regions_of_an_empty_mask():
    n, lab, stats = imgops.regions(np.zeros((5, 7), np.uint8))
    assert n == 1 and not lab.any() and stats.tolist() == [[0, 0, 7, 5, 35]]


def test_regions_join_at_corners():
    n, lab, stats = imgops.regions(np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], np.uint8))
    assert n == 2 and stats[1].tolist() == [0, 0, 3, 3, 3]
    n, lab, stats = imgops.regions(np.array([[1, 0, 1], [0, 0, 0], [1, 0, 1]], np.uint8))
    assert n == 5 and sorted(s[4] for s in stats[1:].tolist()) == [1, 1, 1, 1]


# ------------------------------------------------------------------ colour / files
def test_gray_matches():
    assert far(imgops.gray(SRC), "gray")[0] <= 1


def test_write_image_round_trips(tmp_path):
    from PIL import Image
    imgops.write_image(tmp_path / "a.png", SRC)
    back = np.asarray(Image.open(tmp_path / "a.png").convert("RGB"))[..., ::-1]
    assert np.array_equal(back, SRC)
    imgops.write_image(tmp_path / "g.png", imgops.gray(SRC))
    assert Image.open(tmp_path / "g.png").mode == "L"
    imgops.write_image(tmp_path / "a.jpg", SRC)
    assert Image.open(tmp_path / "a.jpg").size == (144, 90)

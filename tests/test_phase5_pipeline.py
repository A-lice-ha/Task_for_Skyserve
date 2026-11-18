import json

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import box

from src.evaluation.evaluate import score_method
from src.evaluation.windows import change_map, rule_map
from src.evaluation.windows import small_change_retention
from src.training.assign_splits import BUFFER_M, train_cores
from src.training.weak_labels import AMBIGUOUS, GAIN, INVALID, STABLE, classify
from src.utils.export_ai_usage import clean

CONFIG = json.loads(open("configs/weak_labels.json", encoding="utf-8").read())


def _stack(h=40, w=40, built=False, vegetated=False):
    """Six-band reflectance: bare soil (or vegetation), optionally a built-up patch."""
    s = np.empty((6, h, w), np.float32)
    base = [0.05, 0.08, 0.07, 0.30, 0.20, 0.15] if vegetated else [0.06, 0.10, 0.15, 0.22, 0.20, 0.17]
    s[:] = np.array(base)[:, None, None]
    if built:
        s[:, 15:25, 15:25] = np.array([0.12, 0.14, 0.16, 0.20, 0.32, 0.30])[:, None, None]
    return s


def test_new_built_surface_is_gain():
    valid = np.ones((40, 40), bool)
    codes = classify(_stack(), _stack(built=True), valid, CONFIG)
    assert (codes[17:23, 17:23] == GAIN).all()
    assert codes[0, 0] == STABLE


def test_gain_on_previously_vegetated_land_is_ambiguous():
    # Dry-season senescence and clearing for construction look alike.
    valid = np.ones((40, 40), bool)
    codes = classify(_stack(vegetated=True), _stack(built=True, vegetated=True), valid, CONFIG)
    assert (codes[17:23, 17:23] == AMBIGUOUS).all()


def test_one_pixel_shift_of_existing_building_is_not_change():
    valid = np.ones((40, 40), bool)
    before = _stack(built=True)
    after = np.roll(before, 1, axis=2)  # residual misregistration of one pixel
    codes = classify(before, after, valid, CONFIG)
    assert not (codes == GAIN).any()


def test_regional_haze_marks_pixels_ambiguous_and_invalid_is_kept():
    valid = np.ones((40, 40), bool)
    valid[0, 0] = False
    after = _stack(built=True)
    after[0] += 0.08  # area-wide blue increase
    codes = classify(_stack(), after, valid, CONFIG)
    assert (codes[17:23, 17:23] == AMBIGUOUS).all()
    assert codes[0, 0] == INVALID


def test_train_cores_keep_buffer_from_held_out_blocks():
    blocks = gpd.GeoDataFrame(
        {"block_id": [f"b{i}" for i in range(3)], "split": ["train", "test", "train"]},
        geometry=[box(0, 0, 10000, 10000), box(10000, 0, 20000, 10000), box(20000, 0, 30000, 10000)],
        crs=32643)
    cores = train_cores(blocks)
    held = blocks[blocks["split"] == "test"].union_all()
    assert cores.geometry.distance(held).min() >= BUFFER_M - 1e-6
    assert set(cores["block_id"]) == {"b0", "b2"}


def _reviews(classes, splits):
    return [{"feature_id": f"T_R{i:05d}_C00000", "city": "Delhi", "change_class": c, "split": s,
             "mask_quality": "wrong"} for i, (c, s) in enumerate(zip(classes, splits))]


def test_threshold_is_selected_on_val_and_uncertain_is_not_scored():
    classes = ["construction", "no_change", "construction", "natural_vegetation", "uncertain"]
    splits = ["val", "val", "test", "test", "test"]
    maps = np.zeros((5, 16, 16), np.uint8)
    maps[0, :5, :5] = 1   # 25 px true change in val
    maps[1, :1, :3] = 1   # 3 px noise in val
    maps[2, :6, :6] = 1   # 36 px in test
    maps[3, :2, :2] = 1   # 4 px natural in test
    maps[4, :8, :8] = 1   # uncertain, large
    result = score_method("m", maps, _reviews(classes, splits), np.zeros_like(maps))
    assert result["k_selected_on_val"] == 4
    test = result["splits"]["test/all"]
    assert (test["n"], test["tp"], test["fp"]) == (2, 1, 1)
    assert result["uncertain_heldout"] == {"n": 1, "flagged": 1}


def test_change_map_drops_tiny_components_and_invalid():
    probs = np.zeros((1, 3, 10, 10), np.float32)
    probs[:, 0] = 1
    probs[0, :, 2:5, 2:5] = np.array([0.1, 0.8, 0.1])[:, None, None]  # 9 px gain
    probs[0, :, 8, 8] = np.array([0.1, 0.8, 0.1])                      # 1 px speck
    valid = np.ones((1, 10, 10), bool)
    valid[0, 2, 2] = False
    out = change_map(probs, valid)
    assert out[0, 8, 8] == 0 and out[0, 2, 2] == 0 and (out[0] == 1).sum() == 8
    weak = np.array([[[1, 2, 5, 255]]], np.uint8)
    assert rule_map(weak).tolist() == [[[1, 2, 0, 0]]]


def test_small_change_retention_counts_overlap():
    fp = np.zeros((1, 10, 10), np.uint8)
    fp[0, 0, :3] = 1
    fp[0, 5, 5:7] = 1
    q = np.zeros_like(fp)
    q[0, 0, 1] = 1
    assert small_change_retention(fp, q) == {
        "fp32_small_components": 2, "retained_by_int8": 1, "retention": 0.5}


def test_ai_usage_export_strips_injected_context_and_emails():
    text = "hi <system-reminder>secret context</system-reminder> mail me at a.b@example.org"
    assert clean(text) == "hi  mail me at [email redacted]"


def test_ranking_matches_known_values_and_handles_ties():
    from src.evaluation.evaluate import ranking
    truth = np.array([True, False, True, False])
    perfect = ranking(np.array([4.0, 1.0, 3.0, 2.0]), truth)
    assert perfect["auroc"] == 1.0 and perfect["average_precision"] == 1.0
    tied = ranking(np.ones(4), truth)
    assert tied["auroc"] == 0.5 and tied["average_precision"] == 0.5


def test_grouped_cv_keeps_blocks_together():
    from src.evaluation.evaluate import grouped_cv
    change = np.array([500, 400, 0, 0, 300, 0, 0, 0, 600, 0])
    truth = change > 0
    groups = np.array(["a", "a", "b", "b", "c", "c", "d", "d", "e", "e"])
    cls = np.where(truth, "construction", "natural_vegetation")
    out = grouped_cv(change, truth, groups, cls)
    assert out["blocks"] == 5 and len(out["k_per_fold"]) == 5
    assert out["f1"] == 1.0 and out["natural_false_alarm_rate"] == 0.0


def test_reviewed_negative_windows_become_all_no_change():
    from src.training.reviewed_patches import corrected_target
    weak = np.array([[1, 2], [3, 5]], np.uint8)
    valid = np.array([[True, True], [True, False]])
    assert corrected_target(weak, valid, "natural_vegetation").tolist() == [[0, 0], [0, 255]]
    assert corrected_target(weak, valid, "construction").tolist() == [[1, 2], [0, 255]]


def test_band_stats_ignore_unlabelled_pixels():
    from src.training.train import band_stats
    x = np.zeros((2, 2, 6, 4, 4), np.float16)
    x[:, :, :, :2] = 1.0           # labelled half
    x[:, :, :, 2:] = 9.0           # ignored half must not affect stats
    y = np.full((2, 4, 4), 255, np.uint8)
    y[:, :2] = 0
    stats = band_stats(x, y)
    assert len(stats) == 6 and all(abs(m - 1.0) < 1e-6 and s < 1e-3 for m, s in stats)

import json

import numpy as np
from affine import Affine
from rasterio.crs import CRS
import pytest

from src.training.generate_candidates import (
    _feature,
    _indices,
    _interval_type,
    _persistent_water_pixels,
    _select_candidates,
    _stable_water_review_area,
    _validate_replaceable_candidate,
)


def test_indices_use_expected_sentinel_band_order():
    bands = np.zeros((6, 2, 2), dtype=np.float32)
    bands[1] = 0.3  # B03
    bands[2] = 0.2  # B04
    bands[3] = 0.6  # B08
    bands[4] = 0.4  # B11

    indices = _indices(bands)

    assert np.allclose(indices["ndvi"], 0.5)
    assert np.allclose(indices["ndbi"], -0.2)
    assert np.allclose(indices["mndwi"], -0.14285715)


def test_interval_type_uses_acquisition_dates():
    assert _interval_type("2022-12-23", "2023-05-12") == (
        "short_interval_cross_season"
    )
    assert _interval_type("2023-10-06", "2026-10-22") == "multi_year"


def test_stable_water_screen_excludes_open_water_but_keeps_shore_and_transition():
    stable_water = np.zeros((100, 100), dtype=np.uint8)
    stable_water[10:90, 10:90] = 1
    water_transition = np.zeros_like(stable_water, dtype=bool)
    water_transition[50, 50] = True

    review_area = _stable_water_review_area(
        stable_water, water_transition, buffer_pixels=5
    )

    assert not review_area[50, 30]
    assert review_area[11, 50]
    assert review_area[50, 50]
    assert review_area[5, 50]


def test_persistent_water_requires_both_dates_and_low_vegetation_signal():
    before_mndwi = np.array([[0.2, 0.1, 0.1]])
    after_mndwi = np.array([[0.1, 0.1, 0.1]])
    before_ndvi = np.array([[0.05, 0.25, 0.05]])
    after_ndvi = np.array([[0.1, 0.1, 0.05]])
    valid = np.ones((1, 3), dtype=bool)

    water = _persistent_water_pixels(
        before_mndwi, after_mndwi, before_ndvi, after_ndvi, valid
    )

    assert water.tolist() == [[True, False, True]]


def test_selection_includes_top_signal_and_random_below_threshold():
    patches = []
    for index in range(10):
        value = 1.0 if index == 0 else 0.0
        patches.append(
            {
                "row": index * 256,
                "col": 0,
                "spatial_block_id": f"BLOCK_{index // 5}",
                "percentiles": {
                    name: (value, value, value)
                    for name in ("ndvi", "ndbi", "mndwi")
                },
            }
        )

    candidates = _select_candidates(patches)

    assert sum(
        kind == "top_ranked_index_change"
        for _, kind, _ in candidates
    ) == 1
    assert sum(
        kind == "random_below_threshold_background"
        for _, kind, _ in candidates
    ) == 2
    assert candidates[0][0]["row"] == 0
    assert candidates[0][2] is None
    assert all(
        probability is not None
        for _, kind, probability in candidates
        if kind == "random_below_threshold_background"
    )


def test_high_signal_windows_are_ranked_within_each_block():
    patches = []
    for block_id in ("BLOCK_A", "BLOCK_B"):
        for index in range(10):
            value = 1.0 if index == 0 else 0.0
            patches.append(
                {
                    "row": len(patches) * 256,
                    "col": 0,
                    "spatial_block_id": block_id,
                    "percentiles": {
                        name: (value, value, value)
                        for name in ("ndvi", "ndbi", "mndwi")
                    },
                }
            )

    candidates = _select_candidates(patches)

    high = [
        patch
        for patch, kind, _ in candidates
        if kind == "top_ranked_index_change"
    ]
    assert len(high) == 2
    assert {patch["spatial_block_id"] for patch in high} == {
        "BLOCK_A",
        "BLOCK_B",
    }


def test_geojson_feature_remains_unreviewed_and_has_block_tags():
    patch = {
        "row": 0,
        "col": 0,
        "transform": Affine(10, 0, 200000, 0, -10, 2200000),
        "spatial_block_id": "T43QBB_E0200_N2200",
        "valid_fraction": 1.0,
        "review_area_fraction": 0.9,
        "index_signal_score": 2.0,
        "percentiles": {
            "ndvi": (-0.1, 0.0, 0.2),
            "ndbi": (-0.2, 0.1, 0.3),
            "mndwi": (-0.3, 0.0, 0.4),
        },
        "water_transition_fraction": 0.05,
    }
    scene = {
        "city": "Mumbai",
        "tile": "T43QBB",
        "pair_id": "MUM_T43QBB_20221223_20230512",
        "t1_date": "2022-12-23",
        "t2_date": "2023-05-12",
    }

    feature = _feature(
        scene,
        patch,
        "top_ranked_index_change",
        None,
        CRS.from_epsg(32643),
    )

    assert feature["properties"]["review_status"] == "unreviewed_candidate"
    assert feature["properties"]["change_class"] is None
    assert feature["properties"]["split_assignment"] == "unassigned"
    assert feature["properties"]["spatial_block_id"] == patch["spatial_block_id"]
    assert feature["properties"]["planned_split_buffer_m"] == 2000
    assert feature["properties"]["candidate_selection_probability"] is None
    assert feature["properties"]["review_area_fraction"] == 0.9
    assert feature["geometry"]["type"] == "Polygon"


def test_overwrite_guard_refuses_files_with_reviewed_features(tmp_path):
    path = tmp_path / "candidates.geojson"
    path.write_text(
        json.dumps(
            {
                "metadata": {"candidate_only": True, "not_ground_truth": True},
                "features": [
                    {"properties": {"review_status": "reviewed"}}
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FileExistsError, match="reviewed candidate"):
        _validate_replaceable_candidate(path)

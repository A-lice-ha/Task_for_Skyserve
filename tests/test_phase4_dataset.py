import json

import numpy as np
import pytest
import rasterio
import torch
from affine import Affine

from src.training.dataset import IGNORE_INDEX, LabeledPairPatchDataset


def _write_raster(path, data, count, dtype, nodata=None):
    profile = {
        "driver": "GTiff",
        "width": 32,
        "height": 32,
        "count": count,
        "dtype": dtype,
        "crs": "EPSG:4326",
        "transform": Affine(0.001, 0, 0, 0, -0.001, 1),
        "nodata": nodata,
    }
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(data)


def _feature(change_class="construction", coordinates=None):
    if coordinates is None:
        coordinates = [
            [
                [0.005, 0.995],
                [0.015, 0.995],
                [0.015, 0.985],
                [0.005, 0.985],
                [0.005, 0.995],
            ]
        ]
    return {
        "type": "Feature",
        "properties": {
            "change_class": change_class,
            "confidence": "high",
            "annotator": "reviewer",
            "notes": "visually verified",
        },
        "geometry": {"type": "Polygon", "coordinates": coordinates},
    }


def _dataset(tmp_path, features=None, valid_data=None):
    before_path = tmp_path / "before.tif"
    after_path = tmp_path / "after.tif"
    valid_path = tmp_path / "valid.tif"
    annotation_path = tmp_path / "annotations.geojson"
    image = np.full((6, 32, 32), 0.25, dtype=np.float32)
    if valid_data is None:
        valid_data = np.ones((1, 32, 32), dtype=np.uint8)
    _write_raster(before_path, image, 6, "float32", -9999)
    _write_raster(after_path, image + 0.05, 6, "float32", -9999)
    _write_raster(valid_path, valid_data, 1, "uint8", 0)
    annotation_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [_feature()] if features is None else features,
            }
        ),
        encoding="utf-8",
    )
    return LabeledPairPatchDataset(
        before_path,
        after_path,
        valid_path,
        annotation_path,
        [(0, 0)],
        patch_size=32,
    )


def test_dataset_returns_six_band_pair_and_only_explicit_labels(tmp_path):
    dataset = _dataset(tmp_path)

    sample = dataset[0]

    assert sample["before"].shape == (6, 32, 32)
    assert sample["after"].shape == (6, 32, 32)
    assert sample["label"].shape == (32, 32)
    assert sample["label"].dtype == torch.int64
    assert torch.all(sample["label"][5:15, 5:15] == 1)
    assert torch.all(sample["label"][:5] == IGNORE_INDEX)
    assert torch.isfinite(sample["before"]).all()


def test_invalid_pixels_are_ignored_and_zeroed(tmp_path):
    valid = np.ones((1, 32, 32), dtype=np.uint8)
    valid[0, 7, 7] = 0
    dataset = _dataset(tmp_path, valid_data=valid)

    sample = dataset[0]

    assert sample["label"][7, 7].item() == IGNORE_INDEX
    assert torch.all(sample["before"][:, 7, 7] == 0)


def test_conflicting_annotation_overlap_is_rejected(tmp_path):
    second = _feature(
        "demolition",
        [
            [
                [0.010, 0.990],
                [0.020, 0.990],
                [0.020, 0.980],
                [0.010, 0.980],
                [0.010, 0.990],
            ]
        ],
    )
    dataset = _dataset(tmp_path, features=[_feature(), second])

    with pytest.raises(ValueError, match="Conflicting annotation"):
        dataset[0]


def test_dataset_rejects_patch_outside_raster(tmp_path):
    dataset = _dataset(tmp_path)
    with pytest.raises(ValueError, match="exceeds raster dimensions"):
        LabeledPairPatchDataset(
            dataset.before_path,
            dataset.after_path,
            dataset.common_valid_path,
            dataset.annotations_path,
            [(1, 0)],
            patch_size=32,
        )


def test_dataset_requires_annotation_metadata(tmp_path):
    bad = _feature()
    del bad["properties"]["annotator"]
    with pytest.raises(ValueError, match="missing properties"):
        _dataset(tmp_path, features=[bad])

import cv2
import numpy as np
import rasterio
from affine import Affine

from src.preprocessing.phase2 import estimate_registration


def _write_stack(path, band4):
    profile = {
        "driver": "GTiff",
        "width": band4.shape[1],
        "height": band4.shape[0],
        "count": 6,
        "dtype": "float32",
        "crs": "EPSG:32643",
        "transform": Affine(10, 0, 500000, 0, -10, 3200000),
        "nodata": -9999,
    }
    with rasterio.open(path, "w", **profile) as dataset:
        for index in range(1, 7):
            dataset.write(band4, index)
        dataset.write_mask(np.full(band4.shape, 255, dtype=np.uint8))


def test_registration_recovers_translation_from_stable_features(tmp_path):
    rng = np.random.default_rng(42)
    reference_image = rng.random((512, 512), dtype=np.float32) * 0.1
    for _ in range(140):
        x, y = rng.integers(12, 500, size=2)
        radius = int(rng.integers(3, 11))
        value = float(rng.uniform(0.3, 1.0))
        cv2.circle(reference_image, (int(x), int(y)), radius, value, -1)
    moving_image = cv2.warpAffine(
        reference_image,
        np.float32([[1, 0, 3], [0, 1, -2]]),
        (512, 512),
    )
    reference_path = tmp_path / "reference.tif"
    moving_path = tmp_path / "moving.tif"
    _write_stack(reference_path, reference_image)
    _write_stack(moving_path, moving_image)

    with rasterio.open(reference_path) as reference, rasterio.open(
        moving_path
    ) as moving:
        transform, metrics = estimate_registration(reference, moving)

    assert metrics["inliers"] >= 20
    assert abs(transform.c + 3) < 0.5
    assert abs(transform.f - 2) < 0.5
    assert metrics["rmse_m"] < 8

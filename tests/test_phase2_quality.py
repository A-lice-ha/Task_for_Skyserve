import csv

import numpy as np
import rasterio
from affine import Affine

from src.preprocessing.phase2 import (
    _record_timing,
    summarize_atmospheric_quality,
    write_atmospheric_quality,
)


def _write_raster(path, values, dtype):
    profile = {
        "driver": "GTiff",
        "width": values.shape[1],
        "height": values.shape[0],
        "count": 1,
        "dtype": dtype,
        "crs": "EPSG:32643",
        "transform": Affine(10, 0, 500000, 0, -10, 3200000),
    }
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(values.astype(dtype), 1)


def _make_product(tmp_path):
    product = tmp_path / "sample.SAFE"
    granule = product / "GRANULE" / "sample"
    granule.mkdir(parents=True)
    (product / "MTD_MSIL2A.xml").write_text(
        "<root><AOT_QUANTIFICATION_VALUE>1000</AOT_QUANTIFICATION_VALUE>"
        "<WVP_QUANTIFICATION_VALUE>1000</WVP_QUANTIFICATION_VALUE></root>",
        encoding="utf-8",
    )
    _write_raster(
        granule / "sample_SCL_20m.jp2",
        np.array([[8, 3, 2], [7, 0, 4]], dtype=np.uint8),
        "uint8",
    )
    _write_raster(
        granule / "sample_AOT_10m.jp2",
        np.array([[0, 100, 200], [300, 400, 0]], dtype=np.uint16),
        "uint16",
    )
    _write_raster(
        granule / "sample_WVP_10m.jp2",
        np.array([[0, 1000, 2000], [3000, 0, 0]], dtype=np.uint16),
        "uint16",
    )
    return product


def test_summarize_atmospheric_quality_counts_masks_and_scaled_quantiles(tmp_path):
    product = _make_product(tmp_path)

    summary = summarize_atmospheric_quality(product)

    assert summary["scl_pixels"] == 6
    assert summary["cloud_cirrus_percent"] == 100 / 6
    assert summary["cloud_shadow_percent"] == 100 / 6
    assert summary["dark_area_percent"] == 100 / 6
    assert summary["unclassified_percent"] == 100 / 6
    assert summary["aot_valid_pixels"] == 4
    assert summary["aot_median"] == 0.2
    assert summary["aot_p98"] == 0.4
    assert summary["wvp_valid_pixels"] == 3
    assert summary["wvp_median"] == 2.0
    assert summary["wvp_p98"] == 3.0


def test_write_atmospheric_quality_creates_csv(tmp_path):
    product = _make_product(tmp_path)
    output = tmp_path / "reports" / "atmospheric_quality.csv"

    write_atmospheric_quality([product], output)

    with output.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 1
    assert rows[0]["product"] == product.name
    assert rows[0]["scl_class_8_pixels"] == "1"


def test_record_timing_appends_stage_records(tmp_path):
    output_root = tmp_path / "processed"

    _record_timing(output_root, "prepare", "scene-a", "completed", 12.5)
    _record_timing(output_root, "prepare", "scene-b", "skipped", 0.0)

    timing_path = tmp_path / "reports" / "phase2_timings.csv"
    with timing_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [row["item"] for row in rows] == ["scene-a", "scene-b"]
    assert [row["status"] for row in rows] == ["completed", "skipped"]
    assert rows[0]["elapsed_seconds"] == "12.500"

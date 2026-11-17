"""Windowed dataset for manually annotated bi-temporal raster patches."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import numpy as np
import rasterio
import torch
from rasterio.features import rasterize
from rasterio.warp import transform_geom
from rasterio.windows import Window, transform as window_transform
from torch.utils.data import Dataset


CHANGE_CLASSES = (
    "no_change",
    "construction",
    "demolition",
    "road",
    "bridge",
    "infrastructure_other",
    "uncertain",
)
CLASS_TO_ID = {name: index for index, name in enumerate(CHANGE_CLASSES)}
IGNORE_INDEX = 255
NODATA = -9999.0
REQUIRED_ANNOTATION_PROPERTIES = {
    "change_class",
    "confidence",
    "annotator",
    "notes",
}


def _read_annotations(path: Path, raster_crs) -> list[tuple[dict, int]]:
    try:
        collection = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read annotation GeoJSON {path}: {error}") from error
    if collection.get("type") != "FeatureCollection":
        raise ValueError(f"{path} must contain a GeoJSON FeatureCollection")
    features = []
    for index, feature in enumerate(collection.get("features", [])):
        if feature.get("type") != "Feature":
            raise ValueError(f"Annotation feature {index} is not a GeoJSON Feature")
        properties = feature.get("properties") or {}
        missing = REQUIRED_ANNOTATION_PROPERTIES - properties.keys()
        if missing:
            raise ValueError(
                f"Annotation feature {index} is missing properties: "
                f"{', '.join(sorted(missing))}"
            )
        class_name = properties["change_class"]
        if class_name not in CLASS_TO_ID:
            raise ValueError(
                f"Annotation feature {index} has unknown change_class "
                f"{class_name!r}; expected one of {', '.join(CHANGE_CLASSES)}"
            )
        geometry = feature.get("geometry")
        if not geometry or geometry.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError(
                f"Annotation feature {index} must have Polygon or MultiPolygon geometry"
            )
        if raster_crs.to_epsg() != 4326:
            geometry = transform_geom("EPSG:4326", raster_crs, geometry)
        features.append((geometry, CLASS_TO_ID[class_name]))
    if not features:
        raise ValueError(f"{path} contains no annotation features")
    return features


class LabeledPairPatchDataset(Dataset):
    """Read annotated patches from registered reflectance rasters.

    Annotations follow RFC 7946 GeoJSON coordinates (WGS 84). Pixels outside
    annotated polygons, and invalid pixels, receive ``IGNORE_INDEX``. They are
    never silently treated as no-change.
    """

    def __init__(
        self,
        before_path: str | Path,
        after_path: str | Path,
        common_valid_path: str | Path,
        annotations_path: str | Path,
        patch_origins: Sequence[tuple[int, int]],
        patch_size: int = 256,
    ):
        self.before_path = Path(before_path)
        self.after_path = Path(after_path)
        self.common_valid_path = Path(common_valid_path)
        self.annotations_path = Path(annotations_path)
        self.patch_size = int(patch_size)
        if self.patch_size <= 0:
            raise ValueError("patch_size must be positive")
        if not patch_origins:
            raise ValueError("patch_origins must contain at least one (row, col)")

        with rasterio.open(self.before_path) as before, rasterio.open(
            self.after_path
        ) as after, rasterio.open(self.common_valid_path) as valid:
            if (
                before.width != after.width
                or before.height != after.height
                or before.transform != after.transform
                or before.crs != after.crs
            ):
                raise ValueError("Before and after rasters must share the same grid")
            if (
                before.width != valid.width
                or before.height != valid.height
                or before.transform != valid.transform
                or before.crs != valid.crs
            ):
                raise ValueError("Common-valid mask must share the image grid")
            if before.count != after.count:
                raise ValueError("Before and after rasters must have the same band count")
            if before.count != 6:
                raise ValueError(f"Expected six reflectance bands, found {before.count}")
            self.height = before.height
            self.width = before.width
            self.annotations = _read_annotations(self.annotations_path, before.crs)

        self.patch_origins = tuple((int(row), int(col)) for row, col in patch_origins)
        for row, col in self.patch_origins:
            if (
                row < 0
                or col < 0
                or row + self.patch_size > self.height
                or col + self.patch_size > self.width
            ):
                raise ValueError(
                    f"Patch origin {(row, col)} with size {self.patch_size} "
                    f"exceeds raster dimensions {(self.height, self.width)}"
                )

    def __len__(self) -> int:
        return len(self.patch_origins)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        row, col = self.patch_origins[index]
        window = Window(col, row, self.patch_size, self.patch_size)
        with rasterio.open(self.before_path) as before_ds, rasterio.open(
            self.after_path
        ) as after_ds, rasterio.open(self.common_valid_path) as valid_ds:
            before = before_ds.read(window=window).astype(np.float32, copy=False)
            after = after_ds.read(window=window).astype(np.float32, copy=False)
            valid = valid_ds.read(1, window=window) > 0
            patch_affine = window_transform(window, before_ds.transform)

        valid &= np.isfinite(before).all(axis=0) & np.isfinite(after).all(axis=0)
        valid &= (before != NODATA).all(axis=0) & (after != NODATA).all(axis=0)

        labels = np.full(
            (self.patch_size, self.patch_size), IGNORE_INDEX, dtype=np.uint8
        )
        for geometry, class_id in self.annotations:
            burned = rasterize(
                [(geometry, 1)],
                out_shape=labels.shape,
                transform=patch_affine,
                fill=0,
                dtype="uint8",
            ).astype(bool)
            conflict = burned & (labels != IGNORE_INDEX) & (labels != class_id)
            if np.any(conflict):
                raise ValueError(
                    f"Conflicting annotation classes overlap in patch origin {(row, col)}"
                )
            labels[burned] = class_id

        labels[~valid] = IGNORE_INDEX
        before[:, ~valid] = 0
        after[:, ~valid] = 0
        return {
            "before": torch.from_numpy(np.ascontiguousarray(before)),
            "after": torch.from_numpy(np.ascontiguousarray(after)),
            "label": torch.from_numpy(labels.astype(np.int64)),
        }

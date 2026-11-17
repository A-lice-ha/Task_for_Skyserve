"""Generate spatially tagged, unreviewed review windows from Sentinel-2 pairs."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

import cv2
import numpy as np
import rasterio
from rasterio.windows import Window, transform as window_transform
from rasterio.warp import transform_geom
from shapely.geometry import box, mapping


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = ROOT / "data" / "labels" / "candidates"
BLOCK_SIZE_M = 10_000
PATCH_SIZE = 256
MIN_VALID_FRACTION = 0.8
HIGH_SIGNAL_FRACTION = 0.1
RANDOM_BACKGROUND_FRACTION = 0.18
RANDOM_SEED = 20261117
OPEN_WATER_BUFFER_M = 500
MIN_REVIEW_AREA_FRACTION = 0.05
NODATA = -9999.0
MIN_INDEX_DENOMINATOR = 1e-6
SCORE_THRESHOLDS = {
    "ndvi": 0.2,
    "ndbi": 0.2,
    "mndwi": 0.3,
}
SCENES = (
    {
        "city": "Mumbai",
        "tile": "T43QBB",
        "pair_id": "MUM_T43QBB_20221223_20230512",
        "before": "S2A_MSIL2A_20221223T054241",
        "after": "T43QBB_20221223T054241_20230512T053641_after_registered.tif",
        "mask": "T43QBB_20221223T054241_20230512T053641_common_valid.tif",
        "t1_date": "2022-12-23",
        "t2_date": "2023-05-12",
        "output": "mumbai_candidates.geojson",
    },
    {
        "city": "Delhi",
        "tile": "T43RGM",
        "pair_id": "DEL_T43RGM_20231006_20261022",
        "before": "S2A_MSIL2A_20231006T052701",
        "after": "T43RGM_20231006T052701_20261022T053241_after_registered.tif",
        "mask": "T43RGM_20231006T052701_20261022T053241_common_valid.tif",
        "t1_date": "2023-10-06",
        "t2_date": "2026-10-22",
        "output": "delhi_candidates.geojson",
    },
)


def _find_one(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise FileNotFoundError(
            f"Expected exactly one file matching {pattern!r} in {directory}, "
            f"found {len(matches)}"
        )
    return matches[0]


def _index(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    output = np.full(numerator.shape, np.nan, dtype=np.float32)
    np.divide(
        numerator,
        denominator,
        out=output,
        where=np.abs(denominator) > MIN_INDEX_DENOMINATOR,
    )
    return output


def _indices(reflectance: np.ndarray) -> dict[str, np.ndarray]:
    # Stack order is B02, B03, B04, B08, B11, B12.
    return {
        "ndvi": _index(reflectance[3] - reflectance[2], reflectance[3] + reflectance[2]),
        "ndbi": _index(reflectance[4] - reflectance[3], reflectance[4] + reflectance[3]),
        "mndwi": _index(reflectance[1] - reflectance[4], reflectance[1] + reflectance[4]),
    }


def _interval_type(t1_date: str, t2_date: str) -> str:
    days = (date.fromisoformat(t2_date) - date.fromisoformat(t1_date)).days
    if days <= 200:
        return "short_interval_cross_season"
    if days >= 900:
        return "multi_year"
    return "intermediate"


def _pair_paths(scene: dict) -> tuple[Path, Path, Path]:
    processed = ROOT / "outputs" / "processed"
    registered = ROOT / "outputs" / "registered"
    before_path = _find_one(
        processed, f"{scene['before']}*.reflectance.tif"
    )
    after_path = registered / scene["after"]
    mask_path = registered / scene["mask"]
    for path in (after_path, mask_path):
        if not path.is_file():
            raise FileNotFoundError(f"Required registered pair raster not found: {path}")
    return before_path, after_path, mask_path


def _validate_replaceable_candidate(path: Path) -> None:
    try:
        collection = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Refusing to overwrite unreadable candidate file {path}") from error
    metadata = collection.get("metadata") or {}
    if not metadata.get("candidate_only") or not metadata.get("not_ground_truth"):
        raise FileExistsError(
            f"Refusing to overwrite a file not marked as candidate-only: {path}"
        )
    protected_values = {
        "review_status": "unreviewed_candidate",
        "change_category": "candidate",
        "change_class": None,
        "non_target_subtype": None,
        "confounder": None,
        "confidence": "unreviewed",
        "annotator": None,
        "notes": "",
        "split_assignment": "unassigned",
    }
    for feature in collection.get("features", []):
        properties = feature.get("properties") or {}
        if any(properties.get(key) != value for key, value in protected_values.items()):
            raise FileExistsError(
                f"Refusing to overwrite edited or reviewed candidate features: {path}"
            )


def _validate_datasets(before, after, valid, city: str) -> None:
    if before.count != 6 or after.count != 6:
        raise ValueError(f"{city} pair must contain six reflectance bands")
    if before.descriptions != after.descriptions:
        raise ValueError(f"{city} before/after band descriptions differ")
    expected_bands = ("B02", "B03", "B04", "B08", "B11", "B12")
    if before.descriptions != expected_bands:
        raise ValueError(
            f"Expected bands {expected_bands} for {city}, found {before.descriptions}"
        )
    if before.crs is None or before.crs != after.crs or before.crs != valid.crs:
        raise ValueError(f"{city} pair rasters have inconsistent coordinate systems")
    reference = (before.width, before.height, before.transform)
    if reference != (after.width, after.height, after.transform):
        raise ValueError(f"{city} before/after rasters are not on the same grid")
    if reference != (valid.width, valid.height, valid.transform):
        raise ValueError(f"{city} common-valid mask is not on the image grid")
    if before.crs.to_epsg() != 32643:
        raise ValueError(f"Expected EPSG:32643 for {city}; found {before.crs}")


def _patch_stats(
    before: np.ndarray,
    after: np.ndarray,
    valid: np.ndarray,
    row: int,
    col: int,
    transform,
    spatial_block_id: str,
    review_area: np.ndarray | None = None,
) -> dict | None:
    if valid.mean() < MIN_VALID_FRACTION:
        return None
    before_ok = np.isfinite(before).all(axis=0) & (before != NODATA).all(axis=0)
    after_ok = np.isfinite(after).all(axis=0) & (after != NODATA).all(axis=0)
    valid = valid & before_ok & after_ok
    if valid.mean() < MIN_VALID_FRACTION:
        return None

    review_valid = valid if review_area is None else valid & review_area
    review_area_fraction = float(review_valid.mean())
    if review_area_fraction < MIN_REVIEW_AREA_FRACTION:
        return None

    before_indices = _indices(before)
    after_indices = _indices(after)
    deltas = {
        name: after_indices[name] - before_indices[name]
        for name in SCORE_THRESHOLDS
    }
    review_valid &= np.logical_and.reduce(
        [np.isfinite(delta) for delta in deltas.values()]
    )
    if review_valid.mean() < MIN_REVIEW_AREA_FRACTION:
        return None

    percentiles = {
        name: np.percentile(delta[review_valid], (10, 50, 90))
        for name, delta in deltas.items()
    }
    # Scores are recomputed after pair-wide medians are known.
    water_transition = (
        (deltas["mndwi"] < -0.3)
        & (deltas["ndbi"] > 0.1)
        & review_valid
    )
    return {
        "row": row,
        "col": col,
        "transform": transform,
        "valid_fraction": float(valid.mean()),
        "review_area_fraction": review_area_fraction,
        "percentiles": {
            name: tuple(float(value) for value in values)
            for name, values in percentiles.items()
        },
        "water_transition_fraction": float(
            water_transition.sum() / review_valid.sum()
        ),
        "spatial_block_id": spatial_block_id,
    }


def _stable_water_review_area(
    stable_water: np.ndarray,
    water_transition: np.ndarray,
    buffer_pixels: int,
) -> np.ndarray:
    """Exclude persistent-water interiors while retaining shore/transition zones."""
    stable_water = stable_water.astype(bool, copy=False)
    distance_to_nonwater = cv2.distanceTransform(
        stable_water.astype(np.uint8), cv2.DIST_L2, 5
    )
    near_shore = distance_to_nonwater <= buffer_pixels
    return ~stable_water | near_shore | water_transition.astype(bool, copy=False)


def _persistent_water_pixels(
    before_mndwi: np.ndarray,
    after_mndwi: np.ndarray,
    before_ndvi: np.ndarray,
    after_ndvi: np.ndarray,
    common_valid: np.ndarray,
) -> np.ndarray:
    finite = np.logical_and.reduce(
        [
            np.isfinite(index)
            for index in (before_mndwi, after_mndwi, before_ndvi, after_ndvi)
        ]
    )
    return (
        common_valid
        & finite
        & (before_mndwi > 0.0)
        & (after_mndwi > 0.0)
        & (before_ndvi < 0.2)
        & (after_ndvi < 0.2)
    )


def _mumbai_review_area_mask(
    before_path: Path, after_path: Path, valid_path: Path
) -> np.ndarray:
    """Build a review-only sea screen; the source common-valid mask is unchanged."""
    with rasterio.open(before_path) as before, rasterio.open(after_path) as after, rasterio.open(
        valid_path
    ) as valid:
        _validate_datasets(before, after, valid, "Mumbai")
        stable_water = np.zeros((before.height, before.width), dtype=np.uint8)
        water_transition = np.zeros_like(stable_water, dtype=bool)
        for row in range(0, before.height, 512):
            for col in range(0, before.width, 512):
                window = Window(
                    col,
                    row,
                    min(512, before.width - col),
                    min(512, before.height - row),
                )
                before_bands = before.read((2, 3, 4, 5), window=window).astype(
                    np.float32, copy=False
                )
                after_bands = after.read((2, 3, 4, 5), window=window).astype(
                    np.float32, copy=False
                )
                common_valid = valid.read(1, window=window) > 0
                before_mndwi = _index(
                    before_bands[0] - before_bands[3],
                    before_bands[0] + before_bands[3],
                )
                after_mndwi = _index(
                    after_bands[0] - after_bands[3],
                    after_bands[0] + after_bands[3],
                )
                before_ndvi = _index(
                    before_bands[2] - before_bands[1],
                    before_bands[2] + before_bands[1],
                )
                after_ndvi = _index(
                    after_bands[2] - after_bands[1],
                    after_bands[2] + after_bands[1],
                )
                before_ndbi = _index(
                    before_bands[3] - before_bands[2],
                    before_bands[3] + before_bands[2],
                )
                after_ndbi = _index(
                    after_bands[3] - after_bands[2],
                    after_bands[3] + after_bands[2],
                )
                stable = _persistent_water_pixels(
                    before_mndwi,
                    after_mndwi,
                    before_ndvi,
                    after_ndvi,
                    common_valid,
                )
                transition = (
                    common_valid
                    & np.isfinite(before_mndwi)
                    & np.isfinite(after_mndwi)
                    & np.isfinite(before_ndbi)
                    & np.isfinite(after_ndbi)
                    & ((after_mndwi - before_mndwi) < -0.3)
                    & ((after_ndbi - before_ndbi) > 0.1)
                )
                height, width = stable.shape
                stable_water[
                    row : row + height, col : col + width
                ] = stable.astype(np.uint8)
                water_transition[
                    row : row + height, col : col + width
                ] = transition
    review_area = _stable_water_review_area(
        stable_water,
        water_transition,
        buffer_pixels=OPEN_WATER_BUFFER_M // 10,
    )
    review_mask_path = ROOT / "outputs" / "processed" / "mumbai_candidate_review_area.tif"
    review_mask_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(before_path) as reference:
        profile = reference.profile.copy()
    profile.update(
        driver="GTiff",
        count=1,
        dtype="uint8",
        nodata=255,
        compress="deflate",
        predictor=2,
        tiled=True,
        blockxsize=512,
        blockysize=512,
    )
    with rasterio.open(review_mask_path, "w", **profile) as output:
        output.write(review_area.astype(np.uint8), 1)
        output.update_tags(
            description=(
                "Candidate-review area: 1=retain for review, 0=persistent "
                "offshore water screened; not a validity mask or land classification"
            ),
            water_rule=(
                "MNDWI>0 and NDVI<0.2 in both dates; exclude persistent-water "
                "interiors >500m from non-water, preserving water-to-impervious "
                "transition pixels"
            ),
        )
    return review_area


def _collect_patch_stats(
    scene: dict, before_path: Path, after_path: Path, mask_path: Path
) -> tuple[list[dict], rasterio.crs.CRS]:
    patches = []
    review_area = (
        _mumbai_review_area_mask(before_path, after_path, mask_path)
        if scene["city"].lower() == "mumbai"
        else None
    )
    with rasterio.open(before_path) as before, rasterio.open(after_path) as after, rasterio.open(
        mask_path
    ) as valid:
        _validate_datasets(before, after, valid, scene["city"])
        if (
            before.transform.b != 0
            or before.transform.d != 0
            or not np.isclose(before.transform.a, 10.0)
            or not np.isclose(before.transform.e, -10.0)
        ):
            raise ValueError(f"{scene['city']} grid must be north-up at 10 m")
        left, bottom, right, top = before.bounds
        x_start = math.floor(left / BLOCK_SIZE_M) * BLOCK_SIZE_M
        y_start = math.floor(bottom / BLOCK_SIZE_M) * BLOCK_SIZE_M
        x_stop = math.ceil(right / BLOCK_SIZE_M) * BLOCK_SIZE_M
        y_stop = math.ceil(top / BLOCK_SIZE_M) * BLOCK_SIZE_M
        for block_y in range(y_start, y_stop, BLOCK_SIZE_M):
            block_top = block_y + BLOCK_SIZE_M
            row_start = max(0, math.ceil((top - block_top) / 10.0))
            row_stop = min(before.height, math.floor((top - block_y) / 10.0))
            for block_x in range(x_start, x_stop, BLOCK_SIZE_M):
                col_start = max(0, math.ceil((block_x - left) / 10.0))
                col_stop = min(
                    before.width,
                    math.floor((block_x + BLOCK_SIZE_M - left) / 10.0),
                )
                block_width = col_stop - col_start
                block_height = row_stop - row_start
                if block_width < PATCH_SIZE or block_height < PATCH_SIZE:
                    continue
                block_window = Window(
                    col_start,
                    row_start,
                    block_width,
                    block_height,
                )
                block_before = before.read(window=block_window).astype(
                    np.float32, copy=False
                )
                block_after = after.read(window=block_window).astype(
                    np.float32, copy=False
                )
                block_valid = valid.read(1, window=block_window) > 0
                block_id = (
                    f"{scene['tile']}_E{block_x // 1000:04d}_"
                    f"N{block_y // 1000:04d}"
                )
                for local_row in range(
                    0, block_height - PATCH_SIZE + 1, PATCH_SIZE
                ):
                    for local_col in range(
                        0, block_width - PATCH_SIZE + 1, PATCH_SIZE
                    ):
                        patch_before = block_before[
                            :,
                            local_row : local_row + PATCH_SIZE,
                            local_col : local_col + PATCH_SIZE,
                        ]
                        patch_after = block_after[
                            :,
                            local_row : local_row + PATCH_SIZE,
                            local_col : local_col + PATCH_SIZE,
                        ]
                        patch_valid = block_valid[
                            local_row : local_row + PATCH_SIZE,
                            local_col : local_col + PATCH_SIZE,
                        ]
                        row = row_start + local_row
                        col = col_start + local_col
                        window = Window(col, row, PATCH_SIZE, PATCH_SIZE)
                        patch_review_area = (
                            review_area[
                                row : row + PATCH_SIZE,
                                col : col + PATCH_SIZE,
                            ]
                            if review_area is not None
                            else None
                        )
                        stats = _patch_stats(
                            patch_before,
                            patch_after,
                            patch_valid,
                            row,
                            col,
                            window_transform(window, before.transform),
                            block_id,
                            patch_review_area,
                        )
                        if stats is not None:
                            patches.append(stats)
        return patches, before.crs


def _score_patches(patches: list[dict]) -> None:
    medians = {
        name: float(
            np.median([patch["percentiles"][name][1] for patch in patches])
        )
        for name in SCORE_THRESHOLDS
    }
    for patch in patches:
        scores = []
        for name, threshold in SCORE_THRESHOLDS.items():
            q10, _, q90 = patch["percentiles"][name]
            distance = max(
                abs(q10 - medians[name]),
                abs(q90 - medians[name]),
            )
            scores.append(distance / threshold)
        patch["index_signal_score"] = float(max(scores))
        patch["pair_delta_medians"] = medians


def _select_candidates(
    patches: list[dict], rng: np.random.Generator | None = None
) -> list[tuple[dict, str, float | None]]:
    if not patches:
        raise ValueError("No valid 256x256 patches found for candidate review")
    _score_patches(patches)
    patches_by_block: dict[str, list[dict]] = defaultdict(list)
    for patch in patches:
        patches_by_block[patch["spatial_block_id"]].append(patch)

    high = []
    for block_id in sorted(patches_by_block):
        above_threshold = [
            patch
            for patch in patches_by_block[block_id]
            if patch["index_signal_score"] >= 1.0
        ]
        if above_threshold:
            high_count = max(
                1, int(np.ceil(len(patches_by_block[block_id]) * HIGH_SIGNAL_FRACTION))
            )
            high.extend(
                sorted(
                    above_threshold,
                    key=lambda patch: patch["index_signal_score"],
                    reverse=True,
                )[:high_count]
            )
    high_ids = {(patch["row"], patch["col"]) for patch in high}

    below_by_block: dict[str, list[dict]] = defaultdict(list)
    for patch in patches:
        key = (patch["row"], patch["col"])
        if key not in high_ids and patch["index_signal_score"] < 1.0:
            below_by_block[patch["spatial_block_id"]].append(patch)

    if rng is None:
        rng = np.random.default_rng(RANDOM_SEED)
    random_background = []
    for block_id in sorted(below_by_block):
        block_patches = below_by_block[block_id]
        sample_count = int(np.ceil(len(block_patches) * RANDOM_BACKGROUND_FRACTION))
        if sample_count:
            chosen = rng.choice(len(block_patches), size=sample_count, replace=False)
            random_background.extend(block_patches[int(index)] for index in chosen)

    candidates = [
        (patch, "top_ranked_index_change", None) for patch in high
    ]
    for block_id in sorted(below_by_block):
        block_patches = below_by_block[block_id]
        sample_count = int(np.ceil(len(block_patches) * RANDOM_BACKGROUND_FRACTION))
        if not sample_count:
            continue
        selected_ids = {
            (patch["row"], patch["col"])
            for patch in random_background
            if patch["spatial_block_id"] == block_id
        }
        inclusion_probability = sample_count / len(block_patches)
        candidates.extend(
            (
                patch,
                "random_below_threshold_background",
                inclusion_probability,
            )
            for patch in block_patches
            if (patch["row"], patch["col"]) in selected_ids
        )
    return candidates


def _feature(
    scene: dict,
    patch: dict,
    review_type: str,
    selection_probability: float | None,
    source_crs,
) -> dict:
    col = patch["col"]
    row = patch["row"]
    patch_transform = patch["transform"]
    corners = [
        patch_transform @ (0, 0),
        patch_transform @ (PATCH_SIZE, 0),
        patch_transform @ (PATCH_SIZE, PATCH_SIZE),
        patch_transform @ (0, PATCH_SIZE),
    ]
    geometry = mapping(
        box(
            min(point[0] for point in corners),
            min(point[1] for point in corners),
            max(point[0] for point in corners),
            max(point[1] for point in corners),
        )
    )
    geometry = transform_geom(source_crs, "EPSG:4326", geometry)
    d = patch["percentiles"]
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "feature_id": f"{scene['tile']}_R{row:05d}_C{col:05d}",
            "region": scene["city"].lower(),
            "tile": scene["tile"],
            "pair_id": scene["pair_id"],
            "spatial_block_id": patch["spatial_block_id"],
            "split_assignment": "unassigned",
            "planned_split_buffer_m": 2000,
            "interval_type": _interval_type(scene["t1_date"], scene["t2_date"]),
            "t1_date": scene["t1_date"],
            "t2_date": scene["t2_date"],
            "review_type": review_type,
            "candidate_selection_probability": (
                round(selection_probability, 6)
                if selection_probability is not None
                else None
            ),
            "review_status": "unreviewed_candidate",
            "change_category": "candidate",
            "change_class": None,
            "non_target_subtype": None,
            "confounder": None,
            "confidence": "unreviewed",
            "annotator": None,
            "notes": "",
            "valid_fraction": round(patch["valid_fraction"], 4),
            "review_area_fraction": round(patch["review_area_fraction"], 4),
            "index_signal_score": round(patch["index_signal_score"], 4),
            "delta_ndvi_p10": round(d["ndvi"][0], 4),
            "delta_ndvi_p50": round(d["ndvi"][1], 4),
            "delta_ndvi_p90": round(d["ndvi"][2], 4),
            "delta_ndbi_p10": round(d["ndbi"][0], 4),
            "delta_ndbi_p50": round(d["ndbi"][1], 4),
            "delta_ndbi_p90": round(d["ndbi"][2], 4),
            "delta_mndwi_p10": round(d["mndwi"][0], 4),
            "delta_mndwi_p50": round(d["mndwi"][1], 4),
            "delta_mndwi_p90": round(d["mndwi"][2], 4),
            "water_rule_fraction": round(
                patch["water_transition_fraction"], 4
            ),
        },
    }


def generate_scene(scene: dict, output_dir: Path, overwrite: bool = False) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / scene["output"]
    if output.exists():
        if not overwrite:
            raise FileExistsError(
                f"Refusing to overwrite existing candidate GeoJSON: {output}"
            )
        _validate_replaceable_candidate(output)
    before_path, after_path, mask_path = _pair_paths(scene)
    patches, crs = _collect_patch_stats(scene, before_path, after_path, mask_path)
    selected = _select_candidates(patches)
    features = [
        _feature(scene, patch, review_type, probability, crs)
        for patch, review_type, probability in selected
    ]
    collection = {
        "type": "FeatureCollection",
        "name": f"{scene['city'].lower()}_unreviewed_candidate_windows",
        "metadata": {
            "pair_id": scene["pair_id"],
            "city": scene["city"],
            "tile": scene["tile"],
            "t1_date": scene["t1_date"],
            "t2_date": scene["t2_date"],
            "source": "metadata-scaled six-band reflectance and registered later date",
            "candidate_only": True,
            "not_ground_truth": True,
            "patch_size_pixels": PATCH_SIZE,
            "patch_size_m": PATCH_SIZE * 10,
            "high_signal_selection": (
                f"within each 10 km block, top {HIGH_SIGNAL_FRACTION:.0%} of "
                "all eligible windows whose pair-centered index-difference "
                "score is at least 1; not a change-class prediction"
            ),
            "random_background_selection": (
                f"{RANDOM_BACKGROUND_FRACTION:.0%} of below-score-threshold "
                "patches sampled independently within each 10 km block"
            ),
            "random_background_inclusion_probability": (
                "recorded per feature as candidate_selection_probability; "
                "not applicable to deterministic high-signal candidates"
            ),
            "score_thresholds": SCORE_THRESHOLDS,
            "review_area_filter": (
                "For Mumbai only, persistent water is inferred where MNDWI > 0 "
                "and NDVI < 0.2 in both dates. Persistent-water interiors more "
                f"than {OPEN_WATER_BUFFER_M} m from non-water are excluded from "
                "candidate scoring; coastal buffers and water-to-impervious "
                "transition pixels are retained. This is a review-area heuristic, "
                "not a land/water classification or a change label."
                if scene["city"].lower() == "mumbai"
                else "No water-area exclusion is applied."
            ),
            "water_rule_reported_only": (
                "delta_mndwi < -0.3 and delta_ndbi > 0.1; this is recorded as "
                "evidence only and does not assign a change class; for Mumbai "
                "it also preserves those transition pixels in candidate review"
            ),
            "planned_split_buffer_m": 2000,
            "split_assignments": "unassigned; no split has been applied",
            "random_seed": RANDOM_SEED,
            "review_required": (
                "Each polygon is a 256x256-pixel review window, not an object "
                "boundary or label. Verify imagery, haze, seasonality, tide, "
                "shadow, registration, and historical evidence before labeling."
            ),
            "reference_policy": (
                "Do not consult information created after T2. Use historical "
                "OSM only at or before T2. If the two Sentinel-2 dates do not "
                "resolve the cause, retain uncertain or leave unlabeled."
            ),
        },
        "features": features,
    }
    temporary_output = output.with_name(output.name + ".tmp")
    try:
        temporary_output.write_text(
            json.dumps(collection, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary_output.replace(output)
    except Exception:
        temporary_output.unlink(missing_ok=True)
        raise
    high = sum(review_type == "top_ranked_index_change" for _, review_type, _ in selected)
    random_count = len(features) - high
    print(
        f"{scene['city']}: {len(patches)} eligible patches; "
        f"{high} high-index review windows; "
        f"{random_count} random below-threshold windows; wrote {output}"
    )
    return len(features)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--city",
        action="append",
        choices=("mumbai", "delhi"),
        help="Generate only this city; may be supplied more than once",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the generated candidate files; reviewed labels are never modified",
    )
    args = parser.parse_args()
    selected_scenes = (
        SCENES
        if args.city is None
        else tuple(
            scene for scene in SCENES if scene["city"].lower() in args.city
        )
    )
    for scene in selected_scenes:
        generate_scene(scene, args.output_dir.resolve(), overwrite=args.overwrite)


if __name__ == "__main__":
    main()

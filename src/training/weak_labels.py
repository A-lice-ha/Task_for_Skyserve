"""Rule-based weak change labels from registered Sentinel-2 pairs.

The output is a single-band uint8 GeoTIFF per city on the registered grid:

    0  stable                (no target change, no large natural change)
    1  built_gain            (NDBI increase robust to 1-px misregistration)
    2  built_loss            (NDBI decrease robust to 1-px misregistration)
    3  vegetation_change     (natural, non-target)
    4  water_change          (natural, non-target: tide, water level)
    5  ambiguous             (rule conflict, seasonal drying, haze, tiny blobs)
    255 invalid              (outside common-valid / screened water)

These are weak, automatically derived labels for training only. They are not
ground truth and must never be used as evaluation references; human-reviewed
windows are the only evaluation data (assignment section 5).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import rasterio
from rasterio.windows import Window

from src.training.generate_candidates import NODATA, SCENES, _pair_paths

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "configs" / "weak_labels.json"
DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "weak_labels"
REVIEW_AREA = {"Mumbai": ROOT / "outputs" / "processed" / "mumbai_candidate_review_area.tif"}

STABLE, GAIN, LOSS, VEGETATION, WATER, AMBIGUOUS, INVALID = 0, 1, 2, 3, 4, 5, 255
CODE_NAMES = {
    STABLE: "stable",
    GAIN: "built_gain",
    LOSS: "built_loss",
    VEGETATION: "vegetation_change",
    WATER: "water_change",
    AMBIGUOUS: "ambiguous",
    INVALID: "invalid",
}
# Mapping used for model training: natural changes are hard negatives.
TRAIN_TARGET = {STABLE: 0, VEGETATION: 0, WATER: 0, GAIN: 1, LOSS: 2}
TRAIN_IGNORE = 255


def _ratio(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    out = np.zeros(a.shape, dtype=np.float32)
    den = a + b
    np.divide(a - b, den, out=out, where=np.abs(den) > 1e-6)
    return out


def indices(stack: np.ndarray) -> dict[str, np.ndarray]:
    """Indices from a B02, B03, B04, B08, B11, B12 reflectance stack."""
    b02, b03, b04, b08, b11 = stack[0], stack[1], stack[2], stack[3], stack[4]
    return {
        "ndvi": _ratio(b08, b04),
        "ndbi": _ratio(b11, b08),
        "mndwi": _ratio(b03, b11),
        "blue": b02,
    }


def _neighbourhood_max(values: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return values
    kernel = np.ones((2 * radius + 1, 2 * radius + 1), np.uint8)
    return cv2.dilate(values, kernel)


def _remove_small(codes: np.ndarray, code: int, min_px: int) -> None:
    mask = (codes == code).astype(np.uint8)
    count, components, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    small = np.flatnonzero(stats[:, cv2.CC_STAT_AREA] < min_px)
    small = small[small != 0]
    if small.size:
        codes[np.isin(components, small)] = AMBIGUOUS


def classify(
    before: np.ndarray,
    after: np.ndarray,
    valid: np.ndarray,
    config: dict,
) -> np.ndarray:
    """Apply the weak-label rules to one block. Arrays are (6, H, W) and (H, W)."""
    b, a = indices(before), indices(after)
    radius = int(config["registration_tolerance_px"])
    gain_cfg, loss_cfg = config["built_gain"], config["built_loss"]
    nat_cfg, amb_cfg = config["natural"], config["ambiguous"]

    # Change must exceed the best match within the registration tolerance, so a
    # one-pixel edge shift alone cannot create a built-up change.
    ndbi_gain = a["ndbi"] - _neighbourhood_max(b["ndbi"], radius)
    ndbi_loss = b["ndbi"] - _neighbourhood_max(a["ndbi"], radius)
    dndvi = a["ndvi"] - b["ndvi"]
    dmndwi = a["mndwi"] - b["mndwi"]
    dblue = a["blue"] - b["blue"]

    codes = np.full(valid.shape, STABLE, dtype=np.uint8)
    codes[np.abs(dndvi) >= nat_cfg["vegetation_abs_dndvi_min"]] = VEGETATION
    codes[np.abs(dmndwi) >= nat_cfg["water_abs_dmndwi_min"]] = WATER

    gain = (
        (ndbi_gain >= gain_cfg["ndbi_increase_min"])
        & (a["ndvi"] <= gain_cfg["ndvi_after_max"])
        & (dndvi <= gain_cfg["ndvi_change_max"])
        & (a["mndwi"] <= gain_cfg["mndwi_after_max"])
    )
    loss = (
        (ndbi_loss >= loss_cfg["ndbi_decrease_min"])
        & (dndvi <= loss_cfg["ndvi_increase_max"])
        & (a["mndwi"] <= loss_cfg["mndwi_after_max"])
    )
    codes[gain] = GAIN
    codes[loss] = LOSS
    # Dry-season senescence of vegetation mimics new bare/built surfaces.
    codes[gain & (b["ndvi"] >= amb_cfg["ndvi_before_vegetated_min"])] = AMBIGUOUS
    # Haze raises blue reflectance and depresses SWIR contrast -> false loss.
    codes[loss & (dblue >= loss_cfg["blue_increase_haze_max"])] = AMBIGUOUS
    codes[gain & loss] = AMBIGUOUS

    _remove_small(codes, GAIN, int(config["min_component_px"]))
    _remove_small(codes, LOSS, int(config["min_component_px"]))

    # Regional haze/thin cloud missed by SCL (observed in the 2026-10-22 Delhi
    # scene): a smooth, area-wide blue increase. Averaging over a large kernel
    # keeps compact bright new roofs from triggering it. Every index is
    # unreliable under haze, so all codes become ambiguous there.
    haze_cfg = config.get("haze")
    if haze_cfg:
        k = int(haze_cfg["kernel_px"])
        regional_dblue = cv2.blur(np.where(valid, dblue, 0).astype(np.float32), (k, k))
        codes[regional_dblue >= haze_cfg["regional_blue_increase_min"]] = AMBIGUOUS
    codes[~valid] = INVALID
    return codes


def _blocks(height: int, width: int, size: int):
    for row in range(0, height, size):
        for col in range(0, width, size):
            yield row, col, min(size, height - row), min(size, width - col)


def generate(city: str, config: dict, output_dir: Path) -> dict:
    scene = next(s for s in SCENES if s["city"].lower() == city.lower())
    before_path, after_path, mask_path = _pair_paths(scene)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{scene['city'].lower()}_weak_labels.tif"
    review_path = REVIEW_AREA.get(scene["city"])
    size, halo = int(config["block_size"]), int(config["halo"])
    counts = np.zeros(256, dtype=np.int64)

    with rasterio.open(before_path) as before, rasterio.open(after_path) as after, \
            rasterio.open(mask_path) as mask:
        review = rasterio.open(review_path) if review_path else None
        profile = mask.profile.copy()
        profile.update(dtype="uint8", count=1, nodata=INVALID, compress="deflate",
                       tiled=True, blockxsize=512, blockysize=512)
        with rasterio.open(out_path, "w", **profile) as out:
            for row, col, h, w in _blocks(mask.height, mask.width, size):
                r0, c0 = max(0, row - halo), max(0, col - halo)
                r1 = min(mask.height, row + h + halo)
                c1 = min(mask.width, col + w + halo)
                window = Window(c0, r0, c1 - c0, r1 - r0)
                bef = before.read(window=window).astype(np.float32)
                aft = after.read(window=window).astype(np.float32)
                valid = mask.read(1, window=window) == 1
                valid &= np.all(bef != NODATA, axis=0) & np.all(aft != NODATA, axis=0)
                valid &= np.all(np.isfinite(bef), axis=0) & np.all(np.isfinite(aft), axis=0)
                if review is not None:
                    valid &= review.read(1, window=window) == 1
                bef[:, ~valid] = 0
                aft[:, ~valid] = 0
                codes = classify(bef, aft, valid, config)
                core = codes[row - r0:row - r0 + h, col - c0:col - c0 + w]
                out.write(core, 1, window=Window(col, row, w, h))
                counts += np.bincount(core.ravel(), minlength=256)
        if review is not None:
            review.close()

    summary = {
        "city": scene["city"],
        "pair_id": scene["pair_id"],
        "output": out_path.name,
        "config": config,
        "pixel_counts": {CODE_NAMES[c]: int(counts[c]) for c in CODE_NAMES},
        "role": "weak_training_labels_not_ground_truth",
    }
    (output_dir / f"{scene['city'].lower()}_weak_labels.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--city", nargs="+", default=["mumbai", "delhi"])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    for city in args.city:
        summary = generate(city, config, args.output_dir)
        print(json.dumps(summary["pixel_counts"]))


if __name__ == "__main__":
    main()

"""Sen2Cor processing and registered reflectance preparation for Phase 2."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import rasterio
from affine import Affine
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from rasterio.warp import reproject


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEN2COR = Path(
    r"C:\Users\Satleo Labs-O\Downloads\Sen2Cor-02.13.02-win64"
    r"\Sen2Cor-02.13.02-win64\L2A_Process.bat"
)
BANDS = ("B02", "B03", "B04", "B08", "B11", "B12")
SPECTRAL_BAND_IDS = {
    "B01": 0,
    "B02": 1,
    "B03": 2,
    "B04": 3,
    "B05": 4,
    "B06": 5,
    "B07": 6,
    "B08": 7,
    "B8A": 8,
    "B09": 9,
    "B10": 10,
    "B11": 11,
    "B12": 12,
}
SCL_INVALID_CLASSES = {0, 1, 3, 7, 8, 9, 10, 11}
NODATA = -9999.0
BLOCK_SIZE = 512


@dataclass(frozen=True)
class Radiometry:
    gain: float
    offsets: dict[str, float]
    offset_tag: str
    gain_tag: str


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _metadata_values(root: ET.Element, tag: str) -> list[ET.Element]:
    return [element for element in root.iter() if _local_name(element.tag) == tag]


def read_radiometry(metadata_path: Path) -> Radiometry:
    """Read the product's scale denominator and per-band additive offsets."""
    root = ET.parse(metadata_path).getroot()
    boa_gain = _metadata_values(root, "BOA_QUANTIFICATION_VALUE")
    if boa_gain:
        gain_tag = "BOA_QUANTIFICATION_VALUE"
        offset_tag = "BOA_ADD_OFFSET"
        gain_element = boa_gain[0]
    else:
        gain_tag = "QUANTIFICATION_VALUE"
        offset_tag = "RADIO_ADD_OFFSET"
        gain_values = _metadata_values(root, gain_tag)
        if not gain_values:
            raise ValueError(f"{metadata_path} has no {gain_tag}")
        gain_element = gain_values[0]

    if gain_element.text is None:
        raise ValueError(f"{metadata_path} has an empty {gain_tag}")
    gain = float(gain_element.text)
    if not np.isfinite(gain) or gain <= 0:
        raise ValueError(f"{metadata_path} has invalid {gain_tag}: {gain}")

    offsets: dict[str, float] = {}
    for element in _metadata_values(root, offset_tag):
        band_id = element.attrib.get("band_id")
        if band_id is None or element.text is None:
            continue
        band = next(
            (name for name, index in SPECTRAL_BAND_IDS.items() if str(index) == band_id),
            None,
        )
        if band is not None:
            offsets[band] = float(element.text)

    missing = [band for band in BANDS if band not in offsets]
    if missing:
        raise ValueError(
            f"{metadata_path} is missing {offset_tag} for: {', '.join(missing)}"
        )
    return Radiometry(gain, offsets, offset_tag, gain_tag)


def apply_radiometry(digital_numbers: np.ndarray, gain: float, offset: float) -> np.ndarray:
    """Convert valid stored values to reflectance using metadata scaling."""
    return (digital_numbers.astype(np.float32) + np.float32(offset)) / np.float32(gain)


def _short_windows_path(path: Path, preserve_name: bool = False) -> str:
    if os.name != "nt":
        raise OSError("The Sen2Cor batch launcher requires Windows.")
    import ctypes
    from ctypes import wintypes

    get_short_path = ctypes.windll.kernel32.GetShortPathNameW
    get_short_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    get_short_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    target = path.parent.resolve() if preserve_name else path.resolve()
    length = get_short_path(str(target), buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError(f"Could not resolve a short Windows path for {path}")
    short_path = str(Path(buffer.value) / path.name) if preserve_name else buffer.value
    if " " in short_path:
        raise OSError(f"Windows short path still contains spaces: {short_path}")
    return short_path


def _find_l2a_products(root: Path) -> list[Path]:
    return sorted(
        metadata.parent
        for metadata in root.rglob("MTD_MSIL2A.xml")
        if metadata.is_file()
    )


def _scene_key(name: str) -> str:
    tile = re.search(r"_(T\d{2}[A-Z]{3})_", name)
    sensing_time = re.search(r"_(\d{8}T\d{6})_N\d{4}_", name)
    if not tile or not sensing_time:
        raise ValueError(f"Could not read tile and sensing time from {name}")
    return f"{tile.group(1)}_{sensing_time.group(1)}"


def run_sen2cor(
    product: Path,
    sen2cor_bat: Path,
    output_root: Path,
    resolution: int = 10,
) -> Path:
    product = product.resolve()
    sen2cor_bat = sen2cor_bat.resolve()
    if not product.is_dir() or not (product / "MTD_MSIL1C.xml").is_file():
        raise FileNotFoundError(f"Not a Sentinel-2 L1C SAFE product: {product}")
    if not sen2cor_bat.is_file():
        raise FileNotFoundError(f"Sen2Cor batch file not found: {sen2cor_bat}")
    if resolution not in (10, 20, 60):
        raise ValueError("Sen2Cor resolution must be 10, 20, or 60")

    output_dir = (output_root / "l2a" / _scene_key(product.name)).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = _find_l2a_products(output_dir)
    if existing:
        print(f"Sen2Cor output already exists; skipping: {existing[0]}")
        return existing[0]

    userprofile = Path.home()
    short_userprofile = _short_windows_path(userprofile)
    short_bat = _short_windows_path(sen2cor_bat)
    short_product = _short_windows_path(product, preserve_name=True)
    short_output = _short_windows_path(output_dir, preserve_name=True)
    command = (
        f"call {short_bat} {short_product} --output_dir {short_output} "
        f"--resolution {resolution}"
    )
    environment = os.environ.copy()
    environment["USERPROFILE"] = short_userprofile
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONEXECUTABLE", None)
    log_path = output_root / "logs" / f"{product.name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command_line = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", command]
    print(f"Running Sen2Cor for {product.name}; log: {log_path}", flush=True)
    started = time.monotonic()
    try:
        result = subprocess.run(
            command_line,
            cwd=short_output,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        log_path.write_text(stdout + stderr, encoding="utf-8", errors="replace")
        raise RuntimeError(
            f"Sen2Cor exceeded the 1800-second timeout for {product.name}; "
            f"inspect {log_path}"
        ) from error
    except subprocess.CalledProcessError as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        log_path.write_text(
            stdout + stderr, encoding="utf-8", errors="replace"
        )
        raise RuntimeError(
            f"Sen2Cor failed for {product.name} with exit code "
            f"{error.returncode}; inspect {log_path}. "
            f"Error details: {stderr[-2000:] or stdout[-2000:]}"
        ) from error
    log_path.write_text(
        result.stdout + result.stderr, encoding="utf-8", errors="replace"
    )
    elapsed = time.monotonic() - started
    print(
        f"Sen2Cor completed {product.name} in {elapsed:.1f}s; log: {log_path}",
        flush=True,
    )
    products = _find_l2a_products(output_dir)
    if len(products) != 1:
        raise RuntimeError(
            f"Expected one L2A product under {output_dir}, found {len(products)}; "
            f"inspect {log_path}"
        )
    return products[0]


def _find_band_file(granule: Path, band: str) -> Path:
    candidates = [
        path
        for path in granule.rglob("*.jp2")
        if re.search(rf"_{band}(?:_(?:10|20|60)m)?\.jp2$", path.name, re.IGNORECASE)
    ]
    if not candidates:
        raise FileNotFoundError(f"No {band} JP2 found under {granule}")

    def rank(path: Path) -> tuple[int, str]:
        match = re.search(r"_(10|20|60)m\.jp2$", path.name, re.IGNORECASE)
        resolution_rank = {"10": 0, "20": 1, "60": 2}.get(match.group(1), 3) if match else 3
        return resolution_rank, str(path)

    return min(candidates, key=rank)


def _find_scl_file(granule: Path) -> Path:
    candidates = sorted(
        path for path in granule.rglob("*SCL*.jp2") if path.is_file()
    )
    if not candidates:
        raise FileNotFoundError(f"No Sen2Cor scene-classification JP2 under {granule}")
    return candidates[0]


def _find_granule(product: Path) -> Path:
    granules = sorted(path for path in (product / "GRANULE").iterdir() if path.is_dir())
    if len(granules) != 1:
        raise ValueError(f"Expected one granule in {product}, found {len(granules)}")
    return granules[0]


def _windows(dataset: rasterio.io.DatasetReader):
    for row in range(0, dataset.height, BLOCK_SIZE):
        for column in range(0, dataset.width, BLOCK_SIZE):
            yield Window(
                column,
                row,
                min(BLOCK_SIZE, dataset.width - column),
                min(BLOCK_SIZE, dataset.height - row),
            )


def _write_product_summary(
    product: Path,
    output_path: Path,
    mask_path: Path,
    radiometry: Radiometry,
) -> Path:
    summary_path = output_path.with_name(
        output_path.name.removesuffix(".reflectance.tif") + ".summary.csv"
    )
    with rasterio.open(mask_path) as valid_dataset:
        valid_pixels = 0
        total_pixels = valid_dataset.width * valid_dataset.height
        for window in _windows(valid_dataset):
            valid_pixels += int(
                np.count_nonzero(valid_dataset.read(1, window=window))
            )
        pixel_area_m2 = abs(
            valid_dataset.transform.a * valid_dataset.transform.e
            - valid_dataset.transform.b * valid_dataset.transform.d
        )
        row = {
            "product": product.name,
            "crs": str(valid_dataset.crs),
            "width": valid_dataset.width,
            "height": valid_dataset.height,
            "pixel_area_m2": pixel_area_m2,
            "total_pixels": total_pixels,
            "valid_pixels": valid_pixels,
            "valid_percent": 100.0 * valid_pixels / total_pixels,
            "valid_area_km2": valid_pixels * pixel_area_m2 / 1_000_000,
            "gain_tag": radiometry.gain_tag,
            "gain": radiometry.gain,
            "offset_tag": radiometry.offset_tag,
            "band_offsets": json.dumps(radiometry.offsets, sort_keys=True),
        }
    with summary_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)
    print(
        f"{product.name}: {row['valid_percent']:.2f}% valid "
        f"({row['valid_area_km2']:.2f} km^2)"
    )
    return summary_path


def prepare_product(product: Path, output_root: Path) -> Path:
    metadata_path = product / "MTD_MSIL2A.xml"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"Not a Sen2Cor L2A product: {product}")
    radiometry = read_radiometry(metadata_path)
    granule = _find_granule(product)
    band_paths = {band: _find_band_file(granule, band) for band in BANDS}
    scl_path = _find_scl_file(granule)
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"{product.name}.reflectance.tif"
    mask_path = output_root / f"{product.name}.valid.tif"
    summary_path = output_path.with_name(
        output_path.name.removesuffix(".reflectance.tif") + ".summary.csv"
    )
    if output_path.exists() and mask_path.exists():
        if not summary_path.exists():
            _write_product_summary(product, output_path, mask_path, radiometry)
        return output_path
    if output_path.exists() or mask_path.exists():
        raise FileExistsError(
            f"Incomplete prepared output for {product.name}; remove or restore "
            f"both {output_path} and {mask_path} before rerunning"
        )

    with ExitStack() as stack:
        sources = {
            band: stack.enter_context(rasterio.open(path))
            for band, path in band_paths.items()
        }
        reference = sources["B02"]
        if reference.crs is None or reference.transform.a <= 0:
            raise ValueError(f"Invalid reference grid: {band_paths['B02']}")
        if not np.isclose(reference.transform.a, 10.0, atol=1e-5):
            raise ValueError(
                f"B02 grid is {reference.transform.a} m, expected a 10 m grid"
            )

        aligned = {
            band: stack.enter_context(
                WarpedVRT(
                    source,
                    crs=reference.crs,
                    transform=reference.transform,
                    width=reference.width,
                    height=reference.height,
                    resampling=Resampling.bilinear,
                    src_nodata=0,
                    nodata=0,
                    dtype="uint16",
                )
            )
            for band, source in sources.items()
        }
        scl = stack.enter_context(rasterio.open(scl_path))
        aligned_scl = stack.enter_context(
            WarpedVRT(
                scl,
                crs=reference.crs,
                transform=reference.transform,
                width=reference.width,
                height=reference.height,
                resampling=Resampling.nearest,
                src_nodata=0,
                nodata=0,
            )
        )

        profile = reference.profile.copy()
        profile.update(
            driver="GTiff",
            count=len(BANDS),
            dtype="float32",
            nodata=NODATA,
            compress="deflate",
            predictor=3,
            tiled=True,
            blockxsize=BLOCK_SIZE,
            blockysize=BLOCK_SIZE,
            BIGTIFF="IF_SAFER",
        )
        mask_profile = profile.copy()
        mask_profile.update(count=1, dtype="uint8", nodata=0, predictor=2)
        with rasterio.open(output_path, "w", **profile) as output, rasterio.open(
            mask_path, "w", **mask_profile
        ) as valid_output:
            for index, band in enumerate(BANDS, start=1):
                output.set_band_description(index, band)
            output.update_tags(
                product=product.name,
                radiometry="(DN + metadata additive offset) / metadata quantification value",
                quantification_value=str(radiometry.gain),
                quantification_tag=radiometry.gain_tag,
                offset_tag=radiometry.offset_tag,
                scl_source=str(scl_path.relative_to(product)),
            )
            for window in _windows(reference):
                valid = np.ones(
                    (int(window.height), int(window.width)), dtype=bool
                )
                for index, band in enumerate(BANDS, start=1):
                    digital_numbers = aligned[band].read(1, window=window)
                    band_valid = digital_numbers != 0
                    reflectance = apply_radiometry(
                        digital_numbers, radiometry.gain, radiometry.offsets[band]
                    )
                    reflectance[~band_valid] = NODATA
                    output.write(reflectance, index, window=window)
                    valid &= band_valid & np.isfinite(reflectance)
                scl_values = aligned_scl.read(1, window=window)
                valid &= ~np.isin(scl_values, tuple(SCL_INVALID_CLASSES))
                output.write_mask(valid.astype(np.uint8) * 255, window=window)
                valid_output.write(valid.astype(np.uint8), 1, window=window)
    _write_product_summary(product, output_path, mask_path, radiometry)
    return output_path


def _preview(dataset: rasterio.io.DatasetReader, band_index: int = 3):
    max_dimension = 4096
    scale = min(1.0, max_dimension / max(dataset.width, dataset.height))
    width = max(1, round(dataset.width * scale))
    height = max(1, round(dataset.height * scale))
    values = dataset.read(
        band_index,
        out_shape=(height, width),
        resampling=Resampling.average,
    ).astype(np.float32)
    mask = dataset.dataset_mask(
        out_shape=(height, width), resampling=Resampling.nearest
    ) > 0
    if np.count_nonzero(mask) < 100:
        raise ValueError(f"Too few valid pixels for registration in {dataset.name}")
    low, high = np.percentile(values[mask], (2, 98))
    if not high > low:
        raise ValueError(f"Insufficient contrast for registration in {dataset.name}")
    image = np.clip((values - low) * (255.0 / (high - low)), 0, 255).astype(
        np.uint8
    )
    image[~mask] = 0
    return image, mask.astype(np.uint8) * 255


def estimate_registration(
    reference: rasterio.io.DatasetReader,
    moving: rasterio.io.DatasetReader,
) -> tuple[Affine, dict[str, float | int]]:
    reference_image, reference_mask = _preview(reference)
    moving_image, moving_mask = _preview(moving)
    detector = cv2.ORB_create(nfeatures=12000, fastThreshold=7)
    reference_points, reference_descriptors = detector.detectAndCompute(
        reference_image, reference_mask
    )
    moving_points, moving_descriptors = detector.detectAndCompute(
        moving_image, moving_mask
    )
    if reference_descriptors is None or moving_descriptors is None:
        raise RuntimeError("Could not find registration features in both dates")

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    candidate_matches = matcher.knnMatch(
        moving_descriptors, reference_descriptors, k=2
    )
    matches = [
        first
        for pair in candidate_matches
        if len(pair) == 2
        for first, second in [pair]
        if first.distance < 0.75 * second.distance
    ]
    unique_matches: dict[int, cv2.DMatch] = {}
    for match in sorted(matches, key=lambda item: item.distance):
        unique_matches.setdefault(match.trainIdx, match)
    matches = list(unique_matches.values())
    if len(matches) < 30:
        raise RuntimeError(
            f"Too few stable registration matches: {len(matches)} (need at least 30)"
        )

    source_points = np.float32(
        [moving_points[match.queryIdx].pt for match in matches]
    )
    target_points = np.float32(
        [reference_points[match.trainIdx].pt for match in matches]
    )
    matrix, inlier_mask = cv2.estimateAffinePartial2D(
        source_points,
        target_points,
        method=cv2.RANSAC,
        ransacReprojThreshold=1.5,
        maxIters=10000,
        confidence=0.999,
        refineIters=20,
    )
    if matrix is None or inlier_mask is None:
        raise RuntimeError("RANSAC could not estimate a registration transform")
    inliers = inlier_mask.ravel().astype(bool)
    inlier_count = int(np.count_nonzero(inliers))
    inlier_ratio = inlier_count / len(matches)
    if inlier_count < 20 or inlier_ratio < 0.2:
        raise RuntimeError(
            f"Unreliable registration: {inlier_count}/{len(matches)} inliers"
        )

    predicted = cv2.transform(source_points[inliers, None, :], matrix)[:, 0, :]
    residuals = predicted - target_points[inliers]
    reference_scale_x = reference.width / reference_image.shape[1]
    reference_scale_y = reference.height / reference_image.shape[0]
    pixel_width = abs(reference.transform.a)
    pixel_height = abs(reference.transform.e)
    residual_m = np.column_stack(
        (residuals[:, 0] * reference_scale_x * pixel_width,
         residuals[:, 1] * reference_scale_y * pixel_height)
    )
    errors = np.linalg.norm(residual_m, axis=1)
    rmse_m = float(np.sqrt(np.mean(errors**2)))
    median_error_m = float(np.median(errors))
    p90_error_m, p95_error_m, max_error_m = (
        float(value) for value in np.percentile(errors, (90, 95, 100))
    )
    inlier_target_points = target_points[inliers]
    grid_columns = np.clip(
        (inlier_target_points[:, 0] / reference_image.shape[1] * 4).astype(int),
        0,
        3,
    )
    grid_rows = np.clip(
        (inlier_target_points[:, 1] / reference_image.shape[0] * 4).astype(int),
        0,
        3,
    )
    occupied_cells = len(set(zip(grid_columns.tolist(), grid_rows.tolist())))

    source_scale_x = moving.width / moving_image.shape[1]
    source_scale_y = moving.height / moving_image.shape[0]
    scale = np.diag((source_scale_x, source_scale_y, 1.0))
    inverse_scale = np.diag((1 / source_scale_x, 1 / source_scale_y, 1.0))
    preview_transform = np.eye(3)
    preview_transform[:2, :] = matrix
    full_resolution_transform = scale @ preview_transform @ inverse_scale
    affine = Affine(
        full_resolution_transform[0, 0],
        full_resolution_transform[0, 1],
        full_resolution_transform[0, 2],
        full_resolution_transform[1, 0],
        full_resolution_transform[1, 1],
        full_resolution_transform[1, 2],
    )
    metrics: dict[str, float | int] = {
        "matches": len(matches),
        "inliers": inlier_count,
        "inlier_ratio": inlier_ratio,
        "rmse_m": rmse_m,
        "median_error_m": median_error_m,
        "p90_error_m": p90_error_m,
        "p95_error_m": p95_error_m,
        "max_error_m": max_error_m,
        "inlier_grid_cells_occupied": occupied_cells,
        "inlier_grid_cells_total": 16,
        "shift_x_m": float(matrix[0, 2] * source_scale_x * pixel_width),
        "shift_y_m": float(matrix[1, 2] * source_scale_y * pixel_height),
    }
    return affine, metrics


def register_pair(
    before_path: Path,
    after_path: Path,
    output_root: Path,
    pair_name: str,
) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    registered_path = output_root / f"{pair_name}_after_registered.tif"
    metrics_path = output_root / f"{pair_name}_registration.csv"
    valid_path = output_root / f"{pair_name}_common_valid.tif"

    with rasterio.open(before_path) as before, rasterio.open(after_path) as after:
        if (
            before.crs != after.crs
            or before.width != after.width
            or before.height != after.height
            or before.transform != after.transform
        ):
            raise ValueError(
                f"{pair_name} pair grids differ; reproject to a common tile grid "
                "before feature registration"
            )
        affine, metrics = estimate_registration(before, after)
        moving_transform = after.transform * affine

    with rasterio.open(after_path) as after:
        profile = after.profile.copy()
        profile.update(
            driver="GTiff",
            dtype="float32",
            nodata=NODATA,
            compress="deflate",
            predictor=3,
            tiled=True,
            blockxsize=BLOCK_SIZE,
            blockysize=BLOCK_SIZE,
            BIGTIFF="IF_SAFER",
        )
        after_mask = after.dataset_mask()
        with rasterio.open(registered_path, "w", **profile) as destination:
            for index, description in enumerate(after.descriptions, start=1):
                if description:
                    destination.set_band_description(index, description)
            for window in _windows(after):
                valid_block = np.zeros(
                    (int(window.height), int(window.width)), dtype=np.uint8
                )
                reproject(
                    source=after_mask,
                    destination=valid_block,
                    src_transform=moving_transform,
                    src_crs=after.crs,
                    src_nodata=0,
                    dst_transform=after.window_transform(window),
                    dst_crs=after.crs,
                    dst_nodata=0,
                    resampling=Resampling.nearest,
                    init_dest_nodata=True,
                )
                for index in range(1, after.count + 1):
                    block = np.full(
                        (int(window.height), int(window.width)),
                        NODATA,
                        dtype=np.float32,
                    )
                    reproject(
                        source=rasterio.band(after, index),
                        destination=block,
                        src_transform=moving_transform,
                        src_crs=after.crs,
                        src_nodata=after.nodata,
                        dst_transform=after.window_transform(window),
                        dst_crs=after.crs,
                        dst_nodata=NODATA,
                        resampling=Resampling.bilinear,
                        init_dest_nodata=True,
                    )
                    block[valid_block == 0] = NODATA
                    destination.write(block, index, window=window)
                destination.write_mask(valid_block * 255, window=window)

    with rasterio.open(before_path) as before, rasterio.open(after_path) as after:
        after_mask = after.dataset_mask()
        with rasterio.open(valid_path, "w", **{
            **before.profile,
            "driver": "GTiff",
            "count": 1,
            "dtype": "uint8",
            "nodata": 0,
            "compress": "deflate",
            "predictor": 2,
            "tiled": True,
            "blockxsize": BLOCK_SIZE,
            "blockysize": BLOCK_SIZE,
        }) as valid_output:
            for window in _windows(before):
                before_valid = before.dataset_mask(window=window) > 0
                after_valid = np.zeros(
                    (int(window.height), int(window.width)), dtype=np.uint8
                )
                reproject(
                    source=after_mask,
                    destination=after_valid,
                    src_transform=moving_transform,
                    src_crs=after.crs,
                    src_nodata=0,
                    dst_transform=before.window_transform(window),
                    dst_crs=before.crs,
                    dst_nodata=0,
                    resampling=Resampling.nearest,
                    init_dest_nodata=True,
                )
                valid = before_valid & (after_valid > 0)
                valid_output.write(valid.astype(np.uint8), 1, window=window)

    metrics["pixel_transform_a"] = affine.a
    metrics["pixel_transform_b"] = affine.b
    metrics["pixel_transform_c"] = affine.c
    metrics["pixel_transform_d"] = affine.d
    metrics["pixel_transform_e"] = affine.e
    metrics["pixel_transform_f"] = affine.f
    with metrics_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=metrics.keys())
        writer.writeheader()
        writer.writerow(metrics)
    print(
        f"{pair_name}: {metrics['inliers']}/{metrics['matches']} inliers; "
        f"RMSE={metrics['rmse_m']:.2f} m; "
        f"median={metrics['median_error_m']:.2f} m; "
        f"shift=({metrics['shift_x_m']:.2f}, {metrics['shift_y_m']:.2f}) m"
    )
    return registered_path


def _find_pair_products(products: list[Path]) -> list[tuple[str, Path, Path]]:
    by_tile: dict[str, list[Path]] = {}
    for product in products:
        name = product.name
        tile_match = re.search(r"_(T\d{2}[A-Z]{3})_", name)
        if not tile_match:
            raise ValueError(f"Could not read tile from L2A product {name}")
        tile = tile_match.group(1)
        by_tile.setdefault(tile, []).append(product)

    pairs = []
    for tile, tile_products in sorted(by_tile.items()):
        tile_products.sort()
        if len(tile_products) != 2:
            raise ValueError(
                f"Expected exactly two L2A products for tile {tile}, "
                f"found {len(tile_products)}"
            )
        pairs.append((tile, tile_products[0], tile_products[1]))
    return pairs


def command_run(args: argparse.Namespace) -> None:
    products = sorted(ROOT.glob("*.SAFE"))
    if args.product:
        wanted = {Path(value).name for value in args.product}
        products = [product for product in products if product.name in wanted]
        missing = wanted - {product.name for product in products}
        if missing:
            raise FileNotFoundError(f"SAFE products not found: {', '.join(sorted(missing))}")
    if not products:
        raise FileNotFoundError(f"No L1C SAFE products found under {ROOT}")
    for product in products:
        result = run_sen2cor(
            product,
            args.sen2cor_bat,
            args.output_root,
            resolution=args.resolution,
        )
        print(f"L2A product: {result}")


def command_prepare(args: argparse.Namespace) -> None:
    products = _find_l2a_products(args.l2a_root)
    if not products:
        raise FileNotFoundError(f"No MTD_MSIL2A.xml found under {args.l2a_root}")
    for product in products:
        output = prepare_product(product, args.output_root)
        print(f"Prepared reflectance stack: {output}")


def command_register(args: argparse.Namespace) -> None:
    stacks = sorted(args.stack_root.glob("*.reflectance.tif"))
    if len(stacks) != 4:
        raise ValueError(f"Expected four prepared stacks, found {len(stacks)}")
    for tile, before_product, after_product in _find_pair_products(
        [path.parent / path.name.removesuffix(".reflectance.tif") for path in stacks]
    ):
        before_stack = args.stack_root / f"{before_product.name}.reflectance.tif"
        after_stack = args.stack_root / f"{after_product.name}.reflectance.tif"
        register_pair(
            before_stack,
            after_stack,
            args.output_root,
            f"{tile}_{before_product.name.split('_')[2]}_{after_product.name.split('_')[2]}",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "sen2cor", help="Run Sen2Cor on the supplied L1C SAFE products"
    )
    run_parser.add_argument("--sen2cor-bat", type=Path, default=DEFAULT_SEN2COR)
    run_parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "sen2cor")
    run_parser.add_argument("--resolution", type=int, choices=(10, 20, 60), default=10)
    run_parser.add_argument(
        "--product",
        action="append",
        help="Process only this SAFE folder name; may be supplied more than once",
    )
    run_parser.set_defaults(function=command_run)

    prepare_parser = subparsers.add_parser(
        "prepare", help="Apply L2A metadata scaling and create reflectance stacks"
    )
    prepare_parser.add_argument(
        "--l2a-root", type=Path, default=ROOT / "outputs" / "sen2cor" / "l2a"
    )
    prepare_parser.add_argument(
        "--output-root", type=Path, default=ROOT / "outputs" / "processed"
    )
    prepare_parser.set_defaults(function=command_prepare)

    register_parser = subparsers.add_parser(
        "register", help="Estimate and apply same-tile pair registration"
    )
    register_parser.add_argument(
        "--stack-root", type=Path, default=ROOT / "outputs" / "processed"
    )
    register_parser.add_argument(
        "--output-root", type=Path, default=ROOT / "outputs" / "registered"
    )
    register_parser.set_defaults(function=command_register)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.function(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

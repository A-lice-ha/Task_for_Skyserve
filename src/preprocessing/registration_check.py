"""Independent check of residual registration error on stable features.

The registration (phase2.py) fits an affine transform to ORB tie points found
on a downsampled preview and reports residuals on those same points. This
module measures the residual shift independently, with a different method,
at full resolution:

- Candidate checkpoints: 64 x 64 px windows on a 128 px grid that are
  fully valid in both dates, labelled stable by the weak-label rules (no
  built-up, vegetation or water change, no haze/ambiguity) and textured
  (B08 standard deviation >= TEXTURE_MIN), i.e. stable features such as
  existing built-up areas, roads and field boundaries.
- Shift: sub-pixel phase cross-correlation (scikit-image, upsample 20,
  Hanning window) of the earlier date against the later date, separately in
  B04 and B08. A checkpoint is accepted when the two bands agree within
  BAND_AGREE_PX (rejects windows where real change or noise dominates).
  Precision of the method on synthetic shifts: RMSE 0.12 px.
- Compared variants of the later image: unregistered L2A stack, the default
  registration (outputs/registered/), the 15 m-tolerance registration
  (outputs/registered_15m/), and each fitted transform applied correctly
  ("*_applied"). The written registered rasters turned out to be identical
  to the unregistered stacks (D-019), so the "*_applied" variants show what
  the fitted transforms would actually have done.

Tie-point locations were not saved, so checkpoints cannot be spatially
excluded from them; independence comes from the different scale (full
resolution vs preview), method (area correlation vs feature matching) and
sample (thousands of windows vs ~300 points).

Outputs: outputs/reports/registration_checkpoints.csv (per window and
variant) and outputs/reports/registration_check.{json,md}.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import cv2
import numpy as np
import rasterio
from affine import Affine
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window
from skimage.registration import phase_cross_correlation

from src.training.generate_candidates import NODATA, SCENES, _find_one, _pair_paths

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "reports"
SIZE, STRIDE = 64, 128
BANDS = {"B04": 3, "B08": 4}          # 1-based indices in the six-band stack
VALID_MIN, STABLE_MIN = 1.0, 0.90
TEXTURE_MIN = 0.02                     # B08 reflectance standard deviation
BAND_AGREE_PX = 0.5
MAX_SHIFT_PX = 5.0
PIXEL_M = 10.0
GRID = 4                               # coverage cells per side
HANN = cv2.createHanningWindow((SIZE, SIZE), cv2.CV_32F)


def measure_shift(before: np.ndarray, after: np.ndarray) -> tuple[float, float, float]:
    """Displacement (dx, dy) in px of `after` content relative to `before`, and error."""
    a = (before - before.mean()) * HANN
    b = (after - after.mean()) * HANN
    shift, error, _ = phase_cross_correlation(a, b, upsample_factor=20)
    return float(-shift[1]), float(-shift[0]), float(error)


def variants(scene: dict) -> dict[str, Path]:
    _, registered, _ = _pair_paths(scene)
    after_stamp = registered.name.split("_")[2]           # e.g. 20230512T053641
    return {
        "unregistered": _find_one(ROOT / "outputs" / "processed", f"S2A_MSIL2A_{after_stamp}*.reflectance.tif"),
        "registered": registered,
        "registered_15m": ROOT / "outputs" / "registered_15m" / registered.name,
    }


PAD = 8


def fitted_affine(registration_csv: Path) -> Affine:
    row = next(csv.DictReader(registration_csv.open(encoding="utf-8")))
    return Affine(*(float(row[f"pixel_transform_{k}"]) for k in "abcdef"))


def warp_window(src, band: int, affine: Affine, col: int, row: int) -> np.ndarray:
    """The SIZE x SIZE window of `src` after correctly applying the fitted affine.

    Reads a padded block as an ndarray, so rasterio honours src_transform (a
    rasterio.band source silently ignores it; see D-019).
    """
    block = src.read(band, window=Window(col - PAD, row - PAD, SIZE + 2 * PAD, SIZE + 2 * PAD),
                     boundless=True, fill_value=NODATA).astype(np.float32)
    out = np.full((SIZE, SIZE), NODATA, np.float32)
    reproject(block, out, src_transform=src.transform * affine * Affine.translation(col - PAD, row - PAD),
              src_crs=src.crs, src_nodata=NODATA,
              dst_transform=src.transform * Affine.translation(col, row), dst_crs=src.crs,
              dst_nodata=NODATA, resampling=Resampling.bilinear)
    return out


def check_scene(scene: dict) -> list[dict]:
    city = scene["city"].lower()
    before_path, registered_path, mask_path = _pair_paths(scene)
    paths = {k: v for k, v in variants(scene).items() if v.is_file()}
    fitted = {f"{name}_applied": fitted_affine(paths[name].with_name(
        paths[name].name.replace("_after_registered.tif", "_registration.csv")))
        for name in ("registered", "registered_15m") if name in paths}
    weak_path = ROOT / "outputs" / "weak_labels" / f"{city}_weak_labels.tif"
    srcs = {name: rasterio.open(p) for name, p in paths.items()}
    rows = []
    with rasterio.open(before_path) as b, rasterio.open(mask_path) as m, rasterio.open(weak_path) as w:
        for name, src in srcs.items():
            if (src.width, src.height, src.transform) != (b.width, b.height, b.transform):
                raise ValueError(f"{name} is not on the before grid")
        height, width = b.height, b.width
        for row in range(0, height - SIZE + 1, STRIDE):
            strip = Window(0, row, width, SIZE)
            bef = {k: b.read(i, window=strip) for k, i in BANDS.items()}
            aft = {n: {k: s.read(i, window=strip) for k, i in BANDS.items()} for n, s in srcs.items()}
            mask, weak = m.read(1, window=strip), w.read(1, window=strip)
            for col in range(0, width - SIZE + 1, STRIDE):
                sl = np.s_[:, col:col + SIZE]
                valid = (mask[sl] == 1) & (bef["B08"][sl] != NODATA)
                for n in srcs:
                    valid &= aft[n]["B08"][sl] != NODATA
                if valid.mean() < VALID_MIN or (weak[sl] == 0).mean() < STABLE_MIN:
                    continue
                texture = float(bef["B08"][sl][valid].std())
                if texture < TEXTURE_MIN:
                    continue
                candidates = {n: {k: aft[n][k][sl].astype(np.float32) for k in BANDS} for n in srcs}
                for n, aff in fitted.items():
                    candidates[n] = {k: warp_window(srcs["unregistered"], i, aff, col, row)
                                     for k, i in BANDS.items()}
                for n, after_bands in candidates.items():
                    if any((v == NODATA).any() for v in after_bands.values()):
                        continue
                    per_band = {k: measure_shift(bef[k][sl].astype(np.float32), after_bands[k]) for k in BANDS}
                    (x4, y4, e4), (x8, y8, e8) = per_band["B04"], per_band["B08"]
                    agree = float(np.hypot(x4 - x8, y4 - y8))
                    dx, dy = (x4 + x8) / 2, (y4 + y8) / 2
                    rows.append({
                        "city": city, "variant": n, "row": row, "col": col, "texture_b08": round(texture, 4),
                        "dx_px": round(dx, 3), "dy_px": round(dy, 3), "shift_px": round(float(np.hypot(dx, dy)), 3),
                        "band_disagreement_px": round(agree, 3), "error_b04": round(e4, 4), "error_b08": round(e8, 4),
                        "accepted": agree <= BAND_AGREE_PX and np.hypot(dx, dy) <= MAX_SHIFT_PX,
                    })
    for s in srcs.values():
        s.close()
    return rows


def summarise(rows: list[dict], height: int = 10980, width: int = 10980) -> dict:
    out = {}
    for city in sorted({r["city"] for r in rows}):
        out[city] = {}
        for variant in ("unregistered", "registered", "registered_15m",
                        "registered_applied", "registered_15m_applied"):
            sel = [r for r in rows if r["city"] == city and r["variant"] == variant]
            ok = [r for r in sel if r["accepted"]]
            if not ok:
                continue
            dx = np.array([r["dx_px"] for r in ok]); dy = np.array([r["dy_px"] for r in ok])
            mag = np.hypot(dx, dy)
            # Residual after removing the mean (systematic) offset.
            res = np.hypot(dx - dx.mean(), dy - dy.mean())
            cells = {}
            for r, v in zip(ok, mag):
                key = (r["row"] * GRID // height, r["col"] * GRID // width)
                cells.setdefault(key, []).append(v)
            out[city][variant] = {
                "candidates": len(sel), "accepted": len(ok),
                "mean_dx_m": round(float(dx.mean()) * PIXEL_M, 2), "mean_dy_m": round(float(dy.mean()) * PIXEL_M, 2),
                "median_shift_m": round(float(np.median(mag)) * PIXEL_M, 2),
                "rmse_shift_m": round(float(np.sqrt((mag ** 2).mean())) * PIXEL_M, 2),
                "p90_shift_m": round(float(np.percentile(mag, 90)) * PIXEL_M, 2),
                "p95_shift_m": round(float(np.percentile(mag, 95)) * PIXEL_M, 2),
                "share_over_half_px": round(float((mag > 0.5).mean()), 4),
                "share_over_1px": round(float((mag > 1.0).mean()), 4),
                "rmse_after_removing_mean_m": round(float(np.sqrt((res ** 2).mean())) * PIXEL_M, 2),
                "cells_occupied": f"{len(cells)}/{GRID * GRID}",
                "cell_median_shift_m": {f"r{k[0]}c{k[1]}": round(float(np.median(v)) * PIXEL_M, 2)
                                        for k, v in sorted(cells.items())},
            }
    return out


def write(rows: list[dict], summary: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "registration_checkpoints.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    params = {"window_px": SIZE, "stride_px": STRIDE, "bands": list(BANDS), "valid_min": VALID_MIN,
              "stable_min": STABLE_MIN, "texture_min_b08": TEXTURE_MIN, "band_agree_px": BAND_AGREE_PX,
              "method": "skimage phase_cross_correlation, upsample 20, Hanning window",
              "synthetic_precision_rmse_px": 0.12}
    (OUT / "registration_check.json").write_text(json.dumps({"parameters": params, "results": summary}, indent=1),
                                                 encoding="utf-8")
    lines = ["| City | Variant | Checkpoints (accepted/candidates) | Mean offset dx, dy (m) | Median (m) | RMSE (m) | P90 (m) | > 1 px | RMSE without mean offset (m) | Cells |",
             "|---|---|---|---|---:|---:|---:|---:|---:|---|"]
    for city, variants_ in summary.items():
        for v, s in variants_.items():
            lines.append(f"| {city} | {v} | {s['accepted']}/{s['candidates']} | {s['mean_dx_m']}, {s['mean_dy_m']} | "
                         f"{s['median_shift_m']} | {s['rmse_shift_m']} | {s['p90_shift_m']} | "
                         f"{s['share_over_1px']:.1%} | {s['rmse_after_removing_mean_m']} | {s['cells_occupied']} |")
    (OUT / "registration_check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main() -> None:
    rows = []
    for scene in SCENES:
        rows += check_scene(scene)
    write(rows, summarise(rows))


if __name__ == "__main__":
    main()

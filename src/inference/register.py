"""Build the detection register (polygons + CSV) from a change-class map.

Each connected change component (>= MIN_AREA_PX) becomes one detection with:
  location (centroid lat/lon, polygon), change type, area and its uncertainty,
  length for linear features, model confidence, and review status.

Type assignment (spectral models cannot separate all target classes; rules
are recorded in each row's ``type_basis``):
  built-up loss                               -> demolition
  gain with >= 50 % of pixels water at T1     -> bridge_or_reclamation
  gain, elongated (long/short >= 4, width <= 3 px) -> road
  other gain                                  -> construction
  infrastructure_other is assigned only from human review, never by rule.

Extent uncertainty: area +/- (boundary pixels x 100 m^2), i.e. one pixel of
boundary placement error, which matches the ~1 px residual registration error.
Review status comes from the human review of the enclosing candidate window
(if any): accepted / rejected_<reason> / uncertain / unreviewed.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import shapes
from rasterio.windows import Window
from shapely.geometry import box, shape
from shapely.ops import unary_union

from src.evaluation.evaluate import NATURAL, POSITIVE, REVIEWS
from src.evaluation.windows import load_reviews
from src.training.generate_candidates import SCENES, _pair_paths

ROOT = Path(__file__).resolve().parents[2]
MAPS = ROOT / "outputs" / "maps"
MIN_AREA_PX = 8
PIXEL_M2 = 100.0


def _confidence_tier(p: float) -> str:
    return "high" if p >= 0.8 else "medium" if p >= 0.65 else "low"


def _review_lookup(city: str, crs) -> gpd.GeoDataFrame | None:
    if not REVIEWS.exists():
        return None
    reviews = {r["feature_id"]: r for r in load_reviews(REVIEWS) if r["city"].lower() == city}
    cands = gpd.read_file(ROOT / "data" / "labels" / "candidates" / f"{city}_candidates.geojson")
    cands = cands[cands["feature_id"].isin(reviews)].to_crs(crs)
    cands["human_class"] = cands["feature_id"].map(lambda f: reviews[f]["change_class"])
    cands["human_confidence"] = cands["feature_id"].map(lambda f: reviews[f].get("confidence"))
    cands["human_notes"] = cands["feature_id"].map(lambda f: reviews[f].get("notes", ""))
    return cands[["feature_id", "human_class", "human_confidence", "human_notes", "geometry"]]


def _status(change_type: str, human: str | None) -> str:
    if human is None:
        return "unreviewed"
    if human == "uncertain":
        return "uncertain"
    if human in POSITIVE:
        loss_expected = human == "demolition"
        return "accepted" if loss_expected == (change_type == "demolition") else "accepted_type_disputed"
    if human in NATURAL:
        return f"rejected_{human}"
    return "rejected_no_change"


def build(city: str, tag: str, out: Path = MAPS) -> gpd.GeoDataFrame:
    scene = next(s for s in SCENES if s["city"].lower() == city)
    before_path, _, _ = _pair_paths(scene)
    cls_path = out / f"{city}_{tag}_change_class.tif"
    prob_path = out / f"{city}_{tag}_change_prob.tif"
    rows = []
    with rasterio.open(cls_path) as c, rasterio.open(prob_path) as p, rasterio.open(before_path) as b:
        classes, probs = c.read(1), p.read(1)
        transform, crs = c.transform, c.crs
        # Offset of the (possibly regional) map inside the full raster grid.
        col_off = int(round((transform.c - b.transform.c) / b.transform.a))
        row_off = int(round((transform.f - b.transform.f) / b.transform.e))
        for code, direction in ((1, "gain"), (2, "loss")):
            mask = (classes == code).astype(np.uint8)
            n, comp, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
            for label in range(1, n):
                x, y, w, h, area = stats[label]
                if area < MIN_AREA_PX:
                    continue
                sub = comp[y:y + h, x:x + w] == label
                p_vals = probs[y:y + h, x:x + w][sub].astype(np.float32) / 250
                boundary = int(sub.sum() - cv2.erode(sub.astype(np.uint8), np.ones((3, 3), np.uint8)).sum())
                pts = np.column_stack(np.nonzero(sub))[:, ::-1].astype(np.float32)
                (_, _), (rw, rh), _ = cv2.minAreaRect(pts) if len(pts) >= 3 else ((0, 0), (w, h), 0)
                long_side, short_side = max(rw, rh) + 1, min(rw, rh) + 1
                water_frac = 0.0
                ctype, basis, length_m = ("demolition", "built-up loss", None)
                if direction == "gain":
                    bands = b.read((2, 5), window=Window(col_off + x, row_off + y, w, h)).astype(np.float32)
                    mndwi = (bands[0] - bands[1]) / np.maximum(bands[0] + bands[1], 1e-6)
                    water_frac = float((mndwi[sub] > 0.2).mean())
                    if water_frac >= 0.5:
                        ctype, basis = "bridge_or_reclamation", "gain over T1 water (MNDWI>0.2)"
                    elif long_side / short_side >= 4 and short_side <= 3:
                        ctype, basis = "road", "elongated gain (long/short>=4, width<=3 px)"
                        length_m = round(float(long_side) * 10, 0)
                    else:
                        ctype, basis = "construction", "compact built-up gain"
                geom = unary_union([shape(g) for g, v in shapes(
                    sub.astype(np.uint8), mask=sub,
                    transform=transform * transform.translation(x, y)) if v == 1])
                rows.append({
                    "city": scene["city"], "pair_id": scene["pair_id"],
                    "t1": scene["t1_date"], "t2": scene["t2_date"],
                    "direction": direction, "change_type": ctype, "type_basis": basis,
                    "area_m2": float(area * PIXEL_M2),
                    "area_uncertainty_m2": float(boundary * PIXEL_M2),
                    "length_m": length_m, "t1_water_fraction": round(water_frac, 3),
                    "confidence": round(float(p_vals.mean()), 3),
                    "confidence_tier": _confidence_tier(float(p_vals.mean())),
                    "geometry": geom,
                })
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)
    if gdf.empty:
        return gdf
    reviews = _review_lookup(city, crs)
    gdf["review_window"] = None
    gdf["human_class"] = None
    if reviews is not None and not reviews.empty:
        joined = gpd.sjoin(gdf[["geometry"]].assign(geometry=gdf.geometry.centroid),
                           reviews, how="left", predicate="within")
        joined = joined[~joined.index.duplicated()]
        gdf["review_window"] = joined["feature_id"]
        gdf["human_class"] = joined["human_class"]
    gdf["review_status"] = [
        _status(t, h if isinstance(h, str) else None) for t, h in zip(gdf["change_type"], gdf["human_class"])]
    # Second-stage context score of the enclosing 2.56 km window (if computed).
    ctx_path = out / f"{city}_{tag}_context.tif"
    if ctx_path.exists():
        with rasterio.open(ctx_path) as ctx:
            grid, thr = ctx.read(1), float(ctx.tags().get("threshold", "nan"))
            cents = gdf.geometry.centroid
            rr, cc = rasterio.transform.rowcol(ctx.transform, cents.x.values, cents.y.values)
            rr = np.clip(np.asarray(rr), 0, grid.shape[0] - 1)
            cc = np.clip(np.asarray(cc), 0, grid.shape[1] - 1)
            gdf["context_score"] = np.round(grid[rr, cc], 4)
            gdf["context_flag"] = gdf["context_score"] >= thr
            # Priority for analysts: context first, then pixel confidence.
            gdf["priority_score"] = np.round(gdf["context_score"] * gdf["confidence"], 4)
    centroids = gdf.geometry.centroid.to_crs(4326)
    gdf.insert(0, "detection_id", [f"{city[:3].upper()}-{i:05d}" for i in range(1, len(gdf) + 1)])
    gdf["lat"], gdf["lon"] = centroids.y.round(6), centroids.x.round(6)
    gdf["model"] = tag
    return gdf


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="map tag used by predict_tile.py")
    parser.add_argument("--city", nargs="+", default=["mumbai", "delhi"])
    parser.add_argument("--out", type=Path, default=MAPS)
    args = parser.parse_args(argv)
    frames = []
    for city in args.city:
        gdf = build(city, args.tag, args.out)
        if gdf.empty:
            continue
        gdf.to_file(args.out / f"{city}_{args.tag}_detections.gpkg", layer="detections", driver="GPKG")
        if "context_flag" in gdf:
            # Compact, committed layer: detections inside windows the
            # second-stage context filter flags, highest priority first.
            gdf[gdf["context_flag"]].sort_values("priority_score", ascending=False).to_file(
                args.out / f"priority_detections_{args.tag}.gpkg", layer=city, driver="GPKG")
        frames.append(gdf)
        print(city, len(gdf), gdf["change_type"].value_counts().to_dict(),
              gdf["review_status"].value_counts().to_dict())
    if frames:
        reg = pd.concat([f.drop(columns="geometry") for f in frames])
        reg.to_csv(args.out / f"detection_register_{args.tag}.csv", index=False)


if __name__ == "__main__":
    main()

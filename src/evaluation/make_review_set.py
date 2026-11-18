"""Build a local, blind before/after review page for candidate windows.

Writes outputs/review/{index.html, chips/*.jpg|png, windows.json}. Open
index.html in a browser, label windows, then use "Export labels" and save the
file as data/labels/reviews/review_labels.json.

The page hides how each window was selected (ranked vs random) and shuffles
order with a fixed seed so the reviewer's judgement is not anchored to the
candidate score.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path

import cv2
import geopandas as gpd
import numpy as np
import rasterio
from rasterio.windows import Window

from src.training.generate_candidates import NODATA, SCENES, _pair_paths

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "review"
WEAK = ROOT / "outputs" / "weak_labels"
SIZE = 256
SCALE = 2
SEED = 20261118


def _rgb(stack: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    rgb = stack[[2, 1, 0]].astype(np.float32)  # B04, B03, B02
    rgb = (rgb - lo[:, None, None]) / np.maximum(hi - lo, 1e-6)[:, None, None]
    rgb = np.clip(rgb, 0, 1) ** (1 / 1.4)
    img = (rgb.transpose(1, 2, 0)[:, :, ::-1] * 255).astype(np.uint8)  # BGR
    return cv2.resize(img, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_NEAREST)


def _overlay(codes: np.ndarray) -> np.ndarray:
    """Transparent PNG outlining weak gain (red) and loss (cyan) components."""
    rgba = np.zeros((SIZE * SCALE, SIZE * SCALE, 4), np.uint8)
    big = cv2.resize(codes, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_NEAREST)
    for code, colour in ((1, (40, 40, 255, 255)), (2, (255, 230, 0, 255))):
        mask = (big == code).astype(np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        cv2.drawContours(rgba, contours, -1, colour, 1)
    invalid = big == 255
    rgba[invalid] = (128, 128, 128, 140)
    return rgba


def build(output: Path = OUT) -> int:
    chips = output / "chips"
    chips.mkdir(parents=True, exist_ok=True)
    windows, block_ids = [], []
    for scene in SCENES:
        city = scene["city"].lower()
        candidates = gpd.read_file(ROOT / "data" / "labels" / "candidates" / scene["output"])
        before_path, after_path, _ = _pair_paths(scene)
        with rasterio.open(before_path) as before, rasterio.open(after_path) as after, \
                rasterio.open(WEAK / f"{city}_weak_labels.tif") as weak:
            for _, cand in candidates.iterrows():
                row, col = map(int, re.search(r"R(\d+)_C(\d+)", cand["feature_id"]).groups())
                window = Window(col, row, SIZE, SIZE)
                bef, aft = before.read(window=window), after.read(window=window)
                codes = weak.read(1, window=window)
                valid = np.all(bef != NODATA, 0) & np.all(aft != NODATA, 0)
                pool = np.concatenate(
                    [bef[[2, 1, 0]][:, valid], aft[[2, 1, 0]][:, valid]], axis=1
                ) if valid.any() else np.zeros((3, 1))
                lo, hi = np.percentile(pool, 2, axis=1), np.percentile(pool, 98, axis=1)
                fid = cand["feature_id"]
                cv2.imwrite(str(chips / f"{fid}_t1.jpg"), _rgb(bef, lo, hi),
                            [cv2.IMWRITE_JPEG_QUALITY, 85])
                cv2.imwrite(str(chips / f"{fid}_t2.jpg"), _rgb(aft, lo, hi),
                            [cv2.IMWRITE_JPEG_QUALITY, 85])
                cv2.imwrite(str(chips / f"{fid}_mask.png"), _overlay(codes))
                centroid = cand.geometry.centroid
                block_ids.append(cand["spatial_block_id"])
                windows.append({
                    "feature_id": fid,
                    "city": scene["city"],
                    "t1": scene["t1_date"],
                    "t2": scene["t2_date"],
                    "lat": round(centroid.y, 5),
                    "lon": round(centroid.x, 5),
                    "weak_gain_px": int((codes == 1).sum()),
                    "weak_loss_px": int((codes == 2).sum()),
                })
    # Evaluation windows (val/test/zero-shot blocks) first, each group shuffled.
    splits_path = ROOT / "data" / "manifests" / "spatial_splits.json"
    splits = json.loads(splits_path.read_text(encoding="utf-8"))["cities"] if splits_path.exists() else {}
    rank = {"zero_shot": 0, "test": 0, "val": 0, "train": 1}
    for w, cand_block in zip(windows, block_ids):
        w["priority"] = rank.get(splits.get(w["city"].lower(), {}).get("blocks", {}).get(cand_block), 2)
    rng = random.Random(SEED)
    rng.shuffle(windows)
    windows.sort(key=lambda w: w["priority"])
    (output / "windows.json").write_text(json.dumps(windows, indent=1), encoding="utf-8")
    html = (ROOT / "src" / "evaluation" / "review_page.html").read_text(encoding="utf-8")
    (output / "index.html").write_text(
        html.replace("/*WINDOWS*/[]", json.dumps(windows)), encoding="utf-8"
    )
    return len(windows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args(argv)
    print(f"Wrote {build(args.output)} review windows to {args.output / 'index.html'}")


if __name__ == "__main__":
    main()

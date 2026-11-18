"""Tile-wide second-stage context score (window filter) for a change map.

Applies the logistic window filter fitted on train-block human reviews
(src/evaluation/window_filter.py -> <run>/window_filter.json) to every
non-overlapping 256 x 256 px window of a full-tile change map. Output:
outputs/maps/<city>_<tag>_context.tif, one float32 value per window (P of
man-made change in that 2.56 km window; -1 where the window has no valid
pixels), on a 2,560 m grid aligned with the 10 m grid.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

from src.evaluation.window_filter import window_features
from src.training.generate_candidates import NODATA, SCENES, _pair_paths

ROOT = Path(__file__).resolve().parents[2]
MAPS = ROOT / "outputs" / "maps"
SIZE = 256


def load_filter(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def score(features: np.ndarray, params: dict) -> float:
    z = (features - np.array(params["mean"])) / np.array(params["scale"])
    return float(1 / (1 + np.exp(-(z @ np.array(params["coef"]) + params["intercept"]))))


def build(city: str, tag: str, params: dict, out: Path = MAPS) -> Path:
    scene = next(s for s in SCENES if s["city"].lower() == city)
    before_path, after_path, mask_path = _pair_paths(scene)
    cls_path = out / f"{city}_{tag}_change_class.tif"
    with rasterio.open(before_path) as b, rasterio.open(after_path) as a, \
            rasterio.open(mask_path) as m, rasterio.open(cls_path) as c:
        if c.width != b.width or c.height != b.height:
            raise ValueError("context score needs a full-tile change map")
        rows, cols = int(np.ceil(b.height / SIZE)), int(np.ceil(b.width / SIZE))
        grid = np.full((rows, cols), -1.0, np.float32)
        for r in range(rows):
            for q in range(cols):
                win = Window(q * SIZE, r * SIZE, min(SIZE, b.width - q * SIZE), min(SIZE, b.height - r * SIZE))
                valid = m.read(1, window=win) == 1
                if not valid.any():
                    continue
                bef, aft = b.read(window=win), a.read(window=win)
                valid &= np.all(bef != NODATA, 0) & np.all(aft != NODATA, 0)
                if not valid.any():
                    continue
                bef[:, ~valid] = 0
                aft[:, ~valid] = 0
                cmap = c.read(1, window=win)
                cmap[(cmap == 255) | ~valid] = 0
                grid[r, q] = score(window_features(np.stack([bef, aft]), valid, cmap), params)
        profile = {"driver": "GTiff", "width": cols, "height": rows, "count": 1, "dtype": "float32",
                   "crs": b.crs, "nodata": -1.0,
                   "transform": b.transform * b.transform.scale(SIZE, SIZE)}
    path = out / f"{city}_{tag}_context.tif"
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(grid, 1)
        dst.update_tags(description="P(man-made change) per 2.56 km window, train-block window filter",
                        threshold=str(params["threshold"]))
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--filter", type=Path, default=ROOT / "outputs" / "models" / "ground_ce_final" / "window_filter.json")
    parser.add_argument("--city", nargs="+", default=["mumbai", "delhi"])
    args = parser.parse_args(argv)
    params = load_filter(args.filter)
    for city in args.city:
        path = build(city, args.tag, params)
        with rasterio.open(path) as src:
            g = src.read(1)
        v = g[g >= 0]
        print(city, path.name, f"windows={v.size}", f"above_threshold={(v >= params['threshold']).sum()}",
              f"median={np.median(v):.3f}")


if __name__ == "__main__":
    main()

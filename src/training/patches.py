"""Sample a compact bi-temporal patch bank from the frozen spatial splits.

Train patches come only from train-block cores (2 km away from any held-out
block). Weak-validation patches come from val blocks and are used only for
early stopping / hyperparameter ranking on weak labels; human-reviewed
windows remain the evaluation reference.

Half of each city's patches are drawn from cells containing weak built-up
change ("positive"), half uniformly from valid cells, which keeps natural
changes (vegetation, water) present as hard negatives.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.windows import Window

from src.training.generate_candidates import NODATA, SCENES, _pair_paths
from src.training.weak_labels import GAIN, LOSS, INVALID, AMBIGUOUS, TRAIN_TARGET, TRAIN_IGNORE

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "patches"
SPLITS = ROOT / "data" / "labels" / "spatial_splits.gpkg"
PATCH = 96
SEED = 20261118
PER_CITY = {"train": 1100, "val": 220}
MIN_POSITIVE_PX = 15
MIN_VALID_FRACTION = 0.8


def target_from_codes(codes: np.ndarray) -> np.ndarray:
    lut = np.full(256, TRAIN_IGNORE, np.uint8)
    for code, target in TRAIN_TARGET.items():
        lut[code] = target
    return lut[codes]


def _cell_stats(codes: np.ndarray, allowed: np.ndarray, size: int):
    rows, cols = codes.shape[0] // size, codes.shape[1] // size
    c = codes[: rows * size, : cols * size].reshape(rows, size, cols, size)
    a = allowed[: rows * size, : cols * size].reshape(rows, size, cols, size)
    inside = a.all(axis=(1, 3))
    usable = ((c != INVALID) & (c != AMBIGUOUS)).mean(axis=(1, 3))
    positive = ((c == GAIN) | (c == LOSS)).sum(axis=(1, 3))
    return inside, usable, positive


def sample(city: str, split: str, rng: np.random.Generator) -> dict[str, np.ndarray]:
    scene = next(s for s in SCENES if s["city"].lower() == city)
    before_path, after_path, _ = _pair_paths(scene)
    layer = f"{city}_train_cores" if split == "train" else f"{city}_blocks"
    zones = gpd.read_file(SPLITS, layer=layer)
    if split != "train":
        zones = zones[zones["split"] == split]
    with rasterio.open(ROOT / "outputs" / "weak_labels" / f"{city}_weak_labels.tif") as weak:
        codes = weak.read(1)
        allowed = rasterize(
            ((g, 1) for g in zones.to_crs(weak.crs).geometry),
            out_shape=codes.shape, transform=weak.transform, dtype="uint8",
        ).astype(bool)
    inside, usable, positive = _cell_stats(codes, allowed, PATCH)
    ok = inside & (usable >= MIN_VALID_FRACTION)
    pos_cells = np.argwhere(ok & (positive >= MIN_POSITIVE_PX))
    all_cells = np.argwhere(ok)
    n = PER_CITY[split]
    n_pos = min(n // 2, len(pos_cells))
    chosen = np.concatenate([
        pos_cells[rng.choice(len(pos_cells), n_pos, replace=False)] if n_pos else np.empty((0, 2), int),
        all_cells[rng.choice(len(all_cells), min(n - n_pos, len(all_cells)), replace=False)],
    ])
    chosen = np.unique(chosen, axis=0)

    x = np.empty((len(chosen), 2, 6, PATCH, PATCH), np.float16)
    y = np.empty((len(chosen), PATCH, PATCH), np.uint8)
    with rasterio.open(before_path) as before, rasterio.open(after_path) as after:
        for i, (r, c) in enumerate(chosen):
            window = Window(int(c) * PATCH, int(r) * PATCH, PATCH, PATCH)
            bef = before.read(window=window)
            aft = after.read(window=window)
            invalid = np.any(bef == NODATA, 0) | np.any(aft == NODATA, 0)
            bef[:, invalid] = 0
            aft[:, invalid] = 0
            x[i, 0], x[i, 1] = bef, aft
            cell = codes[r * PATCH:(r + 1) * PATCH, c * PATCH:(c + 1) * PATCH]
            target = target_from_codes(cell)
            target[invalid] = TRAIN_IGNORE
            y[i] = target
    origins = chosen * PATCH
    return {"x": x, "y": y, "origins": origins,
            "city": np.array([city] * len(chosen))}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {"patch": PATCH, "seed": args.seed, "per_city": PER_CITY, "splits": {}}
    for split in ("train", "val"):
        rng = np.random.default_rng([args.seed, 0 if split == "train" else 1])
        parts = [sample(city, split, rng) for city in ("mumbai", "delhi")]
        bank = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
        np.savez(OUT / f"{split}.npz", **bank)
        counts = np.bincount(bank["y"].ravel(), minlength=256)
        summary["splits"][split] = {
            "patches": int(len(bank["y"])),
            "by_city": {c: int((bank["city"] == c).sum()) for c in ("mumbai", "delhi")},
            "pixels": {"no_change": int(counts[0]), "built_gain": int(counts[1]),
                       "built_loss": int(counts[2]), "ignore": int(counts[255])},
        }
        if split == "train":
            x = bank["x"].astype(np.float32)
            valid = bank["y"] != TRAIN_IGNORE
            stats = []
            for band in range(6):
                values = x[:, :, band][np.repeat(valid[:, None], 2, axis=1)]
                stats.append([float(values.mean()), float(values.std() + 1e-6)])
            summary["band_mean_std_train"] = stats
            del x
        print(split, json.dumps(summary["splits"][split]))
    (OUT / "patch_bank.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

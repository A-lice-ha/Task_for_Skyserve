"""Freeze whole-block spatial splits with a 2 km training buffer.

Every 10 km macro-block is assigned to train, val, test, zero_shot or
excluded. Training patches may only come from each train block's *core*: the
block minus a 2 km buffer around every non-train block, so no training pixel
lies within 2 km of a validation, test or zero-shot block.

Outputs:
  data/manifests/spatial_splits.json   block -> split manifest (seeded)
  data/labels/spatial_splits.gpkg      layers {city}_blocks, {city}_train_cores
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parents[2]
GRIDS = ROOT / "data" / "labels" / "metro_label_grids.gpkg"
MANIFEST = ROOT / "data" / "manifests" / "spatial_splits.json"
GPKG = ROOT / "data" / "labels" / "spatial_splits.gpkg"
SEED = 20261118
FRACTIONS = {"train": 0.50, "val": 0.25, "test": 0.25}
BUFFER_M = 2000.0
MIN_BLOCK_KM2 = 25.0
# Zero-shot geographic holdouts (lon, lat), verified inside the footprints.
ZERO_SHOT = {
    "mumbai": ("navi_mumbai_panvel", (73.08, 19.02)),
    "delhi": ("noida", (77.36, 28.55)),
}


def assign(city: str, blocks: gpd.GeoDataFrame, seed: int = SEED) -> gpd.GeoDataFrame:
    blocks = blocks.copy()
    blocks["split"] = "excluded"
    blocks["zone_role"] = "too_small_or_edge"
    name, (lon, lat) = ZERO_SHOT[city]
    point = gpd.GeoSeries([Point(lon, lat)], crs=4326).to_crs(blocks.crs).iloc[0]
    zero = blocks.geometry.contains(point)
    if zero.sum() != 1:
        raise ValueError(f"Zero-shot point {name} is not inside exactly one {city} block")
    blocks.loc[zero, ["split", "zone_role"]] = ["zero_shot", name]

    eligible = blocks.index[(~zero) & (blocks["area_km2"] >= MIN_BLOCK_KM2)].tolist()
    rng = random.Random(f"{seed}-{city}")
    rng.shuffle(eligible)
    n = len(eligible)
    n_train = round(n * FRACTIONS["train"])
    n_val = round(n * FRACTIONS["val"])
    for position, index in enumerate(eligible):
        split = "train" if position < n_train else "val" if position < n_train + n_val else "test"
        blocks.loc[index, ["split", "zone_role"]] = [split, "conventional"]
    return blocks


def train_cores(blocks: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    held = blocks[blocks["split"].isin(["val", "test", "zero_shot"])].union_all()
    exclusion = held.buffer(BUFFER_M)
    train = blocks[blocks["split"] == "train"].copy()
    train["geometry"] = train.geometry.difference(exclusion)
    train = train[~train.geometry.is_empty]
    train["core_km2"] = (train.geometry.area / 1e6).round(2)
    return train[["block_id", "split", "core_km2", "geometry"]]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    manifest = {
        "seed": args.seed,
        "fractions_of_eligible_blocks": FRACTIONS,
        "train_buffer_m": BUFFER_M,
        "min_block_km2": MIN_BLOCK_KM2,
        "zero_shot": {city: name for city, (name, _) in ZERO_SHOT.items()},
        "cities": {},
    }
    if GPKG.exists():
        GPKG.unlink()
    for city in ("mumbai", "delhi"):
        blocks = assign(city, gpd.read_file(GRIDS, layer=f"{city}_blocks"), args.seed)
        cores = train_cores(blocks)
        blocks.to_file(GPKG, layer=f"{city}_blocks", driver="GPKG")
        cores.to_file(GPKG, layer=f"{city}_train_cores", driver="GPKG")
        manifest["cities"][city] = {
            "blocks": dict(zip(blocks["block_id"], blocks["split"])),
            "counts": blocks["split"].value_counts().to_dict(),
            "train_core_km2": round(float(cores["core_km2"].sum()), 1),
        }
        print(city, manifest["cities"][city]["counts"],
              "train core km2:", manifest["cities"][city]["train_core_km2"])
    MANIFEST.write_text(json.dumps(manifest, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

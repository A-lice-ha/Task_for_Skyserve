"""Create QGIS-ready macro-block and polygon-label layers for both city pairs."""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import pandas as pd
import rasterio
from rasterio.windows import Window
from shapely.geometry import box


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "data" / "labels" / "metro_label_grids.gpkg"
GRID_SIZE_M = 10_000
SCENES = (
    {
        "city": "Mumbai",
        "tile": "T43QBB",
        "pair_id": "MUM_T43QBB_20221223_20230512",
        "dates": "2022-12-23_to_2023-05-12",
        "before": "S2A_MSIL2A_20221223T054241",
        "after": "T43QBB_20221223T054241_20230512T053641_after_registered.tif",
        "mask": "T43QBB_20221223T054241_20230512T053641_common_valid.tif",
        "grid_layer": "mumbai_blocks",
        "label_layer": "mumbai_labels",
    },
    {
        "city": "Delhi",
        "tile": "T43RGM",
        "pair_id": "DEL_T43RGM_20231006_20261022",
        "dates": "2023-10-06_to_2026-10-22",
        "before": "S2A_MSIL2A_20231006T052701",
        "after": "T43RGM_20231006T052701_20261022T053241_after_registered.tif",
        "mask": "T43RGM_20231006T052701_20261022T053241_common_valid.tif",
        "grid_layer": "delhi_blocks",
        "label_layer": "delhi_labels",
    },
)
LABEL_FIELDS = {
    "label_id": "str",
    "block_id": "str",
    "change_class": "str",
    "confidence": "str",
    "annotator": "str",
    "confounder": "str",
    "notes": "str",
}


def _find_one(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise FileNotFoundError(
            f"Expected exactly one file matching {pattern!r} in {directory}, "
            f"found {len(matches)}"
        )
    return matches[0]


def _aligned_window(dataset: rasterio.io.DatasetReader, geometry) -> Window:
    min_x, min_y, max_x, max_y = geometry.bounds
    row_start, col_start = dataset.index(min_x, max_y)
    row_stop, col_stop = dataset.index(max_x, min_y)
    col_start = max(0, col_start)
    row_start = max(0, row_start)
    col_stop = min(dataset.width, col_stop)
    row_stop = min(dataset.height, row_stop)
    return Window(
        col_start,
        row_start,
        max(0, col_stop - col_start),
        max(0, row_stop - row_start),
    )


def _grid_features(scene: dict, raster_path: Path, mask_path: Path) -> gpd.GeoDataFrame:
    features = []
    with rasterio.open(raster_path) as raster, rasterio.open(mask_path) as mask:
        if raster.crs is None or raster.crs != mask.crs:
            raise ValueError(f"Image and common-valid mask CRS differ for {scene['city']}")
        if (
            raster.width != mask.width
            or raster.height != mask.height
            or raster.transform != mask.transform
        ):
            raise ValueError(f"Image and common-valid mask grids differ for {scene['city']}")
        if raster.crs.to_epsg() != 32643:
            raise ValueError(
                f"Expected EPSG:32643 for {scene['city']} pair, found {raster.crs}"
            )
        min_x, min_y, max_x, max_y = raster.bounds
        x_start = (int(min_x) // GRID_SIZE_M) * GRID_SIZE_M
        y_start = (int(min_y) // GRID_SIZE_M) * GRID_SIZE_M
        x_stop = ((int(max_x) + GRID_SIZE_M - 1) // GRID_SIZE_M) * GRID_SIZE_M
        y_stop = ((int(max_y) + GRID_SIZE_M - 1) // GRID_SIZE_M) * GRID_SIZE_M
        tile_extent = box(min_x, min_y, max_x, max_y)

        for y in range(y_start, y_stop, GRID_SIZE_M):
            for x in range(x_start, x_stop, GRID_SIZE_M):
                full_cell = box(x, y, x + GRID_SIZE_M, y + GRID_SIZE_M)
                clipped_cell = full_cell.intersection(tile_extent)
                if clipped_cell.is_empty:
                    continue
                window = _aligned_window(mask, clipped_cell)
                if window.width <= 0 or window.height <= 0:
                    valid_percent = 0.0
                else:
                    values = mask.read(1, window=window)
                    valid_percent = float((values > 0).mean() * 100.0)
                block_id = (
                    f"{scene['tile']}_E{x // 1000:04d}_N{y // 1000:04d}"
                )
                features.append(
                    {
                        "block_id": block_id,
                        "city": scene["city"],
                        "tile": scene["tile"],
                        "pair_id": scene["pair_id"],
                        "pair_dates": scene["dates"],
                        "split": "unassigned",
                        "zone_role": "unassigned",
                        "area_km2": clipped_cell.area / 1_000_000,
                        "common_pct": valid_percent,
                        "notes": "",
                        "geometry": clipped_cell,
                    }
                )
    return gpd.GeoDataFrame(features, geometry="geometry", crs="EPSG:32643")


def _empty_label_layer(scene: dict) -> gpd.GeoDataFrame:
    columns = {
        **LABEL_FIELDS,
        "city": "str",
        "tile": "str",
        "pair_id": "str",
    }
    return gpd.GeoDataFrame(
        {name: pd.Series(dtype="object") for name in columns},
        geometry=gpd.GeoSeries([], crs="EPSG:32643"),
        crs="EPSG:32643",
    )


def create_grids(output_path: Path = DEFAULT_OUTPUT) -> dict[str, int]:
    """Write block grids and empty annotation layers to a GeoPackage."""
    processed = ROOT / "outputs" / "processed"
    registered = ROOT / "outputs" / "registered"
    prepared_scenes = {
        scene["city"]: _find_one(
            processed, f"{scene['before']}*.reflectance.tif"
        )
        for scene in SCENES
    }
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing GeoPackage: {output_path}"
        )

    block_counts = {}
    try:
        for index, scene in enumerate(SCENES):
            blocks = _grid_features(
                scene,
                prepared_scenes[scene["city"]],
                registered / scene["mask"],
            )
            blocks.to_file(
                output_path,
                layer=scene["grid_layer"],
                driver="GPKG",
                engine="pyogrio",
                **({"append": True} if index else {}),
            )
            _empty_label_layer(scene).to_file(
                output_path,
                layer=scene["label_layer"],
                driver="GPKG",
                engine="pyogrio",
                geometry_type="Polygon",
                append=True,
            )
            block_counts[scene["city"]] = len(blocks)
    except Exception:
        if output_path.exists():
            output_path.unlink()
        raise
    return block_counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    counts = create_grids(args.output)
    print(f"Created {args.output.resolve()}")
    for city, count in counts.items():
        print(f"{city}: {count} 10 km grid blocks")


if __name__ == "__main__":
    main()

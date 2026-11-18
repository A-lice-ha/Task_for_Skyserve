"""Sliding-window inference over a registered pair -> georeferenced change maps.

Writes, per city, into outputs/maps/:
  <city>_<model>_change_class.tif  uint8: 0 none, 1 built-up gain, 2 built-up loss, 255 invalid
  <city>_<model>_change_prob.tif   uint8: P(change) x 250 (255 invalid)

Works with a PyTorch run directory or an exported ONNX graph (FP32 or INT8),
so the ground and onboard deployments use the same tiling. ``--region`` limits
the run to a pixel window for a quick, reproducible demonstration.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

from src.evaluation.windows import change_map, torch_predictor
from src.training.generate_candidates import NODATA, SCENES, _pair_paths

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "maps"
TILE = 256
MARGIN = 16  # discard predictions within 16 px of each tile edge


def make_predictor(model: Path, threads: int):
    if model.suffix == ".onnx":
        from src.quantization.quantize import onnx_predictor
        run = json.loads((model.parent / "run.json").read_text(encoding="utf-8"))
        n_bands = 6 if run["config"]["model"] == "ground" else 4
        return onnx_predictor(model, n_bands, threads), run
    return torch_predictor(model, threads)


def run_city(city: str, model: Path, out: Path, region: tuple[int, int, int, int] | None,
             threads: int = 4, tag: str | None = None) -> dict:
    scene = next(s for s in SCENES if s["city"].lower() == city)
    before_path, after_path, mask_path = _pair_paths(scene)
    predictor, run = make_predictor(model, threads)
    tag = tag or (model.parent.name + "_" + model.stem if model.suffix == ".onnx" else model.name)
    out.mkdir(parents=True, exist_ok=True)
    cls_path = out / f"{city}_{tag}_change_class.tif"
    prob_path = out / f"{city}_{tag}_change_prob.tif"
    step = TILE - 2 * MARGIN
    started = time.perf_counter()
    tiles = 0
    with rasterio.open(before_path) as b, rasterio.open(after_path) as a, rasterio.open(mask_path) as m:
        r0, c0, h, w = region or (0, 0, m.height, m.width)
        profile = m.profile.copy()
        profile.update(dtype="uint8", count=1, nodata=255, compress="deflate", tiled=True,
                       blockxsize=256, blockysize=256, width=w, height=h,
                       transform=m.window_transform(Window(c0, r0, w, h)))
        with rasterio.open(cls_path, "w", **profile) as cls_out, \
                rasterio.open(prob_path, "w", **profile) as prob_out:
            for row in range(r0, r0 + h, step):
                for col in range(c0, c0 + w, step):
                    # Read a full tile with margin context, clamped to the raster.
                    tr = min(max(row - MARGIN, 0), m.height - TILE)
                    tc = min(max(col - MARGIN, 0), m.width - TILE)
                    win = Window(tc, tr, TILE, TILE)
                    valid = m.read(1, window=win) == 1
                    core_h = min(step, r0 + h - row)
                    core_w = min(step, c0 + w - col)
                    rr, cc = row - tr, col - tc
                    if not valid[rr:rr + core_h, cc:cc + core_w].any():
                        blank = np.full((core_h, core_w), 255, np.uint8)
                        cls_out.write(blank, 1, window=Window(col - c0, row - r0, core_w, core_h))
                        prob_out.write(blank, 1, window=Window(col - c0, row - r0, core_w, core_h))
                        continue
                    bef, aft = b.read(window=win), a.read(window=win)
                    valid &= np.all(bef != NODATA, 0) & np.all(aft != NODATA, 0)
                    bef[:, ~valid] = 0
                    aft[:, ~valid] = 0
                    probs = predictor(bef[None].astype(np.float32), aft[None].astype(np.float32))
                    classes = change_map(probs, valid[None])[0]
                    p_change = np.round((1 - probs[0, 0]) * 250).astype(np.uint8)
                    classes[~valid] = 255
                    p_change[~valid] = 255
                    cls_out.write(classes[rr:rr + core_h, cc:cc + core_w], 1,
                                  window=Window(col - c0, row - r0, core_w, core_h))
                    prob_out.write(p_change[rr:rr + core_h, cc:cc + core_w], 1,
                                   window=Window(col - c0, row - r0, core_w, core_h))
                    tiles += 1
    summary = {"city": city, "model": str(model.relative_to(ROOT)) if model.is_relative_to(ROOT) else str(model),
               "region_row_col_h_w": [r0, c0, h, w], "tiles": tiles, "threads": threads,
               "seconds": round(time.perf_counter() - started, 1),
               "class_map": cls_path.name, "prob_map": prob_path.name}
    (out / f"{city}_{tag}_inference.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", type=Path, required=True, help="run dir or .onnx file")
    parser.add_argument("--city", nargs="+", default=["mumbai", "delhi"])
    parser.add_argument("--region", type=int, nargs=4, metavar=("ROW", "COL", "H", "W"))
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--tag")
    args = parser.parse_args(argv)
    for city in args.city:
        print(json.dumps(run_city(city, args.model, args.out, tuple(args.region) if args.region else None,
                                  args.threads, args.tag)))


if __name__ == "__main__":
    main()

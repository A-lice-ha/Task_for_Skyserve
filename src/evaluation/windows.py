"""Load candidate review windows and run predictors on them.

All evaluation (FP32, INT8 and the rule baseline) runs on the same cached
256x256 windows, so comparisons share identical inputs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
import rasterio
import torch
from rasterio.windows import Window

from src.training.generate_candidates import NODATA, SCENES, _pair_paths
from src.training.train import load_model, normalise

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "outputs" / "review" / "eval_windows.npz"
SIZE = 256
MIN_COMPONENT_PX = 4

Predictor = Callable[[np.ndarray, np.ndarray], np.ndarray]


def origin(feature_id: str) -> tuple[int, int]:
    row, col = map(int, re.search(r"R(\d+)_C(\d+)", feature_id).groups())
    return row, col


def build_cache(feature_ids_by_city: dict[str, list[str]], path: Path = CACHE) -> None:
    ids, xs, valids, weaks = [], [], [], []
    for scene in SCENES:
        city = scene["city"].lower()
        wanted = feature_ids_by_city.get(city, [])
        if not wanted:
            continue
        before_path, after_path, mask_path = _pair_paths(scene)
        with rasterio.open(before_path) as b, rasterio.open(after_path) as a, \
                rasterio.open(mask_path) as m, \
                rasterio.open(ROOT / "outputs" / "weak_labels" / f"{city}_weak_labels.tif") as w:
            for fid in wanted:
                row, col = origin(fid)
                win = Window(col, row, SIZE, SIZE)
                bef, aft = b.read(window=win), a.read(window=win)
                valid = (m.read(1, window=win) == 1) & np.all(bef != NODATA, 0) & np.all(aft != NODATA, 0)
                bef[:, ~valid] = 0
                aft[:, ~valid] = 0
                ids.append(fid)
                xs.append(np.stack([bef, aft]).astype(np.float16))
                valids.append(valid)
                weaks.append(w.read(1, window=win))
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, ids=np.array(ids), x=np.stack(xs), valid=np.stack(valids), weak=np.stack(weaks))


def load_cache(path: Path = CACHE) -> dict[str, np.ndarray]:
    data = np.load(path)
    return {k: data[k] for k in data.files}


def torch_predictor(run_dir: Path, threads: int = 6) -> tuple[Predictor, dict]:
    """Return predictor(before, after) -> change probabilities (N, C, H, W)."""
    torch.set_num_threads(threads)
    model, run = load_model(run_dir)
    n_bands = 6 if run["config"]["model"] == "ground" else 4
    stats = run["band_mean_std_train"]

    @torch.no_grad()
    def predict(before: np.ndarray, after: np.ndarray) -> np.ndarray:
        b = normalise(torch.from_numpy(before[:, :n_bands].astype(np.float32)), stats, n_bands)
        a = normalise(torch.from_numpy(after[:, :n_bands].astype(np.float32)), stats, n_bands)
        return model(b, a).softmax(1).numpy()

    return predict, run


def predict_windows(predictor: Predictor, x: np.ndarray, batch: int = 4) -> np.ndarray:
    probs = []
    for i in range(0, len(x), batch):
        chunk = x[i:i + batch]
        probs.append(predictor(chunk[:, 0], chunk[:, 1]).astype(np.float16))
    return np.concatenate(probs)


def change_map(probs: np.ndarray, valid: np.ndarray, threshold: float = 0.5) -> np.ndarray:
    """Class map from probabilities: 0 none, 1 gain, 2 loss (binary models: 1 change)."""
    p = probs.astype(np.float32)
    classes = p.argmax(1).astype(np.uint8)
    classes[(1 - p[:, 0]) < threshold] = 0
    classes[~valid] = 0
    for i in range(len(classes)):
        for code in np.unique(classes[i])[1:]:
            mask = (classes[i] == code).astype(np.uint8)
            n, comp, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
            small = np.flatnonzero(stats[:, cv2.CC_STAT_AREA] < MIN_COMPONENT_PX)
            small = small[small != 0]
            if small.size:
                classes[i][np.isin(comp, small)] = 0
    return classes


def rule_map(weak: np.ndarray) -> np.ndarray:
    """Rule baseline: the weak-label generator's built-up gain/loss pixels."""
    out = np.zeros(weak.shape, np.uint8)
    out[weak == 1] = 1
    out[weak == 2] = 2
    return out


def load_reviews(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["labels"] if isinstance(data, dict) else data

def small_change_retention(fp: np.ndarray, q: np.ndarray, max_px: int = 10) -> dict:
    """Share of small FP32 change components (<= max_px) still detected by INT8."""
    total = kept = 0
    for a, b in zip(fp, q):
        n, comp, stats, _ = cv2.connectedComponentsWithStats((a > 0).astype(np.uint8), connectivity=8)
        for label in range(1, n):
            if stats[label, cv2.CC_STAT_AREA] <= max_px:
                total += 1
                kept += bool((b[comp == label] > 0).any())
    return {"fp32_small_components": total, "retained_by_int8": kept,
            "retention": round(kept / total, 4) if total else None}

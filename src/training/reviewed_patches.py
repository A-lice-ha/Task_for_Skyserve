"""Human-corrected training patches from reviewed windows in TRAIN blocks only.

The blind review showed that the weak-label rules mark large agricultural and
haze-affected areas as built-up change. Reviewed windows that lie in train
blocks may be used for training without touching validation, test or
zero-shot labels:

- reviewed natural_vegetation / natural_water / acquisition_artifact /
  no_change -> every valid pixel becomes "no change" (hard negatives);
- reviewed construction / demolition / road / bridge / infrastructure_other
  -> the weak gain/loss outline is kept (other pixels as in the weak labels);
- uncertain windows are skipped.

Each 256 px window gives nine 96 px crops (stride 80). Output:
outputs/patches/reviewed_train.npz and reviewed_train.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from src.evaluation.evaluate import NEGATIVE, POSITIVE, attach_splits
from src.evaluation.windows import load_cache, load_reviews
from src.training.weak_labels import TRAIN_IGNORE, TRAIN_TARGET

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "patches"
PATCH = 96
OFFSETS = (0, 80, 160)


def weak_to_target(weak: np.ndarray) -> np.ndarray:
    y = np.full(weak.shape, TRAIN_IGNORE, np.uint8)
    for code, target in TRAIN_TARGET.items():
        y[weak == code] = target
    return y


def corrected_target(weak: np.ndarray, valid: np.ndarray, human_class: str) -> np.ndarray:
    if human_class in NEGATIVE:
        y = np.zeros(weak.shape, np.uint8)
    else:
        y = weak_to_target(weak)
    y[~valid] = TRAIN_IGNORE
    return y


def build(reviews_path: Path = ROOT / "data" / "labels" / "reviews" / "review_labels.json") -> dict:
    reviews = attach_splits(load_reviews(reviews_path))
    cache = load_cache()
    index = {fid: i for i, fid in enumerate(cache["ids"])}
    xs, ys, used = [], [], {}
    for r in reviews:
        cls = r["change_class"]
        if r["split"] != "train" or cls not in POSITIVE | NEGATIVE:
            continue
        i = index[r["feature_id"]]
        y = corrected_target(cache["weak"][i], cache["valid"][i], cls)
        for oy in OFFSETS:
            for ox in OFFSETS:
                ycrop = y[oy:oy + PATCH, ox:ox + PATCH]
                if (ycrop != TRAIN_IGNORE).mean() < 0.5:
                    continue
                xs.append(cache["x"][i][:, :, oy:oy + PATCH, ox:ox + PATCH])
                ys.append(ycrop)
        used[cls] = used.get(cls, 0) + 1
    x, y = np.stack(xs).astype(np.float16), np.stack(ys)
    OUT.mkdir(parents=True, exist_ok=True)
    np.savez(OUT / "reviewed_train.npz", x=x, y=y)
    summary = {"windows_by_class": used, "patches": int(len(x)),
               "pixel_targets": {str(k): int(v) for k, v in zip(*np.unique(y, return_counts=True))},
               "source": "human reviews of windows in train blocks only"}
    (OUT / "reviewed_train.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


if __name__ == "__main__":
    print(json.dumps(build(), indent=1))

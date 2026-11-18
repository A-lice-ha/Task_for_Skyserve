"""Second-stage window filter learned from human reviews in TRAIN blocks only.

The per-pixel models flag nearly every reviewed window, because agricultural,
water and haze changes produce more "built-up" pixels than real construction
(D-013, D-014). This stage scores a window from features of the model's change
map and the spectral context of the changed pixels, using a small logistic
regression with fixed settings (no tuning):

- log change pixels, log largest component, component count, share of change
  in components <= 50 px (compactness);
- mean T1 NDVI, mean dNDVI, mean dNDBI and mean dMNDWI over changed pixels;
- regional blue increase (haze proxy) over the window.

Fit on decided reviewed windows in train blocks; decision threshold chosen on
the same train windows (max F1); evaluated once on validation + test +
zero-shot windows. Writes outputs/evaluation/window_filter.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from src.evaluation.evaluate import (NATURAL, NEGATIVE, OUT, POSITIVE, REVIEWS, attach_splits,
                                     bootstrap_f1, prf, ranking)
from src.evaluation.windows import (change_map, load_cache, load_reviews, predict_windows,
                                    torch_predictor)

FEATURES = ["log_change_px", "log_largest_px", "components", "compact_share",
            "t1_ndvi", "d_ndvi", "d_ndbi", "d_mndwi", "blue_increase"]
EPS = 1e-6


def _nd(a, b):
    return (a - b) / (a + b + EPS)


def window_features(x: np.ndarray, valid: np.ndarray, cmap: np.ndarray) -> np.ndarray:
    """x: (2, 6, H, W) reflectance; cmap: (H, W) 0/1/2 change classes."""
    b, a = x[0].astype(np.float32), x[1].astype(np.float32)
    ndvi_b, ndvi_a = _nd(b[3], b[2]), _nd(a[3], a[2])
    ndbi_b, ndbi_a = _nd(b[4], b[3]), _nd(a[4], a[3])
    mndwi_b, mndwi_a = _nd(b[1], b[4]), _nd(a[1], a[4])
    change = cmap > 0
    n, _, stats, _ = cv2.connectedComponentsWithStats(change.astype(np.uint8), connectivity=8)
    areas = stats[1:, cv2.CC_STAT_AREA] if n > 1 else np.array([0])
    total = int(change.sum())
    m = change if total else valid

    def mean(v):
        return float(v[m].mean()) if m.any() else 0.0

    return np.array([
        np.log1p(total), np.log1p(areas.max()), float(n - 1),
        float(areas[areas <= 50].sum() / total) if total else 0.0,
        mean(ndvi_b), mean(ndvi_a - ndvi_b), mean(ndbi_a - ndbi_b), mean(mndwi_a - mndwi_b),
        float((a[0] - b[0])[valid].mean()) if valid.any() else 0.0,
    ], np.float32)


def make_filter():
    """Fixed settings, never tuned: standardised features, C=0.1, balanced weights."""
    return make_pipeline(StandardScaler(), LogisticRegression(C=0.1, class_weight="balanced",
                                                              max_iter=1000))


def auroc_ci(score: np.ndarray, truth: np.ndarray, n: int = 1000, seed: int = 20261118) -> list[float]:
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        idx = rng.integers(0, len(truth), len(truth))
        if truth[idx].any() and (~truth[idx]).any():
            vals.append(ranking(score[idx], truth[idx])["auroc"])
    return [round(float(np.percentile(vals, 2.5)), 4), round(float(np.percentile(vals, 97.5)), 4)]


def best_threshold(score: np.ndarray, truth: np.ndarray) -> float:
    best_t, best_f1 = 0.5, -1.0
    for t in np.unique(score):
        f1 = prf(score >= t, truth)["f1"]
        if f1 > best_f1:
            best_t, best_f1 = float(t), f1
    return best_t


def run(run_dir: Path, reviews_path: Path = REVIEWS) -> dict:
    reviews = attach_splits(load_reviews(reviews_path))
    cache = load_cache()
    index = {fid: i for i, fid in enumerate(cache["ids"])}
    order = np.array([index[r["feature_id"]] for r in reviews])
    x, valid = cache["x"][order], cache["valid"][order]
    predictor, _ = torch_predictor(run_dir)
    maps = change_map(predict_windows(predictor, x), valid)
    feats = np.stack([window_features(x[i], valid[i], maps[i]) for i in range(len(reviews))])

    cls = np.array([r["change_class"] for r in reviews])
    split = np.array([r["split"] for r in reviews])
    decided = np.isin(cls, list(POSITIVE | NEGATIVE))
    truth = np.isin(cls, list(POSITIVE))
    fit = decided & (split == "train")
    held = decided & np.isin(split, ["val", "test", "zero_shot"])

    model = make_filter()
    model.fit(feats[fit], truth[fit])
    score = model.predict_proba(feats)[:, 1]
    thr = best_threshold(score[fit], truth[fit])
    pred = score >= thr

    scaler, logreg = model[0], model[-1]
    params = {"features": FEATURES, "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
              "coef": logreg.coef_[0].tolist(), "intercept": float(logreg.intercept_[0]),
              "threshold": thr, "window_px": 256, "base_model": run_dir.name,
              "fit": "decided human-reviewed windows in train blocks only"}
    (run_dir / "window_filter.json").write_text(json.dumps(params, indent=1), encoding="utf-8")

    natural = held & np.isin(cls, list(NATURAL))
    test = decided & (split == "test")
    coef = model[-1].coef_[0]
    result = {
        "base_model": run_dir.name,
        "fit_windows": {"n": int(fit.sum()), "positives": int(truth[fit].sum())},
        "features": FEATURES,
        "standardised_coefficients": {f: round(float(c), 3) for f, c in zip(FEATURES, coef)},
        "threshold_from_train_windows": round(thr, 4),
        "train_fit_ranking": ranking(score[fit], truth[fit]),
        "heldout_ranking": {**ranking(score[held], truth[held]),
                            "auroc_95ci": auroc_ci(score[held], truth[held])},
        "heldout_pixelcount_ranking": {**ranking(np.expm1(feats[held, 0]), truth[held]),
                                       "auroc_95ci": auroc_ci(np.expm1(feats[held, 0]), truth[held])},
        "heldout": {**prf(pred[held], truth[held]), "f1_95ci": bootstrap_f1(pred[held], truth[held])},
        "test": {**prf(pred[test], truth[test]), "f1_95ci": bootstrap_f1(pred[test], truth[test])},
        "heldout_natural_false_alarm_rate": round(float(pred[natural].mean()), 4),
        "per_window": [{"feature_id": r["feature_id"], "split": r["split"], "human_class": r["change_class"],
                        "score": round(float(s), 4), "flagged": bool(p)}
                       for r, s, p in zip(reviews, score, pred)],
    }
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, default=Path("outputs/models/ground_ce_final"))
    parser.add_argument("--reviews", type=Path, default=REVIEWS)
    args = parser.parse_args(argv)
    result = run(args.run, args.reviews)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "window_filter.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "per_window"}, indent=1))


if __name__ == "__main__":
    main()

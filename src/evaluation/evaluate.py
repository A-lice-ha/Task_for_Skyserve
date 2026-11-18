"""Evaluate change detectors against human-reviewed windows.

Reference: data/labels/reviews/review_labels.json exported from the blind
review page (outputs/review/index.html). The automatic weak labels are never
used as a reference, except as an explicitly labelled localisation proxy on
windows whose outline the reviewer marked "correct".

Window decision: a detector flags a window as containing man-made change when
its change pixels (after removing components < 4 px) reach k. k is selected on
validation windows only and then frozen for test and zero-shot windows.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

from src.evaluation.windows import (CACHE, build_cache, change_map, load_cache, load_reviews,
                                    predict_windows, rule_map, torch_predictor)

ROOT = Path(__file__).resolve().parents[2]
REVIEWS = ROOT / "data" / "labels" / "reviews" / "review_labels.json"
SPLITS = ROOT / "data" / "manifests" / "spatial_splits.json"
OUT = ROOT / "outputs" / "evaluation"
POSITIVE = {"construction", "demolition", "road", "bridge", "infrastructure_other"}
NEGATIVE = {"no_change", "natural_vegetation", "natural_water", "acquisition_artifact"}
NATURAL = {"natural_vegetation", "natural_water", "acquisition_artifact"}
K_GRID = [1, 4, 8, 16, 32, 64, 128, 256, 512, 1024]
SEED = 20261118


def attach_splits(reviews: list[dict]) -> list[dict]:
    import geopandas as gpd
    manifest = json.loads(SPLITS.read_text(encoding="utf-8"))["cities"]
    blocks = {}
    for city in ("mumbai", "delhi"):
        cands = gpd.read_file(ROOT / "data" / "labels" / "candidates" / f"{city}_candidates.geojson")
        blocks.update(dict(zip(cands["feature_id"], cands["spatial_block_id"])))
    for r in reviews:
        r["split"] = manifest[r["city"].lower()]["blocks"].get(blocks[r["feature_id"]], "excluded")
    return reviews


def prf(pred: np.ndarray, truth: np.ndarray) -> dict:
    tp = int((pred & truth).sum()); fp = int((pred & ~truth).sum()); fn = int((~pred & truth).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"n": int(len(truth)), "positives": int(truth.sum()), "tp": tp, "fp": fp, "fn": fn,
            "precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4)}


def bootstrap_f1(pred: np.ndarray, truth: np.ndarray, n: int = 1000) -> list[float]:
    if len(truth) < 5:
        return [float("nan"), float("nan")]
    rng = np.random.default_rng(SEED)
    scores = []
    for _ in range(n):
        idx = rng.integers(0, len(truth), len(truth))
        scores.append(prf(pred[idx], truth[idx])["f1"])
    return [round(float(np.percentile(scores, 2.5)), 4), round(float(np.percentile(scores, 97.5)), 4)]


def score_method(name: str, maps: np.ndarray, reviews: list[dict], weak: np.ndarray) -> dict:
    """maps: (N, H, W) class maps aligned with reviews."""
    gain = (maps == 1).sum((1, 2))
    loss = (maps == 2).sum((1, 2))
    change = gain + loss
    cls = np.array([r["change_class"] for r in reviews])
    split = np.array([r["split"] for r in reviews])
    city = np.array([r["city"].lower() for r in reviews])
    decided = np.isin(cls, list(POSITIVE | NEGATIVE))
    truth = np.isin(cls, list(POSITIVE))

    val = decided & (split == "val")
    best_k, best_f1 = K_GRID[-1], -1.0
    for k in K_GRID:
        f1 = prf(change[val] >= k, truth[val])["f1"] if val.any() else 0.0
        if f1 > best_f1:  # ties keep the smaller k (higher sensitivity)
            best_k, best_f1 = k, f1
    pred = change >= best_k

    result = {"method": name, "k_selected_on_val": best_k, "splits": {}}
    for s in ("val", "test", "zero_shot", "train"):
        for c in ("all", "mumbai", "delhi"):
            sel = decided & (split == s) & ((city == c) if c != "all" else True)
            if not sel.any():
                continue
            m = prf(pred[sel], truth[sel])
            if c == "all":
                m["f1_95ci"] = bootstrap_f1(pred[sel], truth[sel])
            result["splits"][f"{s}/{c}"] = m
    held = np.isin(split, ["test", "zero_shot"])
    nat = held & np.isin(cls, list(NATURAL))
    result["natural_false_alarm_rate_heldout"] = {
        "n": int(nat.sum()),
        "rate": round(float(pred[nat].mean()), 4) if nat.any() else None,
        "by_class": {c: {"n": int((held & (cls == c)).sum()),
                         "flagged": int((pred & held & (cls == c)).sum())} for c in sorted(NATURAL)},
    }
    unc = held & (cls == "uncertain")
    result["uncertain_heldout"] = {"n": int(unc.sum()), "flagged": int(pred[unc].sum())}
    # Direction: demolition -> loss, other targets -> gain.
    hit = held & pred & truth
    want_loss = cls == "demolition"
    got_loss = loss > gain
    result["direction_agreement_heldout_tp"] = {
        "n": int(hit.sum()),
        "agree": int((hit & (want_loss == got_loss)).sum()),
    }
    # Localisation proxy only where the reviewer accepted the weak outline.
    accepted = held & np.array([r.get("mask_quality") == "correct" for r in reviews]) & truth
    ious = []
    for i in np.flatnonzero(accepted):
        a = maps[i] > 0
        b = (weak[i] == 1) | (weak[i] == 2)
        union = (a | b).sum()
        if union:
            ious.append((a & b).sum() / union)
    result["pixel_iou_vs_accepted_outline_proxy"] = {
        "n_windows": len(ious), "mean_iou": round(float(np.mean(ious)), 4) if ious else None}
    result["per_window"] = [
        {"feature_id": r["feature_id"], "city": r["city"], "split": r["split"],
         "human_class": r["change_class"], "confidence": r.get("confidence"),
         "gain_px": int(g), "loss_px": int(l), "flagged": bool(p)}
        for r, g, l, p in zip(reviews, gain, loss, pred)]
    return result


def run_evaluation(reviews_path: Path, run_dirs: list[Path], refresh_cache: bool = False) -> dict:
    reviews = attach_splits(load_reviews(reviews_path))
    by_city: dict[str, list[str]] = {}
    for r in reviews:
        by_city.setdefault(r["city"].lower(), []).append(r["feature_id"])
    cache_ok = CACHE.exists() and not refresh_cache
    if cache_ok:
        cache = load_cache()
        cache_ok = set(cache["ids"]) >= {r["feature_id"] for r in reviews}
    if not cache_ok:
        build_cache(by_city)
        cache = load_cache()
    index = {fid: i for i, fid in enumerate(cache["ids"])}
    order = np.array([index[r["feature_id"]] for r in reviews])
    x, valid, weak = cache["x"][order], cache["valid"][order], cache["weak"][order]

    results = [score_method("rule_baseline_weak_labels", rule_map(weak), reviews, weak)]
    for run_dir in run_dirs:
        predictor, run = torch_predictor(run_dir)
        maps = change_map(predict_windows(predictor, x), valid)
        res = score_method(run_dir.name, maps, reviews, weak)
        res["parameters"] = run["parameters"]
        results.append(res)
    return {"reviews": len(reviews),
            "review_classes": {c: sum(r["change_class"] == c for r in reviews)
                               for c in sorted(POSITIVE | NEGATIVE | {"uncertain"})},
            "review_splits": {s: sum(r["split"] == s for r in reviews)
                              for s in ("train", "val", "test", "zero_shot", "excluded")},
            "methods": results}


def write(summary: dict, out: Path = OUT) -> None:
    out.mkdir(parents=True, exist_ok=True)
    slim = {**summary, "methods": [{k: v for k, v in m.items() if k != "per_window"}
                                   for m in summary["methods"]]}
    (out / "metrics.json").write_text(json.dumps(slim, indent=1), encoding="utf-8")
    with (out / "window_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        rows = [{"method": m["method"], **w} for m in summary["methods"] for w in m["per_window"]]
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    table = ["| Method | k | Test P | Test R | Test F1 (95% CI) | Zero-shot F1 | Natural false-alarm rate |",
             "|---|---:|---:|---:|---|---:|---:|"]
    for m in slim["methods"]:
        t = m["splits"].get("test/all", {})
        z = m["splits"].get("zero_shot/all", {})
        nat = m["natural_false_alarm_rate_heldout"]
        table.append(f"| {m['method']} | {m['k_selected_on_val']} | {t.get('precision', '–')} | "
                     f"{t.get('recall', '–')} | {t.get('f1', '–')} {t.get('f1_95ci', '')} | "
                     f"{z.get('f1', '–')} | {nat['rate']} (n={nat['n']}) |")
    (out / "metrics_table.md").write_text("\n".join(table) + "\n", encoding="utf-8")
    print("\n".join(table))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reviews", type=Path, default=REVIEWS)
    parser.add_argument("--runs", type=Path, nargs="*", default=[])
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--refresh-cache", action="store_true")
    args = parser.parse_args(argv)
    write(run_evaluation(args.reviews, args.runs, args.refresh_cache), args.out)


if __name__ == "__main__":
    main()

"""Leave-one-city-out (LOCO) evaluation: train in one city, test in the other.

Each city has only one image pair, so temporal folds are impossible; LOCO is
the closest substitute. It is a confounded stress test: city, season
(Dec->May vs Oct->Oct), interval (140 vs 1,112 days), haze and land cover all
change together.

Protocol (no target-city information is used for training or tuning):
- ground_ce_loco_train_<source>: same recipe as ground_ce_final, trained and
  normalised on the source city's patches only (configs/training/).
- The window threshold k and the window context filter (fit + threshold)
  are chosen on all decided reviewed windows of the source city.
- Metrics are computed on all decided reviewed windows of the target city.
- Reference on the same city: the in-city model ground_ce_final and the rule
  baseline, compared on the target city's held-out (val/test/zero-shot)
  windows, where ground_ce_final never trained.

Writes outputs/evaluation/loco.json and loco.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from src.evaluation.evaluate import (NATURAL, NEGATIVE, OUT, POSITIVE, REVIEWS, attach_splits,
                                     bootstrap_f1, prf, ranking, select_k)
from src.evaluation.window_filter import auroc_ci, best_threshold, make_filter, window_features
from src.evaluation.windows import (change_map, load_cache, load_reviews, predict_windows,
                                    rule_map, torch_predictor)

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "outputs" / "models"
CITIES = ("mumbai", "delhi")
HELD = ("val", "test", "zero_shot")


def _maps(run: str, x, valid) -> np.ndarray:
    predictor, _ = torch_predictor(MODELS / run)
    return change_map(predict_windows(predictor, x), valid)


def _score(change, truth, cls, sel, k) -> dict:
    natural = sel & np.isin(cls, list(NATURAL))
    pred = change >= k
    return {**prf(pred[sel], truth[sel]), "k": int(k), "f1_95ci": bootstrap_f1(pred[sel], truth[sel]),
            "natural_false_alarm_rate": round(float(pred[natural].mean()), 4) if natural.any() else None,
            **{f"rank_{key}": v for key, v in ranking(change[sel].astype(float), truth[sel]).items()
               if key in ("average_precision", "auroc")},
            "rank_auroc_95ci": auroc_ci(change[sel].astype(float), truth[sel])}


def run(reviews_path: Path = REVIEWS) -> dict:
    reviews = attach_splits(load_reviews(reviews_path))
    cache = load_cache()
    index = {fid: i for i, fid in enumerate(cache["ids"])}
    order = np.array([index[r["feature_id"]] for r in reviews])
    x, valid, weak = cache["x"][order], cache["valid"][order], cache["weak"][order]
    cls = np.array([r["change_class"] for r in reviews])
    city = np.array([r["city"].lower() for r in reviews])
    split = np.array([r["split"] for r in reviews])
    decided = np.isin(cls, list(POSITIVE | NEGATIVE))
    truth = np.isin(cls, list(POSITIVE))

    rules = rule_map(weak)
    in_city = _maps("ground_ce_final", x, valid)
    in_city_k = json.loads((OUT / "metrics.json").read_text(encoding="utf-8"))
    in_city_k = next(m["k_selected_on_val"] for m in in_city_k["methods"] if m["method"] == "ground_ce_final")

    result = {"protocol": __doc__.split("Protocol")[1].split("Writes")[0].strip(),
              "windows_per_city": {c: {"decided": int((decided & (city == c)).sum()),
                                       "positives": int((truth & (city == c)).sum())} for c in CITIES},
              "directions": {}}
    for source, target in (("mumbai", "delhi"), ("delhi", "mumbai")):
        run_name = f"ground_ce_loco_train_{source}"
        loco = _maps(run_name, x, valid)
        src = decided & (city == source)
        tgt = decided & (city == target)
        tgt_held = tgt & np.isin(split, HELD)
        change = {"loco": (loco > 0).sum((1, 2)), "rules": (rules > 0).sum((1, 2)),
                  "in_city": (in_city > 0).sum((1, 2))}
        k = {"loco": select_k(change["loco"][src], truth[src]),
             "rules": select_k(change["rules"][src], truth[src])}

        feats = np.stack([window_features(x[i], valid[i], loco[i]) for i in range(len(reviews))])
        filt = make_filter().fit(feats[src], truth[src])
        fscore = filt.predict_proba(feats)[:, 1]
        thr = best_threshold(fscore[src], truth[src])
        fpred = fscore >= thr
        natural = tgt & np.isin(cls, list(NATURAL))

        run_info = json.loads((MODELS / run_name / "run.json").read_text(encoding="utf-8"))
        result["directions"][f"{source}_to_{target}"] = {
            "model": run_name,
            "train_patches_source_only": True,
            "weakval_source_f1": round(run_info["best_weakval"]["f1"], 4),
            "train_seconds": run_info["train_seconds"],
            "source_windows": {"n": int(src.sum()), "positives": int(truth[src].sum())},
            "target_all": {
                "loco_model": _score(change["loco"], truth, cls, tgt, k["loco"]),
                "rule_baseline": _score(change["rules"], truth, cls, tgt, k["rules"]),
                "loco_window_filter": {
                    **prf(fpred[tgt], truth[tgt]), "threshold_from_source": round(thr, 4),
                    "f1_95ci": bootstrap_f1(fpred[tgt], truth[tgt]),
                    "natural_false_alarm_rate": round(float(fpred[natural].mean()), 4) if natural.any() else None,
                    **{f"rank_{key}": v for key, v in ranking(fscore[tgt], truth[tgt]).items()
                       if key in ("n", "positives", "average_precision", "auroc")},
                    "rank_auroc_95ci": auroc_ci(fscore[tgt], truth[tgt]),
                },
            },
            "target_heldout_same_windows": {
                "loco_model": _score(change["loco"], truth, cls, tgt_held, k["loco"]),
                "rule_baseline": _score(change["rules"], truth, cls, tgt_held, k["rules"]),
                "in_city_ground_ce_final": _score(change["in_city"], truth, cls, tgt_held, in_city_k),
            },
        }
    return result


def write(result: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "loco.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    lines = ["| Direction | Windows | Method | k | P | R | F1 (95% CI) | Natural false-alarm rate | AP | AUROC (95% CI) |",
             "|---|---|---|---:|---:|---:|---|---:|---:|---|"]
    for name, d in result["directions"].items():
        for scope, label in (("target_all", "all target"), ("target_heldout_same_windows", "target held-out")):
            for method, m in d[scope].items():
                lines.append(
                    f"| {name} | {label} (n={m['n']}, pos={m['positives']}) | {method} | "
                    f"{m.get('k', m.get('threshold_from_source', ''))} | {m['precision']} | {m['recall']} | "
                    f"{m['f1']} {m['f1_95ci']} | {m['natural_false_alarm_rate']} | "
                    f"{m.get('rank_average_precision')} | {m.get('rank_auroc')} {m.get('rank_auroc_95ci')} |")
    (OUT / "loco.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    write(run())

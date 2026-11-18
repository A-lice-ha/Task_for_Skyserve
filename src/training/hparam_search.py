"""Reproducible grid search (fixed seed, fixed patch bank) for both models.

Stage 1 (ground): loss in {ce, wce, wce_dice} x lr in {1e-3, 3e-3}.
Stage 2 (ground): best stage-1 config without radiometric jitter (ablation).
Stage 3 (onboard): wce_dice at lr in {1e-3, 3e-3}.
Ranking uses weak-validation change F1 only; final selection between the
top configurations is confirmed on human-reviewed validation windows.
Results: outputs/experiments/hparam_search.csv
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from src.training.train import train

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "experiments"
EPOCHS = 6
FIELDS = ["name", "model", "loss", "lr", "radiometric_jitter", "epochs", "parameters",
          "best_epoch", "weakval_precision", "weakval_recall", "weakval_f1",
          "weakval_iou", "train_seconds"]


def _row(result: dict) -> dict:
    cfg, best = result["config"], result["best_weakval"]
    return {"name": cfg["name"], "model": cfg["model"], "loss": cfg["loss"], "lr": cfg["lr"],
            "radiometric_jitter": cfg["radiometric_jitter"], "epochs": cfg["epochs"],
            "parameters": result["parameters"], "best_epoch": best["epoch"],
            **{f"weakval_{k}": round(best[k], 4) for k in ("precision", "recall", "f1", "iou")},
            "train_seconds": result["train_seconds"]}


def run(configs: list[dict], rows: list[dict], path: Path) -> None:
    done = {r["name"] for r in rows}
    for cfg in configs:
        if cfg["name"] in done:
            continue
        rows.append(_row(train({**cfg, "epochs": EPOCHS})))
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "hparam_search.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []
    for r in rows:
        r["weakval_f1"] = float(r["weakval_f1"])
    stage1 = [{"name": f"ground_{loss}_lr{lr:g}", "model": "ground", "loss": loss, "lr": lr}
              for loss in ("ce", "wce", "wce_dice") for lr in (1e-3, 3e-3)]
    run(stage1, rows, path)
    best = max((r for r in rows if r["name"] in {c["name"] for c in stage1}),
               key=lambda r: float(r["weakval_f1"]))
    run([{"name": f"{best['name']}_nojitter", "model": "ground", "loss": best["loss"],
          "lr": float(best["lr"]), "radiometric_jitter": 0.0}], rows, path)
    run([{"name": f"onboard_wce_dice_lr{lr:g}", "model": "onboard", "loss": "wce_dice", "lr": lr}
         for lr in (1e-3, 3e-3)], rows, path)
    print(json.dumps(rows, indent=1, default=str))


if __name__ == "__main__":
    main()

"""Train a ground (3-class) or onboard (binary) Siamese change model on CPU.

Targets come from the weak-label patch bank (see weak_labels.py, patches.py):
    ground : 0 no-change (incl. natural), 1 built-up gain, 2 built-up loss
    onboard: 0 no-change, 1 change (gain or loss)
255 is ignored. Model selection uses weak-validation change F1 only as a
proxy; reported performance comes from human-reviewed windows (evaluate.py).
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from src.models.siamese import SiameseMobileNetV3Small, SiameseUNet, count_parameters

ROOT = Path(__file__).resolve().parents[2]
PATCHES = ROOT / "outputs" / "patches"
MODELS = ROOT / "outputs" / "models"
IGNORE = 255

DEFAULTS = {
    "name": "ground_default",
    "model": "ground",
    "widths": [16, 32, 64, 128],
    "width_mult": 0.5,
    "epochs": 8,
    "batch_size": 16,
    "lr": 1e-3,
    "weight_decay": 1e-4,
    "loss": "wce_dice",
    "augment": True,
    "radiometric_jitter": 0.05,
    "seed": 20261118,
    "threads": 6,
}


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def build_model(cfg: dict) -> torch.nn.Module:
    if cfg["model"] == "ground":
        return SiameseUNet(in_channels=6, num_classes=3, widths=tuple(cfg["widths"]))
    if cfg["model"] == "onboard":
        return SiameseMobileNetV3Small(in_channels=4, width_mult=cfg["width_mult"])
    raise ValueError(f"Unknown model {cfg['model']!r}")


def bands_for(cfg: dict) -> int:
    return 6 if cfg["model"] == "ground" else 4


def prepare_targets(y: torch.Tensor, cfg: dict) -> torch.Tensor:
    if cfg["model"] == "onboard":
        y = y.clone()
        y[(y == 1) | (y == 2)] = 1
    return y


def normalise(x: torch.Tensor, stats: list[list[float]], n_bands: int) -> torch.Tensor:
    mean = torch.tensor([s[0] for s in stats[:n_bands]]).view(1, n_bands, 1, 1)
    std = torch.tensor([s[1] for s in stats[:n_bands]]).view(1, n_bands, 1, 1)
    return (x - mean) / std


def augment(before, after, y, jitter: float):
    k = random.randint(0, 3)
    before, after, y = (torch.rot90(t, k, dims=(-2, -1)) for t in (before, after, y))
    if random.random() < 0.5:
        before, after, y = (torch.flip(t, dims=(-1,)) for t in (before, after, y))
    if jitter > 0:
        # Independent per-date, per-band gain mimics acquisition/atmospheric
        # differences that must not be learned as change.
        n, c = before.shape[:2]
        before = before * (1 + jitter * (2 * torch.rand(n, c, 1, 1) - 1))
        after = after * (1 + jitter * (2 * torch.rand(n, c, 1, 1) - 1))
    return before, after, y


def class_weights(y: np.ndarray, num_classes: int, cfg: dict) -> torch.Tensor:
    t = prepare_targets(torch.from_numpy(y), cfg).numpy()
    counts = np.bincount(t[t != IGNORE].ravel(), minlength=num_classes).astype(float)
    weights = 1.0 / np.sqrt(counts / counts.sum() + 1e-8)
    return torch.tensor(weights / weights.mean(), dtype=torch.float32)


def loss_fn(logits, y, kind: str, weights: torch.Tensor) -> torch.Tensor:
    if kind == "ce":
        return F.cross_entropy(logits, y, ignore_index=IGNORE)
    loss = F.cross_entropy(logits, y, weight=weights, ignore_index=IGNORE)
    if kind == "wce_dice":
        valid = (y != IGNORE).unsqueeze(1)
        probs = logits.softmax(1) * valid
        onehot = F.one_hot(torch.where(y == IGNORE, 0, y), logits.shape[1])
        onehot = onehot.permute(0, 3, 1, 2) * valid
        inter = (probs * onehot).sum((0, 2, 3))[1:]
        denom = (probs + onehot).sum((0, 2, 3))[1:]
        loss = loss + (1 - (2 * inter + 1) / (denom + 1)).mean()
    return loss


@torch.no_grad()
def predict(model, x: np.ndarray, stats, n_bands: int, batch: int = 32) -> np.ndarray:
    model.eval()
    out = []
    for i in range(0, len(x), batch):
        chunk = torch.from_numpy(x[i:i + batch, :, :n_bands].astype(np.float32))
        b = normalise(chunk[:, 0], stats, n_bands)
        a = normalise(chunk[:, 1], stats, n_bands)
        out.append(model(b, a).softmax(1).numpy().astype(np.float16))
    return np.concatenate(out)


def change_scores(prob: np.ndarray, y: np.ndarray, cfg: dict, threshold: float = 0.5) -> dict:
    """Pixel metrics for 'any built-up change' against weak targets."""
    change_prob = 1 - prob[:, 0].astype(np.float32)
    t = prepare_targets(torch.from_numpy(y), cfg).numpy()
    valid = t != IGNORE
    truth = (t > 0)[valid]
    pred = (change_prob >= threshold)[valid]
    tp = int((truth & pred).sum()); fp = int((~truth & pred).sum()); fn = int((truth & ~pred).sum())
    precision = tp / max(tp + fp, 1); recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    return {"precision": precision, "recall": recall, "f1": f1, "iou": tp / max(tp + fp + fn, 1)}


def train(cfg: dict) -> dict:
    cfg = {**DEFAULTS, **cfg}
    set_seed(cfg["seed"])
    torch.set_num_threads(cfg["threads"])
    bank = json.loads((PATCHES / "patch_bank.json").read_text(encoding="utf-8"))
    stats = bank["band_mean_std_train"]
    train_npz, val_npz = np.load(PATCHES / "train.npz"), np.load(PATCHES / "val.npz")
    x_train, y_train = train_npz["x"], train_npz["y"]
    x_val, y_val = val_npz["x"], val_npz["y"]
    if cfg.get("extra_patches"):
        # Human-corrected patches (train blocks only), repeated to raise their weight.
        extra = np.load(ROOT / cfg["extra_patches"])
        reps = int(cfg.get("extra_repeat", 1))
        x_train = np.concatenate([x_train] + [extra["x"]] * reps)
        y_train = np.concatenate([y_train] + [extra["y"]] * reps)
    n_bands = bands_for(cfg)

    model = build_model(cfg)
    if cfg.get("init_from"):
        model.load_state_dict(torch.load(ROOT / cfg["init_from"] / "model.pt", weights_only=True))
    num_classes = 3 if cfg["model"] == "ground" else 2
    weights = class_weights(y_train, num_classes, cfg)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg["lr"], weight_decay=cfg["weight_decay"])
    steps = cfg["epochs"] * int(np.ceil(len(x_train) / cfg["batch_size"]))
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=cfg["lr"], total_steps=steps)

    run_dir = MODELS / cfg["name"]
    run_dir.mkdir(parents=True, exist_ok=True)
    history, best = [], {"f1": -1}
    started = time.perf_counter()
    for epoch in range(cfg["epochs"]):
        model.train()
        order = np.random.permutation(len(x_train))
        losses = []
        t0 = time.perf_counter()
        for i in range(0, len(order), cfg["batch_size"]):
            idx = np.sort(order[i:i + cfg["batch_size"]])
            chunk = torch.from_numpy(x_train[idx][:, :, :n_bands].astype(np.float32))
            y = prepare_targets(torch.from_numpy(y_train[idx]).long(), cfg)
            before, after = chunk[:, 0], chunk[:, 1]
            if cfg["augment"]:
                before, after, y = augment(before, after, y, cfg["radiometric_jitter"])
            logits = model(normalise(before, stats, n_bands), normalise(after, stats, n_bands))
            loss = loss_fn(logits, y, cfg["loss"], weights)
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
            losses.append(float(loss))
        prob = predict(model, x_val, stats, n_bands)
        scores = change_scores(prob, y_val, cfg)
        record = {"epoch": epoch + 1, "train_loss": float(np.mean(losses)),
                  "epoch_seconds": round(time.perf_counter() - t0, 1),
                  **{f"weakval_{k}": round(v, 4) for k, v in scores.items()}}
        history.append(record)
        print(json.dumps(record), flush=True)
        # "last": weak-val F1 rewards the weak rules' biases that human-corrected
        # fine-tuning is meant to remove, so keep the final epoch instead.
        if scores["f1"] > best["f1"] or cfg.get("select") == "last":
            best = {**scores, "epoch": epoch + 1}
            torch.save(model.state_dict(), run_dir / "model.pt")
            _write_run(run_dir, cfg, model, weights, stats, best, started, history, partial=True)

    return _write_run(run_dir, cfg, model, weights, stats, best, started, history, partial=False)


def _write_run(run_dir, cfg, model, weights, stats, best, started, history, partial: bool) -> dict:
    result = {
        "config": cfg,
        "parameters": count_parameters(model),
        "class_weights": weights.tolist(),
        "band_mean_std_train": stats,
        "best_weakval": best,
        "train_seconds": round(time.perf_counter() - started, 1),
        "history": history,
        "selection": ("last epoch" if cfg.get("select") == "last"
                      else "best epoch by weak-validation change F1 (proxy only)"),
        "status": "in_progress" if partial else "complete",
    }
    (run_dir / "run.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def load_model(run_dir: Path) -> tuple[torch.nn.Module, dict]:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    model = build_model(run["config"])
    model.load_state_dict(torch.load(run_dir / "model.pt", weights_only=True))
    model.eval()
    return model, run


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, help="JSON file overriding DEFAULTS")
    parser.add_argument("--set", nargs="*", default=[], help="key=json_value overrides")
    args = parser.parse_args(argv)
    cfg = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    for item in args.set:
        key, value = item.split("=", 1)
        cfg[key] = json.loads(value)
    result = train(cfg)
    print(json.dumps({"name": result["config"]["name"], "best": result["best_weakval"],
                      "params": result["parameters"]}))


if __name__ == "__main__":
    main()

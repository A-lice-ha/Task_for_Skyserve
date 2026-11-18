"""Export trained models to ONNX, quantize to INT8, and compare FP32 vs INT8.

Method: ONNX Runtime static post-training quantization (default variant
'qdq_conv_u8_pct'): QDQ format, Conv layers only, UINT8 activations,
per-channel INT8 weights, percentile calibration on 64 training-split patches
(no evaluation data). The default was changed from MinMax ('qdq_conv_u8')
because percentile calibration kept more small FP32 detections at the same
speed; that choice used only FP32-vs-INT8 fidelity, never human labels. Alternatives in VARIANTS are swept and recorded for comparison. Both FP32 and INT8 graphs run
under ONNX Runtime CPU with identical thread settings, so comparisons are
like-for-like. Normalisation and softmax are inside the exported graph, so
deployment input is raw surface reflectance.

Outputs per model in outputs/models/<run>/: model_fp32.onnx, model_int8.onnx,
quantization.json. Summary: outputs/quantization/comparison.{json,md}.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
import torch
from onnxruntime.quantization import (CalibrationDataReader, CalibrationMethod, QuantFormat,
                                      QuantType, quantize_static)
from onnxruntime.quantization.shape_inference import quant_pre_process

from src.evaluation.evaluate import REVIEWS, attach_splits, score_method
from src.evaluation.windows import (build_cache, change_map, load_cache, load_reviews,
                                    predict_windows, small_change_retention)
from src.training.train import load_model

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "quantization"
CALIBRATION_PATCHES = 64
SIZE = 256
SEED = 20261118


class Deployable(torch.nn.Module):
    """Raw reflectance in, class probabilities out."""

    def __init__(self, model: torch.nn.Module, stats: list[list[float]], n_bands: int):
        super().__init__()
        self.model = model
        self.register_buffer("mean", torch.tensor([s[0] for s in stats[:n_bands]]).view(1, -1, 1, 1))
        self.register_buffer("std", torch.tensor([s[1] for s in stats[:n_bands]]).view(1, -1, 1, 1))

    def forward(self, before: torch.Tensor, after: torch.Tensor) -> torch.Tensor:
        return self.model((before - self.mean) / self.std, (after - self.mean) / self.std).softmax(1)


def export_fp32(run_dir: Path) -> tuple[Path, int]:
    model, run = load_model(run_dir)
    n_bands = 6 if run["config"]["model"] == "ground" else 4
    wrapped = Deployable(model, run["band_mean_std_train"], n_bands).eval()
    dummy = torch.zeros(1, n_bands, SIZE, SIZE)
    path = run_dir / "model_fp32.onnx"
    torch.onnx.export(wrapped, (dummy, dummy), str(path), input_names=["before", "after"],
                      output_names=["probabilities"], opset_version=17,
                      dynamic_axes={"before": {0: "n"}, "after": {0: "n"}, "probabilities": {0: "n"}})
    return path, n_bands


class PatchReader(CalibrationDataReader):
    def __init__(self, n_bands: int):
        bank = np.load(ROOT / "outputs" / "patches" / "train.npz")
        rng = np.random.default_rng(SEED)
        idx = np.sort(rng.choice(len(bank["y"]), CALIBRATION_PATCHES, replace=False))
        x = bank["x"][idx][:, :, :n_bands].astype(np.float32)
        # Training patches are 96 px; tile 2x2 + crop is not needed because the
        # graph is fully convolutional, but static shapes must match: pad to 256.
        pad = np.zeros((len(idx), 2, n_bands, SIZE, SIZE), np.float32)
        pad[..., :x.shape[-2], :x.shape[-1]] = x
        self.items = iter([{"before": p[0:1], "after": p[1:2]} for p in pad])

    def get_next(self):
        return next(self.items, None)


VARIANTS = {
    # name: (format, activation type, op types to quantize or None for all, calibration)
    "qdq_all_s8": (QuantFormat.QDQ, QuantType.QInt8, None, CalibrationMethod.MinMax),
    "qdq_conv_u8": (QuantFormat.QDQ, QuantType.QUInt8, ["Conv"], CalibrationMethod.MinMax),
    "qop_conv_u8": (QuantFormat.QOperator, QuantType.QUInt8, ["Conv"], CalibrationMethod.MinMax),
    "qdq_conv_u8_pct": (QuantFormat.QDQ, QuantType.QUInt8, ["Conv"], CalibrationMethod.Percentile),
}
DEFAULT_VARIANT = "qdq_conv_u8_pct"


def quantize(fp32: Path, n_bands: int, variant: str = DEFAULT_VARIANT,
             out_name: str = "model_int8.onnx") -> Path:
    fmt, act, ops, calib = VARIANTS[variant]
    pre = fp32.with_name("model_fp32_pre.onnx")
    quant_pre_process(str(fp32), str(pre), skip_symbolic_shape=True)
    int8 = fp32.with_name(out_name)
    # Post-ReLU activations are non-negative, so unsigned activations use the
    # full 8-bit range; Sub/Abs temporal differences and softmax stay FP32.
    quantize_static(str(pre), str(int8), PatchReader(n_bands), quant_format=fmt,
                    activation_type=act, weight_type=QuantType.QInt8, per_channel=True,
                    calibrate_method=calib, op_types_to_quantize=ops)
    pre.unlink()
    return int8


def session(path: Path, threads: int) -> ort.InferenceSession:
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])


def onnx_predictor(path: Path, n_bands: int, threads: int = 4):
    sess = session(path, threads)

    def predict(before: np.ndarray, after: np.ndarray) -> np.ndarray:
        outs = [sess.run(None, {"before": before[i:i + 1, :n_bands].astype(np.float32),
                                "after": after[i:i + 1, :n_bands].astype(np.float32)})[0]
                for i in range(len(before))]
        return np.concatenate(outs)
    return predict


def measure(path: Path, n_bands: int, threads: int, runs: int = 30) -> dict:
    """Run in a fresh process: latency per 256x256 window and peak RSS."""
    import psutil
    proc = psutil.Process()
    base = proc.memory_info().rss
    peak = [base]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            peak[0] = max(peak[0], proc.memory_info().rss)
            time.sleep(0.001)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    sess = session(path, threads)
    rng = np.random.default_rng(SEED)
    feed = {"before": rng.uniform(0, 0.3, (1, n_bands, SIZE, SIZE)).astype(np.float32),
            "after": rng.uniform(0, 0.3, (1, n_bands, SIZE, SIZE)).astype(np.float32)}
    for _ in range(5):
        sess.run(None, feed)
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        sess.run(None, feed)
        times.append((time.perf_counter() - t0) * 1000)
    stop.set()
    sampler.join()
    return {"threads": threads, "latency_ms_median": round(float(np.median(times)), 2),
            "latency_ms_p90": round(float(np.percentile(times, 90)), 2),
            "peak_rss_mib": round(peak[0] / 2**20, 1),
            "peak_rss_delta_mib": round((peak[0] - base) / 2**20, 1)}


def measure_subprocess(path: Path, n_bands: int, threads: int) -> dict:
    out = subprocess.run([sys.executable, "-m", "src.quantization.quantize", "--measure", str(path),
                          "--bands", str(n_bands), "--threads", str(threads)],
                         capture_output=True, text=True, cwd=ROOT, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])



def compare(run_dir: Path, reviews_path: Path | None, variant: str = DEFAULT_VARIANT) -> dict:
    fp32, n_bands = export_fp32(run_dir)
    int8 = quantize(fp32, n_bands, variant)
    windows = json.loads((ROOT / "outputs" / "review" / "windows.json").read_text(encoding="utf-8"))
    by_city: dict[str, list[str]] = {}
    for w in windows:
        by_city.setdefault(w["city"].lower(), []).append(w["feature_id"])
    cache = load_cache() if (ROOT / "outputs" / "review" / "eval_windows.npz").exists() else None
    if cache is None or len(set(cache["ids"])) < len(windows):
        build_cache(by_city)
        cache = load_cache()
    x, valid, weak = cache["x"], cache["valid"], cache["weak"]
    maps = {}
    for name, path in (("fp32", fp32), ("int8", int8)):
        maps[name] = change_map(predict_windows(onnx_predictor(path, n_bands), x), valid)
    sweep = {}
    sub = slice(0, min(80, len(x)))
    for variant in VARIANTS:
        path = quantize(fp32, n_bands, variant, f"variant_{variant}.onnx")
        vmap = change_map(predict_windows(onnx_predictor(path, n_bands, threads=1), x[sub]), valid[sub])
        sweep[variant] = {
            "size_mib": round(path.stat().st_size / 2**20, 3),
            "pixel_agreement": round(float((vmap == maps["fp32"][sub])[valid[sub]].mean()), 5),
            "small_change_retention": small_change_retention(maps["fp32"][sub], vmap),
            "latency_1thr": measure_subprocess(path, n_bands, 1)["latency_ms_median"],
        }
        path.unlink()
    agree = (maps["fp32"] == maps["int8"])[valid]
    fp_change, q_change = maps["fp32"] > 0, maps["int8"] > 0
    inter = (fp_change & q_change).sum()
    union = (fp_change | q_change).sum()
    result = {
        "run": run_dir.name,
        "variant": variant,
        "method": f"ORT static PTQ variant '{variant}': {VARIANTS[variant][0].name}, "
                  f"ops {VARIANTS[variant][2] or 'all'}, activations {VARIANTS[variant][1].name}, "
                  f"per-channel INT8 weights, calibration {VARIANTS[variant][3].name}; "
                  "Sub/Abs differences and softmax kept FP32",
        "variant_sweep_first_80_windows": sweep,
        "calibration": f"{CALIBRATION_PATCHES} train-split 96 px patches zero-padded to 256 (seed {SEED})",
        "file_size_mib": {"fp32": round(fp32.stat().st_size / 2**20, 3),
                          "int8": round(int8.stat().st_size / 2**20, 3)},
        "pixel_agreement_all_windows": round(float(agree.mean()), 5),
        "change_iou_fp32_vs_int8": round(float(inter / union), 4) if union else None,
        "small_change_retention": small_change_retention(maps["fp32"], maps["int8"]),
        "runtime": {name: [measure_subprocess(path, n_bands, t) for t in (1, 4)]
                    for name, path in (("fp32", fp32), ("int8", int8))},
        "windows": len(x),
    }
    if reviews_path and reviews_path.exists():
        reviews = attach_splits(load_reviews(reviews_path))
        index = {fid: i for i, fid in enumerate(cache["ids"])}
        order = np.array([index[r["feature_id"]] for r in reviews])
        result["detection"] = {
            name: {k: v for k, v in score_method(f"{run_dir.name}_{name}", m[order], reviews,
                                                 weak[order]).items() if k != "per_window"}
            for name, m in maps.items()}
    (run_dir / "quantization.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=Path, nargs="*", default=[])
    parser.add_argument("--reviews", type=Path, default=REVIEWS)
    parser.add_argument("--measure", type=Path)
    parser.add_argument("--bands", type=int, default=6)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--variant", choices=sorted(VARIANTS), default=DEFAULT_VARIANT)
    args = parser.parse_args(argv)
    if args.measure:
        print(json.dumps(measure(args.measure, args.bands, args.threads)))
        return
    OUT.mkdir(parents=True, exist_ok=True)
    results = [compare(r, args.reviews, args.variant) for r in args.runs]
    (OUT / "comparison.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    lines = ["| Model | Precision | Size MiB | Latency ms (1 thr) | Latency ms (4 thr) | Peak RSS MiB (1 thr) | Test F1 | Pixel agreement | Small-change retention |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        for name in ("fp32", "int8"):
            one, four = r["runtime"][name]
            f1 = r.get("detection", {}).get(name, {}).get("splits", {}).get("test/all", {}).get("f1", "–")
            lines.append(f"| {r['run']} | {name.upper()} | {r['file_size_mib'][name]} | "
                         f"{one['latency_ms_median']} | {four['latency_ms_median']} | {one['peak_rss_mib']} | "
                         f"{f1} | {r['pixel_agreement_all_windows'] if name == 'int8' else 1.0} | "
                         f"{r['small_change_retention']['retention'] if name == 'int8' else 1.0} |")
    (OUT / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

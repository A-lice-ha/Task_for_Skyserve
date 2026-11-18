# Phase 3 — Model architecture

> **Update (2026-11-18).** The models were trained in Phase 4/5 with these
> changes: the ground U-Net uses widths (16, 32, 64, 128) — 1,175,571
> parameters — and a **3-class head** (no change, built-up gain, built-up
> loss); change types are assigned per detection afterwards
> ([DECISION_LOG D-006](DECISION_LOG.md), D-010). The onboard model is
> unchanged (279,274 parameters, binary). Trained checkpoints, ONNX FP32 and
> INT8 artifacts: `outputs/models/ground_ce_final/`,
> `outputs/models/onboard_final/`. Results: [PHASE_5_EVALUATION.md](PHASE_5_EVALUATION.md).
> The text below is the original Phase 3 design record.

## Implemented models

Both models consume aligned before/after image patches in PyTorch NCHW layout
and return full-resolution **logits**. Apply softmax (ground) or two-class
softmax (onboard) only when probabilities are needed. Neither model includes
training, tiling, geospatial I/O, or a trained checkpoint.

| Model | Input bands per date | Encoder/fusion | Output |
|---|---|---|---|
| Ground `SiameseUNet` | B02, B03, B04, B08, B11, B12 | Shared four-stage convolutional encoder; concatenate both features and their absolute difference at each scale; U-Net decoder | 7 classes: no-change, construction, demolition, road, bridge, infrastructure-other, uncertain |
| Onboard `SiameseMobileNetV3Small` | B02, B03, B04, B08 | Shared torchvision MobileNetV3-Small encoder at width multiplier 0.5; depthwise-separable decoder; absolute-difference fusion | 2 classes: no-change, change |

Use the common-valid mask when preparing input patches and exclude
atmospherically suspect areas identified during Phase 2. In particular,
Sen2Cor's SCL mask did not capture the visually observed diffuse veil in the
2026-10-22 T43RGM scene. The binary onboard proposal is not a substitute for
the ground model's semantic classes or human review.

## Decision record

- **Ground model: custom Siamese U-Net.** Its shared encoder accepts six
  reflectance bands rather than assuming RGB input. Explicitly combining
  before, after, and absolute-difference features retains temporal direction
  for construction-versus-demolition classification. A compact custom
  encoder avoids downloading or depending on RGB-pretrained weights and keeps
  the initial CPU-only implementation straightforward. This is an
  architecture baseline, not a claim that it outperforms a pretrained
  ResNet/EfficientNet.
- **Onboard model: half-width MobileNetV3-Small.** It uses the torchvision
  architecture with an input stem adapted to four Sentinel-2 bands; weights
  are randomly initialized (`weights=None`) because RGB ImageNet weights are
  not directly valid for these bands and no suitable multispectral pretrained
  weights are included. Shared weights avoid duplicating the encoder. The
  intended target is fewer than five million parameters; latency, peak memory,
  power, and target-device compatibility require measurement on the eventual
  onboard hardware.
- **Output meaning.** Both heads output logits. No class probabilities or
  change decisions are calibrated until training and validation data exist.
  The `uncertain` ground class is explicitly modeled rather than forced into
  a structural change class.
- **Operational constraint.** Inference should use small tiles/patches and a
  validity mask. Running a full Sentinel-2 tile as one tensor is inappropriate
  for the available CPU/RAM. Registration and atmospheric-quality limitations
  documented in Phase 2 remain upstream sources of false changes.

## Verification

`tests/test_phase3_models.py` checks output resolution and class dimensions,
finite logits, gradient flow, invalid-input handling, and the onboard
parameter-count limit. Both models were also profiled on the CPU-only
development machine with batch size 1, 256×256 synthetic input patches, and
four CPU threads:

| Model | Trainable parameters | Torch-profiler FLOPs | First forward | Sampled peak process RSS |
|---|---:|---:|---:|---:|
| Ground Siamese U-Net | 4,696,743 | 40.35 GFLOPs | 0.246 s | 455.3 MiB |
| Onboard MobileNetV3-Small | 279,274 | 0.118 GFLOPs | 0.025 s | 241.3 MiB |

Peak RSS was sampled every 2 ms during the first forward pass and includes
Python/PyTorch process overhead; short-lived peaks may be missed. FLOPs are
the profiler's operator estimates. These are synthetic-patch CPU proxy
measurements, not trained-model accuracy, sustained throughput, full-tile
inference measurements, or target-device latency, memory, and power results.

# Phase 5 — Training results, evaluation, quantization and outputs

All numbers below were measured on 2026-11-18 on the development machine
(AMD Ryzen 5 7530U, 6 cores / 12 threads, 15.3 GiB RAM, no GPU) and are
reproducible with the commands in the [README](../README.md). Decisions are
referenced as D-xxx in [DECISION_LOG.md](DECISION_LOG.md).

## 5.1 Reference labels

Every one of the 306 candidate windows (256 × 256 px, 2.56 km) was reviewed
blind on the offline review page (D-008):
`data/labels/reviews/review_labels.json`.

| Class | Windows | | Class | Windows |
|---|---:|---|---|---:|
| construction | 16 | | natural_vegetation | 126 |
| road | 7 | | acquisition_artifact | 74 |
| demolition | 1 | | natural_water | 14 |
| bridge / infrastructure_other | 0 | | no_change | 6 |
| | | | uncertain | 62 |

Only **24 windows (8 %) contain man-made change**; 62 are undecidable from two
10 m dates. Reviewer confidence: 27 high, 120 medium, 159 low. By split:
train 156, validation 71, test 75, zero-shot 4 (val/test/zero-shot positives:
2 / 6 / 2). Every positive window's weak outline was marked "partial", none
"correct", so the planned pixel-IoU proxy (D-009) has no eligible windows.

The candidate windows were enriched by a change-index ranking, so the class
mix is not the population mix; metrics describe the detector on these
reviewed windows, not area-wide accuracy.

## 5.2 Training and hyperparameter search (weak labels)

Weak-validation scores compare against the weak labels on validation blocks;
they are a proxy used only to rank runs (D-010).

| Run | Loss | lr | Jitter | Epochs | Weak-val P | R | F1 | Train s |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| ground_ce_lr0.001 | CE | 1e-3 | 0.05 | 6 | 0.541 | 0.276 | 0.365 | 720 |
| ground_ce_lr0.003 | CE | 3e-3 | 0.05 | 6 | 0.537 | 0.446 | 0.488 | 740 |
| ground_wce_lr0.001 | wCE | 1e-3 | 0.05 | 6 | 0.250 | 0.808 | 0.382 | 589 |
| ground_wce_lr0.003 | wCE | 3e-3 | 0.05 | 6 | 0.265 | 0.827 | 0.402 | 579 |
| ground_wce_dice_lr0.001 | wCE+Dice | 1e-3 | 0.05 | 6 | 0.328 | 0.757 | 0.457 | 520 |
| ground_wce_dice_lr0.003 | wCE+Dice | 3e-3 | 0.05 | 6 | 0.392 | 0.700 | 0.502 | 435 |
| ground_wce_dice_lr0.003_nojitter | wCE+Dice | 3e-3 | 0 | 6 | 0.381 | 0.745 | 0.504 | 435 |
| onboard_wce_dice_lr0.001 | wCE+Dice | 1e-3 | 0.05 | 6 | 0.103 | 0.208 | 0.137 | 118 |
| onboard_wce_dice_lr0.003 | wCE+Dice | 3e-3 | 0.05 | 6 | 0.132 | 0.232 | 0.168 | 118 |
| **ground_wce_dice_final** | wCE+Dice | 3e-3 | 0.05 | 12 | 0.500 | 0.697 | **0.582** | 866 |
| **ground_ce_final** (learned baseline) | CE | 3e-3 | 0.05 | 12 | 0.593 | 0.521 | 0.555 | 861 |
| **onboard_final** | wCE+Dice | 3e-3 | 0.05 | 25 | 0.171 | 0.262 | 0.207 | 528 |
| ground_wce_dice_hf (D-013) | wCE+Dice | 3e-4 | 0.05 | +3 | 0.474 | 0.626 | 0.539 | 347 |

Observations:

- Class imbalance: plain CE has the highest precision but the lowest recall;
  inverse-sqrt class weighting raises recall at a large precision cost; adding
  Dice recovers most of the precision.
- Radiometric jitter made no measurable difference on weak validation (0.502
  vs 0.504); it was kept because it targets acquisition differences that weak
  validation cannot measure.
- The onboard model is weak. Its MobileNetV3-Small encoder downsamples 32×
  (a 96 px patch becomes 3 × 3 at the bottleneck), so the few-pixel changes
  that dominate this data depend on its shallow skip features alone — a
  likely cause, not a tested one.
- Configs: `configs/training/*.json`; per-run histories:
  `outputs/experiments/runs/*.json`; search table:
  `outputs/experiments/hparam_search.csv`.

## 5.3 Evaluation against the human review

Protocol (D-009): a method flags a window when its change pixels (components
≥ 4 px) reach k; k is chosen on validation windows and frozen. Uncertain
windows are never scored. All methods see identical inputs
(`outputs/review/eval_windows.npz`). The rule baseline is the weak-label
generator itself. Full results: `outputs/evaluation/metrics.json`,
per-window decisions: `window_predictions.csv`.

| Method | k | Test P | Test R | Test F1 (95 % CI) | Zero-shot F1 (n = 4) | Natural false-alarm rate (held-out, n = 53) | Held-out AP / AUROC (n = 122, 10 pos.) | Block-CV F1 (95 % CI) |
|---|---:|---:|---:|---|---:|---:|---|---|
| Rule baseline (weak labels) | 256 | 0.100 | 0.667 | 0.174 [0.043, 0.327] | 0.40 | 0.698 | 0.067 / 0.374 | 0.125 [0.053, 0.212] |
| **ground_ce_final** (selected) | 128 | 0.114 | 0.833 | 0.200 [0.046, 0.351] | 0.50 | 0.736 | 0.066 / 0.355 | 0.143 [0.059, 0.224] |
| ground_wce_dice_final | 256 | 0.111 | 0.833 | 0.196 [0.044, 0.340] | 0.50 | 0.755 | 0.065 / 0.345 | 0.146 [0.061, 0.230] |
| onboard_final | 64 | 0.109 | 0.833 | 0.192 [0.044, 0.333] | 0.40 | 0.793 | 0.065 / 0.350 | 0.138 [0.056, 0.217] |
| ground_wce_dice_hf (D-013) | 128 | 0.104 | 0.833 | 0.185 [0.042, 0.327] | 0.50 | 0.811 | 0.068 / 0.381 | 0.143 [0.059, 0.226] |

**Reading the table honestly:**

- Validation has only 2 positive windows, so every method selects a k that
  flags most windows. Test precision (0.10–0.11) is the base rate: the
  methods are **no better than flagging every window**. Flagging all 122
  held-out windows would give F1 = 0.15, which equals the block-CV scores.
- The **window's change-pixel count ranks windows worse than chance**
  (AUROC 0.35, 95 % CI 0.24–0.48 for the selected model). Agricultural,
  water and haze windows produce more "built-up" pixels than true
  construction, which is small and compact at 10 m. The learned models
  reproduce the weak rules' bias rather than correcting it; none is
  materially better than the rules it was trained on.
- Natural false alarms (held-out): `ground_ce_final` flags 29/36
  vegetation, 8/14 acquisition-artefact and 2/3 water windows.
- Direction (gain vs loss) agreed on 6/6 held-out true positives for
  `ground_ce_final` (rule baseline 2/5).
- Uncertain held-out windows: 17, of which 2 flagged (not scored).
- Localisation: the planned pixel IoU proxy has no eligible windows (no
  positive window's outline was marked "correct"). Evidence panels (5.4)
  show model outlines frequently cover bright fallow fields inside windows
  that also contain real construction.

### Block-grouped cross-validation (D-014)

5 folds grouped by 10 km block over all 122 decided held-out windows
(100 blocks); k chosen on 4 folds, applied to the 5th. Selected k varied
widely between folds (e.g. 1 / 128 / 1 / 1 / 1 for `ground_ce_final`),
confirming that a pixel-count threshold has no stable operating point.

### Human-corrected fine-tune (D-013) — negative result

Fine-tuning on 1,095 crops from 122 reviewed train-block windows (negatives
forced to no-change) lowered detected change on artefact and water windows
(median 576 → 266 and 1,188 → 444 px) but also on true positives
(322 → 206 px); held-out AUROC 0.345 → 0.381, test F1 0.196 → 0.185.

## 5.4 Second-stage window filter (context score)

Because the pixel models cannot separate the classes by amount of change, a
small logistic regression scores each 2.56 km window from features of the
change map and the spectral context of the changed pixels
(`src/evaluation/window_filter.py`): change area, largest component,
component count, share of change in compact components (≤ 50 px), mean T1
NDVI, ΔNDVI, ΔNDBI, ΔMNDWI over changed pixels, and window-mean blue
increase (haze). Settings fixed in advance (standardised features,
C = 0.1, balanced class weights); fitted only on the 122 decided reviewed
windows in **train blocks** (14 positives); threshold = max-F1 on those
same train windows; evaluated once on held-out windows.

| | AP | AUROC (95 % CI) | P | R | F1 | Natural false-alarm rate |
|---|---:|---|---:|---:|---:|---:|
| Pixel count, ground_ce_final (held-out) | 0.066 | 0.355 [0.244, 0.479] | 0.078* | 0.80* | 0.143* | 0.835* |
| **Window filter (held-out)** | **0.176** | **0.761 [0.637, 0.863]** | 0.125 | 0.10 | 0.111 | **0.055** |
| Window filter (fit windows, optimistic) | 0.522 | 0.910 | | | | |

\* block-CV operating point. Prevalence 8.2 %.

Standardised coefficients (sign = direction of evidence for man-made
change): compact share +0.78, ΔNDBI +0.44, blue increase (haze) −0.51,
component count −0.48, largest component −0.36, change area −0.21,
ΔNDVI −0.16. These match the reviewed failure modes: many scattered or
large patches and haze indicate natural/acquisition change.

**Conclusion:** the context filter is the only component that ranks windows
better than chance (CI excludes 0.5) and cuts natural false alarms from
~74 % to 5.5 %. Its threshold, chosen on the (over-fitted) train windows,
is too strict on held-out windows (recall 1/10), so it is used to
**prioritise** detections rather than to delete them. 10 held-out positives
are too few for a reliable operating point.


## 5.5 Quantization (ONNX Runtime static INT8)

Method (D-011): QDQ, Conv layers only, UINT8 activations, per-channel INT8
weights, MinMax calibration on 64 train-split patches; normalisation, the
temporal difference (Sub/Abs) and softmax remain FP32. Latency is the median
of 30 batch-1 256 × 256 forward passes in a fresh process; peak RSS is the
highest resident memory sampled in that process. Accuracy is measured on all 306 reviewed windows
with the same inputs, threshold and k as FP32.

| Model | Precision | Size MiB | Latency ms, 1 thread | Latency ms, 4 threads | Peak RSS MiB | Test F1 | Pixel agreement with FP32 | Small-change retention |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| ground_ce_final | FP32 | 4.50 | 123.7 | 54.4 | 409 | 0.200 | 1 | 1 |
| ground_ce_final | INT8 | 1.21 | 83.5 | 42.0 | 399 | 0.200 | 0.9977 | 0.850 |
| onboard_final | FP32 | 1.13 | 11.2 | 6.0 | 296 | 0.192 | 1 | 1 |
| onboard_final | INT8 | 0.73 | 12.1 | 8.5 | 307 | 0.200 | 0.9961 | 0.762 |

Parameter counts do not change (1,175,571 and 279,274). Small-change
retention is the share of FP32 change components of ≤ 10 px (≤ 1,000 m²)
that INT8 still detects (7,253 components for the ground model). Change-pixel
IoU between FP32 and INT8 ground maps: 0.827.

Variant sweep on the ground model (first 80 windows, `quantization.json`):

| Variant | MiB | Latency ms (1 thr) | Pixel agreement | Small-change retention |
|---|---:|---:|---:|---:|
| QDQ, all ops, INT8 activations | 1.20 | 124.7 | 0.9985 | 0.919 |
| **QDQ, Conv only, UINT8 activations (chosen)** | 1.21 | 84.0 | 0.9977 | 0.852 |
| QOperator, Conv only, UINT8 | 1.18 | 83.4 | 0.9942 | 0.326 |
| QDQ Conv-only, percentile calibration | 1.21 | 84.6 | 0.9987 | 0.896 |

Interpretation:

- INT8 is 3.7× smaller and 1.5× faster (1 thread) for the ground model, with
  unchanged window-level F1, but it **loses 15 % of the smallest
  detections** — the changes that matter most at 10 m. Quantizing all ops
  keeps more of them (92 %) but gives no speed-up on this CPU. Percentile
  calibration retains more (90 %) at the same speed and is the next thing to
  adopt; it was not switched in after seeing test-window results.
- Peak memory is dominated by the ONNX Runtime process (~270 MiB baseline),
  so INT8 hardly changes it here.
- For the onboard model INT8 is *slower* on this x86 CPU: the network is
  already small and the quantize/dequantize overhead dominates. INT8 is only
  worthwhile on hardware with integer accelerators; this was not tested on a
  target onboard device.
- The ground register is produced with FP32 because of the small-change loss
  (D-015); INT8 full-tile maps are produced too for comparison.
- QAT was not attempted: PTQ kept window-level metrics unchanged, and the
  small-change loss has a cheaper fix (percentile calibration).

## 5.6 Full-tile change maps, detection register and evidence

**Change maps** (`src/inference/predict_tile.py`; 256 px tiles, 16 px
overlap margin; EPSG:32643, 10 m, aligned with the input grid):
`outputs/maps/<city>_ground_fp32_change_class.tif` (0 none, 1 built-up
gain, 2 built-up loss, 255 invalid) and `_change_prob.tif` (P(change) × 250,
not committed — regenerate with the command in the README).

| City | Tiles | FP32 s (6 thr) | INT8 s (6 thr) | FP32 change km² (share of valid) | FP32 vs INT8 pixel agreement | Change IoU |
|---|---:|---:|---:|---:|---:|---:|
| Mumbai | 2,480 | 286 | 160 | 104.3 (0.97 %) | 0.9979 | 0.798 |
| Delhi | 2,500 | 285 | 221 | 183.0 (1.52 %) | 0.9973 | 0.834 |

Only the Mumbai INT8 run had the CPU to itself; the other three overlapped
with evaluation jobs, so their wall times are pessimistic. Controlled
per-window latency is in 5.5.

**Context grid:** `outputs/maps/<city>_ground_fp32_context.tif` — the window
filter's score for every 2.56 km cell (1,839 Mumbai / 1,849 Delhi cells; 39
and 193 above the threshold).

**Detection register** (`src/inference/register.py`):
`outputs/maps/detection_register_ground_fp32.csv` (all detections) and
`<city>_ground_fp32_detections.gpkg` (polygons, not committed: 60 MB;
regenerate). Committed compact layer:
`outputs/maps/priority_detections_ground_fp32.gpkg` (detections in
context-flagged cells, highest priority first).

Fields: `detection_id`, `city`, `pair_id`, `t1`, `t2`, `lat`, `lon`,
polygon, `direction`, `change_type` and `type_basis` (rule used),
`area_m2` ± `area_uncertainty_m2` (one boundary pixel = registration
residual), `length_m` (roads), `t1_water_fraction`, `confidence` (mean
P(change)) and `confidence_tier`, `context_score`, `context_flag`,
`priority_score` (= context × confidence), `review_window`, `human_class`,
`review_status`, `model`.

| City | Detections (≥ 8 px) | construction | demolition | road | bridge/reclamation | In context-flagged cells |
|---|---:|---:|---:|---:|---:|---:|
| Mumbai | 28,693 | 26,759 | 84 | 512 | 1,338 | 108 (0.15 km²) |
| Delhi | 58,289 | 54,004 | 3,307 | 930 | 48 | 723 (1.10 km²) |

Confidence tiers over all 86,982 detections: 10,836 high, 58,864 medium,
17,282 low.

**Review status is window-level**: every detection inside a reviewed
window inherits that window's human class, so "accepted" means "lies in a
window that contains real man-made change", not that the polygon itself was
verified. Within held-out reviewed windows, 14 of 30 detections in
context-flagged cells are in accepted windows (47 %) versus 124 of 3,498
(3.5 %) in unflagged cells — the context score concentrates real change, but
keeps only 10 % of accepted detections. The 6,916 rejected detections in
reviewed windows (mostly `rejected_natural_vegetation` and
`rejected_acquisition_artifact`) are the evidence base for rejected changes.

**The full register is dominated by false alarms.** With ~8 % of reviewed
windows containing real change and the pixel models flagging bright fallow
fields as built-up gain, most of the 86,982 detections are agricultural. The
register should be read through `priority_score` / `context_flag`, and
counts above are not estimates of construction activity.

**Evidence gallery** (`outputs/evidence/README.md`; `src/evaluation/evidence.py`):
T1 | T2 | T2 + model outline panels for four windows each of accepted
detections, rejected natural changes, false alarms, missed changes and
uncertain cases, with the human class and model pixel counts.

## 5.7 Limits

- One image pair per city: held-out blocks test geographic, not temporal,
  generalisation; city, season, interval and haze are confounded.
- Human labels are two-date Sentinel-2 interpretations by one reviewer
  (159 of 306 with low confidence), not independent ground truth.
- 24 positive windows in total (10 held-out): every CI is wide; per-class
  (road, demolition) metrics are not meaningful and are not claimed.
- Candidate windows were selected by a change index, so rates describe these
  windows, not the whole tiles.
- 10 m pixels with ~1 px residual misregistration cannot resolve individual
  buildings or narrow roads; detection polygons are approximate.


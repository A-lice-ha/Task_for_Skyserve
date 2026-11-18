# Project Summary: Sentinel-2 Man-Made Change Detection

Status: **complete for submission (2026-11-18).** This page is a short
overview; the full account is in the [technical report](docs/TECHNICAL_REPORT.md),
[Phase 5 results](docs/PHASE_5_EVALUATION.md), the
[decision log](docs/DECISION_LOG.md) and the [README](README.md) (reproduction
steps and a demo).

## 1. Objective

Detect, localise and analyse structural man-made change between two
Sentinel-2 dates, and separate it from natural variation (vegetation,
agriculture, water) and acquisition effects (haze, cloud, misregistration).
The project is CPU-only and has separate onboard and ground models.

## 2. Data

| Pair | City | Tile | Before → after | Interval |
|---|---|---|---|---:|
| A | Mumbai | T43QBB | 2022-12-23 → 2023-05-12 | 140 days |
| B | Delhi | T43RGM | 2023-10-06 → 2026-10-22 | 1,112 days |

The two tiles are disjoint and are never compared with each other. Each city
has only one pair, so held-out areas test geographic, not temporal,
generalisation.

## 3. Preprocessing and registration

- Sen2Cor 2.13.02 converts L1C to L2A surface reflectance. The six-band
  10 m stack (B02, B03, B04, B08, B11, B12) uses an SCL-based validity mask.
- The later date is registered to the earlier one with ORB/RANSAC.
- An independent check on stable features measured residual misalignment
  of 0.10 px (Delhi) and 0.33 px (Mumbai) median, max 0.6 px. It also found
  that the warp had silently not been applied (rasterio pitfall). This is
  fixed; its effect is at most 0.5 m, so results were not regenerated
  (D-019).
- Downstream, a 3×3 tolerance in the labelling rules and ± one boundary
  pixel on areas cover the remaining misalignment.
- The 2026 Delhi scene has regional haze that SCL misses. It is handled by
  a regional haze rule and the review class `acquisition_artifact`.

## 4. Labels and splits

- **Training labels:** rule-based weak labels from `configs/weak_labels.json`:
  - built-up gain or loss with a registration tolerance;
  - vegetation and water changes as hard negatives;
  - haze, previously vegetated land and tiny blobs as ambiguous.
- **Evaluation reference:** a blind human review of all 306 candidate
  windows (`data/labels/reviews/review_labels.json`).
  - 24 contain man-made change (16 construction, 7 road, 1 demolition).
  - 62 are uncertain.
  - The rest are vegetation (126), acquisition artefacts (74), water (14)
    and no change (6).
- **Splits:** whole 10 km blocks, 50/25/25 per city, a 2 km training
  buffer, and one untouched zero-shot block per city (Navi Mumbai/Panvel;
  Noida). With only 2 validation positives, the window threshold is also
  checked by 5-fold block-grouped cross-validation, and each city is
  also held out entirely (leave-one-city-out).

## 5. Models

| | Ground | Onboard |
|---|---|---|
| Architecture | Siamese U-Net (16–128 channels) | Siamese MobileNetV3-Small |
| Bands | 6 | 4 (10 m only) |
| Output | no change / built-up gain / loss | change / no change |
| Parameters | 1,175,571 | 279,274 |
| Selected run | `ground_ce_final` | `onboard_final` |

- **Hyperparameter search:** 9 runs covering loss (CE, weighted CE,
  weighted CE + Dice) × learning rate, a jitter ablation, and onboard
  learning rates.
- **Final runs:** 12 epochs (ground) and 25 epochs (onboard).
- **Change types** are assigned per detection by recorded rules:
  - loss → demolition
  - gain over water → bridge or reclamation
  - elongated gain → road
  - other gain → construction

## 6. Results (measured)

- **Main finding (negative):**
  - The pixel models reproduce the weak rules' bias. More detected change
    means a window is *less* likely to be man-made (held-out AUROC 0.355).
  - No model clearly beats the rule baseline. Test F1 is 0.20 for the ground
    model against 0.17 for the rules, with overlapping CIs.
  - Block-CV F1 is 0.14, the same as flagging every window.
  - A fine-tune corrected with the human labels did not help.
- **Second-stage window context filter:** fitted only on train-block
  reviews.
  - Held-out AUROC 0.761 (CI 0.637–0.863).
  - Natural false-alarm rate 5.5%, against 74% without it.
  - It ranks the detection register rather than deleting detections.
- **Leave-one-city-out:** the cross-city pixel model equals flagging every
  window; training in the target city does not help; the context filter
  does not transfer between cities (Mumbai → Delhi AUROC 0.60, CI includes
  0.5), so it needs reviewed windows from each city.
- **Quantization** (ONNX Runtime static INT8, Conv-only, percentile
  calibration), ground model:
  - Size: 4.50 → 1.21 MiB.
  - Latency: 1.25–1.5× faster per 256 px window.
  - Window-level F1 equivalent within noise; 99.9% pixel agreement with FP32.
  - Keeps 90% of the smallest changes (85% with MinMax calibration). The
    register still uses FP32.
  - INT8 gives no speed-up for the onboard model on this CPU; its
    small-change retention rose from 76% to 87%.
- **Outputs:**
  - Full-tile change maps for both cities.
  - Detection register: 86,982 detections, 831 in context-flagged cells.
  - Evidence panels for accepted, rejected, missed and uncertain cases.

## 7. Historical OSM

The resumable Overpass downloader (`src/utils/historical_osm.py`) is
implemented and tested. A bug fix keeps a tile's features when one element
fails to convert. The extraction was **dropped**: after about 2.5 hours the
public service had returned only 26 responses (Mumbai 2022-12-23). **No
result uses OSM.** The cache under `data/labels/osm/` allows resumption
(D-003).

## 8. Reproducibility

- Pinned CPU dependencies (`requirements.txt`).
- Product checksums and a frozen split manifest.
- Versioned configs (`configs/`) and seed 20261118 throughout.
- Every run's config and history (`outputs/experiments/runs/`).
- 54 unit tests.
- Committed artifacts: both models (PyTorch, ONNX FP32, ONNX INT8),
  quantization reports, FP32 change-class maps, context grids, the register
  CSV, the priority GeoPackage and the evidence panels. Larger rasters are
  regenerated with the README commands.
- Decisions D-001 to D-019 are in the decision log.
- AI usage: `AI_USAGE.md` and `docs/ai_usage/` hold the full Copilot and
  Claude Code transcripts.

## 9. Limitations and next steps

- One pair per city; one reviewer; 24 positive windows (10 held-out), so
  all intervals are wide.
- Candidate windows were enriched by a change index, so the rates are not
  area-wide.
- About 26–32% of each tile is ambiguous under the rules.
- 10 m pixels cannot resolve individual buildings.
- Highest-value next steps:
  - more reviewed positives, with date-matched high-resolution references;
  - a third date per city, to separate crop cycles from construction;
  - training the pixel model against reviewed context instead of the weak
    rules;
  - benchmarks on real onboard hardware.

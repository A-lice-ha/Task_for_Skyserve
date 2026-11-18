# Decision log

Each entry records the decision, the alternatives considered, the reason, and
the evidence or experiment behind it. Earlier preprocessing decisions are
detailed in the phase documents and summarised here so the log is complete.

| ID | Date | Area |
|---|---|---|
| D-001 | 2026-11-17 | Atmospheric correction |
| D-002 | 2026-11-17 | Registration |
| D-003 | 2026-11-18 | Historical OSM role and downloader fix |
| D-004 | 2026-11-18 | Label strategy: weak labels + blind human review |
| D-005 | 2026-11-18 | Weak-label rules |
| D-006 | 2026-11-18 | Ground model output classes |
| D-007 | 2026-11-18 | Spatial splits |
| D-008 | 2026-11-18 | Blind review protocol |
| D-009 | 2026-11-18 | Evaluation unit and metrics |
| D-010 | 2026-11-18 | CPU training configuration and search |
| D-011 | 2026-11-18 | Quantization method |
| D-012 | 2026-11-18 | AI usage record |
| D-013 | 2026-11-18 | Human-corrected fine-tune (train-block reviews) |
| D-014 | 2026-11-18 | Block-grouped cross-validation instead of more retraining folds |
| D-015 | 2026-11-18 | Final model selection and deployed precision |
| D-016 | 2026-11-18 | Second-stage window context filter for prioritisation |
| D-017 | 2026-11-18 | Percentile calibration replaces MinMax for INT8 |
| D-018 | 2026-11-18 | Leave-one-city-out stress test |
| D-019 | 2026-11-18 | Independent registration check; registration no-op bug |

---

## D-001 Atmospheric correction: Sen2Cor 2.13.02 to L2A surface reflectance

- **Decision:** process all four L1C products with Sen2Cor via subprocess and
  build six-band (B02, B03, B04, B08, B11, B12) 10 m reflectance stacks using
  the metadata offsets and gains.
- **Alternatives:** TOA reflectance with relative radiometric normalisation;
  other atmospheric processors.
- **Reason:** the pairs span different seasons and solar zenith (Mumbai
  46.5° → 17.1°), so surface reflectance reduces acquisition-driven spectral
  differences. Sen2Cor also provides SCL masks.
- **Evidence / limits:** [PHASE_2_PREPROCESSING.md](PHASE_2_PREPROCESSING.md).
  SCL did not capture the diffuse haze seen in the 2026-10-22 Delhi scene
  (handled in D-005).

## D-002 Registration with stable features; ~1 px residual accepted and modelled

- **Decision:** register each later date to its earlier date; keep a 15 m
  RANSAC comparison; treat ~1 px residual as a known error source.
- **Evidence:** registration CSVs in `outputs/registered*/`; the user checked
  points visually (Google hybrid) and found about 1 pixel after co-registration
  for both pairs.
- **Consequence:** downstream rules and evaluation must not count a one-pixel
  edge shift as change (D-005).
- **Revised by D-019:** an independent check measured 0.10 px (Delhi) and
  0.33 px (Mumbai) median residual, and found that the warp had not been
  applied. The ~1 px assumption was conservative.

## D-003 Historical OSM is supporting evidence only; per-element conversion failures

- **Decision:** download dated OSM snapshots (Mumbai T1/T2, Delhi T1) with the
  resumable Overpass downloader, and use them only as supporting context, never
  as labels or model inputs.
- **Bug found and fixed (2026-11-18):** one unconvertible route relation (Mumbai
  Metro Line 3) raised inside `osm2geojson` with `raise_on_failure=True`, which
  discarded every feature of that tile/category (855 Construction features in
  the first affected tile) and recorded the tile as failed. Conversion now
  drops only the failing element and lists it in `skipped_elements.json`.
  Verified by replaying the cached responses offline and by a unit test.
- **Outcome (2026-11-18): extraction dropped.** After about 2.5 hours the
  public Overpass service had returned only 26 responses (Mumbai 2022-12-23
  only), mostly blocked on the building/infrastructure category. Given the
  deadline, the project owner chose to stop it. No reported result uses OSM;
  change types come from the rules in D-006 and from human review. The
  downloader and its cache remain for later resumption.

## D-004 Labels: automatic weak training labels + blind human review for evaluation

- **Context:** on 2026-11-18 no labels existed (all 306 candidate windows
  unreviewed, label layers empty) and the deadline was the same day.
- **Options considered:** (a) full manual polygons in QGIS; (b) weak labels
  only; (c) weak labels for training plus a fast human review of candidate
  windows used as the only evaluation reference.
- **Decision:** (c), chosen by the project owner. The assignment forbids
  treating an automatic difference map as ground truth, so weak labels are
  used only for training; every reported metric uses the human review.
- **Consequence:** models learn the weak-label rules' biases; the reviewed
  windows measure how far that matters. Pixel-level localisation is measured
  only as a proxy (D-009).

## D-005 Weak-label rules (configs/weak_labels.json)

Codes: stable, built gain, built loss, vegetation change, water change,
ambiguous, invalid. Thresholds are assumptions to be evaluated, not
calibrated values.

1. **Registration tolerance.** Built-up gain requires
   `NDBI_after − max₃ₓ₃(NDBI_before) ≥ 0.10` (and symmetrically for loss), so
   a one-pixel shift of an existing edge cannot produce change. Unit-tested.
2. **Dry-season vegetation.** Gain on land vegetated at T1 (NDVI ≥ 0.40) is
   ambiguous: senescence and clearing for construction look alike in a
   December → May Mumbai pair.
3. **Regional haze.** A 31×31 px (≈310 m) mean blue increase ≥ 0.035 marks all
   pixels ambiguous. Evidence: a hazy Delhi window had median ΔB02 = 0.048
   (p90 0.090) versus 0.001 in a clear window; averaging keeps compact new
   bright roofs from triggering it. This removed spurious outlines across
   hazy farmland seen in review chips.
4. **Natural changes as hard negatives.** |ΔNDVI| ≥ 0.20 → vegetation change;
   |ΔMNDWI| ≥ 0.30 → water change (tides, turbidity, water level). Both count
   as no-change for training.
5. **Small blobs.** Gain/loss components < 4 px (400 m²) become ambiguous.
6. **Screened water.** Mumbai persistent offshore water (review-area raster
   = 0) is invalid.

Ambiguous and invalid pixels are ignored in training (`ignore_index=255`).

## D-006 Ground model: 3 output classes, change types assigned after detection

- **Decision:** train the Siamese U-Net with 3 classes (no change incl.
  natural, built-up gain, built-up loss). Types are assigned per detection:
  loss → demolition; gain over T1 water → bridge/reclamation; elongated gain →
  road; other gain → construction. `infrastructure_other` and `uncertain` come
  only from human review.
- **Alternative:** the 7-class head from Phase 3.
- **Reason:** spectral weak labels cannot separate road, bridge and other
  infrastructure, so a 7-class head would learn labels that do not exist in
  the training data. Shape and context rules are transparent and are recorded
  per detection (`type_basis`).
- **Onboard model:** unchanged binary (change / no change) on 4 bands.

## D-007 Spatial splits: whole 10 km blocks, 50/25/25, 2 km training buffer, zero-shot blocks

- **Decision:** seeded assignment of eligible blocks (≥ 25 km²) per city to
  train/val/test at 50/25/25; one zero-shot block per city (Navi
  Mumbai/Panvel, Noida), verified inside the footprints. Training patches come
  only from train-block cores: train blocks minus a 2 km buffer around every
  non-train block (unit-tested).
- **Deviation from Phase 4 plan (70/15/15):** training uses weak labels that
  cover every train block, so training area is abundant (≈3,880 km² of cores
  per city), while evaluation is limited by how many windows can be reviewed.
  Shifting blocks to val/test raises the number of reviewable evaluation
  windows (150 of 306 candidates fall in val/test/zero-shot blocks).
- **Limit:** one temporal pair per city, so held-out blocks test geographic,
  not temporal, generalisation.

## D-008 Blind review protocol

- Review page shows only the two dates (T1, T2) with the weak-label outline,
  which can be toggled off; how a window was selected (ranked vs random) and
  its score are hidden; order is shuffled with a fixed seed; windows in
  val/test/zero-shot blocks come first.
- Classes: construction, demolition, road, bridge, infrastructure_other,
  no_change, natural_vegetation (incl. agriculture), natural_water (incl.
  tide, erosion, deposition), acquisition_artifact (haze, cloud, shadow,
  misregistration), uncertain. Plus outline quality (correct / partial /
  wrong / none) and confidence (high / medium / low).
- **Agriculture** is non-target (natural_vegetation) unless converted to
  built surfaces.
- No post-T2 evidence may decide a label; undecidable windows are uncertain.

## D-009 Evaluation unit: reviewed window; threshold chosen on validation only

- A detector flags a window when its change pixels (components ≥ 4 px) reach
  k; k is chosen on validation windows from a fixed grid and frozen for test
  and zero-shot. Precision/recall/F1 with seeded bootstrap 95 % CIs; natural
  false-alarm rate on held-out windows labelled natural/acquisition;
  direction agreement (gain vs loss); uncertain windows reported separately,
  never scored.
- **Localisation:** pixel IoU against the weak outline only where the reviewer
  marked that outline "correct", reported explicitly as a proxy, because no
  independent polygons exist.
- **Baseline:** the rule-based weak labels themselves (no learning), scored
  identically. A model must beat the rules it was trained on to justify
  itself, mainly by rejecting natural/acquisition changes.

## D-010 CPU training configuration and reproducible search

- Hardware: AMD Ryzen 5 7530U (6C/12T), 15.3 GiB RAM (~4 GiB free during
  work), no GPU. Ground U-Net widths reduced from (32, 64, 128, 256) to
  (16, 32, 64, 128): 1.18 M parameters, ~100–150 s per epoch on 1,923 patches
  of 96 px. Patch bank: 50 % weak-change patches, 50 % uniform, seed
  20261118.
- Augmentation: rot90/flip and ±5 % independent per-date, per-band gain
  (simulates acquisition/atmospheric differences).
- Search (`src/training/hparam_search.py`, 6 epochs each, fixed seed):
  loss ∈ {CE, weighted CE, weighted CE + Dice} × lr ∈ {1e-3, 3e-3}; then a
  no-jitter ablation of the best; then the onboard model at two learning
  rates. Plain CE serves as the learned baseline. Weak-validation F1 is used
  only to rank runs; the final choice is confirmed on reviewed validation
  windows.

## D-011 Quantization: ONNX Runtime static INT8, Conv-only, UINT8 activations

- **Variants compared** (smoke model, 40 windows, 1 thread): quantize every op
  (QDQ, INT8 activations) → 392 ms, slower than FP32 (339 ms); Conv-only with
  UINT8 activations → 251 ms (QDQ) / 249 ms (QOperator); percentile
  calibration gave lower FP32 agreement. All were ~1.2 MiB vs 4.5 MiB.
- **Decision:** Conv-only QDQ, UINT8 activations (post-ReLU activations are
  non-negative, so the full range is used), per-channel INT8 weights, MinMax
  calibration on 64 train-split patches. The temporal-difference ops
  (Sub/Abs), normalisation and softmax stay FP32, because the change signal is
  a small difference between two similar feature maps.
- The variant sweep is re-run on the final models and stored in
  `outputs/models/<run>/quantization.json`. QAT is not attempted unless PTQ
  loses unacceptable sensitivity.
- **Superseded in part by D-017:** calibration changed from MinMax to
  percentile.

## D-012 AI usage record generated from local session logs

- `src/utils/export_ai_usage.py` exports every Copilot and Claude Code session
  whose working directory is this repository: full prompts and replies in time
  order with model names, and tool calls and results truncated with markers.
  Injected system context and e-mail addresses are removed. Unrelated
  projects' sessions on the same machine are excluded.

## D-013 Human-corrected fine-tune using reviewed train-block windows

- **Finding that triggered it:** on the 306 reviewed windows, change-pixel
  count ranks windows *worse than chance* for every method (held-out AUROC
  0.34–0.39). Agricultural and haze windows produce more detected change than
  real construction; the models reproduce the weak rules' bias.
- **Decision:** one pre-fixed fine-tune (`configs/training/ground_wce_dice_hf.json`:
  3 epochs, lr 3e-4, last epoch kept) of `ground_wce_dice_final` on the
  original patch bank plus 1,095 crops from the 122 human-reviewed windows in
  **train blocks only** (natural/artefact/no-change windows → all pixels
  no-change; positive windows keep the weak outline;
  `src/training/reviewed_patches.py`). No validation/test/zero-shot review
  was used for training or for choosing this recipe.
- **Why "last epoch":** weak-validation F1 rewards exactly the agricultural
  false positives that the correction removes, so it cannot select epochs here.
- **Result:** median detected change fell on artefact (576 → 266 px) and
  water (1,188 → 444 px) windows but also on true positives (322 → 206 px);
  held-out AUROC 0.345 → 0.381, test F1 0.196 → 0.185 — no meaningful gain.
  Reported as a negative result; 108 reviewed windows are too few to
  re-teach the spectral distinction between crop cycles and construction.

## D-014 Block-grouped cross-validation of the window decision, not retraining folds

- **Question raised:** with little labelled data, is n-fold (temporal)
  training/tuning better than one fixed split?
- **Temporal folds are not possible:** each city has exactly one image pair.
  The nearest substitute is leave-one-city-out, which confounds city, season,
  interval and atmosphere.
- **The scarce resource is human-labelled positives (24 windows; 2 in
  validation), not training data** (weak labels cover every train block).
  Retraining per fold (~15–20 min per fold per model on this CPU) would not
  add any labelled positives.
- **Decision:** keep the frozen 50/25/25 block split for training, and add
  5-fold cross-validation **grouped by 10 km block** over all decided held-out
  windows (val + test + zero-shot; blocks no model trained on). k is selected
  on four folds and applied to the fifth; out-of-fold predictions give P/R/F1
  with bootstrap CIs. The val-only protocol (D-009) is kept and reported
  unchanged; the grouped CV is reported alongside as the more stable estimate.

## D-015 Final model selection and deployed precision

- **Ground:** `ground_ce_final` — highest validation F1 under the
  pre-registered rule (D-009). The margin over the other runs is one window
  and is not meaningful; it also had the best gain/loss direction agreement
  (6/6 held-out true positives).
- **Onboard:** `onboard_final` (only completed onboard configuration).
- **Precision:** the ground segment register uses **FP32** because INT8
  retained only 85 % of small (≤ 10 px) FP32 detections (90 % after
  D-017; still a loss). INT8
  (`model_int8.onnx`, Conv-only QDQ) is the deployable artifact for
  constrained hardware: 3.7× smaller and 1.25–1.5× faster on one thread,
  with window-level test F1 equivalent within noise. For the onboard model INT8 gives no speed-up
  on this CPU (already small; quantize/dequantize overhead dominates), so FP32
  ONNX is recommended unless the target accelerator needs integer kernels.

## D-016 Second-stage window context filter, used to prioritise, not to delete

- **Decision:** add a logistic-regression window filter
  (`src/evaluation/window_filter.py`) on nine features of the change map and
  the spectral context of changed pixels; fixed settings, fitted only on
  decided reviewed windows in train blocks, threshold from those windows,
  evaluated once on held-out windows. Applied tile-wide on a 2.56 km grid
  (`src/inference/context_score.py`) and attached to every detection
  (`context_score`, `context_flag`, `priority_score`).
- **Evidence:** held-out AUROC 0.761 [0.637, 0.863] vs 0.355 for change-pixel
  count; AP 0.176 vs prevalence 0.082; natural false-alarm rate 0.055 vs
  0.736. Detections in flagged cells: 47 % in accepted windows vs 3.5 %
  elsewhere (held-out reviewed windows).
- **Why prioritise rather than filter:** the train-chosen threshold kept only
  1 of 10 held-out positive windows; with 10 held-out positives no reliable
  operating point can be set, so the full register is kept and ordered.
- **Limit found later (D-018):** the filter does not transfer between
  cities; it needs reviewed windows from each deployment city.
- **Alternative not taken:** inverting the pixel-count score after seeing
  held-out AUROC < 0.5 would have used test information.

## D-017 Percentile calibration replaces MinMax for INT8

- **Decision:** default quantization variant `qdq_conv_u8_pct` (QDQ,
  Conv-only, UINT8 activations, per-channel INT8 weights, percentile
  calibration 0.001–99.999) for both final models.
- **Evidence (final models, all 306 windows):** small-change retention
  0.850 → 0.896 (ground) and 0.762 → 0.874 (onboard); ground change IoU
  with FP32 0.827 → 0.892. Latency was unchanged in a same-session sweep
  (125 vs 128 ms on one thread). MinMax results are archived in
  `outputs/experiments/quantization/`.
- **Selection rule:** fidelity to the FP32 model (pixel agreement and
  small-change retention), which needs no human labels. Detection metrics
  were reported, not optimised. Ground INT8 test F1 moved 0.200 → 0.192 (two
  more flagged natural windows), which is inside the confidence interval.
- **Not changed:** the ground register stays FP32 (D-015); INT8 still
  misses about 10 % of the smallest detections.

## D-018 Leave-one-city-out stress test

- **Decision:** add leave-one-city-out runs (`ground_ce_loco_train_<city>`,
  same recipe as `ground_ce_final`, trained and normalised on one city) as
  the closest available substitute for temporal folds. Thresholds and the
  context filter are chosen only on source-city reviews
  (`src/evaluation/loco.py`).
- **Result:** Mumbai → Delhi pixel model F1 0.215 [0.139, 0.290], equal to
  flagging every window; AUROC 0.30. On Delhi held-out windows the in-city
  model is no better than the Mumbai-trained one. The context filter
  transfers weakly Mumbai → Delhi (AUROC 0.60, CI includes 0.5) and not at
  all Delhi → Mumbai (0.46). Delhi → Mumbai has only 4 target positives.
- **Consequence:** the D-016 filter is city-specific; deploying it in a new
  city requires reviewed windows from that city. D-014's choice not to
  retrain per fold is unchanged: retraining moved no metric outside its
  confidence interval.

## D-019 Independent registration check; registration step was a no-op

- **Check:** `src/preprocessing/registration_check.py`.
  - Sub-pixel phase correlation (B04 and B08 must agree within 0.5 px;
    method RMSE 0.12 px on synthetic shifts).
  - Run on 64 px stable, textured, fully valid windows: 695 checkpoints in
    Delhi and 230 in Mumbai.
- **Result:** median residual 1.0 m in Delhi and 3.3 m in Mumbai; P90 1.6 /
  4.5 m; maximum 3.8 / 6.0 m; no checkpoint above 1 px. The Mumbai residual
  is mostly a constant ~3 m offset.
- **Bug:** the written "registered" rasters equal the unregistered stacks.
  `reproject(source=rasterio.band(...), src_transform=...)` uses the file's
  own transform and ignores `src_transform` (rasterio 1.4.3; reproduced in
  isolation). Fixed in `phase2.warp_block`, with a regression test.
- **Effect:** applying the fitted transforms correctly changes the median
  shift by +0.12 m (Delhi) and −0.47 m (Mumbai); the 15 m fit is an
  identity. Results were not regenerated: all were computed on the
  unwarped alignment, whose accuracy is now measured. Re-running
  `phase2.py register` with the fixed code gives rasters that differ by
  at most 0.5 m.
- **Alternatives considered:** regenerating the registered rasters and
  every downstream step (weak labels, patches, training, evaluation, maps)
  for a sub-0.05 px median change. Rejected as not worth the risk on the
  deadline; recorded here instead.

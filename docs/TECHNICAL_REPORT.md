# Man-Made Change Detection from Sentinel-2 Imagery

**Technical report — submission version, 2026-11-18.**
All numbers are measured unless marked as a plan or limit. Detailed results:
[PHASE_5_EVALUATION.md](PHASE_5_EVALUATION.md); decisions:
[DECISION_LOG.md](DECISION_LOG.md); reproduction: [README](../README.md).

## Executive summary

- **Data.** Two same-tile Sentinel-2A pairs: Mumbai T43QBB 2022-12-23 →
  2023-05-12 (140 days) and Delhi T43RGM 2023-10-06 → 2026-10-22 (1,112
  days). Processed to L2A with Sen2Cor into six-band 10 m stacks. An
  independent check on stable features measured residual misalignment of
  0.10 px (Delhi) and 0.33 px (Mumbai) median, max 0.6 px. It also found that
  the registration step had been a no-op because of a rasterio pitfall (fixed;
  effect ≤ 0.5 m). Delhi's 2026 scene has regional haze that the SCL mask misses.
- **Labels.** No labels were supplied. Training used rule-based weak labels
  (built-up gain/loss with a one-pixel registration tolerance, haze and
  vegetation ambiguity rules). Evaluation used a **blind human review of all
  306 candidate windows**: only 24 (8 %) contain man-made change, 62 are
  undecidable, the rest are agricultural, water or acquisition effects.
- **Models.** Ground: Siamese U-Net, 1.18 M parameters, 3 classes (no change,
  built-up gain, loss), change types assigned per detection by transparent
  rules. Onboard: Siamese MobileNetV3-Small, 0.28 M parameters, 4 bands,
  binary. CPU-only training with a 9-run search over loss × learning rate
  plus a jitter ablation; 10 km block splits with a 2 km buffer and a
  zero-shot block per city.
- **Main result (negative, measured).** The pixel models reproduce the weak
  rules' bias: the amount of detected change ranks windows **worse than
  chance** (held-out AUROC 0.355, 95 % CI 0.244–0.479), and no learned model is
  materially better than the rule baseline (test F1 0.20 vs 0.17, CIs fully
  overlapping; block-CV F1 0.14 vs 0.13, equal to flagging every window).
  A human-corrected fine-tune did not change this.
- **What works.** A second-stage window context filter fitted only on
  train-block reviews ranks held-out windows well (AUROC 0.761,
  CI 0.637–0.863; natural false-alarm rate 5.5 % vs 74 %). It is applied
  tile-wide to **prioritise** the detection register: 47 % of detections in
  flagged cells lie in reviewer-accepted windows vs 3.5 % elsewhere.
- **Leave-one-city-out.** Training in one city and testing in the other
  confirms the finding: the cross-city pixel model equals flagging every
  window, training in the target city does not help, and the context
  filter does not transfer between cities (Mumbai → Delhi AUROC 0.60, CI
  includes 0.5). The filter needs reviewed windows from each city.
- **Quantization.** ONNX Runtime static INT8 (Conv-only, percentile
  calibration): ground model 4.50 → 1.21 MiB and 1.25–1.5× faster per
  256 px window, with window-level F1 equivalent within noise and 99.9 %
  pixel agreement. It keeps 90 % of the smallest (≤ 10 px) FP32 detections,
  up from 85 % with MinMax calibration. The ground register uses FP32. INT8
  gives no speed-up for the already small onboard model on this CPU.
- **Deliverables.** Georeferenced full-tile change maps for both cities, a
  detection register (86,982 polygons with type, area ± uncertainty,
  confidence, context score and review status; 831 prioritised), evidence
  panels for accepted, rejected, missed and uncertain cases, original and
  INT8 model artifacts, configs, seeds, 54 unit tests and the AI usage record.

## 1. Objective, scope, and change classes

The objective is to detect and localize structural man-made changes between
observations while distinguishing them from natural surface variation and
acquisition effects. Candidate target classes are:

- construction / new building;
- demolition / removed building;
- new or extended road;
- bridge or other transport infrastructure change;
- other infrastructure change; and
- uncertain, when the imagery cannot establish the cause or class.

Seasonal vegetation, agriculture, water-level change, erosion/deposition,
cloud/shadow, and registration or illumination artifacts are not positive
structural-change labels. Agricultural activity is treated as non-target
unless independent evidence supports a structural infrastructure change; if
the cause remains ambiguous it is marked uncertain rather than forced into a
target class.

The unit of analysis is a registered pair of observations over their common
valid footprint. A two-date comparison can establish that the observed surface
differs between acquisitions, but not the exact date or permanence of a
change. Temporary changes and intermediate events may be missed.

## 2. Data audit and requirements

The detailed metadata record and audit method are in
[`PHASE_1_DATA_AUDIT.md`](./PHASE_1_DATA_AUDIT.md). Archive byte sizes and
SHA-256 checksums are recorded in
[`../data/manifests/sentinel2_products.csv`](../data/manifests/sentinel2_products.csv).

| Pair | Before → after | Tile / orbit | Interval | Main interpretation risk |
|---|---|---|---:|---|
| A | 2022-12-23 → 2023-05-12 | T43QBB / 5 | 140 days | Winter-to-spring vegetation and illumination change |
| B | 2023-10-06 → 2026-10-22 | T43RGM / 105 | 1,112 days | Long interval; temporary changes and intervening events are unobserved |

All products are Sentinel-2A L1C, descending orbit, in EPSG:32643. Baselines
are 05.10 except the 2026 product (05.13). Each contains 14 non-empty JP2
images (13 bands plus TCI), a `MSK_CLASSI_B00.jp2` classification mask, and
nine supplied quality reports with `PASSED` status. Product-level cloudy-pixel
assessments range from 0% to 0.0077%; these are metadata assessments, not
measured clear-area fractions.

The metadata gives 10 m, 20 m, and 60 m grids. The 10 m working grid is chosen
to retain the finest available output grid for building/road analysis; 20 m and
60 m bands remain intrinsically coarser after resampling. Use bilinear
resampling for continuous reflectance and nearest-neighbor for categorical
masks. Required band groups:

| Purpose | Bands |
|---|---|
| Visible/NIR structural features | B02, B03, B04, B08 |
| Red-edge, vegetation and SWIR features | B05, B06, B07, B8A, B11, B12 |
| Aerosol/cirrus context | B01, B09, B10 |
| Invalid-pixel screening | `MSK_CLASSI_B00`, `MSK_DETFOO_*`, `MSK_QUALIT_*` |

Useful indices include NDVI = (B08 − B04)/(B08 + B04), NDWI = (B03 −
B08)/(B03 + B08), MNDWI = (B03 − B11)/(B03 + B11), and NDBI = (B11 −
B08)/(B11 + B08). Nodata and near-zero denominators must be masked.

L1C data are top-of-atmosphere reflectance, not surface reflectance. The
products report quantification value 10,000 and per-band `RADIO_ADD_OFFSET`
-1,000. These values are retained as source metadata and are used internally by
Sen2Cor during L1C processing. The analysis stack is built from the L2A output
and uses its own `BOA_ADD_OFFSET` and `BOA_QUANTIFICATION_VALUE`; L1C offsets
must not be applied a second time to L2A pixels.

No reference labels were supplied. OSM features may provide dated contextual
evidence but are not ground truth without checking temporal coverage,
completeness, and positional accuracy. Date-appropriate high-resolution
imagery or authoritative construction records are preferable for label review.
A DEM is an optional additional input if terrain illumination or relief is
material; land-cover data may aid context but cannot substitute for labels.

The historical OSM downloader is implemented and documented in
[`HISTORICAL_OSM.md`](./HISTORICAL_OSM.md), but the extraction was **dropped**
for this submission: the public Overpass service returned only 26 responses
(Mumbai T1) in about 2.5 hours. No result uses OSM (D-003).

## 3. Preprocessing and registration

The Phase 2 workflow is implemented in
[`../src/preprocessing/phase2.py`](../src/preprocessing/phase2.py). The
reproducible preprocessing sequence is:

1. Read product metadata, band grids, quality reports, masks, and nodata.
2. Run Sen2Cor 2.13.02 on each L1C SAFE product using the supplied batch file.
   The wrapper invokes it with `subprocess`, redirects per-product logs, and
   uses Windows short paths because the batch file contains unquoted paths
   that otherwise break when the Windows user profile contains spaces.
   Standard output and errors are captured in a per-product log, with a
   30-minute subprocess timeout.
3. Convert L2A DN to reflectance using
   `(DN + BOA_ADD_OFFSET) / BOA_QUANTIFICATION_VALUE` from that L2A product's
   metadata. The implementation fails if the gain or any selected band's
   offset is missing; it does not silently assume a scale.
4. Resample selected continuous bands onto the product's B02 10 m grid and use
   the Sen2Cor scene classification layer for invalid pixels. Nodata,
   defective, cloud-shadow, unclassified, cloud, cirrus, and snow classes are
   excluded. A per-product valid-pixel mask is preserved.
5. Estimate a robust affine transform from ORB tie points per same-tile pair,
   warp the later image to the earlier image's grid, and record tie-point
   residuals, transform, and RMSE in CSV.
6. Intersect the two per-date validity masks and save the common-valid mask
   beside the registered image.

All four supplied products completed Sen2Cor processing and stack preparation.
Their SCL-derived valid areas are 99.9937% (12,055.286 km²) for T43QBB
2022-12-23, 89.2650% (10,761.823 km²) for T43QBB 2023-05-12, 99.9968%
(12,055.654 km²) for T43RGM 2023-10-06, and 99.9990% (12,055.923 km²) for
T43RGM 2026-10-22. The common-valid fractions are 89.2592% for the T43QBB
pair and 99.9958% for the T43RGM pair. These are SCL-mask coverage values,
not guarantees of cloud- or haze-free observations.
The common-valid mask also does not mean land-only: clear sea pixels are valid
under SCL and remain in the full-tile coverage denominator. The source mask is
preserved; land/AOI filtering is a separate labeling concern.

### Cloud/shadow coverage and atmospheric quality

Native 20 m SCL class counts show 0.0000% cloud/cirrus (classes 8/9/10) and
0.0000% cloud shadow (class 3) in the 2026-10-22 T43RGM scene; the earlier
2023-10-06 T43RGM scene has 0.0001% cloud/cirrus and 0.0000% shadow. The
2023-05-12 T43QBB scene has 5.4475% cloud/cirrus and 0.2372% shadow. The
2022-12-23 T43QBB scene has 0.0013% cloud/cirrus and 0.0000% shadow.

The user's RGB spot-check of the 2026-10-22 scene nevertheless shows a broad
diffuse veil consistent with haze or smoke-like contamination. The screenshot
does not distinguish smoke from haze, thin cloud, or display/stretch effects.
This confirms SCL's limitations for the observed scene: its nearly 100% valid
mask does not establish atmospheric clarity, and suspect areas should not be
used without visual review and conservative exclusion.

The user also reports visible haze over Delhi. The date/product for that
observation has not been specified, so it cannot yet be associated with an
individual scene's SCL or AOT statistics. Separately, visual checks against
Google Hybrid at several points in each image reportedly show 9–12 m
displacement. This is an approximate absolute-geolocation comparison against
a basemap, not survey-grade control or a pairwise registration measurement;
the basemap and selected points also have positional uncertainty. Similar
absolute offsets across dates do not demonstrate that relative alignment is
accurate. Independent stable control points are still required.

The user identifies the later T43RGM raster as
`S2A_MSIL2A_20261022T053241_N9999_R105_T43RGM_20261117T084830.SAFE.reflectance`
and reports a visual comparison of co-registered imagery in Delhi and Mumbai
with about one pixel of residual displacement in each location (approximately
10 m on the output grid). This qualitative assessment is not an independent
checkpoint measurement; locations, method, and uncertainty were not recorded.
It is a useful visual cross-check but does not supersede the tie-point residual
statistics or independent stable-point validation.

Metadata-scaled Sen2Cor AOT (10 m) has median/P98 values of 0.372/0.401 for
the earlier T43RGM scene and 0.419/0.443 for the later scene. The later
scene's median is about 12.6% higher. This is consistent with investigating
increased aerosol haze, but AOT is a processor estimate—not a ground-truth
smoke detector or a validated pass/fail quality score. The full per-scene
SCL/AOT/water-vapour results and interpretation are in
[`PHASE_2_PREPROCESSING.md`](./PHASE_2_PREPROCESSING.md).

Sen2Cor log totals measure 12:56, 13:43, 14:36, and 13:32 per scene (54:47
for all four, sequentially). Preparation of the four full-tile stacks is
estimated at 30–60 minutes, and registration of both pairs at 10–25 minutes;
these latter stages were not separately timed on the original run. The updated
workflow records per-item elapsed time for future non-skipped runs. Thus the
first Phase 2 run is budgeted at approximately 1 hr 35 min–2 hr 20 min on the
inspected CPU-only machine. See the Phase 2 document for assumptions and
caveats.

Run the implemented stages from the repository root:

```powershell
.\venv\Scripts\python.exe src\preprocessing\phase2.py sen2cor
.\venv\Scripts\python.exe src\preprocessing\phase2.py prepare
.\venv\Scripts\python.exe src\preprocessing\phase2.py quality
.\venv\Scripts\python.exe src\preprocessing\phase2.py register
```

By default, Sen2Cor products and logs go under `outputs/sen2cor/`, reflectance
stacks and an SCL/AOT/water-vapour quality summary under
`outputs/processed/`, and registration outputs under `outputs/registered/`.
The quality summary is reproducible and does not apply an unvalidated AOT
threshold. Per-item stage timings are appended to
`outputs/reports/phase2_timings.csv`; skipped outputs are recorded as skipped,
not as measured processing runs. Large derived rasters are ignored by Git.

Although products from a single MGRS tile usually have compatible map
coordinates, this does not prove pixel-level alignment. Residual shifts can
create bright/dark edges around buildings and roads that mimic change. The
supplied geometric-quality XML states `Absolute location is valid
(20.0<30.0)`. This is consistent with the user's approximately 20 m QGIS
observation, but the XML statement is an absolute-location check against a
30 m acceptance threshold, not pairwise relative registration accuracy.
Neither establishes that a pair is aligned well enough for change detection.
The workflow estimates relative alignment separately for each same-tile pair.
Measured registration results are:

| Pair (earlier → later) | Matches / inliers | Inlier ratio | RMSE / median residual | P90 / P95 / max residual | 4x4 inlier cells occupied |
|---|---:|---:|---:|---:|---:|
| T43QBB (2022-12-23 → 2023-05-12) | 386 / 299 | 77.5% | 10.30 / 1.44 m | 19.64 / 31.13 / 39.03 m | 9 / 16 |
| T43RGM (2023-10-06 → 2026-10-22) | 271 / 213 | 78.6% | 9.21 / 0.76 m | 1.06 / 31.47 / 38.67 m | 9 / 16 |

These residuals are calculated on the fitted tie points; no independent
check-point errors have been measured. RMSE near 9–10 m, large upper-tail
residuals, and incomplete 4x4 spatial coverage do not establish suitability
for building-scale analysis. The low median residuals are not sufficient
evidence of sub-pixel accuracy. Visual review of the SCL masks and aligned
before/after imagery, residual-map inspection, and independent stable check
points remain outstanding. A 10 m displacement can produce change-like edges
around buildings and roads; unsupported regions must be excluded or marked
uncertain.

The registration implementation was subsequently tightened: RANSAC now uses
a fixed seed (42) and a 15 m ground-distance tolerance converted to the
downsampled preview's pixel units, rather than a fixed 1.5 preview pixels. A
separate comparison run produced 267/386 inliers and 0.39 m fitted RMSE for
T43QBB, and 200/270 inliers and 0 m fitted RMSE for T43RGM; both still occupy
only 9/16 coarse cells. The alternative registered rasters and CSVs are in
`outputs/registered_15m/`; the prior registered TIFFs and CSVs were preserved.
Near-zero fitted residuals are not independent accuracy evidence. Review the
two outputs visually and obtain independent stable checkpoints before
adopting the revised transforms.

The implementation must be windowed: one full-tile 10 m float32 band is about
1.93 GB decimal, exceeding available free RAM on the inspected development
machine. Full-tile multi-band arrays are not a safe processing strategy here.

### Independent registration check on stable features (D-019)

The tie-point residuals above are measured on the same points used for the
fit. Registration was checked independently with
[`../src/preprocessing/registration_check.py`](../src/preprocessing/registration_check.py)
(outputs: `outputs/reports/registration_check.{json,md}` and
`registration_checkpoints.csv`):

- **Checkpoints:** 64 × 64 px windows on a 128 px grid that are fully
  valid, ≥ 90 % stable under the change rules, and textured (B08 standard
  deviation ≥ 0.02). These are existing built-up areas, roads and field
  boundaries.
- **Method:** each window is phase cross-correlated at sub-pixel precision
  (scikit-image, upsampling × 20, Hanning window) in B04 and B08
  separately. A checkpoint is kept only if the two bands agree within
  0.5 px. On synthetic shifts the method's error is RMSE 0.12 px.
- **Independence:** this is area correlation at full resolution, not
  feature matching on a preview, and uses hundreds of windows instead of
  about 300 points.

| Pair | Checkpoints (kept / candidates) | Mean offset dx, dy | Median | RMSE | P90 | Max | > 1 px | Coverage (4×4 cells) |
|---|---|---|---:|---:|---:|---:|---:|---|
| Delhi T43RGM | 695 / 702 | 0.25 m, −0.79 m | 1.0 m (0.10 px) | 1.1 m | 1.6 m | 3.8 m | 0 % | 14 / 16 |
| Mumbai T43QBB | 230 / 314 | −1.07 m, −3.01 m | 3.3 m (0.33 px) | 3.4 m | 4.5 m | 6.0 m | 0 % | 7 / 16 |

The supplied L1C products, processed to L2A, are already co-registered well
below one pixel. In Mumbai most of the residual is a constant offset of
about 3 m, northward in the later image; without it, RMSE is 1.3 m. Mumbai
coverage is limited to 7 of 16 cells because much of the tile is sea or
cloud-masked.

**Registration bug found by this check.** The rasters written by the
registration step are pixel-for-pixel identical to the unregistered L2A
stacks on every valid pixel.

- **Cause:** the warp passed `rasterio.band(...)` as its source. Rasterio
  1.4.3 then uses the file's own transform and silently ignores the fitted
  `src_transform`. Only the validity masks were applied.
- **Fix:** `phase2.warp_block` reads an ndarray block instead. A
  regression test checks that a 0.5 px shift is actually applied.
- **Effect of the fix,** measured with each fitted transform applied
  correctly on the same checkpoints:
  - the default fit changes the median shift from 1.00 to 1.12 m (Delhi,
    slightly worse) and from 3.25 to 2.78 m (Mumbai, slightly better);
  - the 15 m-tolerance fit changes nothing.
- **What the results used:** every result in this report was computed on
  the unwarped alignment measured above. Re-running everything for a change
  of at most 0.5 m was not justified. The registered rasters were therefore
  not regenerated.

### How the preprocessing limits were handled downstream

- **Residual misregistration** (assumed ~1 px from visual checks; measured
  0.10 px median in Delhi and 0.33 px in Mumbai, max 0.6 px): the as-written
  registered rasters, which equal the unregistered L2A stacks (D-019), are
  used throughout. Weak gain/loss requires the
  change to exceed the 3×3 neighbourhood maximum of the earlier date, so a
  one-pixel edge shift cannot create change (unit-tested); register areas
  carry ± one boundary pixel of uncertainty.
- **Haze missed by SCL:** a regional rule (31×31 px mean blue increase
  ≥ 0.035) marks hazy areas ambiguous for training; reviewers labelled 74
  windows `acquisition_artifact`; haze is one of the context-filter features.
- **Valid sea pixels:** persistent offshore water in Mumbai is excluded from
  weak labels and candidate scoring.

## 4. Model and deployment design

Two Siamese (shared-weight) architectures are implemented in
[`../src/models/siamese.py`](../src/models/siamese.py) and trained from
scratch (no pretrained weights exist for these six-band inputs):

| | Ground model | Onboard model |
|---|---|---|
| Architecture | Siamese U-Net, widths (16, 32, 64, 128); features of both dates and their absolute difference fused at each scale | Siamese MobileNetV3-Small (width 0.5), difference fusion, light decoder |
| Input | B02, B03, B04, B08, B11, B12 (10 m; 20 m bands resampled) | B02, B03, B04, B08 (native 10 m only) |
| Output | 3 classes: no change, built-up gain, built-up loss | change / no change |
| Parameters | 1,175,571 | 279,274 (< 5 M limit) |
| FP32 / INT8 size | 4.50 / 1.21 MiB | 1.13 / 0.73 MiB |
| Latency per 256 px window, 1 thread (FP32 / INT8, same session) | 151 / 121 ms | 18 / 21 ms |

Width was halved from the Phase 3 design (4.7 M parameters) to fit CPU
training on the development machine (AMD Ryzen 5 7530U, 6 cores / 12
threads, 15.3 GiB RAM with ~4 GiB free, no GPU). The 7-class head was
replaced with 3 classes because spectral weak labels cannot separate road,
bridge and other infrastructure; types are assigned per detection by
recorded rules (D-006).

**Deployment split.** Onboard: the 4-band binary model screens each pair
and downlinks only change-candidate tiles (on this CPU 6–8 ms per 256 px
window with 4 threads, i.e. roughly 15–20 s per 110 km tile; not measured on
flight hardware). Ground: the 6-band model, the context filter and the
register run on full pairs (~5 min per tile per city on the development
CPU), and analysts review detections in priority order. The onboard model is
weak (weak-validation F1 0.21). Its encoder downsamples 32×, so few-pixel
changes depend on the shallow skip features alone; before use as a screen it
would need a stronger high-resolution decoder or a lower threshold.

## 5. Labels, training, and hyperparameter tuning

**Labels (D-004, D-005).** Weak labels from versioned rules
([`../configs/weak_labels.json`](../configs/weak_labels.json)): built-up
gain/loss from ΔNDBI with a 3×3 registration tolerance; gain on previously
vegetated land, regional haze and components < 4 px are ambiguous (ignored);
vegetation and water changes are explicit no-change (hard negatives);
invalid and screened sea pixels are ignored. Weak labels are used only for
training. The evaluation reference is the blind review of all 306 windows
(D-008).

**Splits (D-007, D-014).** Whole 10 km blocks per city, 50/25/25
train/val/test (seed 20261118), a 2 km buffer between training cores and
every non-training block, and one untouched zero-shot block per city (Navi
Mumbai/Panvel; Noida). Temporal folds are impossible with one pair per city; leave-one-city-out
training is reported instead (§6, D-018).
Because human-labelled positives, not training data, are scarce, the window
decision is also cross-validated over 5 block-grouped folds of all held-out
windows instead of retraining per fold.

### 5.1 Training configuration (D-010)

| Item | Ground model | Onboard model |
|---|---|---|
| Parameters | 1,175,571 | 279,274 |
| Training data | 1,923 patches of 96 × 96 px from train-block cores (953 Mumbai, 970 Delhi); 50 % drawn from cells with weak change, 50 % uniform; seed 20261118 | same patches, 4 bands |
| Validation (for ranking runs) | 420 weak-labelled patches from validation blocks | same |
| Labelled pixels | 16.29 M no change, 156 k gain (0.95 %), 51 k loss (0.31 %); ambiguous/invalid ignored | gain + loss merged to "change" |
| Input normalisation | per-band mean/std of training patches (both dates) | same, 4 bands |
| Augmentation | rot90, horizontal flip, independent ±5 % gain per date and band | same |
| Optimiser | AdamW, weight decay 1e-4, one-cycle LR, batch 16 | same |
| Loss options | CE, inverse-√frequency weighted CE, weighted CE + Dice (`ignore_index` = 255) | weighted CE + Dice |
| Epoch selection | best weak-validation change F1 | same |
| Hardware | AMD Ryzen 5 7530U, 6 threads, no GPU | same |

### 5.2 Hyperparameter search

**Protocol.** The search is in
[`../src/training/hparam_search.py`](../src/training/hparam_search.py), and
results are written to
[`../outputs/experiments/hparam_search.csv`](../outputs/experiments/hparam_search.csv).
The patch bank and seed are fixed, every run lasts 6 epochs, and runs are
resumable. Per-run configs and epoch histories are in
`outputs/experiments/runs/`. Because no human labels existed during the
search, runs were ranked by **weak-validation change F1**, which measures
agreement with the rules, not accuracy. The final choice was made on
human-reviewed validation windows (5.3). The search took about 71 minutes of
CPU time, and the final runs another 38 minutes.

**Search space.** CPU time limited it to the factors expected to matter
most for this data:

1. Ground model: loss ∈ {CE, weighted CE, weighted CE + Dice} ×
   learning rate ∈ {1e-3, 3e-3} (6 runs).
2. Ablation: the best stage-1 configuration without radiometric jitter.
3. Onboard model: weighted CE + Dice at learning rate ∈ {1e-3, 3e-3}.

Fixed, not searched: network widths, batch size, weight decay, patch size,
optimiser and schedule.

| Run | Loss | lr | Jitter | Weak-val P | R | F1 | IoU | Best epoch | Train s |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ground_ce_lr0.001 | CE | 1e-3 | 0.05 | 0.541 | 0.276 | 0.365 | 0.223 | 5 | 720 |
| ground_ce_lr0.003 | CE | 3e-3 | 0.05 | 0.537 | 0.446 | 0.488 | 0.322 | 5 | 740 |
| ground_wce_lr0.001 | wCE | 1e-3 | 0.05 | 0.250 | 0.808 | 0.382 | 0.236 | 6 | 589 |
| ground_wce_lr0.003 | wCE | 3e-3 | 0.05 | 0.265 | 0.827 | 0.402 | 0.251 | 6 | 579 |
| ground_wce_dice_lr0.001 | wCE + Dice | 1e-3 | 0.05 | 0.328 | 0.757 | 0.457 | 0.296 | 6 | 520 |
| **ground_wce_dice_lr0.003** | wCE + Dice | 3e-3 | 0.05 | 0.392 | 0.700 | **0.502** | 0.335 | 6 | 435 |
| ground_wce_dice_lr0.003_nojitter | wCE + Dice | 3e-3 | 0 | 0.381 | 0.745 | 0.504 | 0.337 | 6 | 435 |
| onboard_wce_dice_lr0.001 | wCE + Dice | 1e-3 | 0.05 | 0.103 | 0.208 | 0.137 | 0.074 | 6 | 118 |
| **onboard_wce_dice_lr0.003** | wCE + Dice | 3e-3 | 0.05 | 0.132 | 0.232 | **0.168** | 0.092 | 6 | 118 |

**Findings.**

- **Learning rate:** 3e-3 beat 1e-3 for every loss and for both models.
  Most runs were still improving at epoch 6, so the final runs were trained
  longer.
- **Class imbalance:** change is about 1.3 % of labelled pixels.
  - Plain CE has the highest precision but the lowest recall.
  - Inverse-√frequency weights (0.10 / 1.05 / 1.85 for no change / gain /
    loss) roughly double recall but halve precision.
  - Adding Dice recovers precision and gives the best F1.
- **Radiometric jitter:** no measurable effect on weak validation
  (0.502 vs 0.504). It was kept because it targets acquisition differences
  that weak validation cannot measure, but the ablation does not support it.
- **Onboard model:** much weaker than the ground model at the same budget.

### 5.3 Final configuration and comparison with baselines

The two best ground configurations by loss family were retrained for 12
epochs. Weighted CE + Dice was the search winner. Plain CE was kept as the
**learned baseline**. The onboard winner was retrained for 25 epochs. All
use lr 3e-3 and jitter 0.05; configs are in `configs/training/`. The
selection rule, fixed before any human labels were seen, was the best
window-level F1 on human-reviewed validation windows, with k chosen there
(D-009).

| Model | Weak-val F1 | Human val F1 (k) | Test F1 (95 % CI) | Held-out AUROC |
|---|---:|---|---|---:|
| Rule baseline (no learning) | – | 0.085 (256) | 0.174 [0.043, 0.327] | 0.374 |
| **Ground CE final (selected)** | 0.555 | **0.089** (128) | 0.200 [0.046, 0.351] | 0.355 |
| Ground wCE + Dice final | **0.582** | 0.087 (256) | 0.196 [0.044, 0.340] | 0.345 |
| Onboard wCE + Dice final | 0.207 | 0.087 (64) | 0.192 [0.044, 0.333] | 0.350 |
| Ground wCE + Dice + human-corrected fine-tune (D-013) | 0.539 | 0.087 (128) | 0.185 [0.042, 0.327] | 0.381 |

`ground_ce_final` won under the rule, but the margin is one validation
window, and validation contains only 2 positive windows. In honest terms:

- Tuning clearly improved agreement with the weak labels (F1 0.365 →
  0.582).
- It did **not** produce a model better than the rule baseline on human
  labels; all confidence intervals overlap.
- Weak-validation F1 does not predict human-judged quality here. The
  search winner (wCE + Dice) and the learned baseline (CE) are equivalent
  on human labels.

The one experiment that changed outcomes on human labels was the
second-stage context filter (held-out AUROC 0.761 vs 0.355, D-016), not a
training hyperparameter. The final configuration is therefore justified by
these experiments, not by the choice of architecture alone.

### 5.4 Natural false positives

These were addressed in three ways:

1. Hard-negative weak labels: vegetation and water change are labelled as
   no change.
2. A human-corrected fine-tune on reviewed train-block windows. It brought
   no improvement (D-013).
3. The second-stage context filter (D-016). It is the only step that
   measurably separates natural from man-made change: held-out natural
   false-alarm rate 5.5 % vs 74 %.

The full per-run table with notes is in
[PHASE_5_EVALUATION.md §5.2](PHASE_5_EVALUATION.md).

## 6. Change analysis and validation

Unit of evaluation: the reviewed 2.56 km window (pixel-accurate polygons
were not feasible in the time available). k is chosen on validation and
frozen; uncertain windows are never scored; bootstrap 95 % CIs.

| Method | Test P / R / F1 (95 % CI) | Natural false-alarm rate | Held-out AUROC |
|---|---|---:|---:|
| Rule baseline (weak labels) | 0.100 / 0.667 / 0.174 [0.043, 0.327] | 0.698 | 0.374 |
| Ground U-Net (`ground_ce_final`) | 0.114 / 0.833 / 0.200 [0.046, 0.351] | 0.736 | 0.355 |
| Onboard (`onboard_final`) | 0.109 / 0.833 / 0.192 [0.044, 0.333] | 0.793 | 0.350 |
| Ground + context filter (all held-out) | 0.125 / 0.100 / 0.111 [0, 0.333] | **0.055** | **0.761** |

Gain/loss direction matched the reviewer on 6/6 held-out true positives.
Per-class metrics (road 7 and demolition 1 windows) are not meaningful and
are not claimed. The planned pixel-IoU proxy has no eligible windows,
because no reviewed outline was marked fully correct.

**Detection register** (`outputs/maps/detection_register_ground_fp32.csv`;
priority layer `outputs/maps/priority_detections_ground_fp32.gpkg`): per
detection, location (lat/lon and polygon in EPSG:32643), type and the rule
that assigned it, area ± one-boundary-pixel uncertainty, length for roads,
confidence and tier, context score, priority and window-level review
status. There are 86,982 detections (Mumbai 28,693; Delhi 58,289), of which
831 lie in context-flagged cells. Most detections are agricultural false
alarms, so the register must be read in priority order.

**Leave-one-city-out (D-018).** The ground recipe was retrained on each
city alone. Thresholds and the context filter were chosen on that city's
reviews only, and the result was tested on the other city.

| Mumbai → Delhi (167 windows, 20 positive) | F1 (95 % CI) | Natural false-alarm rate | AUROC (95 % CI) |
|---|---|---:|---|
| Mumbai-trained pixel model | 0.215 [0.139, 0.290] | 0.944 | 0.301 [0.208, 0.402] |
| Rule baseline | 0.172 [0.098, 0.244] | 0.944 | 0.206 [0.114, 0.319] |
| Mumbai-fitted context filter | 0.252 [0.147, 0.353] | 0.573 | 0.602 [0.429, 0.759] |

The cross-city pixel model scores exactly what flagging every window
would. On the same Delhi held-out windows, the Delhi-trained model is no
better (AUROC 0.27 vs 0.41). The context filter does not transfer
reliably: Mumbai → Delhi gives AUROC 0.60 with an interval including 0.5,
and Delhi → Mumbai gives 0.46. Its in-city success (0.76) therefore
depends on reviews from the same city. Delhi → Mumbai has only 4
positive target windows and is close to uninformative. Details:
[PHASE_5_EVALUATION.md](PHASE_5_EVALUATION.md).

**Regions that cannot be assessed reliably.** Of the 10,980² px tiles
(12,056 km² each), the weak-label rules mark as ambiguous 3,860 km² in
Mumbai (32 %) and 3,151 km² in Delhi (26 %). This combines land vegetated
at T1 (where senescence and clearing look alike), regional haze and
sub-4 px blobs; the split between causes was not recorded. Another 1,372 km² of Mumbai is invalid
(cloud/shadow in the May 2023 scene and screened offshore water). The pixel
model still produces predictions there, and they should be treated as
unassessed. 62 of the 306 reviewed windows (20 %) were undecidable from the
two dates.

**What two dates can and cannot establish.** Two dates show that surface
reflectance differs consistently with a new or removed built surface. They
cannot date the change within the interval (140 days for Mumbai, 1,112 for
Delhi), show whether it is permanent, or reveal intermediate events. They
also cannot separate a cleared or fallow field from an early construction
site, which is the dominant error here. A third date, or a dense time
series, would separate crop cycles (which return) from construction (which
persists). Date-matched high-resolution imagery would be needed to confirm
building-scale type and extent.

**Rejected changes with evidence.** 6,916 detections fall in windows the
reviewer rejected (natural vegetation, water, acquisition artefact, no
change). Before/after panels for rejected, false-alarm, missed, accepted and
uncertain cases are in
[`../outputs/evidence/README.md`](../outputs/evidence/README.md).

## 7. Quantization

ONNX Runtime static post-training quantization (D-011, D-017): QDQ format,
Conv layers only, UINT8 activations, per-channel INT8 weights, percentile
calibration on 64 training patches. Normalisation, the temporal difference
and softmax stay FP32. Accuracy is measured on the same 306 windows, and
timings in fresh processes in the same session:

| Model | Size MiB | Params | Latency ms, 1 / 4 threads | Peak RSS MiB | Test F1 | Pixel agreement | Small-change retention |
|---|---:|---:|---|---:|---:|---:|---:|
| Ground FP32 | 4.50 | 1,175,571 | 151.3 / 82.9 | 398 | 0.200 | – | – |
| Ground INT8 | 1.21 | 1,175,571 | 120.8 / 58.4 | 399 | 0.192 | 0.9986 | 0.896 |
| Onboard FP32 | 1.13 | 279,274 | 18.4 / 8.3 | 296 | 0.192 | – | – |
| Onboard INT8 | 0.73 | 279,274 | 20.7 / 12.5 | 306 | 0.192 | 0.9960 | 0.874 |

These latencies were taken while other desktop applications were using the
CPU. On a quieter run the ground model took 124 ms (FP32) and 83 ms (INT8,
MinMax) on one thread. FP32 vs INT8 should be compared within one run:
INT8 is 1.25–1.5× faster for the ground model.

Calibration was first done with MinMax. Switching to percentile calibration
raised small-change retention (share of FP32 components ≤ 10 px still
detected) from 0.850 to 0.896 for the ground model and from 0.762 to 0.874
for the onboard model, with no change in speed. The choice used FP32
fidelity only, not human labels. Ground INT8 test F1 moved 0.200 → 0.192
(two more flagged natural windows), which is within noise.

Full tiles: FP32 vs INT8 pixel agreement 0.9987 / 0.9984 and change IoU
0.874 / 0.896 (Mumbai / Delhi; 0.798 / 0.834 with MinMax). Small-change sensitivity remains the main
cost, so the ground register uses FP32. INT8 gives no speed-up for the
already small onboard model on this CPU. QAT was not needed and was not
attempted.

## 8. Reproducibility, decisions, and AI usage

- Pinned dependencies ([`../requirements.txt`](../requirements.txt), CPU
  PyTorch), product checksums, a frozen split manifest, versioned rule and
  training configs (`configs/`), fixed seed 20261118 everywhere, per-run
  configs and histories (`outputs/experiments/runs/`), and 54 unit tests.
- Step-by-step commands and a 6 × 6 km demo are in the [README](../README.md).
  Committed artifacts: both models (PyTorch, ONNX FP32, ONNX INT8),
  quantization reports, window-filter parameters, FP32 change-class maps,
  context grids, the register CSV, the priority GeoPackage and the evidence
  panels. Larger derived rasters are regenerated by the listed commands.
- Decisions D-001 to D-019 with alternatives and evidence:
  [DECISION_LOG.md](DECISION_LOG.md).
- AI usage: [`../AI_USAGE.md`](../AI_USAGE.md) indexes the full,
  chronological transcripts of every GitHub Copilot and Claude Code session
  on this repository (`docs/ai_usage/`), exported from local session logs
  with injected system context and e-mail addresses removed (D-012).

## 9. Status and limitations

| Assignment area | Status |
|---|---|
| Data audit | Complete (metadata, checksums, SCL coverage, AOT, haze finding) |
| Preprocessing and registration | Complete; residual measured independently (0.10 / 0.33 px median); registration no-op bug found and fixed (D-019) |
| Onboard/ground architecture | Implemented, trained and quantized; onboard not run on flight hardware |
| Training and tuning | Complete: 9-run search, ablation, 3 final runs, human-corrected fine-tune, leave-one-city-out |
| Change analysis and validation | Complete on 306 reviewed windows; maps, register and evidence produced |
| Quantization | Complete for both models: size, latency, memory, accuracy, small-change sensitivity |
| Reproducibility and AI record | Complete |

Main limitations: one pair per city (no temporal generalisation test;
leave-one-city-out showed no cross-city transfer of the pixel model or
the context filter); one
reviewer, two-date 10 m interpretation and 24 positive windows (10
held-out), so all intervals are wide; candidate windows are index-enriched,
so rates are not area-wide; 10 m pixels cannot resolve individual
buildings. The most valuable next steps are more reviewed positives (ideally
with date-matched high-resolution references), a third date per city to
separate crop cycles from construction, and training the pixel model against
the reviewed context rather than the weak rules.

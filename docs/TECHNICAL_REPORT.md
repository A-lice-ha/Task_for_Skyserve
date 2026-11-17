# Man-Made Change Detection from Sentinel-2 Imagery

**Technical report — working document**  
**Status:** Requirements and data audit completed; implementation, experiments,
and results are in progress. This report distinguishes verified facts from
proposed methods. No change detections or model-performance results are claimed
yet.

## Executive summary

The supplied data comprise four Sentinel-2A Level-1C products. They support two
same-tile temporal comparisons: T43QBB from December 2022 to May 2023, and
T43RGM from October 2023 to October 2026. The two tiles are geographically
disjoint and must not be compared pixel by pixel.

The products contain the 13 spectral bands and pass the supplied metadata
quality reports. They do not include reference change labels. Reliable
comparison therefore still requires pixel-level quality assessment,
registration, radiometric assessment, and independently reviewed labels. The
development machine has no identified discrete GPU, so the initial workflow is
designed for windowed CPU processing. Model results, detections, and
quantization measurements will be added only after the corresponding work is
implemented and evaluated.

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

## 4. Model and deployment design

Two CPU-capable PyTorch architectures are implemented in
[`../src/models/siamese.py`](../src/models/siamese.py): a shared-weight,
six-band Siamese U-Net returning seven per-pixel semantic logits, and a
four-band, half-width MobileNetV3-Small Siamese network returning binary
change/no-change logits. The ground model uses a custom encoder rather than
ResNet/EfficientNet because the inputs are multispectral and no suitable
pretrained weights are supplied; the onboard model's first layer is adapted
to four channels and initialized without ImageNet weights. Architectural
tradeoffs and the class/band order are recorded in
[`PHASE_3_ARCHITECTURE.md`](./PHASE_3_ARCHITECTURE.md).

On the inspected CPU-only development machine, batch-1 256×256 synthetic
patch profiling with four CPU threads measured 4,696,743 parameters,
40.35 GFLOPs, 0.246 s first-forward latency, and 455.3 MiB sampled process RSS
for the ground model; the onboard model measured 279,274 parameters,
0.118 GFLOPs, 0.025 s, and 241.3 MiB sampled process RSS. These are proxy
measurements, not target-device benchmarks or sustained throughput; the
onboard <5M parameter limit is met, but its power, memory behavior, and
latency remain unverified on an actual edge device. Neither model has been
trained, calibrated, or evaluated for accuracy.

Observed development system: Windows 11; AMD Ryzen 5 7530U, 6 cores / 12
logical processors; 15.3 GiB RAM (about 4.6 GiB free when inspected); AMD
integrated Radeon graphics; no discrete CUDA-capable GPU identified; about
95.8 GiB free on C:. The initial training/inference baseline is CPU-only.
Batch size, patch size, model size, and runtime targets must be established
empirically on this hardware before any training commitment.

## 5. Labels, training, and tuning

Training cannot be claimed until labels are reviewed. A windowed, masked
dataset reader for human-annotated six-band patches is implemented in
[`../src/training/dataset.py`](../src/training/dataset.py). Unannotated and
invalid pixels are ignored rather than assumed to be no-change, and
conflicting overlapping labels fail explicitly.

A QGIS-ready GeoPackage at
[`../data/labels/metro_label_grids.gpkg`](../data/labels/metro_label_grids.gpkg)
contains 144 indexed 10 km blocks and an empty detailed-polygon annotation
layer for each city. The grids follow the full T43QBB (Mumbai) and T43RGM
(Delhi) raster footprints and have not yet been clipped to verified municipal
study-area boundaries or assigned to spatial splits. Each block's common-valid
percentage is SCL-mask coverage, not an atmospheric quality assessment.
The label layers include pair, block, split, taxonomy, confidence, annotator,
and evidence-date fields; an additive schema update preserved the empty
annotation layers.

Initial unreviewed candidate GeoJSONs are generated by
[`../src/training/generate_candidates.py`](../src/training/generate_candidates.py):
[`../data/labels/candidates/mumbai_candidates.geojson`](../data/labels/candidates/mumbai_candidates.geojson)
contains 135 review windows (108 index-ranked, 27 random below-threshold), and
[`../data/labels/candidates/delhi_candidates.geojson`](../data/labels/candidates/delhi_candidates.geojson)
contains 171 (114 ranked, 57 random). Each feature is a 256×256-pixel review
window, not an object polygon or assigned label. Random below-threshold
windows record per-block inclusion probabilities; ranked windows are
deterministic. All candidates have unassigned splits and null classes. These
enriched candidates are for review/training-candidate
discovery, not unbiased test metrics or ground truth.
The Mumbai candidate generator now screens persistent-water interiors
(MNDWI > 0 and NDVI < 0.2 in both dates) more than 500 m from non-water,
while retaining shoreline buffers and pixels matching the water-to-impervious
review rule. The 135 Mumbai windows remain candidate-only; their review-area
fractions range from 0.8135 to 1.0 (median 0.9955). The screen marks 0.64% of
the full tile as persistent offshore-water interior; this is a heuristic
screened fraction, not an estimate of total sea area. It only changes candidate
scoring/selection; it does not alter the common-valid mask or prove that
retained water transitions are bridges. The Mumbai GeoJSON records the filter
and per-window review-area fraction for QGIS review. A companion
`outputs/processed/mumbai_candidate_review_area.tif` is written on the 10 m
grid (1=retained review area, 0=screened stable offshore water) for optional
QGIS overlay; it is not a land classification or training mask.

The planned label format records `change_category`, `change_class`,
`non_target_subtype`, `confidence`, `annotator`, `notes`, pair dates, city,
spatial block, split assignment, and evidence date. The label
protocol prohibits post-T2 reference information from deciding a label; use
historical OSM only at or before T2. If the two Sentinel dates do not resolve
the cause, use uncertain or leave unlabeled. Without independent date-matched
high-resolution references, evaluation measures agreement with human-
interpreted Sentinel-2 labels, not independent ground-truth accuracy.

The spatial plan uses approximately 10 km macro-blocks, whole-block
conventional splits with a 2 km no-use buffer, and a separate untouched
geographic test AOI in each city. Candidate-enriched samples must not be used
as test samples; use exhaustive test coverage or probability sampling with
known inclusion weights. A 10×10 km block may be too large for exhaustive
manual labeling, so a smaller test AOI may be defined and recorded within it.

The acquisition metadata gives T43QBB a 140-day pair (2022-12-23 to
2023-05-12) and T43RGM a 1,112-day pair (2023-10-06 to 2026-10-22), consistent
with the Mumbai and Delhi footprints respectively. Each city has only one
temporal pair. Thus block holdouts test geographic generalization within
these acquisitions, not temporal generalization; city, season, interval,
atmosphere, and registration conditions are confounded. Leave-one-city-out
is a confounded stress test, not a temporal test. The Mumbai pair's mean solar
zenith changes from 46.52° to 17.09°; Delhi's changes from 36.12° to 41.49°.
Vegetation/harvest, shadow/BRDF, water/tide, and haze are plausible
confounders, not pixel-level explanations without review.

At 10 m with about one-pixel residual displacement, narrow roads, bridge
decks, and individual buildings may be mixed within pixels. Retain requested
classes for annotation if useful, but report instance counts and avoid strong
per-class claims for rare or sub-pixel classes. The seven-class model head
currently has no separate non-target subclasses; reconcile the taxonomy
before training and never silently map vegetation, agriculture, tides, shadow,
or haze to no-change. Confidence and non-target fields are not yet consumed by
the current dataset reader or loss.

For the seven-logit model, the appropriate masked baseline is multi-class
cross-entropy (`ignore_index=255`) with optional multi-class Dice, not binary
BCE. Use training-only normalization statistics and treat independent
per-image percentile clipping as an ablation. OSCD is the closest listed
Sentinel-2 pretraining source but needs geographic-overlap checks; downsampling
very high-resolution RGB datasets to 10 m does not preserve their useful
structural detail. CVA/index and random-forest baselines should precede a
large deep-model search on the CPU-only system. No reviewed annotations,
spatial splits, training run, or tuning trials currently exist.

## 6. Change analysis and validation

Evaluation requires independently reviewed reference labels. Planned metrics
include per-class and aggregate precision, recall, F1, IoU, and precision-recall
curves; spatial localization error should also be reported when reference
geometry supports it. Results must include label provenance, geographic split
strategy, and uncertainty.

The planned detection register is georeferenced and includes a unique ID,
geometry, pair, class, confidence, estimated area, optional linear length,
accept/reject/uncertain status, and a reason for rejection. Accepted and
rejected apparent changes, failures, and unassessable areas require before/after
evidence. No model evaluation, accepted detection, rejected detection,
measurement, or evidence panel has been produced at this stage.

## 7. Quantization

After a model is trained and validated, evaluate CPU-supported INT8
post-training quantization (for example through ONNX Runtime) against the same
FP32 evaluation data and comparable execution conditions. Record calibration
data, supported operations, model size, parameter count, per-class metrics,
peak memory, latency, and sensitivity to small changes. Quantization-aware
training is optional if post-training quantization degrades performance.
There is currently no trained model or quantized artifact, so no quantization
result is available.

## 8. Reproducibility, decisions, and AI usage

The repository currently includes pinned Python dependencies in
[`../requirements.txt`](../requirements.txt), ignore rules, a project
directory structure, and a SHA-256 manifest. The PyTorch dependencies specify
CPU wheels. Reproducible command-line entry points, preprocessing/model code,
tests, fixed seeds, and a decision log remain to be added as implementation
progresses. The environment has not yet been validated end-to-end.

The assignment also requires `AI_USAGE.md` or `AI_USAGE.pdf` containing the
complete chronological prompts and responses from all AI tools used. That
record is separate from this report and has not yet been assembled; a summary
or this report is not a substitute.

## 9. Current status and remaining work

| Assignment area | Status |
|---|---|
| Data audit and execution requirements | Metadata/file audit completed; cloud/usable-pixel and full decode checks remain |
| Preprocessing and registration | All four products processed with Sen2Cor; metadata-driven six-band L2A stacks and SCL masks prepared; two same-tile pairs registered and coverage/residual metrics recorded; visual mask/alignment review and independent check-point validation remain |
| Ground/onboard architecture | Both PyTorch models implemented and synthetic-forward tested; CPU patch profiling recorded; no training or target-device validation |
| Training and tuning | Windowed labeled-patch reader implemented; no reviewed annotations or spatial splits, so training and tuning remain blocked |
| Change analysis and validation | No detections or validation results |
| Quantization | No model or measurements |
| Reproducibility and AI record | Requirements/dependencies and data manifest exist; workflow and complete AI transcript remain |

This report is updated as implementation and experiments produce verifiable
evidence. Planned values and targets must not be presented as measured results.

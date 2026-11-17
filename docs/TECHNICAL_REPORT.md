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
-1,000. Convert digital numbers using the offsets and quantification values
from each product's metadata, and validate the metadata convention before
processing. Clouds, haze, cirrus, shadows, radiometric consistency, full JP2
decoding, and per-pixel usable-area percentages remain to be assessed.

No reference labels were supplied. OSM features may provide dated contextual
evidence but are not ground truth without checking temporal coverage,
completeness, and positional accuracy. Date-appropriate high-resolution
imagery or authoritative construction records are preferable for label review.
A DEM is an optional additional input if terrain illumination or relief is
material; land-cover data may aid context but cannot substitute for labels.

## 3. Preprocessing and registration

The intended reproducible preprocessing sequence is:

1. Read product metadata, band grids, quality reports, masks, and nodata.
2. Convert DN to reflectance using product-specific metadata.
3. Assess cloud, cirrus, defective pixels, shadows, and missing/invalid data;
   create an explicit per-pixel validity mask and report usable coverage.
4. Compare L1C radiometry between dates. Select and document an atmospheric
   correction or relative-normalization method only after inspection; no such
   method has yet been selected or evaluated.
5. Reproject/resample each pair onto a shared 10 m grid, using continuous or
   categorical interpolation as appropriate.
6. Estimate and apply geometric registration using stable features, then
   measure residual tie-point RMSE and inspect local errors.
7. Crop to the shared valid footprint and save analysis-ready georeferenced
   data with provenance.

Although products from a single MGRS tile usually have compatible map
coordinates, this does not prove pixel-level alignment. Residual shifts can
create bright/dark edges around buildings and roads that mimic change. The
planned registration target is below 0.5 of a 10 m pixel RMSE, subject to
verification that the stable tie points and residual distribution support
that threshold. No registration accuracy has been measured yet.

The implementation must be windowed: one full-tile 10 m float32 band is about
1.93 GB decimal, exceeding available free RAM on the inspected development
machine. Full-tile multi-band arrays are not a safe processing strategy here.

## 4. Model and deployment design

The current ground-model candidate is a Siamese U-Net: two shared-weight
encoders process the registered observations, and a decoder predicts a
per-pixel change class. A ResNet-34 or EfficientNet-B2 encoder and a
multi-class output are candidates, not implemented or selected models. The
final class schema will include no-change, the structural target classes, and
uncertain.

The onboard candidate is a smaller Siamese encoder using MobileNetV3-Small,
initially producing binary change/no-change proposals for later ground
classification. Proposed constraints in the implementation plan
(&lt;5M parameters, &lt;500 MB peak memory, &lt;5 s per 256×256 tile and a
2 W power budget) are design targets only. No onboard device or power
measurement is available; CPU/ONNX proxy measurements must be labeled as
proxy results and cannot establish target-device power or latency.

Observed development system: Windows 11; AMD Ryzen 5 7530U, 6 cores / 12
logical processors; 15.3 GiB RAM (about 4.6 GiB free when inspected); AMD
integrated Radeon graphics; no discrete CUDA-capable GPU identified; about
95.8 GiB free on C:. The initial training/inference baseline is CPU-only.
Batch size, patch size, model size, and runtime targets must be established
empirically on this hardware before any training commitment.

## 5. Labels, training, and tuning

Training cannot be claimed until independent labels are created and checked.
Candidate regions may be proposed using spectral/index differences, but those
maps are not ground truth. Human reviewers should assign class, confidence,
date pair, and evidence, and retain an uncertain class where cause is unclear.
External datasets may be considered for pretraining only after documenting
license, sensor/domain differences, label definitions, and the risk of domain
shift.

The planned label format is georeferenced polygons with at least:
`change_class`, `confidence`, `annotator`, `notes`, and source/evidence
references. Patch splits should be geographically blocked, with a separation
buffer, to reduce spatial leakage. No labeled dataset, train/validation/test
split, or trained checkpoint currently exists.

The implementation plan proposes a baseline using AdamW, learning rate
1e-4, weight decay 1e-4, weighted BCE plus Dice, flips/rotations and
photometric augmentation, cosine scheduling, and early stopping. These are
starting hypotheses, not experimental results. The planned search compares
learning rate, batch size, optimizer, loss, and augmentation against that
baseline using validation metrics. Class frequencies and the effect of
vegetation, agriculture, water, shadow, and registration artifacts on false
positives must be measured before selecting a final configuration.

All random seeds, package versions, data manifests, split definitions, model
configurations, and experiment outputs must be recorded. A fixed seed has not
yet been chosen.

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
| Preprocessing and registration | Not implemented; methods and checks defined above |
| Ground/onboard architecture | Candidates documented; neither implemented nor benchmarked |
| Training and tuning | No labels or training runs |
| Change analysis and validation | No detections or validation results |
| Quantization | No model or measurements |
| Reproducibility and AI record | Requirements/dependencies and data manifest exist; workflow and complete AI transcript remain |

This report is updated as implementation and experiments produce verifiable
evidence. Planned values and targets must not be presented as measured results.

# Phase 4 — Training data and supervised training

## Status and blocker

Phase 4 has started with a windowed annotated-patch dataset reader in
[`../src/training/dataset.py`](../src/training/dataset.py). There are no
human-reviewed annotations in `data/labels/`, so training, hyperparameter
search, and accuracy claims are intentionally blocked. Spectral/index
thresholds and unreviewed candidate detections must not be used as ground
truth. Do not silently label every unannotated pixel as no-change.

Phase 2 also records unresolved haze in the later T43RGM scene and approximate
one-pixel residual displacement observed by the user in Delhi and Mumbai.
Exclude suspect haze and registration-artifact areas from trusted labels, or
annotate them as `uncertain` when their status cannot be resolved.

With one temporal pair per city, this project can test geographic
generalization within these particular acquisitions, but it cannot
demonstrate temporal generalization or eliminate city/season/atmosphere
confounding. A held-out block in the same scene is a geographic zero-shot
test only; it shares acquisition conditions with training blocks. Leave-one-
city-out is a confounded stress test, not a temporal test. Do not claim
“zero temporal leakage” from the current data.

## Spatial evaluation design

Use large, buffered blocks to prevent neighboring structures and patches from
leaking across splits:

1. In each city footprint, create a projected 10 km macro-grid or equivalent
   large, non-overlapping administrative zones. Keep paired-date observations
   for the same ground location in one split.
2. Assign whole macro-blocks from both cities to train, validation, and the
   conventional test set. Aim for roughly 70/15/15 by usable labeled area,
   stratifying by city and pair where the number of blocks permits. Leave a
   2 km no-use buffer between blocks assigned to different splits. Do not
   extract patches across block or buffer boundaries.
3. Reserve one additional untouched macro-block in each city as a
   **zero-shot geographic test**. It must not be used for training, model
   selection, threshold tuning, or annotation-guided debugging. Report this
   result separately from the conventional test split. The suggested
   Gurugram/Noida fringe and Navi Mumbai/Panvel corridor are candidates only
   if they are within the supplied raster footprints and contain suitable
   valid pixels; verify coverage before assigning them.
4. Publish a map and machine-readable block-to-split manifest before
   training. Check that the buffers do not overlap usable data in another
   split and that both cities and acquisition pairs are represented in the
   conventional splits. If there are too few independent blocks to meet these
   conditions, report that limitation instead of relaxing spatial
   independence silently.

The 70/15/15 proportions target the conventional labeled area, not the
additional zero-shot holdouts, so overall proportions including those areas
may differ.

### Verify the temporal-pair assignment

Use acquisition metadata, not assumed city names or shorthand, when describing
seasonal and temporal domain shifts. The products currently processed in this
workspace have these date gaps:

| Tile / pair | Acquisition dates | Elapsed time |
|---|---|---:|
| T43QBB | 2022-12-23 → 2023-05-12 | 140 days (about 4.6 months) |
| T43RGM | 2023-10-06 → 2026-10-22 | 1,112 days (about 3.0 years) |

This is consistent with Mumbai (T43QBB) and Delhi (T43RGM) based on their
geographic footprints and the stated study regions. Verify the AOI boundaries
in QGIS before labeling. Each city currently has one temporal pair, so the two
different temporal regimes are not represented in both cities.

## City-aware annotation and error audit

Keep the structural target classes explicit (construction, demolition, road,
bridge, infrastructure-other) and preserve no-change and uncertain labels.
Use notes to record city, pair ID, temporal context, evidence, and confounder.
Do not treat every non-target change as no-change: tidal/mudflat change,
vegetation phenology, water variation, agricultural cycles, cloud/shadow, and
illumination artifacts are either explicitly reviewed as non-target, marked
uncertain, or left ignored. If these phenomena become frequent, add a separate
non-target class only with reviewed labels and a matching model/output schema.

Review these confounders explicitly:

- **Urban structures and shadows:** require evidence of a changed structure
  rather than a shifted shadow. Shadow displacement, particularly over tall
  buildings, is not demolition or construction. A one-pixel (~10 m) residual
  makes ambiguous edges unsuitable as confident labels.
- **Peri-urban land:** crop rotation, fallow fields, vegetation drying, and
  bare-soil brightness changes are not site preparation by themselves. Label
  site preparation as construction only when date-to-date evidence shows
  persistent mechanical grading or infrastructure layout; otherwise use
  uncertain/ignore.
- **Coasts and waterways:** tide, mudflat exposure, and water-level variation
  are non-target hydrological effects, not built infrastructure change.
- **Atmosphere:** haze/smog can change visible-band contrast and residual
  atmospheric effects can affect SWIR too. SWIR may be a useful complementary
  signal, but does not “see through” contamination or replace masking and
  visual quality checks. Exclude cloud/shadow/haze buffers based on observed
  mask/geolocation error and inspect resulting coverage; a blanket 200–300 m
  buffer may remove excessive usable area and should only be used after
  validating its effect.

### Radiometry and metrics

Do not apply separate 2–98% clipping to each date as the only model
normalization: it can erase meaningful between-date radiometric shifts and
make the same reflectance map to different input values. Keep metadata-scaled
reflectance inputs, use fixed physical clipping limits only when justified by
inspected data, and estimate per-band normalization statistics from the
training split only. If per-image percentile normalization is evaluated,
treat it as a separate ablation and compare it on spatially held-out splits.
NDVI, NDBI, MNDWI, and BSI can be supplementary channels or review aids;
compute them from reflectance and do not treat threshold maps as ground truth.

### Candidate GeoJSON generation

The initial, deliberately unreviewed candidate windows can be regenerated
with:

```powershell
.\venv\Scripts\python.exe src\training\generate_candidates.py --overwrite
```

The command writes `data/labels/candidates/mumbai_candidates.geojson` and
`data/labels/candidates/delhi_candidates.geojson`. It reads metadata-scaled
reflectance in B02, B03, B04, B08, B11, B12 order, the registered later date,
and the pair common-valid mask. It computes
`NDVI=(B08-B04)/(B08+B04)`,
`NDBI=(B11-B08)/(B11+B08)`, and
`MNDWI=(B03-B11)/(B03+B11)`.

The current run produced 135 Mumbai windows (108 index-ranked, 27 random
below-threshold) and 171 Delhi windows (114 index-ranked, 57 random
below-threshold). These span the full Sentinel tile footprints, not only
municipal limits; select the relevant Delhi NCR/Mumbai MMR AOIs before review.
The files are unreviewed candidate artifacts. The generator's `--overwrite`
option only replaces files whose features are still marked
`unreviewed_candidate`; it refuses to replace a candidate file with reviewed
features.

Each polygon is a 256×256-pixel (2.56 km square) **review window**, not a
predicted object boundary. For each pair, the generator calculates the
pair-centered spread of the 10th/90th percentile changes in NDVI, NDBI, and
MNDWI. Within each 10 km macro-block, it keeps up to the top 10% of eligible
windows whose score reaches the screening threshold; it also randomly samples
18% of below-threshold windows per block, with a fixed recorded seed (using
ceiling for small block samples). The inclusion probability is recorded for
each random window; deterministic ranked windows have no inclusion
probability. Windows require at least 80% common-valid pixels and are kept
wholly inside one macro-block. GeoJSON properties record pair dates,
`spatial_block_id`, `split_assignment=unassigned`,
`planned_split_buffer_m=2000` (planning metadata; no split buffer has yet been
applied), review type, index-delta percentiles, and the fraction satisfying
the proposed `ΔMNDWI < -0.3 AND ΔNDBI > 0.1` rule.

The water rule is an evidence statistic only, not a reliable bridge detector:
it can fire on tides, exposed soil, seasonal change, haze, or registration
residuals. Pair-centered ranking reduces the effect of a global index shift
but can miss broad real change. Random windows reduce threshold-selection
bias but do not make sampling fully representative; inspect candidates and
also deliberately search for subtle changes outside them. The generator is
for review prioritization/training-candidate discovery only. Do not use its
enriched windows as a test set or claim unbiased precision/recall. Keep test
blocks exhaustively reviewed, or use a probability sample with known
inclusion probabilities. Every feature
starts with `review_status=unreviewed_candidate`, `change_class=null`,
`confidence=unreviewed`, and an unassigned split. These files are not training
labels and must not be passed to the labeled-patch reader until reviewed and
converted to the accepted label schema. The generated polygons remain in
EPSG:4326 for QGIS interoperability. The candidate properties document a
no-future-reference policy: historical OSM may only be consulted at or before
T2, and no post-T2 imagery/records may decide the label. Record any reference
date in `notes`; if the two Sentinel dates are inconclusive, use `uncertain`
or leave the region unlabeled. This is a declared review protocol, not an
automated technical enforcement.

The 2022-12-23 to 2023-05-12 Mumbai pair spans post-monsoon/dry-season to
pre-monsoon conditions. L1C metadata mean solar zenith changes from 46.52° to
17.09°, and azimuth from 156.16° to 91.43°; vegetation senescence,
shadow/BRDF changes, water/tide state, and higher later-scene aerosol and
water-vapour estimates should be reviewed as potential confounders. Delhi's
2023-10-06 and 2026-10-22 dates are 16 days apart in calendar position but
1,112 days apart in elapsed time. Mean solar zenith changes from 36.12° to
41.49° and azimuth from 155.53° to 160.20°. Harvest and post-monsoon haze are
plausible Delhi confounders, not confirmed causes for any particular pixel.
Record scene-specific evidence rather than assigning a confounder solely from
season.

## Transfer learning and training safeguards

The current CPU models are baselines, not a requirement to use deep transfer
learning. If external data are used:

- OSCD is the closest listed pretraining source because it contains Sentinel-2
  imagery; check its geographic coverage and exclude any overlapping test AOI
  or target study areas before pretraining.
- LEVIR-CD and WHU building data are very high-resolution RGB. Do not resize
  their imagery to 10 m and assume that it provides useful Sentinel-2
  representations; roof/road details vanish under that resampling. Use only
  if a clearly justified transfer experiment can be designed and fully
  fine-tuned/evaluated on Sentinel-2.
- S2Looking has off-nadir high-resolution imagery and a substantial
  resolution/sensor mismatch; it is not direct Sentinel-2 pretraining.

For the seven-logit ground model, use masked multi-class cross-entropy
(`ignore_index=255`) and optionally multi-class Dice, not binary
cross-entropy. Apply geometric transforms identically to both dates and the
label. Any date-specific radiometric augmentation should be justified and
validated because arbitrary independent brightness/haze jitter can alter the
change signal. Test before/after swapping as a sanity check; if dates are
swapped, construction and demolition labels must also be exchanged.

Avoid a large hyperparameter search on the CPU-only laptop. Begin with a
non-deep CVA/index baseline and a random forest on reviewed spectral features,
then compare to the Siamese model only if labels support it. Use spatial
block-wise validation and a few fixed seeds; touch the final test sample once.
The existing model profiling measured forward-only time, not the proposed
training-step duration, so benchmark a real training step before estimating
the search budget.

Report confirmed structural-class precision/recall/F1 and IoU on reviewed
target labels, with city, pair, and conventional/zero-shot split shown
separately. Also report uncertain/non-target rejection rate and the fraction
of pixels/area accepted for a target prediction (coverage). Treat uncertain
labels as an abstention/ambiguity audit rather than counting them as ordinary
correct target predictions; document the denominator for each metric.

Candidate-enriched samples are appropriate for finding possible changes and
training annotation, but not for estimating deployed precision. For test
blocks, use exhaustive reviewed coverage or a probability sample with known
inclusion probabilities and appropriate weights. Keep an untouched
probability-based sample of below-threshold areas so subtle changes missed by
the candidate ranking can be found. Report that candidate-set metrics
represent agreement on the selected sample, not unbiased map accuracy or
changed-area estimates.

A practical small pilot is 3–4 buffered macro-blocks per city, chosen to
include urban, peri-urban/agricultural, and hydrological/coastal contexts where
present. Reserve a geographically separate test AOI within each city and
review that AOI exhaustively or through a probability sample independent of
the candidate ranking. A full 10×10 km block is 100 km² and may be too large
to exhaustively label manually; define and record a smaller evaluation AOI
inside its block if needed. Keep the candidate-enriched training annotations
separate from this test sample. The current datasets still cannot establish
temporal generalization because each city contributes only one date pair.

Only consult time-bounded reference information. If OSM is used, use a
historical snapshot dated at or before T2 and record its date; never use live
OSM or imagery/records from after T2 to determine a label. If the two Sentinel
dates do not establish the class/cause, label `uncertain` or leave the region
unlabeled. If no independent date-matched high-resolution reference exists,
describe the results as agreement with human-interpreted Sentinel-2 labels,
not accuracy against independent ground truth.

At 10 m, narrow roads, bridge decks, and individual buildings may be mixed
within pixels; the reported approximately one-pixel registration residual
further limits edge attribution. Keep the requested class names during
annotation if useful, but record instance counts and avoid strong per-class
performance claims for rare or sub-pixel classes. Report metrics only for
classes with adequate independent examples in the relevant split, and state
when a class cannot be evaluated.

The current seven-class model head has `no_change`, `construction`,
`demolition`, `road`, `bridge`, `infrastructure_other`, and `uncertain`.
Before any supervised training, reconcile this with the proposed
`change_category` / `non_target_subtype` taxonomy. Do not silently map
vegetation, agriculture, tides, shadows, or haze to `no_change`; the current
patch reader does not yet support a separate non-target loss category.

## Annotation format

Create one RFC 7946 GeoJSON FeatureCollection per image pair under
`data/labels/`. Coordinates must be WGS 84 longitude/latitude. Annotate
reviewed polygons only; pixels outside polygons are assigned ignore index 255,
not no-change. An explicit `no_change` polygon is needed to supervise that
class. The required per-feature properties are:

```json
{
  "type": "Feature",
  "properties": {
    "feature_id": "MUM_T43QBB_R01024_C02048",
    "region": "mumbai",
    "spatial_block_id": "T43QBB_E0200_N2170",
    "split_assignment": "unassigned",
    "interval_type": "short_interval_cross_season",
    "t1_date": "2022-12-23",
    "t2_date": "2023-05-12",
    "change_category": "target",
    "change_class": "construction",
    "non_target_subtype": null,
    "confidence": "high",
    "annotator": "reviewer-id",
    "evidence_date": null,
    "confounder": null,
    "notes": "New structure visible in the later observation"
  },
  "geometry": {
    "type": "Polygon",
    "coordinates": [[[77.0, 28.0], [77.001, 28.0], [77.001, 28.001], [77.0, 28.001], [77.0, 28.0]]]
  }
}
```

Allowed class IDs follow `GROUND_CLASSES` in the model:
0 no-change, 1 construction, 2 demolition, 3 road, 4 bridge,
5 infrastructure-other, 6 uncertain. Overlapping polygons with conflicting
classes are rejected when rasterized. Reviewers should record evidence and
confidence, consult suitable date-specific imagery/records, and avoid labeling
seasonal vegetation, water variation, cloud/shadow, haze, or misregistration
as structural change.

## Practical QGIS workflow for Delhi and Mumbai

Prepare and label each same-area date pair independently. Keep the date pair,
tile, and city in the filename/metadata; do not compare or label across
different footprints. Use the already registered later-date raster and its
matching earlier-date raster and common-valid mask.

An editable GeoPackage has been generated at
[`../data/labels/metro_label_grids.gpkg`](../data/labels/metro_label_grids.gpkg).
Open it in QGIS and use these layers:

| Layer | Contents |
|---|---|
| `mumbai_blocks` | 144 indexed 10 km macro-blocks over the T43QBB raster footprint |
| `mumbai_labels` | Empty polygon layer ready for detailed Mumbai annotations |
| `delhi_blocks` | 144 indexed 10 km macro-blocks over the T43RGM raster footprint |
| `delhi_labels` | Empty polygon layer ready for detailed Delhi annotations |

The grids are clipped to each full Sentinel raster extent, not municipal
boundaries. They cover about 110 × 110 km per tile, so first use a QGIS AOI
polygon or map selection to decide which blocks are relevant to Delhi NCR or
Mumbai MMR. The outer grid cells are partial. Do not treat all tile pixels as
the city study area. Each block has a stable `block_id`, city, tile, pair and
dates, `common_pct` (Sen2Cor-mask valid fraction only), plus unassigned
`split` and `zone_role` fields. The grid is an indexing/split-planning layer;
do not assign one change class to an entire 10 km block.

Digitize detailed class polygons into the corresponding `*_labels` layer.
Each label layer includes `feature_id`, `spatial_block_id`,
`split_assignment`, `interval_type`, `t1_date`, `t2_date`,
`change_category`, `change_class`, `non_target_subtype`, `confidence`,
`annotator`, `evidence_date`, `confounder`, `notes`, `city`, `tile`, and
`pair_id`. Legacy `label_id` and `block_id` fields are retained for
compatibility. Populate both block-ID fields with the intersected macro-block
ID and use the exact class codes and meanings below:

| Class code | Class | Labeling meaning |
|---|---|---|
| `no_change` | No change | Reviewed area with no target structural change; do not use for unreviewed pixels or unresolved non-target change |
| `construction` | Construction | New or materially extended built structure/infrastructure |
| `demolition` | Demolition | Previously present built structure/infrastructure removed |
| `road` | Road | New or materially changed road infrastructure |
| `bridge` | Bridge | New or materially changed bridge/crossing infrastructure |
| `infrastructure_other` | Other infrastructure | Confirmed target infrastructure not covered by the classes above |
| `uncertain` | Uncertain | Reviewed location where change cause/class cannot be established confidently |

Set `change_category` to `target`, `no_change`, `non_target`, or `uncertain`.
For `target`, set `change_class` to one of the structural classes. For
`no_change`, use `change_class=no_change`. For `non_target`, leave
`change_class` empty and use `non_target_subtype` such as `seasonal_veg`,
`agriculture_cycle`, `hydrological_tidal`, `shadow_illumination`, `cloud`,
`haze`, or `registration_artifact`. For `uncertain`, use
`change_class=uncertain`. Fill `evidence_date` with the date of any dated
reference consulted; it must not be later than T2. Confidence is annotation
metadata only at present—the dataset reader does not yet convert it to a loss
weight or support non-target subtypes as a separate training category.

For known non-target dynamics, use `change_class=uncertain` when the distinction
is unresolved and record a `confounder` such as `vegetation_agriculture`,
`hydrology_tide`, `shadow_illumination`, `cloud`, `haze`, or
`registration_artifact`. If a region is merely unreviewed, leave it unlabeled
instead of assigning `uncertain` or `no_change`. `common_pct` is only a mask
statistic; it does not flag residual haze.

1. **Make QGIS responsive before digitizing.** The reflectance stacks are
   six-band, float32, roughly 2 GB each. Build raster overviews/pyramids once
   for each display raster. In QGIS, open **Layer Properties → Pyramids**,
   select overview levels such as 2, 4, 8, 16, and 32, use **Average**
   resampling for continuous reflectance, and build them. For the validity
   mask use **Nearest neighbour**. This creates a lower-resolution display
   index; it does not change source pixel values. Keep enough free disk space
   for the overview files. Building overviews may take time once, but normal
   zoom/pan should then avoid reading full-resolution data for the whole tile.
2. **Load only what you need.** Add one before raster, one registered after
   raster, and the common-valid mask. Avoid loading all four full-tile stacks
   simultaneously. Set the project CRS to the raster CRS (the current stacks
   are EPSG:32643). In each six-band stack set QGIS multiband RGB to red B04
   (band 3), green B03 (band 2), blue B02 (band 1), then apply a reasonable
   cumulative-count stretch. Use layer visibility or the QGIS swipe tool to
   inspect the same map extent on the two dates. Confirm the valid mask is on
   and exclude the visibly hazy 2026-10-22 T43RGM area from trusted labels.
3. **Choose review sites before drawing labels.** Make a small site list for
   each pair containing representative urban, peri-urban, road, stable
   built-up, vegetation, water, shadow, and uncertain/hazy areas. Review at
   the same scale on both dates. Changes that cannot be distinguished from
   the reported approximately one-pixel residual should be marked uncertain
   or left unlabeled, not forced into a construction/demolition class.
4. **Digitize one class polygon per reviewed feature** in the appropriate
   `*_labels` layer of the supplied GeoPackage. Draw polygons only where the
   date-to-date evidence supports the class. Explicitly label representative
   stable no-change areas; pixels outside reviewed polygons remain ignored by
   the training dataset. Keep unknown causes as `uncertain`.
5. **Export the reviewed layer to GeoJSON in EPSG:4326.** Save it under
   `data/labels/` with an unambiguous pair name. Verify the exported field
   values and polygons overlay the correct pair. The training reader requires
   RFC 7946 longitude/latitude coordinates and the `change_class`,
   `confidence`, `annotator`, and `notes` properties.
6. **Keep evaluation geographically independent.** Follow the 10 km
   macro-block, 2 km buffer, city-stratified conventional split, and additional
   per-city zero-shot holdout protocol above. Freeze and map assignments
   before making patches; do not randomly split neighboring polygons or
   patches.

### If pyramids do not resolve slow loading

Create separate reduced-resolution RGB browse rasters for visual review (for
example, 25% of source width and height) and leave the six-band source stacks
unchanged for training. A browse image is only for navigation and visual
interpretation; digitized features must still be checked against the
full-resolution registered pair before they are accepted. Avoid converting
the source stacks to JPEG or using a lossy display copy as training input.

## Reading labeled patches

`LabeledPairPatchDataset` takes paths to the earlier six-band reflectance
stack, later registered six-band reflectance stack, common-valid mask, and
annotation GeoJSON, plus a list of `(row, column)` pixel patch origins.
It reads patches on demand, returns `before`, `after`, and integer `label`
tensors, and applies the common-valid mask. Invalid pixels and unannotated
pixels use `IGNORE_INDEX=255`. Conflicting annotation classes and misaligned
rasters fail explicitly.

Example setup once reviewed labels exist:

```python
from src.training.dataset import LabeledPairPatchDataset

dataset = LabeledPairPatchDataset(
    before_path="outputs/processed/earlier.reflectance.tif",
    after_path="outputs/registered/pair_after_registered.tif",
    common_valid_path="outputs/registered/pair_common_valid.tif",
    annotations_path="data/labels/pair.geojson",
    patch_origins=[(1024, 2048)],
    patch_size=256,
)
sample = dataset[0]
```

The caller must create patch-origin lists from annotations and assign entire
geographic blocks to train/validation/test before extracting patches. Use the
2 km split buffer and per-city zero-shot holdouts described above; do not
randomly split neighboring patches. Ensure each conventional split has
reviewed examples from supported classes and dates. The dataset reader does
not perform spatial splitting or class balancing.

## Next steps

1. Review and digitize pair-specific polygons in QGIS; keep uncertain cases
   explicit and preserve evidence/annotator metadata.
2. Verify polygon dates/classes and exclude atmospherically contaminated or
   poorly registered areas.
3. Generate the 10 km spatial blocks, 2 km split buffers, city-stratified
   conventional splits, zero-shot holdouts, and a frozen split manifest; then
   inspect class frequencies and annotation coverage.
4. Implement masked multi-class training with the Phase 3 ground model,
   deterministic seeds, validation metrics, early stopping, checkpoints, and
   experiment logging.
5. Only after a trusted validation set exists, tune hyperparameters and report
   held-out metrics. No trials or training results are claimed yet.

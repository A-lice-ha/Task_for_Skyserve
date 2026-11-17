# Phase 1 — Data requirements and audit

Audit of the supplied Sentinel-2 products for man-made change detection.
Product metadata, tile metadata, and the supplied quality reports were inspected
locally. Archive checksums and byte sizes are recorded in
[`data/manifests/sentinel2_products.csv`](../data/manifests/sentinel2_products.csv).

## 1.1 Product audit

All four products are Sentinel-2A Level-1C (top-of-atmosphere reflectance) in
descending orbit. The percentages below are the product/tile metadata cloud
assessment, not a measured valid-pixel percentage.

| Acquisition (UTC) | Tile | Orbit | Baseline | Cloud assessment | Mean sun zenith / azimuth |
|---|---|---:|---:|---:|---:|
| 2022-12-23 05:42:41 | T43QBB | 5 | 05.10 | 0% | 46.52° / 156.16° |
| 2023-05-12 05:36:41 | T43QBB | 5 | 05.10 | 0.0077% | 17.09° / 91.43° |
| 2023-10-06 05:27:01 | T43RGM | 105 | 05.10 | 0% | 36.12° / 155.53° |
| 2026-10-22 05:32:41 | T43RGM | 105 | 05.13 | 0% | 41.49° / 160.20° |

The reported cloud percentages are low, but haze, cloud shadows, cirrus, and
other invalid pixels still need to be evaluated from the supplied masks and
spectral data during preprocessing. These percentages must not be used as a
replacement for that per-pixel assessment.

The tile metadata reports EPSG:32643 (WGS 84 / UTM zone 43N) and grids of
10,980 × 10,980 at 10 m, 5,490 × 5,490 at 20 m, and 1,830 × 1,830 at 60 m.
Each product has 14 non-empty JP2 files in `IMG_DATA`: the 13 Sentinel-2
spectral bands and TCI. The tile classification mask `MSK_CLASSI_B00.jp2` is
present in each product. The supplied format, general, geometric, sensor, and
(datastrip-level) radiometric XML quality reports all have a `PASSED` status.
This is a metadata/file-presence check, not a full decode or pixel-quality
validation.

Each geometric quality report contains the statement
`Absolute location is valid (20.0<30.0)`. This records a 20 m absolute-location
value as passing a 30 m threshold; it is not pairwise relative registration
accuracy. The user's approximately 20 m observation in QGIS is consistent
with the metadata, but local tie-point residuals still need to be measured for
each temporal pair.

All products report a quantification value of 10,000 and a
`RADIO_ADD_OFFSET` of -1,000 for each spectral band. Apply the per-band
metadata when converting DN to reflectance; do not assume offsets or
quantification values if the input products are later replaced.

### Change-detection pairs

| Pair | Before → after | Elapsed time | Assessment |
|---|---|---:|---|
| A | T43QBB: 2022-12-23 → 2023-05-12 | 140 days | Same tile and orbit; usable for comparison after alignment and seasonal-change controls. Winter-to-spring variation is a significant confounder. |
| B | T43RGM: 2023-10-06 → 2026-10-22 | 1,112 days | Same tile and orbit; October-to-October reduces seasonal mismatch, but the long interval permits temporary changes and intervening events to be missed. |

The geographic footprints reported in the product metadata are disjoint:
T43QBB bounds are approximately 72.135–73.194° E, 18.882–19.887° N; T43RGM
bounds are approximately 77.032–78.176° E, 27.901–28.913° N. Therefore, the
cross-tile images are not a valid pixelwise temporal pair.

## 1.2 Required bands and working grid

Use the common 10 m grid for pair registration and outputs, but retain each
band's native resolution in the data record:

| Use | Bands | Native resolution | Notes |
|---|---|---:|---|
| Visible/NIR structural and spectral features | B02, B03, B04, B08 | 10 m | Core high-resolution inputs. |
| Red-edge, vegetation and SWIR/built-up features | B05, B06, B07, B8A, B11, B12 | 20 m | Resample continuous reflectance to the pair grid; this does not add native detail. |
| Aerosol/cirrus and atmospheric screening | B01, B09, B10 | 60 m | Use for screening/context, not as 10 m observations. |
| Cloud, cirrus, defective-pixel masking | `MSK_CLASSI_B00.jp2`, `MSK_DETFOO_*`, `MSK_QUALIT_*` | Product-dependent mask grids | Confirm class meanings and grids before combining masks. |

Use bilinear resampling for continuous reflectance and nearest-neighbor for
categorical masks. A 10 m output grid is selected because the target features
include buildings and roads; upsampled 20 m and 60 m bands must be treated as
coarser inputs. Calculate NDVI as (B08 − B04)/(B08 + B04), NDWI as
(B03 − B08)/(B03 + B08), MNDWI as (B03 − B11)/(B03 + B11), and NDBI as
(B11 − B08)/(B11 + B08), with nodata and near-zero denominators masked.

## 1.3 Gaps and additional data

- **Reference labels:** No ground-truth change labels or detection register were
  supplied. Candidate changes need independent human review and documented
  confidence. OSM building/road data can provide context, but its date,
  completeness, and positional accuracy must be checked before using it as a
  reference.
- **Temporal evidence:** Obtain historical, date-appropriate high-resolution
  imagery or authoritative construction records where available. Two snapshots
  cannot establish when a change happened or whether an apparent change was
  temporary.
- **Atmosphere and valid coverage:** These are L1C products, not surface
  reflectance. No per-pixel clear-area percentages have been computed yet.
  Inspect classification/quality masks and B01/B10 and shadow indicators in
  preprocessing; decide whether Sen2Cor/L2A inputs or relative normalization
  are necessary.
- **Terrain/context:** A DEM (for example Copernicus DEM or SRTM) is useful if
  slope, terrain illumination, or shadow effects are material. Land-cover data
  can support contextual masking but is not change ground truth. Record source,
  date, license, and alignment for any added data.
- **File integrity:** All products contain the expected 13 spectral-band files
  and TCI, with no zero-byte JP2s, and their supplied quality reports pass.
  Full JP2 decoding and consistency checks remain to be done.

## 1.4 Compute and execution assumptions

Observed on the development machine during this audit: Windows 11; AMD Ryzen 5
7530U (6 cores / 12 logical processors); 15.3 GiB RAM (about 4.6 GiB free at
inspection); AMD integrated Radeon graphics; no discrete CUDA-capable GPU was
identified. C: had about 95.8 GiB free. The four ZIP archives total about
2.97 GiB and the extracted SAFE products about 2.97 GiB.

A single 10 m full-tile float32 band is about 1.93 GB (decimal); even a
six-band pair can exceed available RAM before temporary arrays and model
memory. Implement windowed reads and processing, or begin with smaller matched
areas of interest. Do not load full-tile multi-band pairs into memory on this
machine. CPU-only development and inference are the available local baseline;
training throughput and runtime must be measured after preprocessing and
baseline model implementation.

For ground execution, use a CPU-capable workstation/server and document its
actual resources when selected; GPU acceleration is optional, not assumed.
For onboard execution, hardware remains a design assumption rather than an
available device. Use the assignment's proposed edge-class limits only as
targets, and report CPU/ONNX proxy measurements separately from actual power
and target-device measurements.

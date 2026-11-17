# Phase 2 — Preprocessing and registration

## Implementation

Run the three stages from the repository root in an activated Python 3.10
environment with the pinned requirements installed:

```powershell
.\venv\Scripts\python.exe src\preprocessing\phase2.py sen2cor
.\venv\Scripts\python.exe src\preprocessing\phase2.py prepare
.\venv\Scripts\python.exe src\preprocessing\phase2.py register
```

The `sen2cor` stage invokes the supplied Sen2Cor 2.13.02
`L2A_Process.bat` through `subprocess`. It processes each root-level L1C SAFE
product at 10 m output resolution, keeps output products under
`outputs/sen2cor/l2a/`, and saves per-product logs under `outputs/sen2cor/logs/`.
The batch file uses unquoted paths internally. The wrapper resolves Windows
8.3 short paths and supplies the short user-profile path to avoid failures
caused by spaces in the Windows account name. Sen2Cor's configuration is kept
in its normal per-user Documents location. Standard output and errors are
captured with `subprocess.run(check=True, text=True, capture_output=True)` and
written to the per-product log when processing completes; the subprocess has a
30-minute timeout. A nonzero exit includes the failure details and log path.

The `prepare` stage reads `MTD_MSIL2A.xml`, requires the product's
`BOA_QUANTIFICATION_VALUE` and selected per-band `BOA_ADD_OFFSET` values, and
computes:

```text
reflectance = (stored DN + BOA_ADD_OFFSET) / BOA_QUANTIFICATION_VALUE
```

The source L1C metadata in this workspace reports a gain/quantification value
of 10,000 and `RADIO_ADD_OFFSET=-1000` for all 13 bands. Those values describe
the L1C input and Sen2Cor uses them during its conversion. The L1C offset is
not applied again to L2A values; the L2A output metadata is authoritative for
the prepared surface-reflectance stack. Missing gain or offsets stop
processing with an explicit error.

Six reflectance bands (B02, B03, B04, B08, B11, B12) are read in 512-pixel
windows and aligned to the B02 10 m grid. Continuous bands use bilinear
resampling. The Sen2Cor scene classification layer is resampled using nearest
neighbor. The following SCL values are excluded from the per-date valid mask:
0 (no data), 1 (saturated/defective), 3 (cloud shadow), 7 (unclassified),
8/9 (cloud), 10 (thin cirrus), and 11 (snow/ice). Class 2 (dark area) is not
automatically treated as cloud shadow; this conservative distinction should
be revisited with visual quality checks.

The `register` stage groups the prepared products by tile and estimates an
affine transform from ORB features on B04. It uses RANSAC and reports match
count, inlier ratio, residual RMSE/median/percentiles in metres, 4x4 grid
occupancy, estimated shift, and the pixel transform. The later observation is
warped to the earlier observation's grid, and a common-valid mask is saved.
The transform is only accepted after the tie-point count and inlier ratio
checks pass. CSV values describe fitted tie-point residuals; inspect spatial
distribution and independent check points before using the pair for
quantitative detection.

## Geometric accuracy

The user observed approximately 20 m geometric error in QGIS for the Delhi
area. The corresponding supplied geometric-quality report says
`Absolute location is valid (20.0<30.0)`. This is consistent as an absolute
geolocation check. It is not a measurement of relative registration between
the two dates, and the 30 m figure is the quality-report acceptance threshold.
Pairwise feature registration is therefore still required. If residuals or
local distortions remain too large for building-scale inference, those areas
must be excluded or marked uncertain; a single global affine fit does not
correct local terrain/parallax distortions.

## Measured preprocessing and registration results

The four supplied products were processed through Sen2Cor, prepared as
metadata-scaled six-band stacks, and registered in the two same-tile pairs.
Their SCL-derived valid areas are:

| Tile / date | SCL-valid pixels | Valid area |
|---|---:|---:|
| T43QBB, 2022-12-23 | 99.9937% | 12,055.286 km² |
| T43QBB, 2023-05-12 | 89.2650% | 10,761.823 km² |
| T43RGM, 2023-10-06 | 99.9968% | 12,055.654 km² |
| T43RGM, 2026-10-22 | 99.9990% | 12,055.923 km² |

| Same-tile pair (earlier → later) | Common-valid area | Matches / inliers | Inlier ratio | Residual RMSE / median | P90 / P95 / max residual | 4x4 cells occupied |
|---|---:|---:|---:|---:|---:|---:|
| T43QBB (2022-12-23 → 2023-05-12) | 89.2592% | 386 / 299 | 77.5% | 10.30 / 1.44 m | 19.64 / 31.13 / 39.03 m | 9 / 16 |
| T43RGM (2023-10-06 → 2026-10-22) | 99.9958% | 271 / 213 | 78.6% | 9.21 / 0.76 m | 1.06 / 31.47 / 38.67 m | 9 / 16 |

These are fitted tie-point residuals, not independent validation errors.
RMSEs near 9–10 m, long upper residual tails, and matches in only 9 of 16
coarse spatial cells mean the transforms are not yet established as suitable
for building-scale change detection. The low medians alone do not demonstrate
sub-pixel accuracy. Visual review of SCL masks and before/after imagery,
residual-map inspection, and independent stable check points remain necessary.
A 10 m displacement can create change-like edges around buildings and roads.
The high SCL-valid fractions are mask-derived coverage only and do not
guarantee freedom from haze or other residual atmospheric effects.

## Outputs and completion checks

- `outputs/sen2cor/l2a/`: Sen2Cor L2A SAFE products.
- `outputs/sen2cor/logs/`: Sen2Cor stdout/stderr per product.
- `outputs/processed/`: metadata-scaled six-band reflectance stacks and
  per-date validity masks, plus a per-product summary CSV with SCL-valid area,
  grid metadata, and the gain/offset values read from the product metadata.
- `outputs/registered/`: aligned later-date stacks, common-valid masks, and
  registration metric CSV files.

The script is idempotent for completed Sen2Cor products and prepared stacks.
All four Sen2Cor logs report successful termination, and each processed
summary records the L2A gain and per-band offsets used. Before accepting
registration for quantitative detection, inspect reflectance ranges, SCL
masks, before/after panels, and each residual map, and evaluate independent
stable check points.

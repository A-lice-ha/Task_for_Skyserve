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
in its normal per-user Documents location.

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
count, inlier ratio, residual RMSE/median in metres, estimated shift, and the
pixel transform. The later observation is warped to the earlier observation's
grid, and a common-valid mask is saved. The transform is only accepted after
the tie-point count and inlier ratio checks pass. CSV values describe the
fitted tie-point residuals; inspect spatial distribution and independent
check points before using the pair for quantitative detection.

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

## Outputs and completion checks

- `outputs/sen2cor/l2a/`: Sen2Cor L2A SAFE products.
- `outputs/sen2cor/logs/`: Sen2Cor stdout/stderr per product.
- `outputs/processed/`: metadata-scaled six-band reflectance stacks and
  per-date validity masks.
- `outputs/registered/`: aligned later-date stacks, common-valid masks, and
  registration metric CSV files.

The script is idempotent for completed Sen2Cor products and prepared stacks.
Review log files and metadata before continuing if an output is incomplete.
After running the stages, record per-scene valid-area percentages, inspect
reflectance ranges and before/after panels, and review each registration
residual map before accepting the outputs. Those measurements have not yet
been produced.

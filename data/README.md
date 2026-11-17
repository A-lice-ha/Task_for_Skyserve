# Data layout

- `raw/` is for links or local copies of source imagery. The original Sentinel-2
  products currently remain at the repository root to avoid duplicating several
  gigabytes of data.
- `processed/` is for derived raster data.
- `labels/` is for annotations.
- `manifests/sentinel2_products.csv` records the source products and the SHA-256
  checksum and size of each supplied archive. Paths in the manifest are relative
  to the repository root.

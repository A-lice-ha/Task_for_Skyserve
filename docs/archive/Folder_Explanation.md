> **Archived (2026-11-18).** Early description of the supplied folder,
> written with GitHub Copilot on 2026-11-13 (see
> [`../ai_usage/2026-11-13_copilot_99f59a09.md`](../ai_usage/2026-11-13_copilot_99f59a09.md))
> before any code existed. Kept as a record; it is not current. See the [README](../../README.md),
> [technical report](../TECHNICAL_REPORT.md) and
> [decision log](../DECISION_LOG.md) for the submitted work.

# Folder Explanation: Interview Task - Man-Made Change Detection

This folder contains a Sentinel-2 L1C dataset package and the assignment document for a geospatial change-detection task. The goal is to build a reproducible system that detects structural man-made changes between satellite observations while separating them from natural variation, seasonal effects, and acquisition differences.

## 1. Folder overview

At the root of the folder are:

- `Interview Assignment _ Man Made Change Detection.docx`  
  The project brief / assignment specification.

- Four Sentinel-2 `.SAFE` products:
  - `S2A_MSIL1C_20221223T054241_N0510_R005_T43QBB_20240809T025636.SAFE`
  - `S2A_MSIL1C_20230512T053641_N0510_R005_T43QBB_20240908T023323.SAFE`
  - `S2A_MSIL1C_20231006T052701_N0510_R105_T43RGM_20241101T065943.SAFE`
  - `S2A_MSIL1C_20261022T053241_N0513_R105_T43RGM_20261022T091024.SAFE`

- Four matching `.zip` archives for each `.SAFE` dataset.

The folder is structured as a set of satellite image products, not as source code. It is a study dataset for multi-temporal man-made land-cover change analysis.

## 2. Assignment summary

The assignment document describes a task to:

- detect, localize, and analyze structural man-made changes in satellite imagery from different dates;
- distinguish true structural changes from natural variation and acquisition-related differences;
- work with non-aligned observations and establish a common reference footprint;
- define target classes such as bridge extension, new roads, construction, demolition, and infrastructure growth;
- treat vegetation change, water-level shifts, erosion, deposition, and agricultural activity as possible non-target changes;
- keep an uncertain category when evidence is insufficient;
- implement a reproducible workflow, model architecture, preprocessing, and validation pipeline;
- quantify performance and produce before/after evidence for accepted and rejected detections;
- include quantized deployment-ready model artifacts and a Git-based decision record;
- provide AI usage documentation if AI tools were used.

This is a full remote-sensing ML / geospatial analytics project, not just simple image comparison.

## 3. What the `.SAFE` folders represent

The `.SAFE` directories are standard ESA Sentinel-2 Level-1C user product folders. Sentinel-2 is a multispectral satellite mission with 13 bands. Level-1C means the product is a top-of-atmosphere reflectance image, still geometrically corrected but not yet converted to bottom-of-atmosphere reflectance (like Level-2A).

### Interpretation of product names

Example:

`S2A_MSIL1C_20221223T054241_N0510_R005_T43QBB_20240809T025636.SAFE`

Breakdown:

- `S2A` = Sentinel-2A satellite
- `MSIL1C` = Level-1C product type
- `20221223T054241` = sensing start time (2022-12-23 05:42:41 UTC)
- `N0510` = processing baseline / product number context
- `R005` = relative orbit 5
- `T43QBB` = UTM/MGRS tile ID (tile region over the scene)
- `20240809T025636` = product generation date

These four products cover two tile regions (`T43QBB` and `T43RGM`) and multiple seasons / years, which makes them suitable for change detection over time.

## 4. Product timeline in this folder

| Product | Acquisition date | Tile | Orbit | Notes |
|---|---|---:|---:|---|
| 20221223...T43QBB | 2022-12-23 | T43QBB | 5 | Winter / end-of-year scene |
| 20230512...T43QBB | 2023-05-12 | T43QBB | 5 | Spring / agricultural difference window |
| 20231006...T43RGM | 2023-10-06 | T43RGM | 105 | Autumn / different tile region |
| 20261022...T43RGM | 2026-10-22 | T43RGM | 105 | Later future-date scene |

The folder therefore contains a multi-date temporal set with some within-tile comparisons and some cross-tile comparisons. For a strict change analysis, the project must register and align observations before comparing them, because the assignment explicitly says the images are not spatially aligned.

## 5. Standard SAFE folder structure

Each `.SAFE` product follows the ESA SAFE format. A typical example is:

```
S2A_MSIL1C_20221223T054241_N0510_R005_T43QBB_20240809T025636.SAFE/
├── INSPIRE.xml
├── MTD_MSIL1C.xml
├── manifest.safe
├── HTML/
│   ├── UserProduct_index.html
│   ├── UserProduct_index.xsl
│   ├── banner_1.png
│   ├── banner_2.png
│   ├── banner_3.png
│   ├── star_bg.jpg
│   └── ...
├── DATASTRIP/
│   └── DS_S2RP_.../
│       ├── MTD_DS.xml
│       └── QI_DATA/
│           ├── FORMAT_CORRECTNESS.xml
│           ├── GENERAL_QUALITY.xml
│           ├── GEOMETRIC_QUALITY.xml
│           ├── RADIOMETRIC_QUALITY.xml
│           └── SENSOR_QUALITY.xml
├── GRANULE/
│   └── L1C_T43QBB_A039184_20221223T054236/
│       ├── IMG_DATA/
│       ├── QI_DATA/
│       ├── AUX_DATA/
│       ├── MTD_TL.xml
│       └── ...
├── rep_info/
│   └── S2_User_Product_Level-1C_Metadata.xsd
└── preview image file(s)
    └── *.jpg
```

### Meaning of each section

#### `MTD_MSIL1C.xml`
The main product metadata file. It contains:

- sensing start and stop time;
- product type (S2MSI1C);
- orbit number and direction;
- processing baseline;
- image format and tile / granule references;
- product organization (granules, datastrips, image file list).

This is the main file that makes the product machine-readable and traceable.

#### `manifest.safe`
A manifest document that maps the product to its logical data objects and their locations. It is effectively the package index for the SAFE archive and documents the file inventory and metadata associations.

#### `INSPIRE.xml`
INSPIRE-compliant metadata, generally used for geospatial cataloging and discoverability. It describes the dataset in a standards-based way and helps with interoperability and GIS usage.

#### `HTML/`
A human-readable HTML preview of the product metadata. It contains:

- product summary information;
- granule listing;
- bands included;
- quality metadata;
- geographic footprint;
- banner images and supporting CSS.

This is intended for visual inspection in a browser.

#### `DATASTRIP/`
Contains data-strip level metadata and quality information. It is a coarser organization level than the granule and often stores product-level QA and strip metadata.

#### `GRANULE/`
The most important section for imagery processing. Each granule contains:

- `IMG_DATA/` = actual image raster data for multiple spectral bands;
- `QI_DATA/` = quality indicator XML files per granule;
- `AUX_DATA/` = ancillary inputs like CAMS and ECMWF data;
- `MTD_TL.xml` = tile metadata and geometry info.

Each granule may contain band files such as:

- `B01` to `B12` and `B8A`
- `TCI` (true-color image)

These are typically JPEG2000 `.jp2` products or compressed image tiles used for scientific processing.

#### `rep_info/`
Contains the schema file used to validate Sentinel-2 Level-1C metadata structure.

#### Quicklook image
Each SAFE folder contains a quicklook image file such as:

- `S2A_...-ql.jpg`

This is a preview of the scene for visual inspection.

## 6. File-level details from the metadata

From the product metadata we can confirm the following:

- Product level: `Level-1C`
- Product type: `S2MSI1C`
- Spacecraft: `Sentinel-2A`
- Orbit direction: `DESCENDING`
- Product format: `SAFE_COMPACT`
- Image data are in `JPEG2000` format
- Bands include `B01`, `B02`, `B03`, `B04`, `B05`, `B06`, `B07`, `B08`, `B8A`, `B09`, `B10`, `B11`, `B12`, plus `TCI`
- The product includes radiometric offset and reflectance conversion metadata
- Quality indicators show overall product quality as `PASSED` for relevant checks

The HTML metadata summary also reports a global footprint polygon and the tile location. This is important because the assignment requires common coverage and alignment across dates.

## 7. Why this folder matters for change detection

This dataset is highly relevant for change detection because it contains multiple dates over the same general region, with different seasons and observation conditions. That means the project must account for:

- seasonal vegetation changes
- agricultural variation
- clouds and haze
- atmospheric and illumination differences
- radiometric offsets
- geometric misalignment
- human-made changes vs natural changes

The assignment specifically calls out the need to build a system that distinguishes:

- bridge deck extension
- new roads
- building construction and demolition
- other infrastructure development

from:

- vegetation growth or senescence
- water-level differences
- erosion and deposition
- agriculture cycles
- image registration differences

This is exactly the kind of multi-temporal remote sensing problem for which Sentinel-2 is valuable.

## 8. Key challenges the project must handle

### A. Non-alignment between images
The assignment states: images are not spatially aligned. This means the system cannot assume a pixel in one image corresponds to the same location in another. The project must therefore:

- compute a common coverage area;
- estimate transforms between dates;
- co-register images using stable features;
- measure registration error quantitatively.

### B. Natural variation vs structural change
The real challenge is not just detecting brightness differences. The model must distinguish:

- normal seasonal change
- water body changes
- soil moisture variation
- agricultural activity
- shadows, clouds, and atmospheric noise

from actual structural land-cover change.

### C. Multi-date analysis
Because the data include multiple dates, the system likely needs a multi-temporal workflow such as:

- image preprocessing;
- co-registration;
- feature extraction;
- difference analysis;
- uncertainty handling;
- classification / segmentation of changed areas.

### D. Quality and metadata
The presence of `QI_DATA` XML files and metadata indicates a quality-aware workflow is expected. The project can filter low-quality observations and document reliability before detecting changes.

## 9. Interpretation of image bands and relevance to the task

Sentinel-2 Level-1C includes bands with different spatial resolutions. For example:

- 10 m: B2, B3, B4, B8
- 20 m: B5, B6, B7, B8A, B11, B12
- 60 m: B1, B9, B10

These bands are useful for:

- vegetation stress and crop monitoring (red-edge bands)
- built-up land detection
- bare soil / urban discrimination
- water detection
- seasonal vegetation changes
- index generation such as NDVI, NDBI, NDWI, and spectral difference maps

The assignment implies the project should choose appropriate bands, metadata, and spatial resolution for change detection, and justify the decision.

## 10. Why the folder contains zip files alongside the extracted folders

The `.zip` archives are likely packaged copies of the same products. They likely exist as a convenience for download or archival packaging. In a project workflow, the `.SAFE` directories are usually the working data, while the `.zip` versions are distribution copies.

## 11. What the assignment expects in the final solution

The final deliverable described by the docx is a full project pipeline, not just a folder of data. The submission is expected to include:

- a Git repository with meaningful commits;
- preprocessing and registration steps;
- model architecture and chosen deployment assumptions;
- hyperparameter tuning experiments;
- validation and detection metrics;
- georeferenced outputs and detection register;
- before/after evidence for accepted and rejected changes;
- quantized model artifact comparisons;
- AI usage record if AI tools were used;
- reproducible execution instructions.

The task is therefore to operationalize the dataset into a robust remote sensing change-detection system.

## 12. Bottom line

This folder is a satellite-image dataset package built for temporal man-made change detection. The core assets are four Sentinel-2 Level-1C `.SAFE` products, each containing a full metadata stack, granule imagery, quality indicators, and HTML previews. The assignment asks for a scientifically rigorous analysis pipeline that detects real infrastructure change while rejecting natural seasonal and acquisition artifacts.

In short:

- the data are real ESA Sentinel-2 imagery;
- the task is multi-temporal land-use / infrastructure change detection;
- the analysis must be geospatially rigorous, reproducible, and evidence-based;
- the pipeline must handle registration, band choice, uncertainty, and model validation carefully.

## 13. Useful next steps

To continue the project, the next logical actions would be:

1. inspect the product metadata for acquisition windows, tiles, and cloud coverage;
2. select the date pairs relevant for change analysis;
3. load the band stacks for the relevant granules;
4. perform image co-registration and radiometric normalization;
5. generate temporal difference features and masks;
6. label structural changes and natural variation;
7. train and evaluate a supervised or semi-supervised change detector;
8. compare the original and quantized model performance.

This folder is the raw evidence base for that pipeline.

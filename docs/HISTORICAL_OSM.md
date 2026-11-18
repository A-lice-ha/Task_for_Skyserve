# Historical OSM infrastructure reference

> **Extraction status (2026-11-18): not completed; dropped from this
> submission.** The downloader is implemented and tested, but the public
> Overpass service handled the large building/infrastructure queries too
> slowly (about two successful responses per hour after subdivision). After
> about 2.5 hours, only 26 cached responses existed, all for Mumbai
> 2022-12-23; no Mumbai 2023-05-12 or Delhi responses were obtained, and no
> GeoJSON/GeoPackage or manifest was produced. The project owner decided to
> stop the extraction. No result in this repository uses OSM data. The
> downloader can resume from `query_cache/` if extraction is resumed later
> (see [DECISION_LOG D-003](DECISION_LOG.md)).

The downloader in [`../src/utils/historical_osm.py`](../src/utils/historical_osm.py)
creates a dated, supporting OpenStreetMap (OSM) reference layer for the Delhi
or Mumbai study areas. OSM is not independent ground truth: mapping edits are not construction
or demolition dates, and historical OSM may omit deleted or unmapped objects.
Verify candidate changes against the Sentinel-2 pair and independent,
date-appropriate references where available.

## Default area and date

The defaults are:

- Snapshot: **2023-10-06 23:59:59 UTC** (the state at the end of that UTC day).
- Bounding box, in WGS 84 / EPSG:4326 and south, west, north, east order:
  `27.9221550832, 77.0320156186, 28.8908178781, 78.1760832868`.
- Output directory: `data/labels/osm/delhi_20231006/`.

For Delhi, the Sentinel-2 acquisition on 2023-10-06 was at 05:27:01 UTC. The
end-of-day OSM snapshot can include edits made after the satellite pass, so it
does not represent the exact OSM state at overpass time. OSM edit timestamps
also do not establish when physical construction, demolition, or other change
occurred.

Mumbai uses the T43QBB raster-derived WGS84 footprint
`18.8823135663, 72.1523807982, 19.8871534399, 73.1830711420`. By default,
the `--city mumbai` command prepares both dated snapshots:

- 2022-12-23 23:59:59 UTC, matching the before acquisition.
- 2023-05-12 23:59:59 UTC, matching the after acquisition.

The Mumbai before and after Sentinel-2 acquisitions occurred at 05:42:41 UTC
and 05:36:41 UTC respectively. Neither end-of-day OSM snapshot represents the
exact state at the acquisition time.

## Run

Install the project's pinned dependencies, then run from the project root in
PowerShell:

```powershell
.\venv\Scripts\python.exe src\utils\historical_osm.py
```

For Mumbai, query both configured snapshots:

```powershell
.\venv\Scripts\python.exe src\utils\historical_osm.py --city mumbai
```

For only one Mumbai snapshot, override the date:

```powershell
.\venv\Scripts\python.exe src\utils\historical_osm.py --city mumbai --date 2022-12-23
```

Use `--help` to see options for changing the date, bounding box, output folder,
Overpass endpoint, timeout, retry count, and tiling parameters. The date option
is a calendar date; queries use its final second in UTC. The AOI coordinate
values are already longitude/latitude, notwithstanding any projected CRS used
for other source data.

The downloader starts with a 2-by-2 tiling, queries each of five categories
separately, and recursively subdivides tiles when requests fail for likely
size or temporary-service reasons. It retries transient requests with
backoff and caches successful query responses so a later run can resume.
Permanent query errors and tiles that cannot be subdivided further are
recorded as failures. A partial result is not a complete extraction; check
`failed_tiles.json` and the manifest status before using outputs.

The full extraction makes many requests to a public Overpass service. Do not
run it repeatedly or concurrently; follow the endpoint's usage policy and
consider a suitable alternative endpoint for sustained work.

## Classes and outputs

| `class_id` | `class_name` | Query scope |
|---:|---|---|
| 1 | Construction | Construction land use, construction tags, and buildings under construction |
| 2 | Demolition | Demolished and demolition-tagged features |
| 3 | Road | Ways with a `highway` tag |
| 4 | Bridge | Ways with a `bridge` tag |
| 5 | Infrastructure_other | Buildings, utilities, power, pipelines, man-made structures, and selected transport/community features |

When one OSM element matches multiple categories, it is retained once by
element type and ID, using the priority demolition, construction, bridge,
road, then infrastructure-other. This deduplication is a convenient layer
classification, not a claim that the feature has only one relevant tag.

The output directory contains:

- `osm_infrastructure_YYYYMMDD.geojson` and
  `osm_infrastructure_YYYYMMDD.gpkg`: combined features in EPSG:4326.
- `extraction_manifest.json`: snapshot, AOI, counts, attribution, license,
  query counts, limitations, and `complete` or `partial` status.
- `failed_tiles.json`: category, bounding box, depth, and error for failed
  requests; it is empty when there are no recorded failures.
- `skipped_elements.json`: matched OSM elements whose geometry could not be
  built (for example, route relations with incomplete members). They are
  excluded from the layer one element at a time, so the rest of the tile is
  kept; the manifest records their count.
- `query_cache/`: successful raw Overpass responses used to resume work.

Feature properties include `class_id`, `class_name`, `element_type`, `osm_id`,
OSM version and edit metadata when provided, `osm_tags`, `snapshot_date`,
`source`, attribution, and the marker
`reference_role=supporting_osm_reference_not_ground_truth`.

OSM data are © OpenStreetMap contributors and are available under the Open
Database License (ODbL) 1.0. Preserve attribution and review the ODbL terms
when redistributing extracted data.

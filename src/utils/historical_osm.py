"""Download dated OSM infrastructure features for a geographic area."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import osm2geojson
import requests
from shapely.geometry import shape


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AOI = (27.9221550832, 77.0320156186, 28.8908178781, 78.1760832868)
DEFAULT_DATE = date(2023, 10, 6)
DEFAULT_OUTPUT = ROOT / "data" / "labels" / "osm" / "delhi_20231006"
DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"

CITY_CONFIGS = {
    "delhi": {
        "aoi": DEFAULT_AOI,
        "dates": (DEFAULT_DATE,),
        "output": DEFAULT_OUTPUT,
    },
    "mumbai": {
        "aoi": (
            18.8823135663,
            72.1523807982,
            19.8871534399,
            73.1830711420,
        ),
        "dates": (date(2022, 12, 23), date(2023, 5, 12)),
        "output": ROOT / "data" / "labels" / "osm",
    },
}

CATEGORIES = {
    1: "Construction",
    2: "Demolition",
    3: "Road",
    4: "Bridge",
    5: "Infrastructure_other",
}
CLASS_PRIORITY = {5: 1, 3: 2, 4: 3, 1: 4, 2: 5}
CATEGORY_SELECTORS = {
    1: (
        'nwr["landuse"="construction"]{bbox};'
        'nwr["construction"]{bbox};'
        'nwr["building"="construction"]{bbox};'
        'nwr["building:construction"]{bbox};'
    ),
    2: (
        'nwr["demolished"]{bbox};'
        'nwr["building"="demolished"]{bbox};'
        'nwr["demolition"]{bbox};'
        'nwr["demolished:building"]{bbox};'
    ),
    3: 'way["highway"]{bbox};',
    4: 'way["bridge"]{bbox};',
    5: (
        'nwr["building"]{bbox};'
        'nwr["man_made"]{bbox};'
        'nwr["power"]{bbox};'
        'nwr["pipeline"]{bbox};'
        'nwr["utility"]{bbox};'
        'nwr["telecom"]{bbox};'
        'nwr["waterway"="canal"]{bbox};'
        'nwr["aeroway"]{bbox};'
        'nwr["railway"]{bbox};'
        'nwr["public_transport"]{bbox};'
        'nwr["amenity"~"hospital|school|university|fire_station"]{bbox};'
    ),
}


BBox = tuple[float, float, float, float]


class OverpassError(RuntimeError):
    """Base error for Overpass requests and responses."""


class SubdivisionRequired(OverpassError):
    """An oversized or transient query may succeed on smaller tiles."""


class PermanentQueryError(OverpassError):
    """A query or response error that subdivision will not fix."""


@dataclass
class ExtractionResult:
    features: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    successful_queries: int = 0
    cached_queries: int = 0


def validate_bbox(bbox: BBox) -> None:
    south, west, north, east = bbox
    if not all(math.isfinite(value) for value in bbox):
        raise ValueError("AOI coordinates must be finite")
    if not (-90 <= south < north <= 90):
        raise ValueError("AOI latitude bounds must satisfy -90 <= south < north <= 90")
    if not (-180 <= west < east <= 180):
        raise ValueError("AOI longitude bounds must satisfy -180 <= west < east <= 180")


def make_query(class_id: int, bbox: BBox, snapshot: str, timeout: int) -> str:
    if class_id not in CATEGORY_SELECTORS:
        raise ValueError(f"Unknown infrastructure class ID: {class_id}")
    validate_bbox(bbox)
    south, west, north, east = bbox
    bbox_text = f"({south:.10f},{west:.10f},{north:.10f},{east:.10f})"
    selectors = CATEGORY_SELECTORS[class_id].format(bbox=bbox_text)
    return (
        f'[out:json][timeout:{timeout}][date:"{snapshot}"];\n'
        f"(\n{selectors}\n);\nout meta geom;"
    )


def split_bbox(bbox: BBox) -> tuple[BBox, BBox, BBox, BBox]:
    validate_bbox(bbox)
    south, west, north, east = bbox
    mid_lat = (south + north) / 2
    mid_lon = (west + east) / 2
    return (
        (south, west, mid_lat, mid_lon),
        (south, mid_lon, mid_lat, east),
        (mid_lat, west, north, mid_lon),
        (mid_lat, mid_lon, north, east),
    )


def is_memory_error(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "cannot allocate memory",
            "out of memory",
            "memory exhausted",
            "runtime error",
            "timed out",
            "timeout",
            "too many requests",
            "query run out of memory",
        )
    )


def _element_identity(properties: dict[str, Any]) -> tuple[str, str] | None:
    raw_id = properties.get("@id") or properties.get("id")
    element_type = properties.get("type") or properties.get("@type")
    if isinstance(raw_id, str) and "/" in raw_id:
        element_type, osm_id = raw_id.split("/", 1)
        if element_type in {"node", "way", "relation"}:
            return element_type, osm_id
    if raw_id is not None and element_type in {"node", "way", "relation"}:
        return str(element_type), str(raw_id)
    return None


def _element_metadata(data: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    metadata = {}
    for element in data.get("elements", []):
        element_type = element.get("type")
        osm_id = element.get("id")
        if element_type not in {"node", "way", "relation"} or osm_id is None:
            continue
        metadata[(element_type, str(osm_id))] = {
            "element_type": element_type,
            "osm_id": str(osm_id),
            "osm_version": element.get("version"),
            "osm_timestamp": element.get("timestamp"),
            "osm_changeset": element.get("changeset"),
            "osm_uid": element.get("uid"),
            "osm_user": element.get("user"),
            "osm_tags": json.dumps(
                element.get("tags", {}), ensure_ascii=False, sort_keys=True
            ),
        }
    return metadata


def convert_to_features(
    data: dict[str, Any], class_id: int, snapshot: str
) -> list[dict[str, Any]]:
    metadata = _element_metadata(data)
    try:
        converted = osm2geojson.json2geojson(data, raise_on_failure=True)
    except Exception as error:
        raise OverpassError(f"Could not convert Overpass geometries: {error}") from error

    features = []
    for feature in converted.get("features", []):
        geometry = feature.get("geometry")
        if not geometry:
            continue
        properties = dict(feature.get("properties") or {})
        identity = _element_identity(properties)
        if identity is None:
            continue
        tags = properties.get("tags")
        element_metadata = metadata.get(identity, {})
        if not element_metadata.get("osm_tags"):
            element_metadata["osm_tags"] = json.dumps(
                tags if isinstance(tags, dict) else {},
                ensure_ascii=False,
                sort_keys=True,
            )
        properties.update(element_metadata)
        properties["class_id"] = class_id
        properties["class_name"] = CATEGORIES[class_id]
        properties["snapshot_date"] = snapshot
        properties["source"] = "OpenStreetMap"
        properties["attribution"] = "© OpenStreetMap contributors"
        properties["reference_role"] = "supporting_osm_reference_not_ground_truth"
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": properties,
            }
        )
    return features


def merge_feature(
    features: dict[tuple[str, str], dict[str, Any]],
    feature: dict[str, Any],
) -> None:
    properties = feature["properties"]
    identity = _element_identity(properties)
    if identity is None:
        raise ValueError("Converted OSM feature has no typed element identity")
    current = features.get(identity)
    if current is None:
        features[identity] = feature
        return

    current_properties = current["properties"]
    if CLASS_PRIORITY[properties["class_id"]] > CLASS_PRIORITY[
        current_properties["class_id"]
    ]:
        winner, other = feature, current
    else:
        winner, other = current, feature

    winner_properties = winner["properties"]
    other_properties = other["properties"]
    if not winner_properties.get("osm_tags") and other_properties.get("osm_tags"):
        winner_properties["osm_tags"] = other_properties["osm_tags"]
    features[identity] = winner


class HistoricalOSMDownloader:
    def __init__(
        self,
        output_dir: Path,
        endpoint: str = DEFAULT_ENDPOINT,
        snapshot: str = "2023-10-06T23:59:59Z",
        timeout: int = 300,
        max_retries: int = 3,
        min_tile_size: float = 0.025,
        max_depth: int = 10,
        request_delay: float = 2.0,
        session: requests.Session | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.endpoint = endpoint
        self.snapshot = snapshot
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_tile_size = min_tile_size
        self.max_depth = max_depth
        self.request_delay = request_delay
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": "Sentinel2HistoricalOSMReference/1.0 (research use)"}
        )
        self.cache_dir = output_dir / "query_cache"
        self.result = ExtractionResult()

    def _cache_path(self, class_id: int, bbox: BBox) -> Path:
        query = make_query(class_id, bbox, self.snapshot, self.timeout)
        key = hashlib.sha256(
            f"{self.endpoint}\n{query}".encode("utf-8")
        ).hexdigest()
        return self.cache_dir / f"{key}.json"

    def download_tile(self, bbox: BBox, class_id: int) -> dict[str, Any]:
        cache_path = self._cache_path(class_id, bbox)
        if cache_path.is_file():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise PermanentQueryError(
                    f"Could not read cached response {cache_path}: {error}"
                ) from error
            self.result.cached_queries += 1
            return data

        query = make_query(class_id, bbox, self.snapshot, self.timeout)
        for attempt in range(self.max_retries):
            try:
                response = self.session.post(
                    self.endpoint,
                    data={"data": query},
                    timeout=(30, self.timeout),
                )
            except requests.exceptions.RequestException as error:
                if attempt + 1 == self.max_retries:
                    raise SubdivisionRequired(
                        f"Overpass request failed after {self.max_retries} attempts: "
                        f"{error}"
                    ) from error
                time.sleep(self._retry_delay(attempt, None))
                continue

            body = response.text or ""
            if is_memory_error(body):
                raise SubdivisionRequired(body[:1000])
            if response.status_code in {429, 502, 503, 504}:
                if attempt + 1 == self.max_retries:
                    raise SubdivisionRequired(
                        f"Overpass HTTP {response.status_code}: {body[:1000]}"
                    )
                time.sleep(self._retry_delay(attempt, response.headers.get("Retry-After")))
                continue
            if response.status_code >= 400:
                raise PermanentQueryError(
                    f"Overpass HTTP {response.status_code}: {body[:1000]}"
                )
            try:
                data = response.json()
            except ValueError as error:
                raise SubdivisionRequired(
                    f"Overpass returned invalid JSON: {body[:1000]}"
                ) from error
            remark = str(data.get("remark", ""))
            if remark:
                if is_memory_error(remark):
                    raise SubdivisionRequired(remark[:1000])
                raise PermanentQueryError(remark[:1000])
            if "elements" not in data or not isinstance(data["elements"], list):
                raise PermanentQueryError("Overpass response has no elements array")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(cache_path)
            self.result.successful_queries += 1
            return data
        raise SubdivisionRequired("Maximum Overpass retries exceeded")

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(120.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(120.0, 5.0 * (2**attempt))

    def _process_tile(self, bbox: BBox, class_id: int, depth: int) -> None:
        try:
            data = self.download_tile(bbox, class_id)
            features = convert_to_features(data, class_id, self.snapshot)
            for feature in features:
                merge_feature(self.result.features, feature)
            time.sleep(self.request_delay)
        except SubdivisionRequired as error:
            south, west, north, east = bbox
            if (
                depth >= self.max_depth
                or min(north - south, east - west) <= self.min_tile_size
            ):
                self.result.failures.append(
                    {
                        "class_id": class_id,
                        "class_name": CATEGORIES[class_id],
                        "bbox_s_w_n_e": list(bbox),
                        "depth": depth,
                        "error": str(error),
                        "subdivision": "minimum_size_or_depth_reached",
                    }
                )
                return
            for child in split_bbox(bbox):
                self._process_tile(child, class_id, depth + 1)
        except PermanentQueryError as error:
            self.result.failures.append(
                {
                    "class_id": class_id,
                    "class_name": CATEGORIES[class_id],
                    "bbox_s_w_n_e": list(bbox),
                    "depth": depth,
                    "error": str(error),
                    "subdivision": "not_attempted_permanent_query_error",
                }
            )
        except OverpassError as error:
            self.result.failures.append(
                {
                    "class_id": class_id,
                    "class_name": CATEGORIES[class_id],
                    "bbox_s_w_n_e": list(bbox),
                    "depth": depth,
                    "error": str(error),
                    "subdivision": "not_attempted_conversion_error",
                }
            )

    def run(self, bbox: BBox, initial_subdivisions: int = 2) -> ExtractionResult:
        validate_bbox(bbox)
        if initial_subdivisions < 1:
            raise ValueError("initial_subdivisions must be at least one")
        if self.timeout < 1 or self.max_retries < 1 or self.max_depth < 0:
            raise ValueError("timeout/retries/depth configuration is invalid")
        if self.min_tile_size <= 0 or self.request_delay < 0:
            raise ValueError("tile size must be positive and delay non-negative")

        rows = np.linspace(bbox[0], bbox[2], initial_subdivisions + 1)
        columns = np.linspace(bbox[1], bbox[3], initial_subdivisions + 1)
        initial_tiles = [
            (float(rows[row]), float(columns[col]), float(rows[row + 1]), float(columns[col + 1]))
            for row in range(initial_subdivisions)
            for col in range(initial_subdivisions)
        ]
        for class_id in CATEGORIES:
            for tile in initial_tiles:
                self._process_tile(tile, class_id, 0)
        return self.result

    def write_outputs(self, bbox: BBox, city: str | None = None) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        features = list(self.result.features.values())
        cleaned_features = []
        for feature in features:
            geometry = shape(feature["geometry"])
            if geometry.is_empty or not geometry.is_valid:
                continue
            properties = dict(feature["properties"])
            for key, value in list(properties.items()):
                if isinstance(value, (dict, list, tuple)):
                    properties[key] = json.dumps(
                        value, ensure_ascii=False, sort_keys=True
                    )
            cleaned_features.append(
                {"type": "Feature", "geometry": geometry, "properties": properties}
            )
        gdf = gpd.GeoDataFrame.from_features(cleaned_features, crs="EPSG:4326")
        if gdf.empty:
            gdf = gpd.GeoDataFrame(
                {
                    "element_type": [],
                    "osm_id": [],
                    "class_id": [],
                    "class_name": [],
                    "snapshot_date": [],
                    "reference_role": [],
                    "osm_version": [],
                    "osm_timestamp": [],
                    "osm_changeset": [],
                    "osm_uid": [],
                    "osm_user": [],
                    "osm_tags": [],
                    "geometry": gpd.GeoSeries([], crs="EPSG:4326"),
                },
                crs="EPSG:4326",
            )

        stem = f"osm_infrastructure_{self.snapshot[:10].replace('-', '')}"
        geojson_path = self.output_dir / f"{stem}.geojson"
        gpkg_path = self.output_dir / f"{stem}.gpkg"
        failed_path = self.output_dir / "failed_tiles.json"
        manifest_path = self.output_dir / "extraction_manifest.json"
        gdf.to_file(geojson_path, driver="GeoJSON")
        gdf.to_file(gpkg_path, layer="infrastructure", driver="GPKG")
        failed_path.write_text(
            json.dumps(self.result.failures, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        summary = {
            "city": city,
            "snapshot_date": self.snapshot,
            "aoi_crs": "EPSG:4326",
            "aoi_south_west_north_east": list(bbox),
            "query_endpoint": self.endpoint,
            "attribution": "© OpenStreetMap contributors",
            "license": "Open Database License (ODbL) 1.0",
            "feature_count": len(gdf),
            "category_counts": {
                str(class_id): int((gdf["class_id"] == class_id).sum())
                for class_id in CATEGORIES
            },
            "successful_queries": self.result.successful_queries,
            "cached_queries": self.result.cached_queries,
            "failed_tiles": len(self.result.failures),
            "status": "complete" if not self.result.failures else "partial",
            "outputs": {
                "geojson": geojson_path.name,
                "geopackage": gpkg_path.name,
                "failed_tiles": failed_path.name,
            },
            "limitations": [
                "OSM element edit timestamps are not physical construction or demolition dates.",
                "Deleted or unmapped infrastructure may be absent from historical OSM.",
                "OSM is supporting/weak-reference data, not independent ground truth.",
                "This is a historical database snapshot, not an archive of every feature's physical state.",
            ],
        }
        manifest_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", choices=tuple(CITY_CONFIGS), default="delhi")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="Override with one date; by default all configured dates are queried.",
    )
    parser.add_argument("--south", type=float, default=None)
    parser.add_argument("--west", type=float, default=None)
    parser.add_argument("--north", type=float, default=None)
    parser.add_argument("--east", type=float, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--min-tile-size", type=float, default=0.025)
    parser.add_argument("--max-depth", type=int, default=10)
    parser.add_argument("--request-delay", type=float, default=2.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = CITY_CONFIGS[args.city]
    bbox = (
        args.south if args.south is not None else config["aoi"][0],
        args.west if args.west is not None else config["aoi"][1],
        args.north if args.north is not None else config["aoi"][2],
        args.east if args.east is not None else config["aoi"][3],
    )
    validate_bbox(bbox)

    snapshot_dates = (args.date,) if args.date is not None else config["dates"]
    exit_status = 0
    for snapshot_date in snapshot_dates:
        snapshot = f"{snapshot_date.isoformat()}T23:59:59Z"
        if args.output_dir is not None:
            output_dir = args.output_dir.resolve()
        elif args.city == "delhi":
            output_dir = config["output"]
        else:
            output_dir = (config["output"] / f"mumbai_{snapshot_date.isoformat().replace('-', '')}")

        downloader = HistoricalOSMDownloader(
            output_dir=output_dir,
            endpoint=args.endpoint,
            snapshot=snapshot,
            timeout=args.timeout,
            max_retries=args.retries,
            min_tile_size=args.min_tile_size,
            max_depth=args.max_depth,
            request_delay=args.request_delay,
        )
        result = downloader.run(bbox)
        downloader.write_outputs(bbox, city=args.city)
        status = "complete" if not result.failures else "partial"
        print(
            f"OSM extraction {status} for {snapshot_date.isoformat()}: "
            f"{len(result.features)} unique features, "
            f"{result.successful_queries} downloaded queries, "
            f"{result.cached_queries} cached queries, "
            f"{len(result.failures)} failed tiles. "
            f"See {output_dir}"
        )
        if result.failures:
            exit_status = 2
    return exit_status


if __name__ == "__main__":
    sys.exit(main())

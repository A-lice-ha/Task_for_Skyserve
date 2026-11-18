import json

import pytest

from src.utils.historical_osm import (
    CITY_CONFIGS,
    CATEGORIES,
    HistoricalOSMDownloader,
    PermanentQueryError,
    SubdivisionRequired,
    _element_identity,
    convert_to_features,
    make_query,
    merge_feature,
    split_bbox,
    validate_bbox,
)


AOI = (27.9, 77.0, 28.9, 78.2)


def _way_response():
    return {
        "elements": [
            {
                "type": "way",
                "id": 123,
                "version": 4,
                "timestamp": "2023-10-06T12:00:00Z",
                "changeset": 456,
                "uid": 789,
                "user": "osm-user",
                "tags": {"highway": "residential", "bridge": "yes"},
                "nodes": [1, 2],
                "geometry": [
                    {"lat": 28.1, "lon": 77.1},
                    {"lat": 28.2, "lon": 77.2},
                ],
            }
        ]
    }


def test_query_uses_historical_snapshot_and_correct_bbox_order():
    query = make_query(
        3,
        (27.9, 77.0, 28.9, 78.2),
        "2023-10-06T23:59:59Z",
        timeout=120,
    )

    assert '[date:"2023-10-06T23:59:59Z"]' in query
    assert 'way["highway"](27.9000000000,77.0000000000,28.9000000000,78.2000000000)' in query
    assert "out meta geom;" in query


def test_bbox_validation_and_initial_quadrants():
    validate_bbox(AOI)
    tiles = split_bbox(AOI)

    assert len(tiles) == 4
    assert tiles[0] == (27.9, 77.0, 28.4, 77.6)
    with pytest.raises(ValueError, match="latitude"):
        validate_bbox((29.0, 77.0, 28.0, 78.0))


def test_convert_preserves_osm_identity_tags_and_snapshot():
    features = convert_to_features(
        _way_response(), 4, "2023-10-06T23:59:59Z"
    )

    assert len(features) == 1
    properties = features[0]["properties"]
    assert _element_identity(properties) == ("way", "123")
    assert properties["class_id"] == 4
    assert properties["class_name"] == "Bridge"
    assert properties["osm_version"] == 4
    assert properties["osm_timestamp"] == "2023-10-06T12:00:00Z"
    assert json.loads(properties["osm_tags"])["bridge"] == "yes"
    assert properties["reference_role"] == "supporting_osm_reference_not_ground_truth"


def test_merge_prefers_class_priority_for_same_osm_element():
    road = convert_to_features(_way_response(), 3, "2023-10-06T23:59:59Z")[0]
    bridge = convert_to_features(_way_response(), 4, "2023-10-06T23:59:59Z")[0]
    features = {}

    merge_feature(features, bridge)
    merge_feature(features, road)

    assert len(features) == 1
    assert next(iter(features.values()))["properties"]["class_id"] == 4


def test_overpass_memory_failure_is_adaptively_subdivided(tmp_path, monkeypatch):
    downloader = HistoricalOSMDownloader(
        tmp_path, min_tile_size=0.01, request_delay=0
    )
    calls = []

    def fake_download(bbox, class_id):
        calls.append((bbox, class_id))
        if bbox == AOI:
            raise SubdivisionRequired("query run out of memory")
        return {"elements": []}

    monkeypatch.setattr(downloader, "download_tile", fake_download)
    monkeypatch.setattr("src.utils.historical_osm.time.sleep", lambda _: None)
    downloader._process_tile(AOI, 1, 0)

    assert len(calls) == 5
    assert not downloader.result.failures


def test_run_uses_four_initial_tiles_for_each_category(tmp_path, monkeypatch):
    downloader = HistoricalOSMDownloader(tmp_path, request_delay=0)
    calls = []

    def fake_process_tile(bbox, class_id, depth):
        calls.append((bbox, class_id, depth))

    monkeypatch.setattr(downloader, "_process_tile", fake_process_tile)
    result = downloader.run(AOI)

    assert len(calls) == 20
    assert {class_id for _, class_id, _ in calls} == set(CATEGORIES)
    assert all(depth == 0 for _, _, depth in calls)
    assert result is downloader.result


def test_download_tile_uses_cache_after_successful_response(tmp_path, monkeypatch):
    class Response:
        status_code = 200
        text = '{"elements":[]}'
        headers = {}

        @staticmethod
        def json():
            return {"elements": []}

    class Session:
        headers = {}

        def __init__(self):
            self.calls = 0

        def post(self, *_args, **_kwargs):
            self.calls += 1
            return Response()

    session = Session()
    downloader = HistoricalOSMDownloader(
        tmp_path, session=session, request_delay=0
    )
    monkeypatch.setattr("src.utils.historical_osm.time.sleep", lambda _: None)

    assert downloader.download_tile(AOI, 1) == {"elements": []}
    assert downloader.download_tile(AOI, 1) == {"elements": []}
    assert session.calls == 1
    assert downloader.result.successful_queries == 1
    assert downloader.result.cached_queries == 1


def test_write_outputs_creates_attributed_geojson_and_geopackage(tmp_path):
    downloader = HistoricalOSMDownloader(tmp_path, request_delay=0)
    feature = convert_to_features(
        _way_response(), 4, "2023-10-06T23:59:59Z"
    )[0]
    merge_feature(downloader.result.features, feature)

    downloader.write_outputs(AOI, city="mumbai")

    geojson = tmp_path / "osm_infrastructure_20231006.geojson"
    geopackage = tmp_path / "osm_infrastructure_20231006.gpkg"
    manifest = json.loads(
        (tmp_path / "extraction_manifest.json").read_text(encoding="utf-8")
    )
    assert geojson.is_file()
    assert geopackage.is_file()
    assert manifest["status"] == "complete"
    assert manifest["attribution"] == "© OpenStreetMap contributors"
    assert manifest["city"] == "mumbai"


def test_permanent_query_error_is_reported_without_empty_success(tmp_path, monkeypatch):
    downloader = HistoricalOSMDownloader(tmp_path, request_delay=0)

    def fake_download(_bbox, _class_id):
        raise PermanentQueryError("invalid query syntax")

    monkeypatch.setattr(downloader, "download_tile", fake_download)
    downloader._process_tile(AOI, 1, 0)

    assert downloader.result.failures[0]["class_id"] == 1
    assert "invalid query syntax" in downloader.result.failures[0]["error"]


def test_category_class_mapping_is_complete():
    assert set(CATEGORIES) == {1, 2, 3, 4, 5}


def test_mumbai_city_preset_has_pair_dates_and_raster_derived_aoi():
    mumbai = CITY_CONFIGS["mumbai"]

    assert [snapshot.isoformat() for snapshot in mumbai["dates"]] == [
        "2022-12-23",
        "2023-05-12",
    ]
    south, west, north, east = mumbai["aoi"]
    assert 18.8 < south < north < 20.0
    assert 72.1 < west < east < 73.2

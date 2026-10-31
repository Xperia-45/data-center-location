"""
OSM base-layer client: per-layer isolation and explicit distance semantics.

SYNTHETIC PAYLOADS: every Overpass response here is a locally written stand-in.
No test in this file contacts OpenStreetMap. The one thing that is real is the
road-proximity arithmetic, which is asserted numerically.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services import osm

SITE_LAT, SITE_LON = 30.901, 75.8573
KM_PER_DEG_LON_AT_SITE = 111.32 * 0.857759  # cos(30.901 deg)


class FakeResponse:
    def __init__(self, payload=None, status_code=200, raw=None):
        self._payload = payload if payload is not None else {"elements": []}
        self.status_code = status_code
        self.headers = {}
        self._raw = raw

    def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._payload


def _way(way_id, geometry, tags):
    return {"type": "way", "id": way_id, "tags": tags, "geometry": geometry}


def _way_centroid(way_id, lat, lon, tags):
    return {"type": "way", "id": way_id, "tags": tags, "center": {"lat": lat, "lon": lon}}


def _node(node_id, lat, lon, tags):
    return {"type": "node", "id": node_id, "lat": lat, "lon": lon, "tags": tags}


# A long N-S road ~0.19 km east of the site whose centroid is ~16.6 km north of it.
ROAD_GEOMETRY = [
    {"lat": 30.900, "lon": SITE_LON + 0.002},
    {"lat": 31.200, "lon": SITE_LON + 0.002},
]
RIVER_GEOMETRY = [
    {"lat": SITE_LAT - 0.01, "lon": SITE_LON - 0.02},
    {"lat": SITE_LAT + 0.01, "lon": SITE_LON + 0.02},
]

LAYER_FIXTURES = {
    "water": [_way_centroid(101, SITE_LAT + 0.02, SITE_LON + 0.01, {"natural": "water", "name": "Test reservoir"})],
    "waterway": [_way(201, RIVER_GEOMETRY, {"waterway": "river", "name": "Test river"})],
    "wetland": [_way_centroid(301, SITE_LAT - 0.004, SITE_LON + 0.002, {"natural": "wetland", "name": "Test marsh"})],
    "woodland": [_way_centroid(401, SITE_LAT + 0.03, SITE_LON - 0.02, {"natural": "wood", "name": "Test wood"})],
    "protected": [],
    "settlement": [_node(501, SITE_LAT + 0.008, SITE_LON - 0.004, {"place": "village", "name": "Test village"})],
    "road": [_way(601, ROAD_GEOMETRY, {"highway": "primary", "name": "Test primary road"})],
    "landuse": [_way_centroid(701, SITE_LAT - 0.02, SITE_LON - 0.03, {"landuse": "industrial", "name": "Test estate"})],
}


def _detect_layer(query: str) -> str:
    probes = [
        ("highway", "road"),
        ("waterway", "waterway"),
        ("wetland", "wetland"),
        ("natural\"=\"wood", "woodland"),
        ("nature_reserve", "protected"),
        ("place", "settlement"),
        ("residential", "landuse"),
        ("natural\"=\"water", "water"),
    ]
    for needle, layer in probes:
        if needle in query:
            return layer
    raise AssertionError(f"unrecognised query: {query}")


def install_fake_overpass(monkeypatch, *, fail_layers=(), status_code=504, geom_mode=True, raw_body_layers=()):
    calls = []

    async def fake_post(self, url, data=None, **kwargs):
        query = data["data"]
        calls.append(query)
        layer = _detect_layer(query)
        if layer in fail_layers:
            return FakeResponse(status_code=status_code)
        if layer in raw_body_layers:
            return FakeResponse(raw="not json at all")
        return FakeResponse({"elements": LAYER_FIXTURES[layer]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


@pytest.mark.synthetic_payload
def test_linear_features_report_true_geometry_distance_not_centroid_distance(monkeypatch):
    """
    Road-proximity terminology: with geometry available the reported distance is the
    shortest distance to the mapped centreline, and the representative-point distance
    is reported separately so the two can never be confused.
    """
    install_fake_overpass(monkeypatch)
    res = asyncio.run(osm.fetch_osm_layers(SITE_LAT, SITE_LON, 5.0, layers=["road"]))
    road = res["layers"]["road"]["features"][0]
    assert road["distance_basis"] == "geometry_min_distance"
    assert road["distance_km"] == pytest.approx(0.002 * KM_PER_DEG_LON_AT_SITE, abs=0.01)
    assert road["representative_point_kind"] == "first_mapped_vertex"
    assert road["distance_km"] <= road["representative_distance_km"]
    assert "Shortest distance" in road["distance_note"]
    assert "geometry" not in road, "geometry is opt-in (include_geometry), off by default"


@pytest.mark.synthetic_payload
def test_a_centroid_distance_would_have_been_badly_misleading_for_a_long_road(monkeypatch):
    """
    The road-proximity terminology fix, stated numerically: a long mapped road whose
    centroid sits ~16 km away still passes ~0.2 km from the site. Reporting the centroid
    distance as 'distance to the road' would be wrong by two orders of magnitude, so the
    basis is always published alongside the number.
    """
    long_road = {
        "type": "way",
        "id": 602,
        "tags": {"highway": "primary", "name": "Long primary road"},
        "center": {"lat": 31.050, "lon": SITE_LON + 0.002},
    }

    async def fake_post(self, url, data=None, **kwargs):
        return FakeResponse({"elements": [long_road]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    res = asyncio.run(osm.fetch_osm_layers(SITE_LAT, SITE_LON, 5.0, layers=["road"]))
    road = res["layers"]["road"]["features"][0]
    assert road["distance_basis"] == "mapped_centroid"
    assert road["representative_point_kind"] == "overpass_way_centroid"
    assert road["distance_km"] > 15.0
    assert "NOT a perpendicular distance" in road["distance_note"]


@pytest.mark.synthetic_payload
def test_centroid_only_results_are_labelled_as_centroid_distances(monkeypatch):
    install_fake_overpass(monkeypatch)
    res = asyncio.run(osm.fetch_osm_layers(SITE_LAT, SITE_LON, 5.0, layers=["water"]))
    water = res["layers"]["water"]["features"][0]
    assert water["distance_basis"] == "mapped_centroid"
    assert water["representative_point_kind"] == "overpass_way_centroid"
    assert "NOT a perpendicular distance" in water["distance_note"]
    assert water["distance_km"] == water["representative_distance_km"]


@pytest.mark.synthetic_payload
def test_point_features_are_labelled_as_point_features(monkeypatch):
    install_fake_overpass(monkeypatch)
    res = asyncio.run(osm.fetch_osm_layers(SITE_LAT, SITE_LON, 5.0, layers=["settlement"]))
    village = res["layers"]["settlement"]["features"][0]
    assert village["distance_basis"] == "point_feature"
    assert village["representative_point_kind"] == "osm_node"


@pytest.mark.synthetic_payload
def test_one_failing_layer_cannot_discard_the_rest_of_the_geography(monkeypatch):
    install_fake_overpass(monkeypatch, fail_layers={"road"}, status_code=504)
    res = asyncio.run(osm.fetch_osm_layers(SITE_LAT, SITE_LON, 5.0))
    assert res["status"] == "partial"
    assert res["layer_status"]["road"]["status"] == "unavailable"
    assert res["layer_status"]["water"]["status"] == "ok"
    assert "road" in res["layer_status"]["road"]["detail"].lower() or "504" in res["layer_status"]["road"]["detail"]
    assert all(f["kind"] != "road" for f in res["features"])
    assert res["features"], "the surviving layers must still be returned"
    assert res["features"] == sorted(res["features"], key=lambda f: f["distance_km"])


@pytest.mark.synthetic_payload
def test_every_layer_failing_reports_unavailable_and_invents_nothing(monkeypatch):
    install_fake_overpass(monkeypatch, fail_layers=set(osm.LAYER_SPECS), status_code=429)
    res = asyncio.run(osm.fetch_osm_layers(SITE_LAT, SITE_LON, 5.0))
    assert res["status"] == "unavailable"
    assert res["features"] == []
    assert "UNAVAILABLE" in res["data_label"]
    assert all(v["status"] == "unavailable" for v in res["layer_status"].values())


@pytest.mark.synthetic_payload
def test_malformed_and_timeout_failures_are_classified(monkeypatch):
    install_fake_overpass(monkeypatch, raw_body_layers={"water"})
    res = asyncio.run(osm.fetch_osm_layers(SITE_LAT, SITE_LON, 5.0, layers=["water"]))
    assert res["layers"]["water"]["status"] == "unavailable"
    assert res["layers"]["water"]["reason"] == "malformed_response"

    async def timeout_post(self, url, data=None, **kwargs):
        raise httpx.ReadTimeout("simulated")

    monkeypatch.setattr(httpx.AsyncClient, "post", timeout_post)
    res2 = asyncio.run(osm.fetch_osm_layers(SITE_LAT, SITE_LON, 5.0, layers=["water"]))
    assert res2["layers"]["water"]["reason"] == "timeout"


@pytest.mark.fixture_only
def test_query_construction_switches_between_center_and_geom():
    centre = osm.build_layer_query("road", SITE_LAT, SITE_LON, 5.0, include_geometry=False)
    geom = osm.build_layer_query("road", SITE_LAT, SITE_LON, 5.0, include_geometry=True)
    assert "out center" in centre and "out geom" not in centre
    assert "out geom" in geom
    assert "around:5000,30.901000,75.857300" in geom
    assert f"out geom {osm.MAX_ELEMENTS_PER_LAYER};" in geom
    assert 'way(around:5000,30.901000,75.857300)["highway"' in geom
    assert osm.resolve_layers(None) == list(osm.DEFAULT_LAYERS)
    assert osm.resolve_layers(["road", "ROAD", "bogus"]) == ["road"]


@pytest.mark.synthetic_payload
def test_geometry_is_only_attached_when_requested(monkeypatch):
    install_fake_overpass(monkeypatch)
    plain = asyncio.run(osm.fetch_osm_layers(SITE_LAT, SITE_LON, 5.0, layers=["road"], include_geometry=False))
    assert "geometry" not in plain["layers"]["road"]["features"][0]

    with_geom = asyncio.run(osm.fetch_osm_layers(SITE_LAT, SITE_LON, 5.0, layers=["road"], include_geometry=True))
    feature = with_geom["layers"]["road"]["features"][0]
    assert feature["geometry"]["type"] == "LineString"
    assert feature["geometry"]["coordinates"][0] == [pytest.approx(SITE_LON + 0.002), pytest.approx(30.900)]
    assert feature["geometry_truncated"] is False


@pytest.mark.synthetic_payload
def test_legacy_shim_keeps_the_stage12_contract(monkeypatch):
    install_fake_overpass(monkeypatch)
    feats, ok = asyncio.run(osm.legacy_overpass_features(SITE_LAT, SITE_LON, 5.0))
    assert ok is True
    assert feats, "expected the fixture layers to yield features"
    for f in feats:
        assert {"name", "kind", "lat", "lon", "distance_km", "source"} <= set(f)
        assert f["kind"] in {"water", "wetland", "forest", "protected", "settlement"}
    assert [f["distance_km"] for f in feats] == sorted(f["distance_km"] for f in feats)
    # A partially failed legacy query must still degrade all-or-nothing.
    install_fake_overpass(monkeypatch, fail_layers={"settlement"})
    feats2, ok2 = asyncio.run(osm.legacy_overpass_features(SITE_LAT, SITE_LON, 5.0))
    assert ok2 is False and feats2 == []

"""
Regression tests for the four PR #5 review corrections.

  1. parcel containment is a real polygon-to-circle test, not a vertex test
  2. successful OSM layers are cached independently; failed layers are never cached
     and partial coverage is never presented as complete
  3. Copernicus GLO-90 is classified `observation_derived`, not `measured`
  4. a repaired parcel must be polygonal - non-polygonal repair output is extracted
     or rejected

DATA PROVENANCE: fixtures/synthetic payloads only, exactly as labelled per test.
No test in this file contacts an upstream service.
"""

from __future__ import annotations

import asyncio
import collections
import math

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import conn
from app.main import app
from app.services import landscape, osm, parcels
from app.services.geo import KM_PER_DEG_LAT, haversine_km, km_per_deg_lon
from tests.test_osm_layers import LAYER_FIXTURES, FakeResponse, _detect_layer

c = TestClient(app)

SITE = {"lat": 30.901, "lon": 75.8573, "radius_km": 5.0}


def square_at(east_km: float, north_km: float, half_km: float, site: dict = SITE) -> dict:
    """A square parcel placed by local ENU offset (km) from the site."""
    lat_c = site["lat"] + north_km / KM_PER_DEG_LAT
    lon_c = site["lon"] + east_km / km_per_deg_lon(site["lat"])
    d_lat = half_km / KM_PER_DEG_LAT
    d_lon = half_km / km_per_deg_lon(site["lat"])
    return {"type": "Polygon", "coordinates": [[
        [lon_c - d_lon, lat_c - d_lat],
        [lon_c + d_lon, lat_c - d_lat],
        [lon_c + d_lon, lat_c + d_lat],
        [lon_c - d_lon, lat_c + d_lat],
        [lon_c - d_lon, lat_c - d_lat],
    ]]}


# ---------------------------------------------------------------------------
# 1. Parcel containment
# ---------------------------------------------------------------------------

@pytest.mark.fixture_only
def test_containment_reports_both_a_polygon_test_and_a_vertex_test():
    result = parcels.validate_parcel(square_at(0.0, 0.0, 0.5), SITE)
    site = result["site"]
    assert site["within_site_radius"] is True
    assert site["all_vertices_within_site_radius"] is True
    assert "Polygon-to-circle containment" in site["containment_method"]
    assert "covers" in site["containment_method"]
    # The limitation of the vertex-only field must be stated, not implied.
    assert "does NOT" in site["containment_note"]
    assert "all_vertices_within_site_radius" in site["containment_note"]


@pytest.mark.fixture_only
def test_a_parcel_crossing_the_boundary_is_not_contained():
    result = parcels.validate_parcel(square_at(5.0, 0.0, 0.5), SITE)
    assert result["site"]["within_site_radius"] is False
    assert result["site"]["all_vertices_within_site_radius"] is False
    assert result["site"]["max_vertex_distance_km"] > SITE["radius_km"]


@pytest.mark.fixture_only
def test_containment_is_a_polygon_test_not_a_vertex_test(monkeypatch):
    """
    The two results are computed differently, and the polygon test is the stricter one.

    The parcel sits at a 45 degree bearing, 4.95 km from the site: every vertex is inside
    the 5 km circle, so the vertex check passes. With the production 256-segment disc the
    polygon test also passes; with a deliberately coarse 4-segment disc the same polygon
    falls outside - which is only detectable by testing the polygon, never its vertices.
    """
    offset = 4.95 / math.sqrt(2)
    parcel = square_at(offset, offset, 0.01)

    accurate = parcels.validate_parcel(parcel, SITE)
    assert accurate["site"]["all_vertices_within_site_radius"] is True
    assert accurate["site"]["within_site_radius"] is True
    assert accurate["site"]["max_vertex_distance_km"] < SITE["radius_km"]

    monkeypatch.setattr(parcels, "CIRCLE_QUAD_SEGS", 1)  # 4-segment diamond
    coarse = parcels.validate_parcel(parcel, SITE)
    assert coarse["site"]["all_vertices_within_site_radius"] is True
    assert coarse["site"]["within_site_radius"] is False, "the polygon test must not reduce to a vertex test"


@pytest.mark.fixture_only
def test_concave_parcel_is_tested_as_a_whole_polygon():
    lat, lon = SITE["lat"], SITE["lon"]
    kx, ky = km_per_deg_lon(lat), KM_PER_DEG_LAT
    # L-shaped (concave) parcel, max vertex ~1.41 km from the site.
    ring = [(0.0, 0.0), (1.0, 0.0), (1.0, 0.4), (0.4, 0.4), (0.4, 1.0), (0.0, 1.0), (0.0, 0.0)]
    concave = {"type": "Polygon", "coordinates": [[
        [lon + e / kx, lat + n / ky] for e, n in ring
    ]]}
    result = parcels.validate_parcel(concave, SITE)
    assert result["site"]["within_site_radius"] is True
    assert result["area_ha"] == pytest.approx(64.0, rel=0.02)  # 1.0x1.0 minus 0.6x0.6 km


@pytest.mark.fixture_only
def test_planar_and_geodesic_distances_are_both_reported():
    """The ENU plane is an approximation; the geodesic distance is published beside it."""
    far_lon = SITE["lon"] + 0.5
    parcel = {"type": "Polygon", "coordinates": [[
        [far_lon - 0.001, SITE["lat"] - 0.001],
        [far_lon + 0.001, SITE["lat"] - 0.001],
        [far_lon + 0.001, SITE["lat"] + 0.001],
        [far_lon - 0.001, SITE["lat"] + 0.001],
        [far_lon - 0.001, SITE["lat"] - 0.001],
    ]]}
    site = parcels.validate_parcel(parcel, SITE)["site"]
    planar = site["max_vertex_distance_km"]
    geodesic = site["max_geodesic_vertex_distance_km"]
    assert planar > 40 and geodesic > 40
    assert abs(geodesic - planar) > 0.001, "the two measures must be reported separately"
    assert site["within_site_radius"] is False
    expected_geodesic = haversine_km(SITE["lat"], SITE["lon"], SITE["lat"] + 0.001, far_lon + 0.001)
    assert geodesic == pytest.approx(expected_geodesic, abs=0.01)
    assert planar == pytest.approx(0.5 * km_per_deg_lon(SITE["lat"]), rel=0.01)


# ---------------------------------------------------------------------------
# 2. Partial OSM caching
# ---------------------------------------------------------------------------

def install_counting_overpass(monkeypatch, fail_layers=()):
    calls = collections.Counter()

    async def fake_post(self, url, data=None, **kwargs):
        layer = _detect_layer(data["data"])
        calls[layer] += 1
        if layer in fail_layers:
            return FakeResponse(status_code=504)
        return FakeResponse({"elements": LAYER_FIXTURES[layer]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


def cached_osm_keys() -> list[str]:
    db = conn()
    rows = [r[0] for r in db.execute("SELECT cache_key FROM landscape_cache WHERE cache_key LIKE 'osm:v1:%'")]
    db.close()
    return rows


@pytest.mark.synthetic_payload
def test_successful_osm_layers_are_cached_and_failed_layers_are_retried(monkeypatch):
    calls = install_counting_overpass(monkeypatch, fail_layers={"road"})

    first = asyncio.run(osm.fetch_osm_layers(SITE["lat"], SITE["lon"], 3.0))
    assert first["status"] == "partial"
    assert first["layer_status"]["road"]["status"] == "unavailable"
    assert first["layer_status"]["water"]["status"] == "ok"
    assert first["layer_status"]["water"]["cache_hit"] is None

    second = asyncio.run(osm.fetch_osm_layers(SITE["lat"], SITE["lon"], 3.0))
    assert second["status"] == "partial"
    # The layer that worked was reused; the layer that failed was attempted again.
    assert calls["water"] == 1
    assert calls["road"] == 2
    assert second["layer_status"]["water"]["cache_hit"] == "memory"
    assert second["layer_status"]["road"]["cache_hit"] is None
    assert len(second["features"]) == len(first["features"])


@pytest.mark.synthetic_payload
def test_failed_osm_layers_are_never_written_to_any_cache_tier(monkeypatch):
    install_counting_overpass(monkeypatch, fail_layers={"road", "landuse"})
    asyncio.run(osm.fetch_osm_layers(SITE["lat"], SITE["lon"], 3.0))

    osm._layer_cache._mem.clear()  # drop the in-process tier, keep the SQLite tier
    keys = cached_osm_keys()
    assert keys, "the successful layers must be persisted"
    assert any("layer=water" in k for k in keys)
    assert not any("layer=road" in k for k in keys), "a failed layer must never be cached"
    assert not any("layer=landuse" in k for k in keys)


@pytest.mark.synthetic_payload
def test_partial_coverage_is_never_presented_as_complete(monkeypatch):
    install_counting_overpass(monkeypatch, fail_layers={"road"})
    result = asyncio.run(osm.fetch_osm_layers(SITE["lat"], SITE["lon"], 3.0))
    assert result["status"] == "partial"
    assert result["cacheable"] is False
    assert "PARTIAL COVERAGE" in result["partial_coverage_warning"]
    assert "NOT complete geography" in result["partial_coverage_warning"]
    assert "PARTIAL mapped geography" in result["data_label"]

    install_counting_overpass(monkeypatch)  # every layer succeeds now
    complete = asyncio.run(osm.fetch_osm_layers(SITE["lat"], SITE["lon"], 4.0))
    assert complete["status"] == "ok"
    assert complete["cacheable"] is True
    assert complete["partial_coverage_warning"] is None


@pytest.mark.synthetic_payload
def test_partial_osm_does_not_poison_the_landscape_cache(monkeypatch):
    """
    A partial OSM result must not freeze the whole landscape, and the layers that did
    work (DEM, land cover, the successful OSM layers) must not be re-fetched.
    """
    calls = install_counting_overpass(monkeypatch, fail_layers={"road"})
    dem_calls = collections.Counter()
    landcover_calls = collections.Counter()

    class DemResponse:
        status_code = 200
        headers = {}

        def __init__(self, values):
            self._values = values

        def json(self):
            return {"elevation": self._values}

    async def fake_get(self, url, params=None, **kwargs):
        lats = [float(x) for x in params["latitude"].split(",")]
        dem_calls["batches"] += 1
        return DemResponse([round(247.0 + (la - 30.901) * 30.0, 2) for la in lats])

    async def fake_landcover(lat, lon, radius_km, parcel_geojson=None, **kwargs):
        landcover_calls["reads"] += 1
        return {"status": "ok", "reason": None, "detail": "SYNTHETIC STUB - not ESA data",
                "data_class": "mapped", "classes": [], "cacheable": True,
                "aoi": {"kind": "circle"}, "data_label": "SYNTHETIC land-cover stub"}

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(landscape, "landcover_summary", fake_landcover)

    first = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&radius_km=3&grid_size=8")
    body = first.json()
    assert first.headers["cache-control"] == "no-store"
    assert body["all_layers_ok"] is False
    assert body["base_layer"]["cacheable"] is False
    assert body["cache"]["hit"] is None
    assert body["coverage_warnings"] and "PARTIAL COVERAGE" in body["coverage_warnings"][0]

    second = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&radius_km=3&grid_size=8").json()
    assert second["all_layers_ok"] is False
    assert second["cache"]["hit"] is None, "a partial base layer must not be cached as a whole"
    assert landcover_calls["reads"] == 1, "the successful land-cover read must be reused"
    assert dem_calls["batches"] == 1, "the successful DEM grid must be reused"
    assert calls["water"] == 1 and calls["road"] == 2


# ---------------------------------------------------------------------------
# 3. DEM data classification
# ---------------------------------------------------------------------------

@pytest.mark.fixture_only
def test_copernicus_dem_is_observation_derived_not_measured():
    from app.services.datasets import DATASETS, DATA_CLASS_DEFINITIONS, INTEGRITY_RULES

    dem = next(d for d in DATASETS if d["id"] == "copernicus_dem_glo90")
    assert dem["data_class"] == "observation_derived"
    assert "observation_derived" in DATA_CLASS_DEFINITIONS
    assert "processing chain" in DATA_CLASS_DEFINITIONS["observation_derived"]
    # The DSM disclosure must survive the reclassification.
    assert "Digital Surface Model" in dem["surface_model"]
    assert "NOT bare-earth" in dem["surface_model"]
    assert "observation_derived" in " ".join(INTEGRITY_RULES)


@pytest.mark.fixture_only
def test_no_terralex_dataset_claims_to_be_a_direct_measurement():
    """Nothing served today is a point observation, so nothing may carry `measured`."""
    body = c.get("/api/v1/datasets").json()
    assert all(d["data_class"] != "measured" for d in body["datasets"])
    classes = {d["data_class"] for d in body["datasets"]}
    assert "observation_derived" in classes and "mapped" in classes and "modelled" in classes


@pytest.mark.fixture_only
def test_elevation_payload_never_labels_the_dem_as_measured():
    resp = c.get("/api/v1/elevation?lat=30.901&lon=75.8573&radius_km=3&grid_size=4")
    body = resp.json()
    assert body["data_class"] in {"observation_derived", "unavailable"}
    if body["status"] == "ok":
        assert body["data_class"] == "observation_derived"
        assert "Digital Surface Model" in body["surface_model"]
        assert "Observed-derived" in body["data_label"]
    else:
        assert body["data_class"] == "unavailable"
        assert body["surface_model"] is None
        assert "UNAVAILABLE" in body["data_label"]


@pytest.mark.synthetic_payload
def test_landscape_terrain_classification_follows_the_layer_status(monkeypatch):
    class DemResponse:
        status_code = 200
        headers = {}

        def json(self):
            return {"elevation": [250.0] * 16}

    async def fake_get(self, url, params=None, **kwargs):
        return DemResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    body = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&radius_km=3&grid_size=4&layers=dem").json()
    terrain = body["base_layer"]["terrain"]
    assert terrain["status"] == "ok"
    assert terrain["data_class"] == "observation_derived"
    assert "Digital Surface Model" in terrain["surface_model"]


# ---------------------------------------------------------------------------
# 4. Shapely repair
# ---------------------------------------------------------------------------

SPIKED_RING = [
    [75.85, 30.89], [75.87, 30.89], [75.87, 30.91],
    [75.86, 30.90],  # spike in and back out along the same line
    [75.87, 30.91], [75.85, 30.91], [75.85, 30.89],
]


@pytest.mark.fixture_only
def test_repair_keeps_only_the_polygonal_components():
    spiked = {"type": "Polygon", "coordinates": [SPIKED_RING]}
    result = parcels.validate_parcel(spiked, SITE)
    assert result["was_repaired"] is True
    assert result["validity_error"]
    assert result["repair_extracted_polygonal_components"] is True
    assert "LineString" in result["discarded_non_polygonal_components"]
    assert result["geometry_type"] in {"Polygon", "MultiPolygon"}
    assert result["area_ha"] > 0
    # The geometry TerraLex will use is the repaired one, and it is valid.
    from shapely.geometry import shape

    assert shape(result["geometry"]).is_valid
    assert shape(result["geometry"]).geom_type in {"Polygon", "MultiPolygon"}
    assert result["input_geometry"]["coordinates"][0] == SPIKED_RING


@pytest.mark.fixture_only
def test_a_repair_that_leaves_no_area_is_rejected(monkeypatch):
    """A repaired line/point is not a parcel: it must be rejected, never accepted."""
    from shapely.geometry import LineString

    shapely_mod, shape, explain_validity, _ = parcels._shapely_or_raise()
    monkeypatch.setattr(
        parcels,
        "_shapely_or_raise",
        lambda: (shapely_mod, shape, explain_validity, lambda geom: LineString([(75.85, 30.89), (75.87, 30.91)])),
    )
    with pytest.raises(parcels.ParcelValidationError) as exc:
        parcels.validate_parcel({"type": "Polygon", "coordinates": [SPIKED_RING]}, SITE)
    assert "no polygonal area" in str(exc.value)
    resp = c.post("/api/v1/parcel/validate",
                  json={"geojson": {"type": "Polygon", "coordinates": [SPIKED_RING]}, "site": SITE})
    assert resp.status_code == 422


@pytest.mark.fixture_only
def test_bowtie_repair_produces_a_multipolygon_parcel():
    bowtie = {"type": "Polygon", "coordinates": [[
        [75.85, 30.90], [75.86, 30.91], [75.86, 30.90], [75.85, 30.91], [75.85, 30.90],
    ]]}
    result = parcels.validate_parcel(bowtie, SITE)
    assert result["was_repaired"] is True
    assert result["geometry_type"] == "MultiPolygon"
    assert result["area_ha"] > 0


@pytest.mark.synthetic_payload
def test_land_cover_is_masked_with_the_repaired_parcel_geometry(monkeypatch):
    """The validated/repaired geometry - not the raw invalid input - drives land cover."""
    seen = {}

    async def spy_landcover(lat, lon, radius_km, parcel_geojson=None, **kwargs):
        seen["parcel"] = parcel_geojson
        return {"status": "ok", "reason": None, "detail": "SYNTHETIC STUB - not ESA data",
                "data_class": "mapped", "classes": [], "cacheable": True,
                "aoi": {"kind": "parcel"}, "data_label": "SYNTHETIC land-cover stub"}

    monkeypatch.setattr(landscape, "landcover_summary", spy_landcover)
    body = c.post("/api/v1/landscape", json={
        "lat": SITE["lat"], "lon": SITE["lon"], "radius_km": 5.0,
        "layers": ["landcover"],
        "parcel_geojson": {"type": "Polygon", "coordinates": [SPIKED_RING]},
    }).json()

    assert body["parcel"]["was_repaired"] is True
    used = seen["parcel"]
    assert used is not None
    assert used["coordinates"] != SPIKED_RING, "the raw invalid ring must not be used as a mask"
    assert used["coordinates"] == body["parcel"]["geometry"]["coordinates"]
    from shapely.geometry import shape

    assert shape(used).is_valid

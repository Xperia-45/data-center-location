"""
Landscape service: base layer / impact layer separation, caching and API surface.

SYNTHETIC PAYLOADS unless a test says otherwise: the DEM is a formula, the OSM response
is written here, and most land-cover payloads are stub dicts. The one test that reads a
raster (`test_parcel_...`) uses a locally generated SYNTHETIC COG over real HTTP - never
ESA data.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import elevation as elev_service
from app.services import landscape
from tests import range_server as local_http
from tests import synthetic_worldcover as wc

c = TestClient(app)

SITE = {"lat": 30.901, "lon": 75.8573}


# ---------------------------------------------------------------------------
# synthetic upstreams
# ---------------------------------------------------------------------------

class FakeDemResponse:
    status_code = 200
    headers = {}

    def __init__(self, values):
        self._values = values

    def json(self):
        return {"elevation": self._values}


async def synthetic_dem_get(self, url, params=None, **kwargs):
    """Formula-based stand-in for Copernicus DEM (NOT measured elevation)."""
    lats = [float(x) for x in params["latitude"].split(",")]
    lons = [float(x) for x in params["longitude"].split(",")]
    values = [round(247.0 + (la - 30.901) * 30.0 + (lo - 75.8573) * 15.0, 2) for la, lo in zip(lats, lons)]
    return FakeDemResponse(values)


OSM_ELEMENT = {"type": "node", "id": 1, "lat": 30.905, "lon": 75.860,
               "tags": {"natural": "water", "name": "Synthetic test water body"}}


class FakeOsmResponse:
    status_code = 200
    headers = {}

    def json(self):
        return {"elements": [OSM_ELEMENT]}


async def synthetic_osm_post(self, url, data=None, **kwargs):
    return FakeOsmResponse()


def synthetic_landcover(lat, lon, radius_km, parcel_geojson=None, **kwargs):
    async def _stub():
        return {
            "status": "ok",
            "reason": None,
            "detail": "SYNTHETIC STUB - not ESA data",
            "data_class": "mapped",
            "classes": [
                {"value": 10, "label": "Tree cover", "area_ha": 120.0, "fraction": 0.4,
                 "data_class": "mapped", "area_data_class": "estimated"},
                {"value": 40, "label": "Cropland", "area_ha": 180.0, "fraction": 0.6,
                 "data_class": "mapped", "area_data_class": "estimated"},
            ],
            "cacheable": True,
            "aoi": {"kind": "parcel" if parcel_geojson is not None else "circle"},
            "data_label": "SYNTHETIC land-cover stub for tests",
        }

    return _stub()


@pytest.fixture
def stub_upstream(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", synthetic_dem_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", synthetic_osm_post)
    monkeypatch.setattr(landscape, "landcover_summary", synthetic_landcover)


def get_landscape(**kwargs) -> dict:
    defaults = {"lat": SITE["lat"], "lon": SITE["lon"], "radius_km": 5.0}
    defaults.update(kwargs)
    return asyncio.run(landscape.get_landscape(**defaults))


# ---------------------------------------------------------------------------
# layer separation
# ---------------------------------------------------------------------------

@pytest.mark.synthetic_payload
def test_landscape_separates_geography_from_scenario(stub_upstream):
    body = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&radius_km=5").json()
    assert body["schema_version"]
    base = body["base_layer"]
    impact = body["development_impact_layer"]
    assert base["layer_model"] == "geographic_base"
    assert impact["layer_model"] == "development_impact"
    assert {"terrain", "osm", "land_cover"} <= set(base)
    assert impact["status"] == "modelled_only"
    assert impact["data_class"] == "modelled"
    assert impact["multipliers_used"]["land_take_factor"]["calibrated"] is False
    assert impact["geographic_basis"]["base_fingerprint"] == base_fingerprint_of(body)
    assert body["integrity"]["data_class_definitions"]["measured"]
    assert body["all_layers_ok"] is True
    assert "Digital Surface Model" in base["terrain"]["surface_model"]
    assert base["terrain"]["data_class"] == "observation_derived"
    assert base["osm"]["data_class"] == "mapped"


def base_fingerprint_of(body: dict) -> str:
    return body["development_impact_layer"]["geographic_basis"]["base_fingerprint"]


@pytest.mark.synthetic_payload
def test_base_geography_is_identical_across_every_temporal_scenario(stub_upstream):
    """
    The core Stage 3A invariant: Present / Development / Future may only differ in the
    development impact layer. Same site + resolution + datasets => byte-identical base.
    """
    presets = ["baseline", "high-growth", "mitigation"]
    payloads = [get_landscape(preset=p) for p in presets]
    fingerprints = {p["development_impact_layer"]["geographic_basis"]["base_fingerprint"] for p in payloads}
    digests = {p["base_layer"]["content_digest"] for p in payloads}
    assert len(fingerprints) == 1, "base fingerprint must not depend on the scenario"
    assert len(digests) == 1, "base content must not change between scenarios"
    # Byte-identical once volatile bookkeeping (timestamps, cache tier) is excluded.
    assert landscape.landscape_json_canonical(landscape._strip_volatile(payloads[0]["base_layer"])) == \
        landscape.landscape_json_canonical(landscape._strip_volatile(payloads[2]["base_layer"]))
    assert [f["name"] for f in payloads[0]["base_layer"]["osm"]["features"]] == \
        [f["name"] for f in payloads[2]["base_layer"]["osm"]["features"]]
    assert payloads[0]["base_layer"]["terrain"]["values_m"] == payloads[2]["base_layer"]["terrain"]["values_m"]

    land_take = [
        next(i for i in p["development_impact_layer"]["indications"]
             if i["id"] == "indicative_land_take_ha")["value"]
        for p in payloads
    ]
    assert len(set(land_take)) == len(presets), "the impact layer must differ between scenarios"
    assert land_take[0] < land_take[1] and land_take[2] < land_take[0]


@pytest.mark.synthetic_payload
def test_changing_resolution_changes_the_base_layer_identity(stub_upstream):
    a = get_landscape(grid_size=20)
    b = get_landscape(grid_size=12)
    assert a["base_layer"]["content_digest"] != b["base_layer"]["content_digest"]
    assert (a["development_impact_layer"]["geographic_basis"]["base_fingerprint"]
            != b["development_impact_layer"]["geographic_basis"]["base_fingerprint"])
    assert a["cache"]["key"] != b["cache"]["key"]


# ---------------------------------------------------------------------------
# caching with site / resolution isolation
# ---------------------------------------------------------------------------

@pytest.mark.synthetic_payload
def test_cache_is_scoped_to_site_and_resolution(stub_upstream, clear_sqlite_caches):
    from app.services.landscape import _cache as landscape_cache

    clear_sqlite_caches()
    first = get_landscape()
    assert first["cache"]["hit"] is None
    assert first["cache"]["cacheable"] is True

    second = get_landscape()
    assert second["cache"]["hit"] == "memory"
    assert second["cache"]["key"] == first["cache"]["key"]

    # Drop the memory tier only: the SQLite tier must serve the same geography.
    landscape_cache._mem.clear()
    elev_service.clear_elevation_memory_cache()
    third = get_landscape()
    assert third["cache"]["hit"] == "sqlite"

    # A different radius is different geography: it must not reuse the cached layer.
    other = get_landscape(radius_km=8.0)
    assert other["cache"]["hit"] is None
    assert other["cache"]["key"] != first["cache"]["key"]
    assert other["base_layer"]["radius_km"] == 8.0

    # A different site at the same radius is different geography too.
    elsewhere = get_landscape(lat=17.385, lon=78.4867)
    assert elsewhere["cache"]["hit"] is None
    assert elsewhere["cache"]["key"] not in {first["cache"]["key"], other["cache"]["key"]}


@pytest.mark.synthetic_payload
def test_degraded_layers_are_never_cached_and_set_no_store(stub_upstream, monkeypatch, clear_sqlite_caches):
    clear_sqlite_caches()

    class Failing:
        status_code = 504
        headers = {}

        def json(self):
            return {"elements": []}

    async def failing_post(self, url, data=None, **kwargs):
        return Failing()

    monkeypatch.setattr(httpx.AsyncClient, "post", failing_post)
    resp = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&radius_km=5")
    body = resp.json()
    assert body["all_layers_ok"] is False
    assert resp.headers["cache-control"] == "no-store"
    assert body["base_layer"]["osm"]["status"] == "unavailable"
    assert body["base_layer"]["cacheable"] is False
    osm_entry = next(d for d in body["datasets"] if d["id"] == "openstreetmap_overpass")
    assert osm_entry["availability"]["available"] is False

    # Not cached: the next request must try upstream again rather than serve the failure.
    again = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&radius_km=5").json()
    assert again["cache"]["hit"] is None

    # And a healthy request for the same site must be cacheable.
    monkeypatch.setattr(httpx.AsyncClient, "post", synthetic_osm_post)
    healthy = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&radius_km=5")
    assert healthy.json()["all_layers_ok"] is True
    assert "max-age=86400" in healthy.headers["cache-control"]


# ---------------------------------------------------------------------------
# request shaping
# ---------------------------------------------------------------------------

@pytest.mark.synthetic_payload
def test_layer_selection_is_honoured(stub_upstream):
    dem_only = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&radius_km=5&layers=dem").json()
    assert set(dem_only["base_layer"]["layers_requested"]) == {"dem"}
    assert "terrain" in dem_only["base_layer"]
    assert "osm" not in dem_only["base_layer"]
    assert "land_cover" not in dem_only["base_layer"]
    assert dem_only["request"]["layers"] == ["dem"]

    osm_only = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&layers=osm&osm_layers=water,settlement").json()
    assert set(osm_only["base_layer"]) >= {"osm"}
    assert osm_only["request"]["osm_layers"] == ["water", "settlement"]
    assert set(osm_only["base_layer"]["osm"]["layers"]) == {"water", "settlement"}


@pytest.mark.synthetic_payload
def test_geometry_is_opt_in(stub_upstream):
    plain = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&layers=osm").json()
    assert all("geometry" not in f for f in plain["base_layer"]["osm"]["features"])
    with_geom = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&layers=osm&include_geometry=true").json()
    assert with_geom["request"]["include_geometry"] is True


@pytest.mark.synthetic_payload
def test_site_landscape_uses_the_saved_site_inputs(stub_upstream):
    site = c.post("/api/v1/sites", json={"name": "Stage 3A site", "lat": 30.901, "lon": 75.8573,
                                         "radius_km": 5, "footprint_ha": 40, "water_mld": 4, "power_mw": 80}).json()
    body = c.get(f"/api/v1/sites/{site['id']}/landscape").json()
    assert body["site"]["site_id"] == site["id"]
    assert body["site"]["name"] == "Stage 3A site"
    inputs = body["development_impact_layer"]["project_inputs"]
    assert inputs["footprint_ha"] == 40 and inputs["water_mld"] == 4 and inputs["power_mw"] == 80
    assert inputs["data_class"] == "user_input"
    assert c.get("/api/v1/sites/does-not-exist/landscape").status_code == 404


@pytest.mark.synthetic_payload
def test_landscape_layer_catalogue_documents_distance_semantics(stub_upstream):
    body = c.get("/api/v1/landscape/layers").json()
    assert body["osm_layers"]["road"]["linear_feature"] is True
    assert "centroid" in body["distance_basis_definitions"]["mapped_centroid"].lower()
    assert body["landscape_layers"] == ["dem", "osm", "landcover"]


@pytest.mark.synthetic_payload
def test_rejected_parcel_is_reported_and_never_used_as_a_mask(stub_upstream):
    body = c.post("/api/v1/landscape", json={
        "lat": 30.901, "lon": 75.8573, "radius_km": 5,
        "parcel_geojson": {"type": "Point", "coordinates": [75.86, 30.90]},
    }).json()
    assert body["parcel"]["status"] == "rejected"
    assert "Polygon or MultiPolygon" in body["parcel"]["reason"]
    assert body["request"]["parcel_supplied"] is True
    # Land cover still computed, but on the site circle - not on the unusable geometry.
    assert body["base_layer"]["land_cover"]["aoi"]["kind"] == "circle"


# ---------------------------------------------------------------------------
# real read path with a SYNTHETIC COG (parcel-masked land cover, end to end)
# ---------------------------------------------------------------------------

@pytest.mark.synthetic_payload
def test_parcel_masks_land_cover_end_to_end_over_real_http(stub_upstream, tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    from app.services import landcover as landcover_module
    from app.services.geo import KM_PER_DEG_LAT, km_per_deg_lon

    # The stubbed land-cover function must be undone for this test: we want the real reader.
    monkeypatch.setattr(landscape, "landcover_summary", landcover_module.landcover_summary)

    grid = wc.split_vertical_grid(600, 600, wc.CLASS_TREE, wc.CLASS_CROPLAND)
    left = SITE["lon"] - 300 * wc.WORLDCOVER_RES_DEG
    top = SITE["lat"] + 300 * wc.WORLDCOVER_RES_DEG
    wc.write_tile(tmp_path, grid, tile_id="N30E075", left=left, top=top, res=wc.WORLDCOVER_RES_DEG)
    with local_http.LocalRangeServer(str(tmp_path)) as server:
        monkeypatch.setattr(landcover_module, "WORLDCOVER_URL_PREFIX", server.base_url)

        d_lat, d_lon = 0.4 / KM_PER_DEG_LAT, 0.4 / km_per_deg_lon(SITE["lat"])
        west_lon = SITE["lon"] - 0.01
        parcel = {"type": "Polygon", "coordinates": [[
            [west_lon - d_lon, SITE["lat"] - d_lat],
            [west_lon + d_lon, SITE["lat"] - d_lat],
            [west_lon + d_lon, SITE["lat"] + d_lat],
            [west_lon - d_lon, SITE["lat"] + d_lat],
            [west_lon - d_lon, SITE["lat"] - d_lat],
        ]]}

        body = c.post("/api/v1/landscape", json={
            "lat": SITE["lat"], "lon": SITE["lon"], "radius_km": 5.0,
            "layers": ["landcover"], "parcel_geojson": parcel,
        }).json()

    assert body["parcel"]["status"] == "ok"
    assert body["parcel"]["area_ha"] == pytest.approx(64.0, rel=0.05)
    land_cover = body["base_layer"]["land_cover"]
    assert land_cover["status"] == "ok"
    assert land_cover["aoi"]["kind"] == "parcel"
    by_value = {cl["value"]: cl for cl in land_cover["classes"]}
    assert by_value[wc.CLASS_TREE]["fraction"] == pytest.approx(1.0)
    assert by_value[wc.CLASS_CROPLAND]["fraction"] == 0.0
    # Impact layer now has a real mapped basis for the vegetation indication.
    veg = next(i for i in body["development_impact_layer"]["indications"]
               if i["id"] == "indicative_vegetation_cleared_ha")
    assert veg["status"] == "ok"
    assert veg["value"] is not None
    assert "ESA WorldCover tree-cover area" in veg["derivation"]
    assert body["development_impact_layer"]["project_inputs"]["site_area_basis"] == "user parcel geometry"


@pytest.mark.synthetic_payload
def test_impact_layer_reports_unavailable_when_land_cover_is_missing(stub_upstream, monkeypatch):
    async def unavailable_landcover(*args, **kwargs):
        return {"status": "unavailable", "reason": "rasterio_unavailable",
                "detail": "rasterio is not installed", "data_class": "unavailable",
                "cacheable": False, "classes": None}

    monkeypatch.setattr(landscape, "landcover_summary", unavailable_landcover)
    body = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&radius_km=5").json()
    veg = next(i for i in body["development_impact_layer"]["indications"]
               if i["id"] == "indicative_vegetation_cleared_ha")
    assert veg["status"] == "unavailable" and veg["value"] is None
    assert body["all_layers_ok"] is False
    assert body["base_layer"]["land_cover"]["data_class"] == "unavailable"

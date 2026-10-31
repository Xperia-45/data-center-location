"""
ESA WorldCover land-cover layer.

DATA PROVENANCE OF THESE TESTS - read before quoting results:
  * The raster bytes are a LOCALLY GENERATED SYNTHETIC GeoTIFF (tests/synthetic_worldcover.py).
    They are NOT ESA WorldCover data and no result here is a real land-cover measurement.
  * The HTTP transport IS real: the production `/vsicurl` range-request read path runs
    against a local server that implements HEAD / 206 Partial Content, so the byte-window
    logic, window maths, class counting, NoData accounting and subsampling are genuinely
    executed rather than mocked.
  * The tile naming and URL layout asserted here were verified against the real public
    bucket listing (v200/2021/map/ESA_WorldCover_10m_2021_v200_N30E075_Map.tif).
"""

from __future__ import annotations

import asyncio
import math
import sys

import pytest

from tests import range_server as local_http
from tests import synthetic_worldcover as wc

from app.services import landcover

pytest.importorskip("rasterio", reason="rasterio is optional; land cover degrades to unavailable without it")

def _await(coro):
    """Runs an async service call in these synchronous tests."""
    return asyncio.run(coro)


SITE_LAT, SITE_LON = 30.901, 75.8573
GRID = 600  # 600 x 600 px at ~10 m => 5 km x 5 km synthetic tile


@pytest.fixture
def serve_worldcover(tmp_path, monkeypatch):
    """Writes a synthetic WorldCover-shaped COG and serves it over real HTTP with ranges."""
    servers: list[local_http.LocalRangeServer] = []

    def _serve(array, tile_id: str = "N30E075", res: float = wc.WORLDCOVER_RES_DEG,
               site: tuple[float, float] = (SITE_LAT, SITE_LON)) -> str:
        height, width = array.shape
        left = site[1] - (width / 2) * res
        top = site[0] + (height / 2) * res
        path = wc.write_tile(tmp_path, array, tile_id=tile_id, left=left, top=top, res=res)
        assert path.exists()
        server = local_http.LocalRangeServer(str(tmp_path))
        server.__enter__()
        servers.append(server)
        monkeypatch.setattr(landcover, "WORLDCOVER_URL_PREFIX", server.base_url)
        return server.base_url

    yield _serve
    for server in servers:
        server.__exit__(None, None, None)


@pytest.mark.fixture_only
def test_tile_identifiers_match_the_public_bucket_layout():
    assert landcover.tile_id_for(SITE_LAT, SITE_LON) == "N30E075"
    assert landcover.tile_id_for(-33.9, 151.2) == "S36E150"
    assert landcover.tile_id_for(0.5, -0.5) == "N00W003"
    url = landcover.tile_url("N30E075")
    assert url == (
        "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
        "ESA_WorldCover_10m_2021_v200_N30E075_Map.tif"
    )
    assert landcover.tiles_for_bbox(75.0, 30.0, 75.5, 30.5) == ["N30E075"]
    assert landcover.tiles_for_bbox(77.9, 32.9, 78.1, 33.1) == ["N30E075", "N30E078", "N33E075", "N33E078"]


@pytest.mark.synthetic_payload
def test_uniform_cover_reports_one_class_and_no_nodata(serve_worldcover):
    serve_worldcover(wc.uniform_grid(GRID, GRID, wc.CLASS_CROPLAND))
    res = _await(landcover.landcover_summary(SITE_LAT, SITE_LON, 1.0))
    assert res["status"] == "ok", res
    assert res["sampling_method"] == "full_window"
    assert res["stride_pixels"] == 1
    assert res["nodata_pixels"] == 0
    by_value = {c["value"]: c for c in res["classes"]}
    assert by_value[wc.CLASS_CROPLAND]["fraction"] == pytest.approx(1.0)
    assert by_value[wc.CLASS_TREE]["fraction"] == 0.0
    # 1 km radius circle ~ 314 ha; the pixel-area approximation must land close to it.
    assert res["sampled_area_ha"] == pytest.approx(math.pi * 100.0, rel=0.05)
    assert by_value[wc.CLASS_CROPLAND]["data_class"] == "mapped"
    assert by_value[wc.CLASS_CROPLAND]["area_data_class"] == "estimated"
    assert "76.7" in res["accuracy_note"]
    assert "ESA WorldCover project 2021" in res["attribution"]
    assert res["dataset"]["native_resolution_m"] == 10
    assert res["tiles"][0]["tile_id"] == "N30E075"


@pytest.mark.synthetic_payload
def test_symmetric_two_class_split_is_counted_at_about_fifty_percent(serve_worldcover):
    grid = wc.split_vertical_grid(GRID, GRID, wc.CLASS_TREE, wc.CLASS_CROPLAND)
    serve_worldcover(grid)
    res = _await(landcover.landcover_summary(SITE_LAT, SITE_LON, 1.0))
    assert res["status"] == "ok", res
    by_value = {c["value"]: c for c in res["classes"]}
    assert by_value[wc.CLASS_TREE]["fraction"] == pytest.approx(0.5, abs=0.01)
    assert by_value[wc.CLASS_CROPLAND]["fraction"] == pytest.approx(0.5, abs=0.01)
    assert sum(c["fraction"] for c in res["classes"]) == pytest.approx(1.0, abs=1e-6)
    assert res["unexpected_class_values"] == []


@pytest.mark.synthetic_payload
def test_nodata_pixels_are_counted_and_never_folded_into_a_class(serve_worldcover):
    grid = wc.uniform_grid(GRID, GRID, wc.CLASS_GRASSLAND)
    grid[:, GRID // 2:] = wc.WORLDCOVER_NODATA  # eastern half unmapped
    serve_worldcover(grid)
    res = _await(landcover.landcover_summary(SITE_LAT, SITE_LON, 1.0))
    assert res["status"] == "ok", res
    assert res["nodata_pixels"] > 0
    assert res["nodata_fraction"] == pytest.approx(0.5, abs=0.02)
    assert res["valid_pixels"] == res["sampled_pixels"] - res["nodata_pixels"]
    by_value = {c["value"]: c for c in res["classes"]}
    assert by_value[wc.CLASS_GRASSLAND]["fraction"] == pytest.approx(1.0)
    assert all(c["value"] != 0 for c in res["classes"]), "NoData must never appear as a class"


@pytest.mark.synthetic_payload
def test_subsampling_is_declared_and_preserves_proportions(serve_worldcover):
    grid = wc.split_vertical_grid(GRID, GRID, wc.CLASS_TREE, wc.CLASS_BUILTUP)
    serve_worldcover(grid)
    res = _await(landcover.landcover_summary(SITE_LAT, SITE_LON, 1.0, max_sampled_pixels=500))
    assert res["status"] == "ok", res
    assert res["stride_pixels"] > 1
    assert res["sampling_method"] == "systematic_subsample"
    assert "does not change or improve the 10 m native resolution" in res["sampling_note"]
    by_value = {c["value"]: c for c in res["classes"]}
    assert by_value[wc.CLASS_TREE]["fraction"] == pytest.approx(0.5, abs=0.05)
    assert by_value[wc.CLASS_BUILTUP]["fraction"] == pytest.approx(0.5, abs=0.05)
    assert res["sampled_pixels"] < 1200


@pytest.mark.synthetic_payload
def test_oversized_aoi_is_refused_instead_of_being_downsampled(serve_worldcover):
    serve_worldcover(wc.uniform_grid(GRID, GRID, wc.CLASS_CROPLAND))
    res = _await(landcover.landcover_summary(SITE_LAT, SITE_LON, 2.0, max_window_pixels=1000))
    assert res["status"] == "unavailable"
    assert res["reason"] == "aoi_exceeds_landcover_budget"
    assert res["classes"] is None
    assert res["data_class"] == "unavailable"
    assert "Reduce the radius" in res["detail"]


@pytest.mark.synthetic_payload
def test_parcel_mask_restricts_the_statistics_to_the_parcel(serve_worldcover):
    from app.services.geo import KM_PER_DEG_LAT, km_per_deg_lon

    grid = wc.split_vertical_grid(GRID, GRID, wc.CLASS_TREE, wc.CLASS_CROPLAND)
    serve_worldcover(grid)
    d_lat = 0.3 / KM_PER_DEG_LAT
    d_lon = 0.3 / km_per_deg_lon(SITE_LAT)
    west_centre_lon = SITE_LON - 0.01
    parcel = {
        "type": "Polygon",
        "coordinates": [[
            [west_centre_lon - d_lon, SITE_LAT - d_lat],
            [west_centre_lon + d_lon, SITE_LAT - d_lat],
            [west_centre_lon + d_lon, SITE_LAT + d_lat],
            [west_centre_lon - d_lon, SITE_LAT + d_lat],
            [west_centre_lon - d_lon, SITE_LAT - d_lat],
        ]],
    }
    res = _await(landcover.landcover_summary(SITE_LAT, SITE_LON, 1.0, parcel_geojson=parcel))
    assert res["status"] == "ok", res
    assert res["aoi"]["kind"] == "parcel"
    by_value = {c["value"]: c for c in res["classes"]}
    assert by_value[wc.CLASS_TREE]["fraction"] == pytest.approx(1.0)
    assert by_value[wc.CLASS_CROPLAND]["fraction"] == 0.0


@pytest.mark.fixture_only
def test_missing_rasterio_reports_unavailable_with_a_clear_reason(monkeypatch):
    monkeypatch.setitem(sys.modules, "rasterio", None)
    res = _await(landcover.landcover_summary(SITE_LAT, SITE_LON, 1.0))
    assert res["status"] == "unavailable"
    assert res["reason"] == "rasterio_unavailable"
    assert "pip install rasterio" in res["detail"]
    assert res["classes"] is None


@pytest.mark.fixture_only
def test_disabled_by_configuration_is_explicit(monkeypatch):
    monkeypatch.setattr(landcover, "WORLDCOVER_ENABLED", False)
    res = _await(landcover.landcover_summary(SITE_LAT, SITE_LON, 1.0))
    assert res["status"] == "unavailable"
    assert res["reason"] == "disabled_by_configuration"


@pytest.mark.synthetic_payload
def test_unreachable_bucket_degrades_to_unavailable(monkeypatch):
    monkeypatch.setattr(landcover, "WORLDCOVER_URL_PREFIX", "http://127.0.0.1:9")
    res = _await(landcover.landcover_summary(SITE_LAT, SITE_LON, 1.0))
    assert res["status"] == "unavailable"
    assert res["reason"] in {"raster_read_failed", "timeout"}
    assert res["classes"] is None
    assert "No class fraction is estimated" in res["detail"] or "exceeded" in res["detail"]



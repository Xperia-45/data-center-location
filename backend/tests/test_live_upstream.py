"""
LIVE upstream tests - real Open-Meteo, real Overpass, real ESA WorldCover.

These are marked `live` and are DESELECTED BY DEFAULT (see backend/pytest.ini) because
they need outbound internet, which the CI sandbox does not have. Run them deliberately
on a machine with egress:

    cd backend && ../.venv/bin/python -m pytest -q -m live -v

Nothing in this file uses a fixture, a stub or a synthetic payload. If one of these
passes, a real provider answered.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import landcover
from app.services.elevation import get_elevation_grid
from app.services.osm import fetch_osm_layers

pytestmark = pytest.mark.live

c = TestClient(app)

LUDHIANA = {"lat": 30.901, "lon": 75.8573, "radius_km": 5.0}


def test_live_copernicus_dem_returns_observation_derived_elevation():
    grid = asyncio.run(get_elevation_grid(LUDHIANA["lat"], LUDHIANA["lon"], LUDHIANA["radius_km"], grid_size=8))
    assert grid["status"] == "ok", grid
    assert grid["resolution_m"] == 90
    assert "10.5270/ESA-c5d3d65" in grid["attribution"]
    # Ludhiana sits on the Sutlej alluvial plain, roughly 240-260 m AMSL.
    assert 150.0 < grid["center_m"] < 400.0, grid
    assert grid["relief_m"] is not None and grid["relief_m"] >= 0.0


def test_live_overpass_returns_mapped_geography():
    result = asyncio.run(fetch_osm_layers(LUDHIANA["lat"], LUDHIANA["lon"], LUDHIANA["radius_km"]))
    assert result["status"] == "ok", result["layer_status"]
    assert result["features"], "a 5 km radius around a major city must have some mapped features"
    for feature in result["features"]:
        assert feature["distance_basis"] in {
            "point_feature", "geometry_min_distance", "vertex_min_distance", "mapped_centroid"
        }
        assert feature["source"].startswith("OpenStreetMap")


def test_live_worldcover_returns_mapped_land_cover():
    pytest.importorskip("rasterio")
    result = asyncio.run(landcover.landcover_summary(LUDHIANA["lat"], LUDHIANA["lon"], 2.0))
    assert result["status"] == "ok", result
    assert result["native_resolution_m"] == 10
    assert result["tiles"][0]["tile_id"] == "N30E075"
    assert result["nodata_fraction"] <= 1.0
    total = sum(cl["fraction"] for cl in result["classes"])
    assert total == pytest.approx(1.0, abs=1e-3)
    present = [cl["label"] for cl in result["classes"] if cl["sampled_pixels"] > 0]
    assert present, "expected at least one land-cover class with pixels"
    assert "ESA WorldCover project 2021" in result["attribution"]


def test_live_landscape_is_fully_available():
    body = c.get("/api/v1/landscape?lat=30.901&lon=75.8573&radius_km=3&grid_size=8").json()
    assert body["all_layers_ok"] is True, {
        k: body["base_layer"].get(k, {}).get("status") for k in ("terrain", "osm", "land_cover")
    }
    assert body["base_layer"]["terrain"]["status"] == "ok"
    assert body["base_layer"]["osm"]["status"] == "ok"
    assert body["base_layer"]["land_cover"]["status"] == "ok"
    for dataset in body["datasets"]:
        if dataset["id"] in {"copernicus_dem_glo90", "openstreetmap_overpass", "esa_worldcover_2021_v200"}:
            assert dataset["availability"]["available"] is True, dataset["id"]

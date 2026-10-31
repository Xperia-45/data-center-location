"""
Optional parcel GeoJSON validation (Shapely).

FIXTURE-ONLY: geometries are constructed in-test; no upstream contact.
"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import parcels
from app.services.geo import KM_PER_DEG_LAT, km_per_deg_lon

c = TestClient(app)

SITE = {"lat": 30.901, "lon": 75.8573, "radius_km": 5.0}


def square_around_site(side_km: float = 1.0, site: dict = SITE) -> dict:
    d_lat = (side_km / 2) / KM_PER_DEG_LAT
    d_lon = (side_km / 2) / km_per_deg_lon(site["lat"])
    lat, lon = site["lat"], site["lon"]
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - d_lon, lat - d_lat],
            [lon + d_lon, lat - d_lat],
            [lon + d_lon, lat + d_lat],
            [lon - d_lon, lat + d_lat],
            [lon - d_lon, lat - d_lat],
        ]],
    }


def test_one_kilometre_square_measures_about_one_hundred_hectares():
    """Area is computed in a local ENU plane, so a 1 km square must come out near 100 ha."""
    result = parcels.validate_parcel(square_around_site(1.0), SITE)
    assert result["status"] == "ok" and result["valid"] is True
    assert result["area_ha"] == pytest.approx(100.0, rel=0.02)
    assert result["perimeter_km"] == pytest.approx(4.0, rel=0.02)
    assert result["bbox_size_km"]["east_west"] == pytest.approx(1.0, rel=0.02)
    assert result["bbox_size_km"]["north_south"] == pytest.approx(1.0, rel=0.02)
    assert result["data_class"] == "user_input"
    assert "not a surveyed" in result["data_label"].lower()
    assert "ENU" in result["area_method"]
    assert result["site"]["within_site_radius"] is True
    assert result["site"]["min_distance_km"] == pytest.approx(0.0, abs=0.02)


def test_parcel_outside_the_site_circle_is_flagged_not_silently_accepted():
    far = square_around_site(1.0, {"lat": SITE["lat"] + 0.2, "lon": SITE["lon"], "radius_km": 5.0})
    result = parcels.validate_parcel(far, SITE)
    assert result["site"]["within_site_radius"] is False
    assert result["site"]["max_distance_km"] > 5.0


def test_invalid_geometry_is_repaired_and_disclosed():
    bowtie = {
        "type": "Polygon",
        "coordinates": [[
            [75.85, 30.90], [75.86, 30.91], [75.86, 30.90], [75.85, 30.91], [75.85, 30.90],
        ]],
    }
    result = parcels.validate_parcel(bowtie, SITE)
    assert result["was_repaired"] is True
    assert result["validity_error"]
    assert result["area_ha"] > 0


@pytest.mark.parametrize("geojson,expected_fragment", [
    ({"type": "Point", "coordinates": [75.85, 30.90]}, "Polygon or MultiPolygon"),
    ({"type": "GeometryCollection", "geometries": []}, "GeometryCollection"),
    ({"type": "FeatureCollection", "features": []}, "FeatureCollection"),
    ({"type": "Polygon", "coordinates": []}, "no coordinates"),
    ({"type": "Polygon", "coordinates": [[[999.0, 0.0], [1.0, 1.0], [0.0, 0.0], [999.0, 0.0]]]},
     "outside [-180, 180]"),
    ({"type": "Polygon", "coordinates": [[[75.0, 95.0], [1.0, 1.0], [0.0, 0.0], [75.0, 95.0]]]},
     "outside [-90, 90]"),
    ({"type": "Polygon", "coordinates": [[[75.0, "x"], [1.0, 1.0], [0.0, 0.0], [75.0, "x"]]]},
     "numeric"),
    ({"foo": "bar"}, "Polygon or MultiPolygon"),
])
def test_unusable_parcels_are_rejected_with_422(geojson, expected_fragment):
    resp = c.post("/api/v1/parcel/validate", json={"geojson": geojson, "site": SITE})
    assert resp.status_code == 422, resp.text
    assert expected_fragment.lower() in resp.json()["detail"].lower()


def test_non_object_geojson_is_rejected_by_request_validation():
    resp = c.post("/api/v1/parcel/validate", json={"geojson": "not-a-dict", "site": SITE})
    assert resp.status_code == 422  # rejected by Pydantic before the service ever sees it


def test_vertex_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(parcels, "MAX_PARCEL_VERTICES", 12)
    ring = [[75.85 + i * 1e-5, 30.90] for i in range(20)] + [[75.85, 30.90]]
    with pytest.raises(parcels.ParcelValidationError):
        parcels.validate_parcel({"type": "Polygon", "coordinates": [ring]}, SITE)


def test_endpoint_accepts_a_valid_parcel_and_returns_the_geometry():
    resp = c.post("/api/v1/parcel/validate", json={"geojson": square_around_site(0.8), "site": SITE})
    assert resp.status_code == 200
    body = resp.json()
    assert body["geometry"]["type"] == "Polygon"
    assert body["area_ha"] == pytest.approx(64.0, rel=0.03)
    assert body["digest"]


def test_endpoint_reports_503_when_shapely_is_unavailable(monkeypatch):
    def explode():
        raise parcels.ParcelValidationUnavailable("Shapely is not installed")

    monkeypatch.setattr(parcels, "_shapely_or_raise", explode)
    resp = c.post("/api/v1/parcel/validate", json={"geojson": square_around_site(1.0), "site": SITE})
    assert resp.status_code == 503
    assert "Shapely" in resp.json()["detail"]


def test_parcel_digest_is_stable_and_bbox_is_pure_python():
    geom = square_around_site(1.0)
    assert parcels.parcel_digest(geom) == parcels.parcel_digest(geom)
    min_lon, min_lat, max_lon, max_lat = parcels.parcel_bbox(geom)
    assert min_lon < SITE["lon"] < max_lon
    assert min_lat < SITE["lat"] < max_lat
    assert math.isclose(max_lat - min_lat, 1.0 / KM_PER_DEG_LAT, rel_tol=1e-6)

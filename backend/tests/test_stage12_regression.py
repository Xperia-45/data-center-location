"""
Stage 1 / Stage 2 regression pins.

These tests exist so Stage 3A cannot quietly change a contract the deployed frontend
depends on. They add no new behaviour: they re-assert the shapes that shipped in PR #4.

No test here contacts an upstream service; where a live layer would be involved the
service degrades to its labelled fallback and the contract is asserted on that.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

c = TestClient(app)

STAGE12_PATHS = [
    "/api/v1/health",
    "/api/v1/sites",
    "/api/v1/sites/{site_id}",
    "/api/v1/sites/{site_id}/analyze",
    "/api/v1/sites/{site_id}/environment",
    "/api/v1/elevation",
    "/api/v1/sites/{site_id}/scenarios",
    "/api/v1/sites/{site_id}/legal",
    "/api/v1/legal/legislation",
    "/api/v1/legal/legislation/{legislation_id}",
    "/api/v1/legal/provisions/{provision_id}",
    "/api/v1/legal/search",
    "/api/v1/legal/import/template",
    "/api/v1/data-sources",
    "/api/v1/scenario-presets",
]


def _make_site(**overrides) -> dict:
    payload = {"name": "Regression site", "lat": 30.901, "lon": 75.8573,
               "radius_km": 5, "footprint_ha": 10, "water_mld": 1.0,
               "power_mw": 20, "cooling": "air", "built_up_m2": 30000}
    payload.update(overrides)
    resp = c.post("/api/v1/sites", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_all_stage12_routes_are_still_registered():
    openapi = c.get("/openapi.json").json()
    for path in STAGE12_PATHS:
        assert path in openapi["paths"], f"Stage 1/2 route disappeared: {path}"


def test_health_contract():
    body = c.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "environmental-intelligence"
    assert body["storage"]["mode"] in ("ephemeral_default", "configured_volume")


def test_site_record_contract():
    site = _make_site()
    assert set(site) == {
        "id", "name", "lat", "lon", "radius_km", "footprint_ha", "water_mld",
        "power_mw", "cooling", "built_up_m2", "created_at",
    }
    assert c.get(f"/api/v1/sites/{site['id']}").json()["id"] == site["id"]
    assert any(s["id"] == site["id"] for s in c.get("/api/v1/sites").json())


def test_analyze_and_environment_contract():
    site = _make_site()
    analysis = c.post(f"/api/v1/sites/{site['id']}/analyze").json()
    assert {"site_id", "findings", "features", "elevation", "data_label", "disclaimer"} <= set(analysis)
    assert analysis["site_id"] == site["id"]
    assert analysis["findings"], "findings must be produced even when a layer is unavailable"
    categories = {f["category"] for f in analysis["findings"]}
    assert {"Water", "Land and ecology", "Community", "Climate and hazards"} <= categories
    climate = next(f for f in analysis["findings"] if f["category"] == "Climate and hazards")
    assert climate["risk"] == "UNKNOWN", "hazards must stay UNKNOWN, never fabricated"
    for feature in analysis["features"]:
        assert {"name", "kind", "lat", "lon", "distance_km", "source"} <= set(feature)
    assert [f["distance_km"] for f in analysis["features"]] == sorted(
        f["distance_km"] for f in analysis["features"]
    )

    env = c.get(f"/api/v1/sites/{site['id']}/environment").json()
    assert {"site_id", "findings", "features", "elevation", "data_label"} <= set(env)
    assert env["site_id"] == site["id"]


def test_elevation_payload_contract():
    resp = c.get("/api/v1/elevation?lat=30.901&lon=75.8573&radius_km=5&grid_size=4")
    body = resp.json()
    assert resp.status_code == 200
    assert {"status", "lat", "lon", "radius_km", "grid_size", "span_km", "values_m", "min_m",
            "max_m", "center_m", "relief_m", "resolution_m", "source", "source_url",
            "copernicus_doi_url", "license", "attribution", "cache_hit", "data_label"} <= set(body)
    assert body["grid_size"] == 4
    assert body["span_km"] == 12.5  # 2 * (40 / 32) * 5 km - Stage 1 ENU constant
    if body["status"] == "ok":
        assert len(body["values_m"]) == 4 and all(len(r) == 4 for r in body["values_m"])
        assert "max-age=604800" in resp.headers["cache-control"]
    else:
        # Sandbox/offline run: the explicit flat fallback must be unambiguous.
        assert body["values_m"] is None
        assert "UNAVAILABLE" in body["data_label"]
        assert resp.headers["cache-control"] == "no-store"


def test_scenario_contract_and_presets():
    site = _make_site()
    body = c.post(f"/api/v1/sites/{site['id']}/scenarios",
                  json={"preset": "mitigation", "mitigation": ["recycled water"]}).json()
    assert body["label"].startswith("Modelled")
    assert {"preset", "label", "inputs", "outputs", "mitigation_applied"} <= set(body)
    assert {"water_demand_mld", "annual_water_million_litres", "land_pressure_index",
            "energy_mw", "community_exposure_note", "regulatory_note"} <= set(body["outputs"])

    presets = c.get("/api/v1/scenario-presets").json()
    assert set(presets) == {"baseline", "high-growth", "mitigation"}
    assert presets["high-growth"]["water_mld"] == 3.5
    assert presets["mitigation"]["cooling"] == "hybrid-dry"


def test_legal_contract_is_untouched():
    laws = c.get("/api/v1/legal/legislation").json()
    assert len(laws) >= 5
    one = c.get(f"/api/v1/legal/legislation/{laws[0]['id']}").json()
    assert "provisions" in one and "amendments" in one
    assert c.post("/api/v1/legal/search", json={"query": "consent"}).status_code == 200
    assert "format" in c.get("/api/v1/legal/import/template").json()


def test_data_sources_keeps_the_frontend_keys():
    sources = c.get("/api/v1/data-sources").json()
    assert sources and all(set(s) == {"provider", "dataset", "url", "license", "quality", "status"}
                           for s in sources)

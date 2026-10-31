import asyncio
import math
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.services import elevation as elev_service
from app.services import environment as env_service
from app.db import conn

c = TestClient(app)


def _clear_all_elevation_caches():
    elev_service.clear_elevation_memory_cache()
    db = conn()
    db.execute("DELETE FROM elevation_cache")
    db.commit()
    db.close()


def test_health():
    r = c.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "storage" in body
    assert body["storage"]["mode"] in ("ephemeral_default", "configured_volume")


def test_site_validation():
    assert c.post("/api/v1/sites", json={"name": "x", "lat": 999, "lon": 0}).status_code == 422


def test_legal_seed():
    laws = c.get("/api/v1/legal/legislation").json()
    assert len(laws) >= 5
    one = c.get(f"/api/v1/legal/legislation/{laws[0]['id']}").json()
    assert "provisions" in one
    assert c.post("/api/v1/legal/search", json={"query": "consent"}).status_code == 200
    sources = c.get("/api/v1/data-sources").json()
    assert any("Copernicus DEM" in s["dataset"] and "Open-Meteo" in s["provider"] for s in sources)


def test_flow():
    s = c.post(
        "/api/v1/sites",
        json={"name": "Test Ludhiana", "lat": 30.901, "lon": 75.8573, "built_up_m2": 30000, "water_mld": 1},
    ).json()
    assert s["id"]
    legal = c.get(f"/api/v1/sites/{s['id']}/legal").json()
    assert any(r["compliance_status"] == "APPROVAL REQUIRED" for r in legal["results"])
    sc = c.post(
        f"/api/v1/sites/{s['id']}/scenarios",
        json={"preset": "mitigation", "mitigation": ["recycled water"]},
    ).json()
    assert sc["label"].startswith("Modelled")


def test_features_sorted_by_distance_and_provenance_preserved(monkeypatch):
    async def fake_overpass(lat, lon, radius_km):
        raw = [
            {"name": "Far village", "kind": "settlement", "lat": lat + 0.04, "lon": lon, "distance_km": 4.45, "source": "OpenStreetMap / Overpass API"},
            {"name": "Mid forest", "kind": "forest", "lat": lat + 0.018, "lon": lon, "distance_km": 2.0, "source": "OpenStreetMap / Overpass API"},
            {"name": "Near wetland", "kind": "wetland", "lat": lat + 0.003, "lon": lon, "distance_km": 0.33, "source": "OpenStreetMap / Overpass API"},
            {"name": "Near pond", "kind": "water", "lat": lat + 0.006, "lon": lon, "distance_km": 0.67, "source": "OpenStreetMap / Overpass API"},
        ]
        return env_service.sort_features_by_distance(raw), True

    monkeypatch.setattr(env_service, "overpass_features", fake_overpass)
    site = {"id": "site-sort-check", "lat": 30.901, "lon": 75.8573, "radius_km": 5.0, "water_mld": 1.0}
    res = asyncio.run(env_service.analyze(site))
    dists = [f["distance_km"] for f in res["features"]]
    assert dists == sorted(dists)
    assert res["features"][0]["name"] == "Near wetland"
    assert res["data_label"] == "Verified real data (OpenStreetMap)"
    assert "elevation" in res


def test_synthetic_fallback_scales_with_radius_and_preserves_labels(monkeypatch):
    async def unavailable_overpass(lat, lon, radius_km):
        return [], False

    monkeypatch.setattr(env_service, "overpass_features", unavailable_overpass)
    small_site = {"id": "site-r1", "lat": 30.7046, "lon": 76.7179, "radius_km": 1.0, "water_mld": 0.2}
    res = asyncio.run(env_service.analyze(small_site))
    assert "Synthetic demonstration data" in res["data_label"]
    dists = [f["distance_km"] for f in res["features"]]
    assert dists == sorted(dists)
    assert all(d <= small_site["radius_km"] for d in dists)
    assert all(f["source"] == "Synthetic demo fallback" for f in res["features"])


def test_site_switching_environment_isolation():
    ludhiana = c.post(
        "/api/v1/sites",
        json={"name": "Ludhiana node", "lat": 30.901, "lon": 75.8573, "radius_km": 5},
    ).json()
    mohali = c.post(
        "/api/v1/sites",
        json={"name": "Mohali candidate", "lat": 30.7046, "lon": 76.7179, "radius_km": 10},
    ).json()

    a1 = c.post(f"/api/v1/sites/{ludhiana['id']}/analyze").json()
    a2 = c.post(f"/api/v1/sites/{mohali['id']}/analyze").json()

    e1 = c.get(f"/api/v1/sites/{ludhiana['id']}/environment").json()
    e2 = c.get(f"/api/v1/sites/{mohali['id']}/environment").json()

    assert e1["site_id"] == ludhiana["id"] == a1["site_id"]
    assert e2["site_id"] == mohali["id"] == a2["site_id"]
    assert e1["site_id"] != e2["site_id"]
    assert [f["distance_km"] for f in e1["features"]] == sorted(f["distance_km"] for f in e1["features"])
    assert [f["distance_km"] for f in e2["features"]] == sorted(f["distance_km"] for f in e2["features"])
    assert c.get("/api/v1/sites/nonexistent-site-id/environment").status_code == 404


def test_elevation_grid_generation_and_batching():
    # Default grid is 20x20 = 400 points -> 4 batches of 100
    coords20, span_km, n = elev_service.generate_enu_elevation_grid_coords(30.901, 75.8573, 5.0)
    assert n == 20
    assert len(coords20) == 400
    assert span_km == 12.5  # 2 * (40 / 32) * 5 km

    # Row 0 Col 0 is North-West; Row 19 Col 19 is South-East
    nw_lat, nw_lon = coords20[0]
    se_lat, se_lon = coords20[-1]
    assert nw_lat > 30.901 > se_lat
    assert nw_lon < 75.8573 < se_lon

    # Verify ENU latitude-adjusted symmetry around origin
    cos_lat = math.cos(math.radians(30.901))
    east_span_km = (se_lon - nw_lon) * 111.32 * cos_lat
    north_span_km = (nw_lat - se_lat) * 111.32
    assert abs(east_span_km - 12.5) < 0.02
    assert abs(north_span_km - 12.5) < 0.02

    batches20 = elev_service.split_coordinate_batches(coords20)
    assert [len(b) for b in batches20] == [100, 100, 100, 100]

    # Configurable non-20x20 grid (e.g. 15x15 = 225 -> [100, 100, 25])
    coords15, _, n15 = elev_service.generate_enu_elevation_grid_coords(17.385, 78.4867, 8.0, grid_size=15)
    assert n15 == 15
    batches15 = elev_service.split_coordinate_batches(coords15)
    assert [len(b) for b in batches15] == [100, 100, 25]

    with pytest.raises(ValueError):
        elev_service.normalize_grid_size(1)
    with pytest.raises(ValueError):
        elev_service.split_coordinate_batches(coords20, max_batch_size=101)


def test_elevation_caching_and_distinct_profiles(monkeypatch):
    _clear_all_elevation_caches()
    calls = []

    class FakeResponse:
        status_code = 200
        headers = {}

        def __init__(self, values):
            self._values = values

        def json(self):
            return {"elevation": self._values}

    async def fake_get(self, url, params=None):
        lats = [float(x) for x in params["latitude"].split(",")]
        lons = [float(x) for x in params["longitude"].split(",")]
        assert len(lats) == len(lons) <= 100
        calls.append(len(lats))
        # Realistic elevation formula: flat ~248m near Ludhiana (lon ~75.85), steeply sloped near Mohali/Shivaliks (lon ~76.7+)
        vals = []
        for la, lo in zip(lats, lons):
            if lo < 76.2:
                vals.append(round(246.0 + (la - 30.901) * 35.0, 2))
            else:
                vals.append(round(320.0 + max(0.0, (la - 30.68) * 1800.0 + (lo - 76.68) * 1500.0), 2))
        return FakeResponse(vals)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    # 1. Uncached 20x20 Ludhiana fetch -> exactly 4 batched requests of 100 coords
    lud = asyncio.run(elev_service.get_elevation_grid(30.901, 75.8573, 5.0, grid_size=20))
    assert lud["status"] == "ok"
    assert lud["grid_size"] == 20
    assert lud["batch_count"] == 4
    assert calls == [100, 100, 100, 100]
    assert lud["relief_m"] < 15.0
    assert "Open-Meteo" in lud["attribution"] and "10.5270/ESA-c5d3d65" in lud["attribution"]

    # 2. Repeat call hits memory cache -> zero new HTTP calls
    lud_mem = asyncio.run(elev_service.get_elevation_grid(30.901, 75.8573, 5.0, grid_size=20))
    assert lud_mem["cache_hit"] == "memory"
    assert len(calls) == 4

    # 3. Clear memory cache -> hits SQLite cache -> still zero new HTTP calls
    elev_service.clear_elevation_memory_cache()
    lud_sql = asyncio.run(elev_service.get_elevation_grid(30.901, 75.8573, 5.0, grid_size=20))
    assert lud_sql["cache_hit"] == "sqlite"
    assert len(calls) == 4

    # 4. Endpoint sets public Cache-Control on hit
    ep = c.get("/api/v1/elevation?lat=30.901&lon=75.8573&radius_km=5&grid_size=20")
    assert ep.status_code == 200
    assert "max-age=604800" in ep.headers.get("cache-control", "")
    assert len(calls) == 4

    # 5. Mohali / Shivalik profile has markedly higher elevation and relief than Ludhiana
    moh = asyncio.run(elev_service.get_elevation_grid(30.7046, 76.7179, 8.0, grid_size=12))
    assert moh["status"] == "ok"
    assert moh["grid_size"] == 12
    assert moh["center_m"] > lud["center_m"] + 80
    assert moh["relief_m"] > lud["relief_m"] * 10


def test_elevation_graceful_fallback_on_failures(monkeypatch):
    # 1. Timeout -> explicit unavailable fallback, never blocks /analyze
    _clear_all_elevation_caches()

    async def timeout_get(self, url, params=None):
        raise httpx.ReadTimeout("Simulated timeout")

    monkeypatch.setattr(httpx.AsyncClient, "get", timeout_get)
    res_timeout = asyncio.run(elev_service.get_elevation_grid(17.385, 78.4867, 5.0, grid_size=20))
    assert res_timeout["status"] == "unavailable"
    assert res_timeout["reason"] == "timeout"
    assert res_timeout["values_m"] is None

    # 2. HTTP 429 rate-limit -> aborts after batch 1 (does not fire batches 2..4) and activates cooldown
    _clear_all_elevation_caches()
    rl_calls = []

    class RateLimitResp:
        status_code = 429
        headers = {"Retry-After": "15"}

    async def ratelimit_get(self, url, params=None):
        rl_calls.append(1)
        return RateLimitResp()

    monkeypatch.setattr(httpx.AsyncClient, "get", ratelimit_get)
    res_429 = asyncio.run(elev_service.get_elevation_grid(17.385, 78.4867, 5.0, grid_size=20))
    assert res_429["status"] == "unavailable"
    assert res_429["reason"] == "rate_limited"
    assert len(rl_calls) == 1  # Aborted immediately on batch 1

    # Immediate retry respects cooldown without hitting upstream again
    res_cooldown = asyncio.run(elev_service.get_elevation_grid(18.0, 79.0, 5.0, grid_size=10))
    assert res_cooldown["status"] == "unavailable"
    assert res_cooldown["reason"] == "rate_limited"
    assert len(rl_calls) == 1

    # 3. Missing / null values in response -> rejected to fallback, never fabricated
    _clear_all_elevation_caches()

    class NullValueResp:
        status_code = 200
        headers = {}

        def json(self):
            return {"elevation": [250.0, None, 251.0, 252.0]}

    async def null_get(self, url, params=None):
        return NullValueResp()

    monkeypatch.setattr(httpx.AsyncClient, "get", null_get)
    res_null = asyncio.run(elev_service.get_elevation_grid(20.0, 77.0, 5.0, grid_size=2))
    assert res_null["status"] == "unavailable"
    assert res_null["reason"] == "missing_values"

    # 4. Malformed response length -> rejected cleanly, HTTP endpoint sets no-store
    _clear_all_elevation_caches()

    class TruncatedResp:
        status_code = 200
        headers = {}

        def json(self):
            return {"elevation": [100.0]}  # Wrong length for 2x2=4

    async def truncated_get(self, url, params=None):
        return TruncatedResp()

    monkeypatch.setattr(httpx.AsyncClient, "get", truncated_get)
    ep_fail = c.get("/api/v1/elevation?lat=21.0&lon=78.0&radius_km=5&grid_size=2")
    assert ep_fail.status_code == 200
    assert ep_fail.json()["status"] == "unavailable"
    assert ep_fail.headers.get("cache-control") == "no-store"


def test_three_locations_radius_extent_and_repeated_switching_integration(monkeypatch):
    _clear_all_elevation_caches()
    upstream_calls = []

    class SyntheticDemResp:
        status_code = 200
        headers = {}

        def __init__(self, vals):
            self._vals = vals

        def json(self):
            return {"elevation": self._vals}

    async def regional_dem_get(self, url, params=None):
        lats = [float(x) for x in params["latitude"].split(",")]
        lons = [float(x) for x in params["longitude"].split(",")]
        upstream_calls.append(len(lats))
        vals = []
        for la, lo in zip(lats, lons):
            if la < 22.0:
                # Hyderabad Deccan plateau (~495-565 m undulating granite plateau)
                vals.append(round(528.0 + math.sin(la * 90.0) * 24.0 + math.cos(lo * 80.0) * 14.0, 2))
            elif lo < 76.2:
                # Ludhiana Sutlej alluvial plain (~244-252 m gentle plain)
                vals.append(round(247.5 + (la - 30.901) * 32.0 + (lo - 75.8573) * 18.0, 2))
            else:
                # Mohali piedmont rising NE toward Shivalik hills (~305-520+ m)
                ne = max(0.0, (la - 30.67) * 1650.0 + (lo - 76.68) * 1350.0)
                vals.append(round(308.0 + ne, 2))
        return SyntheticDemResp(vals)

    monkeypatch.setattr(httpx.AsyncClient, "get", regional_dem_get)

    lud = c.post("/api/v1/sites", json={"name": "Ludhiana node", "lat": 30.901, "lon": 75.8573, "radius_km": 5}).json()
    moh5 = c.post("/api/v1/sites", json={"name": "Mohali 5km", "lat": 30.7046, "lon": 76.7179, "radius_km": 5}).json()
    moh12 = c.post("/api/v1/sites", json={"name": "Mohali 12km", "lat": 30.7046, "lon": 76.7179, "radius_km": 12}).json()
    hyd = c.post("/api/v1/sites", json={"name": "Hyderabad candidate", "lat": 17.385, "lon": 78.4867, "radius_km": 5}).json()

    a_lud = c.post(f"/api/v1/sites/{lud['id']}/analyze").json()
    a_moh5 = c.post(f"/api/v1/sites/{moh5['id']}/analyze").json()
    a_moh12 = c.post(f"/api/v1/sites/{moh12['id']}/analyze").json()
    a_hyd = c.post(f"/api/v1/sites/{hyd['id']}/analyze").json()

    # 4 sites x 4 batches (20x20 = 400 points) = 16 upstream calls total
    assert len(upstream_calls) == 16

    e_lud, e_moh5, e_moh12, e_hyd = (
        a_lud["elevation"],
        a_moh5["elevation"],
        a_moh12["elevation"],
        a_hyd["elevation"],
    )
    # 1. Genuinely distinct grids across Ludhiana, Mohali, Hyderabad
    assert e_lud["values_m"] != e_moh5["values_m"] != e_hyd["values_m"]
    assert 240 <= e_lud["center_m"] <= 255 and e_lud["relief_m"] < 12
    assert e_moh5["center_m"] > 350 and e_moh5["relief_m"] > 120
    assert 490 <= e_hyd["center_m"] <= 565 and 20 < e_hyd["relief_m"] < 90

    # 2. Radius extent scales terrain domain (5 km -> 12.5 km span; 12 km -> 30.0 km span reaching higher Shivaliks)
    assert e_moh5["span_km"] == 12.5
    assert e_moh12["span_km"] == 30.0
    assert e_moh12["max_m"] > e_moh5["max_m"]

    # 3. Repeated site switching (15 full cycles = 45 site switches) triggers ZERO additional Open-Meteo requests and ZERO cross-site mixups
    for _ in range(15):
        for s_obj, expected_center in [
            (lud, e_lud["center_m"]),
            (moh5, e_moh5["center_m"]),
            (hyd, e_hyd["center_m"]),
        ]:
            env_res = c.get(f"/api/v1/sites/{s_obj['id']}/environment").json()
            leg_res = c.get(f"/api/v1/sites/{s_obj['id']}/legal").json()
            sc_res = c.post(f"/api/v1/sites/{s_obj['id']}/scenarios", json={"preset": "baseline"}).json()
            assert env_res["site_id"] == s_obj["id"]
            assert env_res["elevation"]["lat"] == round(s_obj["lat"], 5)
            assert env_res["elevation"]["lon"] == round(s_obj["lon"], 5)
            assert env_res["elevation"]["center_m"] == expected_center
            assert leg_res["site_id"] == s_obj["id"]
            assert sc_res["preset"] == "baseline"
    assert len(upstream_calls) == 16


"""
Cache-integrity regression tests (P1 from the Stage 3A production verification).

A payload written before the provenance correction classified the Copernicus DEM as
`measured` and carried no `data_class` / `surface_model`. Serving one would republish the
withdrawn classification, so:

  1. the cache namespaces are versioned (`dem:v2`, `landscape:v2`) - old rows are unreachable;
  2. both read paths additionally reject a payload that fails the current-schema check, so a
     stale row cannot be served even if it survives under a current key;
  3. legacy rows are purged at startup (cache tables only).

DATA PROVENANCE: every upstream response here is a synthetic stand-in (formula DEM, a
deliberately malformed cached payload). No test contacts a real provider.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import conn, init_db, purge_legacy_cache_rows
from app.main import app
from app.services import elevation as elev
from app.services import landscape
from app.services.cache import NamespacedCache

c = TestClient(app)

SITE = {"lat": 30.901, "lon": 75.8573}

# A payload exactly as the pre-correction code wrote it: no data_class / surface_model and
# the withdrawn "measured" wording.
LEGACY_DEM_PAYLOAD = {
    "status": "ok",
    "lat": SITE["lat"],
    "lon": SITE["lon"],
    "radius_km": 5.0,
    "grid_size": 4,
    "span_km": 12.5,
    "values_m": [[111.0] * 4 for _ in range(4)],
    "min_m": 111.0,
    "max_m": 111.0,
    "center_m": 111.0,
    "relief_m": 0.0,
    "resolution_m": 90,
    "source": "Copernicus DEM GLO-90 (90m) via Open-Meteo Elevation API",
    "data_label": "Measured DEM terrain (Copernicus GLO-90 ~90m, 4x4 ENU grid)",
}


class FakeDemResponse:
    status_code = 200
    headers = {}

    def __init__(self, values):
        self._values = values

    def json(self):
        return {"elevation": self._values}


async def synthetic_dem_get(self, url, params=None, **kwargs):
    """Formula-based stand-in for the Copernicus DEM (NOT measured elevation)."""
    lats = [float(x) for x in params["latitude"].split(",")]
    return FakeDemResponse([round(250.0 + (la - 30.901) * 30.0, 2) for la in lats])


def insert_cache_row(table: str, key: str, payload: dict) -> None:
    db = conn()
    db.execute(
        f"INSERT OR REPLACE INTO {table}(cache_key, payload, created_at) VALUES (?,?,?)",
        (key, json.dumps(payload), "2026-01-01T00:00:00Z"),
    )
    db.commit()
    db.close()


def cache_rows(table: str, prefix: str) -> list[str]:
    db = conn()
    rows = [r[0] for r in db.execute(f"SELECT cache_key FROM {table} WHERE cache_key LIKE ?", (prefix,))]
    db.close()
    return rows


# ---------------------------------------------------------------------------
# 1. Namespaces are versioned
# ---------------------------------------------------------------------------

@pytest.mark.fixture_only
def test_cache_namespaces_are_versioned():
    key = elev.make_elevation_cache_key(SITE["lat"], SITE["lon"], 5.0, 20)
    assert key.startswith("dem:v2:"), "the DEM cache namespace must be versioned"
    assert not key.startswith("dem:v1:")
    assert elev.DEM_CACHE_NAMESPACE == "dem:v2"
    assert landscape._cache.namespace == "landscape:v2"
    # The OSM / land-cover namespaces were introduced together with the corrected schema,
    # so they have no pre-correction rows and stay at v1.
    assert landscape._landcover_cache.namespace == "landcover:v1"


# ---------------------------------------------------------------------------
# 2. Legacy DEM payloads cannot be served
# ---------------------------------------------------------------------------

@pytest.mark.synthetic_payload
def test_legacy_dem_payload_is_rejected_even_under_the_current_key(monkeypatch):
    """
    The strongest form of the bug: a stale payload sitting under the *current* key (a
    database restored from backup, a namespace ever reused) must still be refused.
    """
    monkeypatch.setattr(httpx.AsyncClient, "get", synthetic_dem_get)
    key = elev.make_elevation_cache_key(SITE["lat"], SITE["lon"], 5.0, 4)
    insert_cache_row("elevation_cache", key, LEGACY_DEM_PAYLOAD)
    elev.clear_elevation_memory_cache()

    body = c.get("/api/v1/elevation", params={**SITE, "radius_km": 5, "grid_size": 4}).json()

    assert body["data_class"] != "measured"
    assert body["data_class"] == "observation_derived"
    assert body["cache_hit"] is None, "the legacy row must not be served as a cache hit"
    assert body["min_m"] != 111.0, "the legacy values must not be returned"
    assert "Measured DEM terrain" not in body["data_label"]


@pytest.mark.synthetic_payload
def test_legacy_dem_namespace_row_is_unreachable(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", synthetic_dem_get)
    insert_cache_row("elevation_cache", "dem:v1:30.9010:75.8573:5.00:4", LEGACY_DEM_PAYLOAD)
    elev.clear_elevation_memory_cache()

    body = c.get("/api/v1/elevation", params={**SITE, "radius_km": 5, "grid_size": 4}).json()
    assert body["data_class"] == "observation_derived"
    assert body["cache_hit"] in (None, "memory")
    assert body["min_m"] != 111.0, "the pre-correction row must never be served"
    # The orphaned v1 row is unreachable by key; startup removes it (test below).
    assert cache_rows("elevation_cache", "dem:v1:%") == ["dem:v1:30.9010:75.8573:5.00:4"]


@pytest.mark.fixture_only
def test_legacy_payload_guard_accepts_only_the_current_schema():
    assert elev._payload_provenance_is_current(LEGACY_DEM_PAYLOAD) is False
    assert elev._payload_provenance_is_current({**LEGACY_DEM_PAYLOAD, "data_class": "measured",
                                                "surface_model": "DSM"}) is False
    assert elev._payload_provenance_is_current({**LEGACY_DEM_PAYLOAD,
                                                "data_class": "observation_derived",
                                                "surface_model": "DSM"}) is True
    # An explicitly unavailable payload makes no classification claim, so it is never
    # mistaken for geography - but it is also never cached (see below).
    assert elev._payload_provenance_is_current({"status": "unavailable"}) is True


# ---------------------------------------------------------------------------
# 3. Legacy landscape payloads cannot bypass the corrected schema
# ---------------------------------------------------------------------------

def legacy_base_layer() -> dict:
    return {
        "content_digest": "sha256:legacy",
        "layer_model": "base",
        "cacheable": True,
        "terrain": {**LEGACY_DEM_PAYLOAD, "data_class": "measured"},
    }


@pytest.mark.fixture_only
def test_landscape_read_guard_rejects_a_measured_terrain_block():
    assert landscape.cached_base_layer_is_current(legacy_base_layer()) is False
    current = legacy_base_layer()
    current["terrain"] = {**current["terrain"], "data_class": "observation_derived",
                          "surface_model": "Digital Surface Model (DSM)"}
    assert landscape.cached_base_layer_is_current(current) is True
    # A base layer without a DEM (or with an unavailable DEM) makes no claim to reject.
    assert landscape.cached_base_layer_is_current({"content_digest": "x"}) is True
    assert landscape.cached_base_layer_is_current(
        {"terrain": {"status": "unavailable", "data_class": "unavailable"}}) is True
    assert landscape.cached_base_layer_is_current("not-a-dict") is False


@pytest.mark.synthetic_payload
def test_legacy_landscape_base_layer_is_not_served(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", synthetic_dem_get)
    key = landscape.cache_key(lat=SITE["lat"], lon=SITE["lon"], radius_km=3.0, grid_size=4,
                              layers=["dem"], include_geometry=False, osm_layers=[], parcel=None)
    assert key.startswith("landscape:v2:")
    insert_cache_row("landscape_cache", key, legacy_base_layer())
    landscape.clear_landscape_cache()
    # Re-insert after the clear so the row is present when the request is served.
    insert_cache_row("landscape_cache", key, legacy_base_layer())

    body = c.get("/api/v1/landscape",
                 params={**SITE, "radius_km": 3, "grid_size": 4, "layers": "dem"}).json()

    terrain = body["base_layer"]["terrain"]
    assert terrain["data_class"] != "measured"
    assert terrain["data_class"] == "observation_derived"
    assert body["base_layer"]["cache_hit"] is None, "the legacy base layer must be a cache miss"
    assert body["base_layer"]["content_digest"] != "sha256:legacy"


@pytest.mark.fixture_only
def test_namespaced_cache_validator_evicts_rejected_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL_SQLITE", str(tmp_path / "guard.db"))
    init_db()
    cache = NamespacedCache("test:v2", table="landscape_cache", max_entries=8,
                            validator=lambda p: p.get("schema") == 2)
    cache.put("test:v2:a", {"schema": 1, "cacheable": True})
    assert cache.get("test:v2:a") is None, "a payload failing the validator is a miss"
    assert cache._mem == {}, "and it is evicted from the memory tier"
    cache.put("test:v2:b", {"schema": 2, "cacheable": True})
    assert cache.get("test:v2:b")["schema"] == 2


# ---------------------------------------------------------------------------
# 4. Fresh entries carry the full provenance
# ---------------------------------------------------------------------------

@pytest.mark.synthetic_payload
def test_fresh_cache_entries_carry_the_correct_classification(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", synthetic_dem_get)
    elev.clear_elevation_memory_cache()
    params = {**SITE, "radius_km": 5, "grid_size": 8}

    first = c.get("/api/v1/elevation", params=params).json()
    assert first["status"] == "ok" and first["cache_hit"] is None
    second = c.get("/api/v1/elevation", params=params).json()
    assert second["cache_hit"] in {"memory", "sqlite"}

    for body in (first, second):
        assert body["data_class"] == "observation_derived"
        assert "Digital Surface Model" in body["surface_model"]
        assert "NOT bare-earth" in body["surface_model"]
        assert "Observed-derived" in body["data_label"]
        assert "measured" not in body["data_label"].lower().replace("not measured", "")


@pytest.mark.synthetic_payload
def test_cached_landscape_terrain_keeps_the_correct_classification(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", synthetic_dem_get)
    landscape.clear_landscape_cache()
    params = {**SITE, "radius_km": 3, "grid_size": 4, "layers": "dem"}

    first = c.get("/api/v1/landscape", params=params).json()["base_layer"]
    second = c.get("/api/v1/landscape", params=params).json()["base_layer"]
    assert second["cache_hit"] in {"memory", "sqlite"}
    for base in (first, second):
        assert base["terrain"]["data_class"] == "observation_derived"
        assert "Digital Surface Model" in base["terrain"]["surface_model"]
        assert "Measured DEM terrain" not in base["terrain"]["data_label"]


# ---------------------------------------------------------------------------
# 5. Unavailable elevation stays explicitly unavailable
# ---------------------------------------------------------------------------

@pytest.mark.synthetic_payload
def test_unavailable_elevation_is_explicit_and_is_never_cached(monkeypatch):
    class Boom:
        status_code = 503
        headers = {}

        def json(self):
            return {"error": True, "reason": "synthetic upstream outage"}

    async def failing_get(self, url, params=None, **kwargs):
        return Boom()

    monkeypatch.setattr(httpx.AsyncClient, "get", failing_get)
    elev.clear_elevation_memory_cache()

    body = c.get("/api/v1/elevation", params={**SITE, "radius_km": 5, "grid_size": 6}).json()
    assert body["status"] == "unavailable"
    assert body["data_class"] == "unavailable"
    assert body["surface_model"] is None
    assert "UNAVAILABLE" in body["data_label"]
    assert body["values_m"] is None
    assert cache_rows("elevation_cache", "dem:v2:30.9010:75.8573:5.00:6") == []


# ---------------------------------------------------------------------------
# 6. Startup purge
# ---------------------------------------------------------------------------

@pytest.mark.fixture_only
def test_startup_purges_only_legacy_cache_rows():
    insert_cache_row("elevation_cache", "dem:v1:30.9010:75.8573:5.00:20", LEGACY_DEM_PAYLOAD)
    insert_cache_row("landscape_cache", "landscape:v1:lat=30.9010", legacy_base_layer())
    insert_cache_row("elevation_cache", "dem:v2:30.9010:75.8573:5.00:20",
                     {**LEGACY_DEM_PAYLOAD, "data_class": "observation_derived",
                      "surface_model": "DSM"})

    removed = purge_legacy_cache_rows()

    assert removed == 2
    assert cache_rows("elevation_cache", "dem:v1:%") == []
    assert cache_rows("landscape_cache", "landscape:v1:%") == []
    assert cache_rows("elevation_cache", "dem:v2:%") != [], "current entries must survive"


@pytest.mark.fixture_only
def test_init_db_purges_legacy_rows_on_startup():
    """The purge must be wired into startup, not just available as a helper."""
    insert_cache_row("elevation_cache", "dem:v1:17.3850:78.4867:5.00:20", LEGACY_DEM_PAYLOAD)
    insert_cache_row("landscape_cache", "landscape:v1:lat=17.3850", legacy_base_layer())
    assert cache_rows("elevation_cache", "dem:v1:%") != []

    init_db()

    assert cache_rows("elevation_cache", "dem:v1:%") == []
    assert cache_rows("landscape_cache", "landscape:v1:%") == []

"""
DEM rate-limit retry-policy regression tests.

PRODUCTION DEFECT UNDER TEST
----------------------------
`GET /api/v1/sites/{id}/environment` re-fetched the elevation grid on *every* call whenever
the stored analysis payload's elevation status was not "ok", and only persisted a successful
refresh. A single Open-Meteo 429 therefore left a permanently non-"ok" row, so every later
page view fired a fresh upstream request against a per-IP free-tier limit -- the retries fed
the limit that produced them, and the process-global cooldown then reported `rate_limited`
for every site.

These tests pin the corrected behaviour:
  * a failed grid is retried at most once per ELEVATION_FAILURE_RETRY_DELAY_SEC, not per call;
  * non-retryable failures (malformed / out-of-range data) are never retried automatically;
  * a recovered grid is persisted, so later views cost no upstream request;
  * the flat reference plane, `data_class: "unavailable"`, null values and the DSM wording on
    success are all preserved -- nothing is fabricated and nothing is silently zeroed;
  * `retry_after_sec` / `retryable` let a caller tell rate limiting from a hard failure.

DATA PROVENANCE: every provider response here is a synthetic stand-in (a formula DEM, a
deliberate 429, a deliberately malformed body). No test contacts a real provider.
"""

from __future__ import annotations

import asyncio
import datetime
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import conn
from app.main import app
from app.services import elevation as elev

c = TestClient(app)

SITES = {
    "Ludhiana": (30.9010, 75.8573),
    "Mohali": (30.7046, 76.7179),
    "Hyderabad": (17.3850, 78.4867),
}

_REAL_GET = httpx.AsyncClient.get
_REAL_POST = httpx.AsyncClient.post


class FakeResp:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        if self.status_code >= 400 and not self._payload:
            raise ValueError("no json")
        return self._payload


class ProviderStub:
    """Intercepts only Open-Meteo elevation traffic; every other request passes through."""

    def __init__(self):
        self.dem_calls = 0
        self.overpass_calls = 0
        self.mode = "ok"

    def install(self, monkeypatch):
        async def get(self_client, url, *args, **kwargs):
            if str(url).startswith(elev.OPEN_METEO_ELEVATION_URL.rsplit("/v1/", 1)[0]):
                self.dem_calls += 1
                if self.mode == "rate_limited":
                    return FakeResp(429, {"error": True, "reason": "rate limited"},
                                    {"Retry-After": "2"})
                if self.mode == "malformed":
                    return FakeResp(200, {"elevation": ["not-a-number"]})
                params = kwargs.get("params") or {}
                lats = str(params.get("latitude", "")).split(",")
                return FakeResp(200, {"elevation": [round(250.0 + i * 0.1, 2)
                                                    for i in range(len(lats))]})
            return await _REAL_GET(self_client, url, *args, **kwargs)

        async def post(self_client, url, *args, **kwargs):
            if "overpass" in str(url) or "interpreter" in str(url):
                self.overpass_calls += 1
                return FakeResp(200, {"elements": []})
            return await _REAL_POST(self_client, url, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "get", get)
        monkeypatch.setattr(httpx.AsyncClient, "post", post)


@pytest.fixture
def stub(monkeypatch):
    s = ProviderStub()
    s.install(monkeypatch)
    return s


def make_site(name: str, radius_km: float = 5.0) -> str:
    lat, lon = SITES[name]
    site = c.post("/api/v1/sites", json={
        "name": name, "lat": lat, "lon": lon, "radius_km": radius_km,
        "footprint_ha": 12, "water_mld": 4.5, "power_mw": 30,
        "cooling": "air", "built_up_m2": 40000,
    }).json()
    return site["id"]


def stored_elevation(site_id: str) -> dict:
    row = conn().execute(
        "SELECT payload FROM analyses WHERE site_id=? ORDER BY created_at DESC LIMIT 1",
        (site_id,),
    ).fetchone()
    return json.loads(row[0])["elevation"]


def backdate_stored_failure(site_id: str, minutes: float) -> None:
    """Simulates the passage of time without freezing the clock."""
    db = conn()
    row = db.execute(
        "SELECT id, payload FROM analyses WHERE site_id=? ORDER BY created_at DESC LIMIT 1",
        (site_id,),
    ).fetchone()
    payload = json.loads(row[1])
    payload["elevation"]["observed_at"] = (
        datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes)
    ).isoformat() + "Z"
    db.execute("UPDATE analyses SET payload=? WHERE id=?", (json.dumps(payload), row[0]))
    db.commit()
    db.close()


def assert_flat_reference_plane(elev_payload: dict) -> None:
    """The fallback must stay an explicit, unmeasured plane - never zeros, never a fake grid."""
    assert elev_payload["status"] == "unavailable"
    assert elev_payload["data_class"] == "unavailable"
    assert elev_payload["surface_model"] is None
    assert elev_payload["attribution"] is None
    for field in ("values_m", "min_m", "max_m", "center_m", "relief_m"):
        assert elev_payload[field] is None, f"{field} must be null, not 0"
    assert "UNAVAILABLE" in elev_payload["data_label"]
    assert "not measured" in elev_payload["data_label"]


# --------------------------------------------------------------------------- core defect

def test_rate_limited_site_does_not_refetch_on_every_environment_call(stub):
    """The regression itself: repeated views of a rate-limited site must not each hit upstream."""
    stub.mode = "rate_limited"
    sid = make_site("Ludhiana")

    stub.dem_calls = 0
    analysed = c.post(f"/api/v1/sites/{sid}/analyze").json()
    assert analysed["elevation"]["reason"] == "rate_limited"
    assert stub.dem_calls == 1, "one analysis = one upstream attempt (aborts at the first 429)"

    for visit in range(1, 4):
        elev._rate_limited_until = 0.0  # let the cooldown expire so only the new policy holds
        body = c.get(f"/api/v1/sites/{sid}/environment").json()
        assert stub.dem_calls == 1, f"visit {visit} must not re-request the DEM inside the window"
        assert_flat_reference_plane(body["elevation"])

    assert stored_elevation(sid)["status"] == "unavailable"
    assert stored_elevation(sid)["observed_at"], "the failure is persisted so it can be aged"


def test_failed_elevation_is_retried_once_the_delay_window_elapses(stub):
    stub.mode = "rate_limited"
    sid = make_site("Mohali")
    c.post(f"/api/v1/sites/{sid}/analyze")
    assert stub.dem_calls == 1

    elev._rate_limited_until = 0.0
    c.get(f"/api/v1/sites/{sid}/environment")
    assert stub.dem_calls == 1, "still inside the retry window"

    backdate_stored_failure(sid, minutes=10)  # default window is 300 s
    elev._rate_limited_until = 0.0
    c.get(f"/api/v1/sites/{sid}/environment")
    assert stub.dem_calls == 2, "outside the window the transient failure is retried exactly once"


def test_non_retryable_failure_is_never_retried_automatically(stub):
    stub.mode = "malformed"
    sid = make_site("Hyderabad")
    analysed = c.post(f"/api/v1/sites/{sid}/analyze").json()
    # A body that cannot be turned into a full grid: bad data, not a transient condition.
    assert analysed["elevation"]["reason"] == "malformed_response"
    assert analysed["elevation"]["retryable"] is False
    calls = stub.dem_calls

    backdate_stored_failure(sid, minutes=60)
    elev._rate_limited_until = 0.0
    body = c.get(f"/api/v1/sites/{sid}/environment").json()
    assert stub.dem_calls == calls, "a malformed provider body will not heal on a retry"
    assert_flat_reference_plane(body["elevation"])


def test_legacy_failure_without_observed_at_is_retried_once(stub):
    """A row written before `observed_at` existed must still recover, then start backing off."""
    stub.mode = "rate_limited"
    sid = make_site("Ludhiana")
    c.post(f"/api/v1/sites/{sid}/analyze")
    db = conn()
    row = db.execute(
        "SELECT id, payload FROM analyses WHERE site_id=? ORDER BY created_at DESC LIMIT 1",
        (sid,),
    ).fetchone()
    payload = json.loads(row[1])
    payload["elevation"].pop("observed_at", None)
    db.execute("UPDATE analyses SET payload=? WHERE id=?", (json.dumps(payload), row[0]))
    db.commit()
    db.close()

    elev._rate_limited_until = 0.0
    c.get(f"/api/v1/sites/{sid}/environment")
    assert stub.dem_calls == 2, "a legacy failure gets one immediate retry"
    stored = stored_elevation(sid)
    assert stored["status"] == "unavailable"
    assert stored.get("observed_at"), "/environment must persist the failure it just observed"

    elev._rate_limited_until = 0.0
    c.get(f"/api/v1/sites/{sid}/environment")
    assert stub.dem_calls == 2, "...and then the window applies to it too"


# ------------------------------------------------------------------------------ recovery

def test_data_integrity_reasons_are_never_retried_automatically():
    """Retrying bad *data* cannot help; only transient transport/limit conditions are retried."""
    for reason in ("malformed_response", "missing_values", "batch_size_exceeded", "api_error"):
        payload = elev.unavailable_elevation_payload(5.0, 4, reason=reason, detail="x")
        assert payload["retryable"] is False, reason
        assert elev.failed_elevation_retry_is_due(payload) is False, reason
    for reason in sorted(elev.RETRYABLE_FAILURE_REASONS):
        fresh = elev.unavailable_elevation_payload(5.0, 4, reason=reason, detail="x")
        assert fresh["retryable"] is True, reason
        assert elev.failed_elevation_retry_is_due(fresh) is False, (
            f"a fresh {reason} must wait out the window"
        )
        stale = dict(fresh, observed_at=(
            datetime.datetime.utcnow() - datetime.timedelta(seconds=elev.FAILURE_RETRY_DELAY_SEC + 1)
        ).isoformat() + "Z")
        assert elev.failed_elevation_retry_is_due(stale) is True, reason
        # A retryable failure stored by an older build (no timestamp) is retried once.
        assert elev.failed_elevation_retry_is_due(dict(fresh, observed_at=None)) is True


def test_recovery_persists_ok_payload_and_stops_upstream_calls(stub):
    stub.mode = "rate_limited"
    sid = make_site("Mohali")
    c.post(f"/api/v1/sites/{sid}/analyze")
    backdate_stored_failure(sid, minutes=10)

    stub.mode = "ok"
    elev._rate_limited_until = 0.0
    before = stub.dem_calls
    body = c.get(f"/api/v1/sites/{sid}/environment").json()["elevation"]
    assert stub.dem_calls - before == 4, "a 20x20 grid is 4 batches of <=100 coordinates"
    assert body["status"] == "ok"
    assert body["data_class"] == "observation_derived"
    assert "Digital Surface Model (DSM)" in body["surface_model"]
    assert "NOT bare-earth" in body["surface_model"]
    assert body["attribution"] and "Copernicus" in body["attribution"]
    assert body["resolution_m"] == 90

    for _ in range(2):
        before = stub.dem_calls
        again = c.get(f"/api/v1/sites/{sid}/environment").json()["elevation"]
        assert stub.dem_calls == before, "a persisted ok grid must cost no upstream request"
        assert again["status"] == "ok"
        assert again["values_m"] == body["values_m"]


# ----------------------------------------------------------------------- retry signalling

def test_rate_limited_payload_publishes_a_retry_hint(stub):
    stub.mode = "rate_limited"
    sid = make_site("Hyderabad")
    body = c.post(f"/api/v1/sites/{sid}/analyze").json()["elevation"]
    assert body["reason"] == "rate_limited"
    assert body["retryable"] is True
    assert body["retry_after_sec"] is not None and 0 < body["retry_after_sec"] <= 120
    assert "429" in body["detail"]
    assert_flat_reference_plane(body)


def test_cooldown_skip_is_distinguishable_from_an_upstream_429(stub):
    """Both are `rate_limited`; the detail and the retry hint must say which one it was."""
    stub.mode = "rate_limited"
    grid = c.get("/api/v1/elevation", params={"lat": 30.901, "lon": 75.8573,
                                              "radius_km": 5, "grid_size": 4}).json()
    assert "429" in grid["detail"]

    skipped = c.get("/api/v1/elevation", params={"lat": 30.7046, "lon": 76.7179,
                                                 "radius_km": 5, "grid_size": 4}).json()
    assert skipped["reason"] == "rate_limited"
    assert "cooldown" in skipped["detail"]
    assert skipped["retry_after_sec"] is not None
    assert skipped["values_m"] is None


# --------------------------------------------------------------------- amplification

def test_three_sites_switching_costs_one_grid_each_and_revisits_are_free(stub):
    ids = {name: make_site(name) for name in SITES}
    stub.dem_calls = 0
    for sid in ids.values():
        c.post(f"/api/v1/sites/{sid}/analyze")
    assert stub.dem_calls == 12, "3 new sites x 4 batches = 12 upstream requests"

    for sid in ids.values():
        before = stub.dem_calls
        c.get(f"/api/v1/sites/{sid}/environment")
        assert stub.dem_calls == before, "revisiting a healthy site must be free"


def test_concurrent_environment_calls_share_a_single_upstream_grid(stub):
    sid = make_site("Ludhiana")
    stub.dem_calls = 0

    async def hit_it():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(*[
                client.get(f"/api/v1/sites/{sid}/environment") for _ in range(5)
            ])
        return responses

    responses = asyncio.run(hit_it())
    bodies = [r.json()["elevation"] for r in responses]
    assert all(b["status"] == "ok" for b in bodies)
    assert stub.dem_calls == 4, "5 concurrent views = one 20x20 grid, not five"
    assert len({json.dumps(b["values_m"]) for b in bodies}) == 1
    assert not elev._INFLIGHT_LOCKS and not elev._INFLIGHT_COUNTS, "no leaked single-flight state"


# ------------------------------------------------------------------- explicit resolution

def test_explicit_grid_size_still_forces_a_fetch(stub):
    """A different resolution is a different grid, so the retry window must not block it."""
    sid = make_site("Ludhiana")
    c.post(f"/api/v1/sites/{sid}/analyze")
    before = stub.dem_calls
    body = c.get(f"/api/v1/sites/{sid}/environment", params={"grid_size": 4}).json()["elevation"]
    assert stub.dem_calls > before
    assert body["status"] == "ok" and body["grid_size"] == 4

"""
Overpass mirror-failover regression tests.

WHY: the deployed backend was observed live on 2026-10-31 returning, for every one of the 8 OSM
layers, `"Overpass transport error: All connection attempts failed"` against its single
hard-coded endpoint - so the topographic sheet had no geography at all. `fetch_layer` now tries
an ordered list of endpoints and only reports a layer unavailable when all of them fail.

These tests pin:
  * failover on transport errors, timeouts, 429/504 and other retryable statuses;
  * NO failover on statuses that are about our request (400 and friends), because every mirror
    runs the same Overpass QL;
  * accurate per-endpoint error reporting when everything fails;
  * that a failure is never reported as a successful empty result, and is never cached;
  * partial layer availability and reuse of the layers that did succeed.

DATA PROVENANCE: every response is a synthetic stand-in. No test contacts a real Overpass
instance, and no feature in these fixtures is real geography.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services import osm as osm_service

PRIMARY = "https://mirror-a.invalid/api/interpreter"
SECONDARY = "https://mirror-b.invalid/api/interpreter"
TERTIARY = "https://mirror-c.invalid/api/interpreter"

SITE = {"lat": 30.901, "lon": 75.8573, "radius_km": 3.0}


class FakeResp:
    def __init__(self, status_code, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self._text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def elements_payload(n=2):
    """Synthetic Overpass body: n closed water ways near the site."""
    return {
        "elements": [
            {
                "type": "way",
                "id": 1000 + i,
                "tags": {"natural": "water", "name": f"Synthetic pond {i}"},
                "geometry": [
                    {"lat": 30.90 + 0.001 * i, "lon": 75.85},
                    {"lat": 30.90 + 0.001 * i, "lon": 75.86},
                    {"lat": 30.91 + 0.001 * i, "lon": 75.86},
                    {"lat": 30.90 + 0.001 * i, "lon": 75.85},
                ],
            }
            for i in range(n)
        ]
    }


class MirrorStub:
    """
    URL-aware stand-in for `httpx.AsyncClient.post`.

    `behaviour` maps an endpoint host to either a FakeResp, an exception instance, or a list of
    those (consumed per attempt) so a test can say "first attempt times out, second succeeds".
    """

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls: list[str] = []

    def install(self, monkeypatch):
        async def post(self_client, url, data=None, **kwargs):
            url = str(url)
            self.calls.append(url)
            host_key = next((k for k in self.behaviour if k in url), None)
            if host_key is None:
                raise AssertionError(f"unexpected endpoint requested: {url}")
            spec = self.behaviour[host_key]
            if isinstance(spec, list):
                index = min(self.calls.count(url) - 1, len(spec) - 1)
                spec = spec[index]
            if isinstance(spec, Exception):
                raise spec
            return spec

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        return self

    def count(self, fragment):
        return sum(1 for u in self.calls if fragment in u)


@pytest.fixture(autouse=True)
def _clean_layer_cache():
    """Per-layer OSM caching would otherwise let one test serve another test's result."""
    osm_service._layer_cache.clear()
    yield
    osm_service._layer_cache.clear()


@pytest.fixture
def mirrors(monkeypatch):
    monkeypatch.setenv("OVERPASS_MIRRORS", f"{PRIMARY}, {SECONDARY}, {TERTIARY}")
    monkeypatch.delenv("OVERPASS_URL", raising=False)
    return osm_service.overpass_endpoints()


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------- configuration

def test_mirrors_are_configured_in_order_and_deduplicated(monkeypatch):
    monkeypatch.setenv("OVERPASS_MIRRORS", f"{PRIMARY}, {SECONDARY} {PRIMARY}")
    assert osm_service.overpass_endpoints() == [PRIMARY, SECONDARY]


def test_legacy_overpass_url_is_honoured_so_pinned_deployments_do_not_change(monkeypatch):
    monkeypatch.delenv("OVERPASS_MIRRORS", raising=False)
    monkeypatch.setenv("OVERPASS_URL", PRIMARY)
    assert osm_service.overpass_endpoints() == [PRIMARY]


def test_a_fresh_deployment_gets_the_public_mirror_list(monkeypatch):
    monkeypatch.delenv("OVERPASS_MIRRORS", raising=False)
    monkeypatch.delenv("OVERPASS_URL", raising=False)
    endpoints = osm_service.overpass_endpoints()
    assert len(endpoints) >= 2, "one endpoint is not a reliable dependency for a deployed service"
    assert endpoints[0] == "https://overpass-api.de/api/interpreter"


# --------------------------------------------------------------------- failover paths

def test_primary_endpoint_succeeds_and_no_mirror_is_contacted(mirrors, monkeypatch):
    stub = MirrorStub({
        "mirror-a": FakeResp(200, elements_payload(3)),
        "mirror-b": FakeResp(200, elements_payload(9)),
        "mirror-c": FakeResp(200, elements_payload(9)),
    }).install(monkeypatch)

    result = run(osm_service.fetch_layer("water", **SITE))

    assert result["status"] == "ok"
    assert result["endpoint_used"] == PRIMARY
    assert len(result["features"]) == 3, "the primary's data is what is served"
    assert stub.calls == [PRIMARY], "no failover when the primary answers"
    assert result["endpoints_attempted"] == [
        {"endpoint": PRIMARY, "status": "ok", "detail": ""}
    ]


def test_primary_connection_failure_falls_over_to_the_secondary(mirrors, monkeypatch):
    stub = MirrorStub({
        "mirror-a": httpx.ConnectError("All connection attempts failed"),
        "mirror-b": FakeResp(200, elements_payload(2)),
        "mirror-c": FakeResp(200, elements_payload(9)),
    }).install(monkeypatch)

    result = run(osm_service.fetch_layer("water", **SITE))

    assert result["status"] == "ok"
    assert result["endpoint_used"] == SECONDARY
    assert len(result["features"]) == 2
    assert stub.calls == [PRIMARY, SECONDARY], "exactly one failover, tertiary untouched"
    assert result["endpoints_attempted"][0]["status"] == "network_error"
    assert "All connection attempts failed" in result["endpoints_attempted"][0]["detail"]


def test_every_mirror_failing_reports_unavailable_with_per_endpoint_errors(mirrors, monkeypatch):
    stub = MirrorStub({
        "mirror-a": httpx.ConnectError("All connection attempts failed"),
        "mirror-b": httpx.ConnectError("Connection refused"),
        "mirror-c": httpx.ConnectError("Name or service not known"),
    }).install(monkeypatch)

    result = run(osm_service.fetch_layer("water", **SITE))

    assert result["status"] == "unavailable"
    assert result["reason"] == "network_error"
    assert result["features"] == []
    assert result["cacheable"] is False, "a failed layer is never cached"
    assert len(stub.calls) == 3, "every mirror was tried"
    for host, message in (("mirror-a", "All connection attempts failed"),
                          ("mirror-b", "Connection refused"),
                          ("mirror-c", "Name or service not known")):
        entry = next(a for a in result["endpoints_attempted"] if host in a["endpoint"])
        assert entry["status"] == "network_error"
        assert message in result["detail"], f"{host}'s error is preserved in the detail"
    assert "No features are inferred" in result["detail"]


def test_timeout_on_the_primary_falls_over_and_all_timeouts_report_timeout(mirrors, monkeypatch):
    stub = MirrorStub({
        "mirror-a": httpx.ReadTimeout("read timed out"),
        "mirror-b": FakeResp(200, elements_payload(1)),
        "mirror-c": FakeResp(200, elements_payload(9)),
    }).install(monkeypatch)
    recovered = run(osm_service.fetch_layer("water", **SITE))
    assert recovered["status"] == "ok"
    assert recovered["endpoint_used"] == SECONDARY
    assert stub.calls == [PRIMARY, SECONDARY]

    MirrorStub({
        "mirror-a": httpx.ReadTimeout("read timed out"),
        "mirror-b": httpx.ConnectTimeout("connect timed out"),
        "mirror-c": httpx.WriteTimeout("write timed out"),
    }).install(monkeypatch)
    failed = run(osm_service.fetch_layer("water", **SITE))
    assert failed["status"] == "unavailable"
    assert failed["reason"] == "timeout"
    assert failed["features"] == []
    assert all(a["status"] == "timeout" for a in failed["endpoints_attempted"])
    assert f"{osm_service.OVERPASS_TIMEOUT_SEC}s" in failed["detail"]


def test_retryable_server_responses_fall_over(mirrors, monkeypatch):
    for status, expected_reason in ((429, "rate_limited"), (504, "gateway_timeout"), (503, "http_error")):
        retry_stub = MirrorStub({
            "mirror-a": FakeResp(status, {"error": "busy"}),
            "mirror-b": FakeResp(200, elements_payload(1)),
            "mirror-c": FakeResp(200, elements_payload(9)),
        }).install(monkeypatch)
        result = run(osm_service.fetch_layer("water", **SITE))
        assert result["status"] == "ok", f"HTTP {status} should have failed over"
        assert result["endpoint_used"] == SECONDARY
        assert result["endpoints_attempted"][0]["status"] == expected_reason
        assert retry_stub.calls == [PRIMARY, SECONDARY]


def test_every_mirror_rate_limiting_is_reported_as_rate_limited(mirrors, monkeypatch):
    MirrorStub({
        "mirror-a": FakeResp(429, {"error": "busy"}),
        "mirror-b": FakeResp(429, {"error": "busy"}),
        "mirror-c": FakeResp(429, {"error": "busy"}),
    }).install(monkeypatch)
    result = run(osm_service.fetch_layer("water", **SITE))
    assert result["status"] == "unavailable"
    assert result["reason"] == "rate_limited"
    assert result["features"] == []


def test_non_retryable_responses_do_not_fan_out_to_every_mirror(mirrors, monkeypatch):
    """A 400 is about our query; every mirror runs the same Overpass QL, so retrying is pointless."""
    stub = MirrorStub({
        "mirror-a": FakeResp(400, {"error": "bad query"}),
        "mirror-b": FakeResp(200, elements_payload(5)),
        "mirror-c": FakeResp(200, elements_payload(5)),
    }).install(monkeypatch)

    result = run(osm_service.fetch_layer("water", **SITE))

    assert result["status"] == "unavailable"
    assert result["reason"] == "http_error"
    assert "400" in result["detail"]
    assert stub.calls == [PRIMARY], "no fan-out on a non-retryable status"


def test_a_non_json_body_falls_over_because_mirror_front_ends_differ(mirrors, monkeypatch):
    stub = MirrorStub({
        "mirror-a": FakeResp(200, None, text="<html>502 Bad Gateway</html>"),
        "mirror-b": FakeResp(200, elements_payload(2)),
        "mirror-c": FakeResp(200, elements_payload(9)),
    }).install(monkeypatch)
    result = run(osm_service.fetch_layer("water", **SITE))
    assert result["status"] == "ok"
    assert result["endpoint_used"] == SECONDARY
    assert result["endpoints_attempted"][0]["status"] == "malformed_response"
    assert stub.calls == [PRIMARY, SECONDARY], "the tertiary is not contacted once one answers"


def test_bounded_attempts_per_endpoint(monkeypatch):
    monkeypatch.setenv("OVERPASS_MIRRORS", f"{PRIMARY}, {SECONDARY}")
    monkeypatch.setattr(osm_service, "OVERPASS_MIRROR_ATTEMPTS", 2, raising=False)
    stub = MirrorStub({
        "mirror-a": httpx.ConnectError("refused"),
        "mirror-b": httpx.ConnectError("refused"),
    }).install(monkeypatch)

    result = run(osm_service.fetch_layer("water", **SITE))

    assert result["status"] == "unavailable"
    assert stub.calls == [PRIMARY, PRIMARY, SECONDARY, SECONDARY], "attempts are bounded, not endless"
    assert len(result["endpoints_attempted"]) == 4


def test_a_failure_is_never_reported_as_a_successful_empty_result(mirrors, monkeypatch):
    MirrorStub({
        "mirror-a": httpx.ConnectError("refused"),
        "mirror-b": httpx.ConnectError("refused"),
        "mirror-c": httpx.ConnectError("refused"),
    }).install(monkeypatch)
    failed = run(osm_service.fetch_layer("water", **SITE))
    assert failed["status"] != "ok"
    assert failed["reason"] is not None
    assert failed["data_class"] == "mapped", "the layer's data class is unchanged"
    assert failed["source"] == "OpenStreetMap contributors via Overpass API"
    assert failed["query"].startswith("[out:json]"), "the query is still published"

    # A genuine empty result must remain distinguishable from a failure.
    MirrorStub({"mirror-a": FakeResp(200, {"elements": []}), "mirror-b": FakeResp(200, {}),
                "mirror-c": FakeResp(200, {})}).install(monkeypatch)
    empty = run(osm_service.fetch_layer("water", **SITE))
    assert empty["status"] == "ok"
    assert empty["reason"] is None
    assert empty["features"] == []
    assert empty["detail"].startswith("0 mapped feature(s) returned")


def test_a_failed_layer_is_not_cached_so_a_later_request_can_recover(mirrors, monkeypatch):
    MirrorStub({
        "mirror-a": httpx.ConnectError("refused"),
        "mirror-b": httpx.ConnectError("refused"),
        "mirror-c": httpx.ConnectError("refused"),
    }).install(monkeypatch)
    first = run(osm_service.fetch_osm_layers(SITE["lat"], SITE["lon"], SITE["radius_km"], layers=["water"]))
    assert first["status"] == "unavailable"

    MirrorStub({"mirror-a": FakeResp(200, elements_payload(2)), "mirror-b": FakeResp(200, {}),
                "mirror-c": FakeResp(200, {})}).install(monkeypatch)
    second = run(osm_service.fetch_osm_layers(SITE["lat"], SITE["lon"], SITE["radius_km"], layers=["water"]))
    assert second["status"] == "ok", "the failure was not cached, so recovery is possible"
    assert len(second["features"]) == 2


# ------------------------------------------------------------------ partial availability

def test_partial_layer_availability_is_preserved_through_failover(monkeypatch):
    monkeypatch.setenv("OVERPASS_MIRRORS", f"{PRIMARY}, {SECONDARY}")

    async def post(self_client, url, data=None, **kwargs):
        url = str(url)
        query = (data or {}).get("data", "")
        post.calls.append((url, "road" if "highway" in query else "water"))
        if "highway" in query:
            raise httpx.ConnectError("All connection attempts failed")
        return FakeResp(200, elements_payload(2))

    post.calls = []
    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    result = run(osm_service.fetch_osm_layers(SITE["lat"], SITE["lon"], SITE["radius_km"],
                                              layers=["water", "road"]))

    assert result["status"] == "partial"
    assert result["cacheable"] is False
    assert result["layers"]["water"]["status"] == "ok"
    assert result["layers"]["road"]["status"] == "unavailable"
    assert result["layers"]["road"]["reason"] == "network_error"
    assert "PARTIAL COVERAGE" in result["partial_coverage_warning"]
    assert result["data_label"].startswith("PARTIAL mapped geography")
    assert sum(1 for u, _ in post.calls if "mirror-a" in u) == 2, "both layers tried the primary"
    assert sum(1 for u, _ in post.calls if "mirror-b" in u) == 1, "only the road layer failed over"

    # The layer that worked is cached and reused; the failed one is retried.
    before = len(post.calls)
    again = run(osm_service.fetch_osm_layers(SITE["lat"], SITE["lon"], SITE["radius_km"],
                                             layers=["water", "road"]))
    assert again["status"] == "partial"
    assert again["layers"]["water"]["cache_hit"] is not None
    assert sum(1 for u, _ in post.calls[before:] if "mirror-a" in u) == 1, "only road re-queried"


def test_every_layer_failing_keeps_the_aggregate_reason_and_attribution(monkeypatch):
    monkeypatch.setenv("OVERPASS_MIRRORS", f"{PRIMARY}, {SECONDARY}")
    MirrorStub({"mirror-a": httpx.ConnectError("refused"), "mirror-b": httpx.ConnectError("refused")}) \
        .install(monkeypatch)

    result = run(osm_service.fetch_osm_layers(SITE["lat"], SITE["lon"], SITE["radius_km"],
                                              layers=["water", "road"]))

    assert result["status"] == "unavailable"
    assert result["reason"] == "all_layers_failed:network_error"
    assert result["features"] == []
    assert result["data_class"] == "unavailable"
    assert result["attribution"] == "\u00a9 OpenStreetMap contributors (ODbL)"
    assert result["license"] == "ODbL"
    assert result["source"] == "OpenStreetMap contributors via Overpass API"
    assert "no geography is inferred" in result["detail"]
    assert "UNAVAILABLE (Overpass unreachable)" in result["data_label"]
    for layer in result["layers"].values():
        assert layer["cacheable"] is False
        assert len(layer["endpoints_attempted"]) == 2

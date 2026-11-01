"""
Concurrency, pacing and backoff behaviour for Overpass retrieval.

WHY THIS EXISTS
---------------
Production reported only 1/8 OSM layers retrieved for a site. The deployed code launches every
requested layer with `asyncio.gather` and had no application-level concurrency limit, so a single
`/landscape` call put eight simultaneous queries on a public Overpass instance - and with mirror
failover each of those could then move on to two more instances. Public instances ask for one or
two concurrent queries per client, so the burst itself is a plausible cause of the partial result.

That causal claim is SUSPECTED, not confirmed: it has not been verified against a live response,
and no test here asserts anything about real Overpass behaviour. What these tests do prove is the
mechanism - that the limiter, the failover and the retry policy behave as specified.

LABELLING (per conftest policy)
-------------------------------
Every stub below is a locally generated stand-in for an upstream response. Nothing here is live
upstream data, and nothing fabricates geography: a stubbed failure still yields an unavailable
layer with empty features.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services import osm as osm_service

PRIMARY = "https://mirror-a.invalid/api/interpreter"
SECONDARY = "https://mirror-b.invalid/api/interpreter"
TERTIARY = "https://mirror-c.invalid/api/interpreter"

ALL_LAYERS = ("water", "waterway", "wetland", "woodland", "protected", "settlement", "road", "landuse")


def _ok_body(n: int = 2) -> dict:
    """synthetic_payload: a minimal Overpass-shaped success body. Never presented as live data."""
    return {
        "elements": [
            {"type": "node", "id": 1000 + i, "lat": 30.90 + 0.001 * i, "lon": 75.85 + 0.001 * i}
            for i in range(n)
        ]
    }


class FakeResp:
    def __init__(self, status_code: int = 200, body=None, text: str | None = None):
        self.status_code = status_code
        self._body = body
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("not JSON")
        return self._body if self._body is not None else _ok_body()


class ConcurrencyProbe:
    """
    Stand-in for `httpx.AsyncClient.post` that measures real overlap.

    `behaviour` maps an endpoint fragment to a FakeResp, an exception, or a list of those consumed
    per attempt (so "first attempt fails, second succeeds" is expressible).

    Each call holds the floor for `hold` seconds so that, absent a limiter, the eight layers would
    genuinely overlap and `peak` would reach 8. That is what makes `peak <= 2` a meaningful
    assertion rather than a race that happens to pass.
    """

    def __init__(self, behaviour, hold: float = 0.02):
        self.behaviour = behaviour
        self.hold = hold
        self.calls: list[str] = []
        self.in_flight = 0
        self.peak = 0

    def install(self, monkeypatch):
        async def post(self_client, url, data=None, **kwargs):
            url = str(url)
            self.calls.append(url)
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                if self.hold:
                    await asyncio.sleep(self.hold)
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
            finally:
                self.in_flight -= 1

        monkeypatch.setattr(httpx.AsyncClient, "post", post)
        return self

    def count(self, fragment: str) -> int:
        return sum(1 for u in self.calls if fragment in u)


@pytest.fixture(autouse=True)
def _clean_layer_cache():
    """Per-layer caching would otherwise let one test serve another test's result."""
    osm_service._layer_cache.clear()
    yield
    osm_service._layer_cache.clear()


@pytest.fixture(autouse=True)
def _mirrors(monkeypatch):
    monkeypatch.setenv("OVERPASS_MIRRORS", f"{PRIMARY}, {SECONDARY}, {TERTIARY}")
    monkeypatch.delenv("OVERPASS_URL", raising=False)
    monkeypatch.setattr(osm_service, "OVERPASS_MIRROR_ATTEMPTS", 1, raising=False)


def _fetch(layers=ALL_LAYERS, **kwargs):
    return asyncio.run(
        osm_service.fetch_osm_layers(30.9010, 75.8573, 3.0, layers=list(layers), **kwargs)
    )


# --------------------------------------------------------------------------- concurrency limit


def test_no_more_than_two_overpass_requests_execute_concurrently(monkeypatch):
    """
    The core guarantee. Eight layers fan out, each stub call holds the floor long enough that an
    unlimited implementation would show a peak of 8. The limiter must cap it at 2.
    """
    probe = ConcurrencyProbe({mirror: FakeResp() for mirror in ("mirror-a", "mirror-b", "mirror-c")})
    probe.install(monkeypatch)

    result = _fetch()

    assert len(probe.calls) == 8, "all eight layers must actually be requested"
    assert probe.peak <= 2, f"observed {probe.peak} concurrent Overpass requests, expected at most 2"
    # And it must still complete: throttling must not drop layers.
    assert result["status"] == "ok", result["detail"]
    assert "8/8 layers retrieved" in result["detail"]


def test_concurrency_limit_is_configurable(monkeypatch):
    """The knob is real: raising it raises the observed peak, and it never exceeds the setting."""
    monkeypatch.setattr(osm_service, "OVERPASS_MAX_CONCURRENCY", 4, raising=False)
    probe = ConcurrencyProbe({m: FakeResp() for m in ("mirror-a", "mirror-b", "mirror-c")})
    probe.install(monkeypatch)

    _fetch()

    assert probe.peak <= 4
    # Four slots and eight layers must actually overlap, or the assertion above proves nothing.
    assert probe.peak > 2, f"expected overlap above 2 with limit 4, saw {probe.peak}"


def test_serialising_the_limiter_still_returns_every_layer(monkeypatch):
    """Limit 1 is the safe extreme: slower, but no layer is lost and nothing is fabricated."""
    monkeypatch.setattr(osm_service, "OVERPASS_MAX_CONCURRENCY", 1, raising=False)
    probe = ConcurrencyProbe({m: FakeResp() for m in ("mirror-a", "mirror-b", "mirror-c")})
    probe.install(monkeypatch)

    result = _fetch()

    assert probe.peak == 1
    assert result["status"] == "ok"
    assert "8/8 layers retrieved" in result["detail"]


def test_semaphore_is_reused_within_one_event_loop():
    """
    The gate must be shared, not per-call, or it would limit nothing. Same loop => same object.
    """

    async def scenario():
        first = osm_service.overpass_semaphore()
        second = osm_service.overpass_semaphore()
        return first, second

    first, second = asyncio.run(scenario())
    assert first is second


def test_semaphore_survives_a_new_event_loop():
    """
    An asyncio.Semaphore binds to the loop that first uses it. Tests (and a restarted worker) get
    fresh loops, so a cached instance must be replaced rather than raising across loops.
    """
    async def current():
        return osm_service.overpass_semaphore()

    first = asyncio.run(current())
    second = asyncio.run(current())
    assert first is not second


# ------------------------------------------------------------------- failover under the limiter


def test_failover_still_works_while_throttled(monkeypatch):
    """
    The primary refuses every layer, so all eight fail over to the secondary - a burst of eight
    retries. Failover must survive the limiter, and the peak must still be capped.
    """
    probe = ConcurrencyProbe(
        {
            "mirror-a": httpx.ConnectError("All connection attempts failed"),
            "mirror-b": FakeResp(),
            "mirror-c": FakeResp(),
        }
    )
    probe.install(monkeypatch)

    result = _fetch()

    assert probe.peak <= 2, f"failover burst reached {probe.peak} concurrent requests"
    assert result["status"] == "ok", result["detail"]
    assert probe.count("mirror-a") == 8
    assert probe.count("mirror-b") == 8
    assert probe.count("mirror-c") == 0, "the tertiary mirror must not be touched once one succeeds"


def test_partial_availability_is_reported_honestly_under_throttling(monkeypatch):
    """
    One layer stays broken on every mirror. Throttling must not turn that into a fabricated
    success: the layer is unavailable, the aggregate is partial, and the warning survives.
    """
    probe = ConcurrencyProbe(
        {
            "mirror-a": httpx.ConnectError("refused"),
            "mirror-b": httpx.ConnectError("refused"),
            "mirror-c": httpx.ConnectError("refused"),
        }
    )

    # Only `road` fails everywhere; everything else succeeds on the tertiary mirror.
    async def post(self_client, url, data=None, **kwargs):
        url = str(url)
        probe.calls.append(url)
        probe.in_flight += 1
        probe.peak = max(probe.peak, probe.in_flight)
        try:
            await asyncio.sleep(0.01)
            if "way%5B%22highway%22" in url or "highway" in str(data or ""):
                raise httpx.ConnectError("refused")
            return FakeResp()
        finally:
            probe.in_flight -= 1

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    result = _fetch()

    assert probe.peak <= 2
    assert result["status"] == "partial", result["detail"]
    assert result["cacheable"] is False
    assert "PARTIAL COVERAGE" in (result["partial_coverage_warning"] or "")
    assert result["layers"]["road"]["status"] == "unavailable"
    assert result["layers"]["road"]["features"] == []
    # The seven that worked are still real.
    ok = [name for name, layer in result["layers"].items() if layer["status"] == "ok"]
    assert len(ok) == 7, ok


def test_recovery_after_a_temporary_failure(monkeypatch):
    """
    A mirror that is down for one request and up for the next must not be cached as broken.
    First call fails over and recovers; the successful layer is then served from cache.
    """
    probe = ConcurrencyProbe(
        {
            "mirror-a": [httpx.ConnectError("transient"), FakeResp()],
            "mirror-b": FakeResp(),
            "mirror-c": FakeResp(),
        }
    )
    probe.install(monkeypatch)

    first = _fetch(layers=("water",))
    assert first["status"] == "ok", first["detail"]
    assert first["layers"]["water"]["endpoint_used"] == SECONDARY
    calls_after_first = len(probe.calls)

    # Second call for the same site/layer must be a cache hit: no new upstream traffic at all.
    second = _fetch(layers=("water",))
    assert second["status"] == "ok"
    assert second["layers"]["water"]["cache_hit"] == "memory"
    assert len(probe.calls) == calls_after_first, "a cached layer must not re-query Overpass"


def test_failed_layer_is_not_cached_so_a_later_request_recovers(monkeypatch):
    """
    The other half of the cache contract: a failure must never be stored, so the next request
    retries and can succeed once the instance comes back.
    """
    state = {"fail": True}

    async def post(self_client, url, data=None, **kwargs):
        await asyncio.sleep(0.005)
        if state["fail"]:
            raise httpx.ConnectError("down")
        return FakeResp()

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    broken = _fetch(layers=("water",))
    assert broken["status"] == "unavailable"
    assert broken["layers"]["water"]["cacheable"] is False

    state["fail"] = False
    healed = _fetch(layers=("water",))
    assert healed["status"] == "ok", healed["detail"]
    # It recovered from the network, not from a stored failure.
    assert healed["layers"]["water"]["cache_hit"] is None


# ------------------------------------------------------------------------- backoff and retries


def test_backoff_is_bounded_and_jittered():
    """
    Exponential growth must stop at the cap, and identical delays must not be produced - eight
    layers backing off in lockstep would hit the next mirror simultaneously, recreating the burst.
    """
    base, cap = 0.5, 4.0
    original_base = osm_service.OVERPASS_BACKOFF_BASE_SEC
    original_cap = osm_service.OVERPASS_BACKOFF_MAX_SEC
    osm_service.OVERPASS_BACKOFF_BASE_SEC = base
    osm_service.OVERPASS_BACKOFF_MAX_SEC = cap
    try:
        delays = [osm_service.backoff_delay(i) for i in range(12)]
        assert all(0.0 <= d <= cap for d in delays), delays
        # Growth is capped, so late attempts cluster at the ceiling rather than exploding.
        assert max(delays) <= cap
        # Jitter: repeated calls at the same index must not all agree.
        same_index = {round(osm_service.backoff_delay(3), 9) for _ in range(25)}
        assert len(same_index) > 1, "backoff produced identical delays; jitter is missing"
    finally:
        osm_service.OVERPASS_BACKOFF_BASE_SEC = original_base
        osm_service.OVERPASS_BACKOFF_MAX_SEC = original_cap


def test_non_retryable_status_is_not_retried_or_backed_off(monkeypatch):
    """
    A 400 is about OUR query. Every mirror runs the same Overpass QL, so fanning out cannot help
    and backing off cannot help. Exactly one request must be made.
    """
    probe = ConcurrencyProbe({"mirror-a": FakeResp(status_code=400)}, hold=0.0)
    probe.install(monkeypatch)

    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def spy_sleep(seconds, *args, **kwargs):
        slept.append(seconds)
        return await real_sleep(0)

    monkeypatch.setattr(osm_service.asyncio, "sleep", spy_sleep)

    result = _fetch(layers=("water",))

    assert probe.count("mirror-a") == 1, "a non-retryable status must not fan out"
    assert probe.count("mirror-b") == 0
    assert probe.count("mirror-c") == 0
    assert result["status"] == "unavailable"
    assert result["layers"]["water"]["reason"] == "http_error"
    assert not slept, f"no backoff sleep is permitted for a non-retryable failure, got {slept}"


def test_retryable_failure_backs_off_before_the_next_attempt(monkeypatch):
    """The mirror budget is >1, so a retryable failure must wait before trying again."""
    monkeypatch.setattr(osm_service, "OVERPASS_MIRROR_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(osm_service, "OVERPASS_BACKOFF_BASE_SEC", 0.5, raising=False)

    probe = ConcurrencyProbe({"mirror-a": httpx.ConnectError("refused")}, hold=0.0)
    probe.install(monkeypatch)

    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def spy_sleep(seconds, *args, **kwargs):
        slept.append(seconds)
        return await real_sleep(0)

    monkeypatch.setattr(osm_service.asyncio, "sleep", spy_sleep)

    result = _fetch(layers=("water",))

    assert result["status"] == "unavailable"
    assert probe.count("mirror-a") == 2, "the per-endpoint attempt budget must be honoured"
    assert slept, "a retryable failure must back off before retrying"
    assert max(slept) <= osm_service.OVERPASS_BACKOFF_MAX_SEC


def test_pacing_delay_is_applied_while_holding_a_slot(monkeypatch):
    """The stagger is real and bounded: one sleep per request, at the configured size."""
    monkeypatch.setattr(osm_service, "OVERPASS_REQUEST_DELAY_SEC", 0.25, raising=False)

    probe = ConcurrencyProbe({m: FakeResp() for m in ("mirror-a", "mirror-b", "mirror-c")})
    probe.install(monkeypatch)

    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def spy_sleep(seconds, *args, **kwargs):
        slept.append(seconds)
        return await real_sleep(0)

    monkeypatch.setattr(osm_service.asyncio, "sleep", spy_sleep)

    _fetch()

    staggers = [s for s in slept if s == 0.25]
    assert len(staggers) == 8, f"expected one stagger per request, got {len(staggers)}"


def test_diagnostics_and_attribution_are_preserved_under_throttling(monkeypatch):
    """Requirement: no existing response field may be lost to the concurrency change."""
    probe = ConcurrencyProbe({m: FakeResp() for m in ("mirror-a", "mirror-b", "mirror-c")})
    probe.install(monkeypatch)

    result = _fetch(layers=("water",))
    layer = result["layers"]["water"]

    for field in (
        "layer", "label", "data_class", "source", "query", "features",
        "cacheable", "status", "reason", "detail", "cache_hit",
        "endpoint_used", "endpoints_attempted",
    ):
        assert field in layer, f"layer lost field {field!r}"

    assert layer["data_class"] == "mapped"
    assert layer["license"] == "ODbL" if "license" in layer else True
    assert "OpenStreetMap" in layer["source"]
    assert result["attribution"].startswith("\u00a9 OpenStreetMap contributors")
    assert result["license"] == "ODbL"
    assert layer["endpoint_used"] == PRIMARY
    assert [a["endpoint"] for a in layer["endpoints_attempted"]] == [PRIMARY]


def test_total_failure_keeps_the_existing_vocabulary(monkeypatch):
    """Throttling must not change how a complete outage is reported."""
    probe = ConcurrencyProbe(
        {m: httpx.ConnectError("All connection attempts failed") for m in ("mirror-a", "mirror-b", "mirror-c")}
    )
    probe.install(monkeypatch)

    result = _fetch(layers=("water", "road"))

    assert probe.peak <= 2
    assert result["status"] == "unavailable"
    assert result["reason"].startswith("all_layers_failed:")
    assert result["data_class"] == "unavailable"
    assert result["cacheable"] is False
    assert "no geography is inferred" in result["detail"]
    for name in ("water", "road"):
        assert result["layers"][name]["features"] == []
        assert result["layers"][name]["cacheable"] is False

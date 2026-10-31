import asyncio
import datetime
import json
import math
import os
import sqlite3
import time
from collections import OrderedDict
import httpx

from ..db import conn

KM_PER_DEG_LAT = 111.32
TERRAIN_HALF_SPAN_UNITS = 40.0
ANALYSIS_RADIUS_UNITS = 32.0  # Matches Stage 1 ENU constant (40 / 32 = 1.25x radius_km)

MIN_ELEVATION_GRID_SIZE = 2
MAX_ELEVATION_GRID_SIZE = 32
DEFAULT_ELEVATION_GRID_SIZE = int(os.getenv("ELEVATION_GRID_SIZE", "20"))
MAX_COORDS_PER_BATCH = 100

OPEN_METEO_ELEVATION_URL = os.getenv(
    "OPEN_METEO_ELEVATION_URL", "https://api.open-meteo.com/v1/elevation"
)
OPEN_METEO_API_KEY = os.getenv("OPEN_METEO_API_KEY", "").strip()
ELEVATION_TIMEOUT_SEC = float(os.getenv("ELEVATION_TIMEOUT_SEC", "5.0"))
ELEVATION_MEM_CACHE_MAX = int(os.getenv("ELEVATION_MEM_CACHE_MAX", "128"))
RATE_LIMIT_COOLDOWN_SEC = float(os.getenv("ELEVATION_RATE_LIMIT_COOLDOWN_SEC", "30.0"))

COPERNICUS_DOI_URL = "https://doi.org/10.5270/ESA-c5d3d65"
OPEN_METEO_DOCS_URL = "https://open-meteo.com/en/docs/elevation-api"
ATTRIBUTION_TEXT = (
    "Elevation data by Open-Meteo.com (CC BY 4.0, non-commercial free tier) • "
    "Copernicus DEM 2021 release GLO-90 (90m, © DLR e.V. / Airbus Defence and Space GmbH, "
    "funded by the European Union; DOI: 10.5270/ESA-c5d3d65)"
)

# Cache namespace version. Bumped `dem:v1` -> `dem:v2` when the payload gained
# `data_class` / `surface_model` (Copernicus GLO-90 is `observation_derived`, NOT
# `measured`). Entries written under the old namespace are unreachable, so a payload
# carrying the withdrawn classification can never be served again.
DEM_CACHE_NAMESPACE = "dem:v2"
DEM_DATA_CLASS = "observation_derived"

_MEM_CACHE: OrderedDict[str, dict] = OrderedDict()
_INFLIGHT_LOCKS: dict[str, asyncio.Lock] = {}
_INFLIGHT_COUNTS: dict[str, int] = {}
_rate_limited_until: float = 0.0


class ElevationFetchError(Exception):
    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason


def clear_elevation_memory_cache() -> None:
    """Test/diagnostic helper to clear in-memory LRU and rate-limit cooldown state."""
    global _rate_limited_until
    _MEM_CACHE.clear()
    _INFLIGHT_LOCKS.clear()
    _INFLIGHT_COUNTS.clear()
    _rate_limited_until = 0.0


def normalize_grid_size(grid_size: int | None = None) -> int:
    raw = DEFAULT_ELEVATION_GRID_SIZE if grid_size is None else int(grid_size)
    if raw < MIN_ELEVATION_GRID_SIZE or raw > MAX_ELEVATION_GRID_SIZE:
        raise ValueError(
            f"grid_size must be between {MIN_ELEVATION_GRID_SIZE} and {MAX_ELEVATION_GRID_SIZE}"
        )
    return raw


def sanitize_radius_km(radius_km: float) -> float:
    return float(radius_km) if radius_km and math.isfinite(radius_km) and radius_km > 0 else 5.0


def make_elevation_cache_key(
    lat: float, lon: float, radius_km: float, grid_size: int = DEFAULT_ELEVATION_GRID_SIZE
) -> str:
    n = normalize_grid_size(grid_size)
    r_km = sanitize_radius_km(radius_km)
    return (
        f"{DEM_CACHE_NAMESPACE}:{round(float(lat), 4):.4f}:"
        f"{round(float(lon), 4):.4f}:{round(r_km, 2):.2f}:{n}"
    )


def generate_enu_elevation_grid_coords(
    lat: float,
    lon: float,
    radius_km: float,
    grid_size: int | None = None,
) -> tuple[list[tuple[float, float]], float, int]:
    """
    Generates an N x N grid in local metric ENU coordinates matching Stage 1 scene bounds:
    - Row 0 .. N-1: North (+half_span_km, world z = -40) -> South (-half_span_km, world z = +40)
    - Col 0 .. N-1: West (-half_span_km, world x = -40) -> East (+half_span_km, world x = +40)
    where half_span_km = (40 / 32) * radius_km = 1.25 * radius_km.
    """
    n = normalize_grid_size(grid_size)
    r_km = sanitize_radius_km(radius_km)
    half_span_km = (TERRAIN_HALF_SPAN_UNITS / ANALYSIS_RADIUS_UNITS) * r_km
    cos_lat = max(1e-6, math.cos(math.radians(float(lat))))

    coords: list[tuple[float, float]] = []
    for row in range(n):
        v = row / (n - 1)
        north_km = (1.0 - 2.0 * v) * half_span_km
        pt_lat = max(-90.0, min(90.0, float(lat) + north_km / KM_PER_DEG_LAT))
        for col in range(n):
            u = col / (n - 1)
            east_km = (2.0 * u - 1.0) * half_span_km
            pt_lon = ((float(lon) + east_km / (KM_PER_DEG_LAT * cos_lat) + 180.0) % 360.0) - 180.0
            coords.append((round(pt_lat, 6), round(pt_lon, 6)))
    return coords, round(half_span_km * 2.0, 4), n


def split_coordinate_batches(
    coords: list[tuple[float, float]],
    max_batch_size: int = MAX_COORDS_PER_BATCH,
) -> list[list[tuple[float, float]]]:
    if max_batch_size < 1 or max_batch_size > MAX_COORDS_PER_BATCH:
        raise ValueError(f"max_batch_size must be between 1 and {MAX_COORDS_PER_BATCH}")
    return [coords[i : i + max_batch_size] for i in range(0, len(coords), max_batch_size)]


def sample_grid_bilinear(values_m: list[list[float]], u_norm: float, v_norm: float) -> float:
    rows = len(values_m)
    cols = len(values_m[0]) if rows else 0
    if rows < 2 or cols < 2:
        raise ValueError("Elevation grid must be at least 2x2")
    u = max(0.0, min(1.0, float(u_norm)))
    v = max(0.0, min(1.0, float(v_norm)))
    gx = u * (cols - 1)
    gz = v * (rows - 1)
    c0 = int(math.floor(gx))
    c1 = min(cols - 1, c0 + 1)
    tx = gx - c0
    r0 = int(math.floor(gz))
    r1 = min(rows - 1, r0 + 1)
    tz = gz - r0
    top = values_m[r0][c0] * (1.0 - tx) + values_m[r0][c1] * tx
    bottom = values_m[r1][c0] * (1.0 - tx) + values_m[r1][c1] * tx
    return top * (1.0 - tz) + bottom * tz


def validate_and_parse_batch_response(payload: object, expected_len: int) -> list[float]:
    if not isinstance(payload, dict):
        raise ElevationFetchError("malformed_response", "JSON root is not an object")
    if payload.get("error") is True:
        raise ElevationFetchError("api_error", str(payload.get("reason", "Open-Meteo returned error")))
    elev = payload.get("elevation")
    if not isinstance(elev, list) or len(elev) != expected_len:
        raise ElevationFetchError(
            "malformed_response",
            f"Expected {expected_len} elevation values, got {len(elev) if isinstance(elev, list) else type(elev).__name__}",
        )
    cleaned: list[float] = []
    for v in elev:
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ElevationFetchError("missing_values", "Elevation array contains null or non-numeric value")
        fv = float(v)
        if not math.isfinite(fv) or fv < -500.0 or fv > 9000.0:
            raise ElevationFetchError("missing_values", "Elevation value is NaN/Inf or outside terrestrial range")
        cleaned.append(round(fv, 2))
    return cleaned


def unavailable_elevation_payload(
    radius_km: float,
    grid_size: int | None = None,
    reason: str = "unavailable",
    detail: str = "",
    lat: float | None = None,
    lon: float | None = None,
) -> dict:
    n = normalize_grid_size(grid_size)
    r_km = sanitize_radius_km(radius_km)
    span_km = round((TERRAIN_HALF_SPAN_UNITS / ANALYSIS_RADIUS_UNITS) * r_km * 2.0, 4)
    return {
        "status": "unavailable",
        "reason": reason,
        "detail": detail or "Real DEM elevation unavailable; displaying flat unmeasured reference plane.",
        "lat": round(float(lat), 5) if lat is not None else None,
        "lon": round(float(lon), 5) if lon is not None else None,
        "radius_km": round(r_km, 3),
        "grid_size": n,
        "span_km": span_km,
        "values_m": None,
        "min_m": None,
        "max_m": None,
        "center_m": None,
        "relief_m": None,
        "resolution_m": None,
        "data_class": "unavailable",
        "surface_model": None,
        "source": "Unavailable (Open-Meteo / Copernicus DEM GLO-90 not loaded)",
        "source_url": OPEN_METEO_DOCS_URL,
        "copernicus_doi_url": COPERNICUS_DOI_URL,
        "license": "n/a",
        "attribution": None,
        "cache_hit": None,
        "data_label": "Fallback flat reference plane - real elevation UNAVAILABLE (not measured terrain)",
    }


def _payload_provenance_is_current(payload: dict) -> bool:
    """
    Rejects cached DEM payloads written before the provenance correction.

    Those rows classified the Copernicus DEM as `measured` and carry no `data_class` or
    `surface_model`. Serving one would republish the withdrawn classification, so a stale
    payload is treated as a cache miss (and re-fetched) instead.
    """
    if payload.get("status") != "ok":
        return True
    return payload.get("data_class") == DEM_DATA_CLASS and bool(payload.get("surface_model"))


def _mem_cache_get(key: str) -> dict | None:
    item = _MEM_CACHE.get(key)
    if item is None:
        return None
    if not _payload_provenance_is_current(item):
        _MEM_CACHE.pop(key, None)
        return None
    _MEM_CACHE.move_to_end(key)
    copy_item = dict(item)
    copy_item["cache_hit"] = "memory"
    return copy_item


def _mem_cache_put(key: str, payload: dict) -> None:
    _MEM_CACHE[key] = dict(payload)
    _MEM_CACHE.move_to_end(key)
    while len(_MEM_CACHE) > ELEVATION_MEM_CACHE_MAX:
        _MEM_CACHE.popitem(last=False)


def _sqlite_cache_get(key: str) -> dict | None:
    try:
        c = conn()
        row = c.execute("SELECT payload FROM elevation_cache WHERE cache_key=?", (key,)).fetchone()
        c.close()
        if not row:
            return None
        parsed = json.loads(row[0])
        if isinstance(parsed, dict) and parsed.get("status") == "ok":
            if not _payload_provenance_is_current(parsed):
                return None
            _mem_cache_put(key, parsed)
            out = dict(parsed)
            out["cache_hit"] = "sqlite"
            return out
    except (sqlite3.Error, ValueError, OSError):
        return None
    return None


def _sqlite_cache_put(key: str, payload: dict) -> None:
    if payload.get("status") != "ok":
        return
    try:
        now = datetime.datetime.utcnow().isoformat() + "Z"
        c = conn()
        c.execute(
            "INSERT OR REPLACE INTO elevation_cache(cache_key, payload, created_at) VALUES (?,?,?)",
            (key, json.dumps(payload), now),
        )
        c.commit()
        c.close()
    except (sqlite3.Error, OSError):
        # Ephemeral or read-only container storage degrades cleanly to in-memory cache
        pass


async def fetch_open_meteo_batches(
    batches: list[list[tuple[float, float]]],
) -> list[float]:
    """
    Fetches coordinate batches (each <= 100 coordinate pairs) over a single keep-alive client.
    Aborts immediately on the first timeout, HTTP 429 rate-limit, HTTP error, or malformed response.
    """
    global _rate_limited_until
    now_mono = time.monotonic()
    if now_mono < _rate_limited_until:
        raise ElevationFetchError("rate_limited", "Skipping request during Open-Meteo rate-limit cooldown")

    endpoint_url = os.getenv("OPEN_METEO_ELEVATION_URL", OPEN_METEO_ELEVATION_URL)
    api_key = os.getenv("OPEN_METEO_API_KEY", OPEN_METEO_API_KEY).strip()
    flat_values: list[float] = []
    headers = {"User-Agent": "TerraLex-EnvIntelligence/1.0 (non-commercial screening prototype)"}
    async with httpx.AsyncClient(timeout=ELEVATION_TIMEOUT_SEC, headers=headers) as client:
        for batch in batches:
            if len(batch) > MAX_COORDS_PER_BATCH:
                raise ElevationFetchError("batch_size_exceeded", f"Batch size {len(batch)} > {MAX_COORDS_PER_BATCH}")
            params: dict[str, str] = {
                "latitude": ",".join(f"{lat:.5f}" for lat, _ in batch),
                "longitude": ",".join(f"{lon:.5f}" for _, lon in batch),
            }
            if api_key:
                params["apikey"] = api_key
            try:
                resp = await client.get(endpoint_url, params=params)
            except httpx.TimeoutException as exc:
                raise ElevationFetchError("timeout", f"Open-Meteo timed out: {exc}") from exc
            except httpx.HTTPError as exc:
                raise ElevationFetchError("network_error", f"Open-Meteo HTTP transport error: {exc}") from exc

            if resp.status_code == 429:
                retry_hdr = resp.headers.get("Retry-After", "")
                cooldown = RATE_LIMIT_COOLDOWN_SEC
                if retry_hdr.isdigit():
                    cooldown = max(5.0, min(120.0, float(retry_hdr)))
                _rate_limited_until = time.monotonic() + cooldown
                raise ElevationFetchError("rate_limited", "Open-Meteo rate limit (HTTP 429)")

            if resp.status_code != 200:
                raise ElevationFetchError("http_error", f"Open-Meteo returned HTTP {resp.status_code}")

            try:
                body = resp.json()
            except ValueError as exc:
                raise ElevationFetchError("malformed_response", "Invalid JSON from Open-Meteo") from exc

            flat_values.extend(validate_and_parse_batch_response(body, len(batch)))
    return flat_values


async def get_elevation_grid(
    lat: float,
    lon: float,
    radius_km: float,
    grid_size: int | None = None,
) -> dict:
    """
    Returns a verified N x N ENU-aligned elevation grid in metres AMSL (default 20x20 in 4x100 batches),
    backed by memory + SQLite caching and single-flight request deduplication.
    Never raises on upstream failure; returns an explicit status='unavailable' fallback payload instead.
    """
    n = normalize_grid_size(grid_size)
    r_km = sanitize_radius_km(radius_km)
    key = make_elevation_cache_key(lat, lon, r_km, n)

    cached = _mem_cache_get(key) or _sqlite_cache_get(key)
    if cached is not None:
        return cached

    lock = _INFLIGHT_LOCKS.setdefault(key, asyncio.Lock())
    _INFLIGHT_COUNTS[key] = _INFLIGHT_COUNTS.get(key, 0) + 1
    try:
        async with lock:
            cached = _mem_cache_get(key) or _sqlite_cache_get(key)
            if cached is not None:
                return cached

            coords, span_km, n = generate_enu_elevation_grid_coords(lat, lon, r_km, n)
            batches = split_coordinate_batches(coords, MAX_COORDS_PER_BATCH)
            try:
                flat = await fetch_open_meteo_batches(batches)
            except ElevationFetchError as exc:
                return unavailable_elevation_payload(
                    r_km, n, reason=exc.reason, detail=exc.message, lat=lat, lon=lon
                )
            except Exception as exc:
                return unavailable_elevation_payload(
                    r_km, n, reason="unexpected_error", detail=str(exc), lat=lat, lon=lon
                )

            if len(flat) != n * n:
                return unavailable_elevation_payload(
                    r_km,
                    n,
                    reason="malformed_response",
                    detail="Total returned sample count mismatch",
                    lat=lat,
                    lon=lon,
                )

            values_m = [flat[r * n : (r + 1) * n] for r in range(n)]
            min_m = round(min(flat), 2)
            max_m = round(max(flat), 2)
            center_m = round(sample_grid_bilinear(values_m, 0.5, 0.5), 2)
            relief_m = round(max_m - min_m, 2)

            payload = {
                "status": "ok",
                "reason": None,
                "lat": round(float(lat), 5),
                "lon": round(float(lon), 5),
                "radius_km": round(r_km, 3),
                "grid_size": n,
                "batch_count": len(batches),
                "span_km": span_km,
                "values_m": values_m,
                "min_m": min_m,
                "max_m": max_m,
                "center_m": center_m,
                "relief_m": relief_m,
                "resolution_m": 90,
                "data_class": DEM_DATA_CLASS,
                "surface_model": (
                    "Digital Surface Model (DSM): elevations include buildings, structures and "
                    "vegetation canopy. This is NOT bare-earth terrain (DTM)."
                ),
                "source": "Copernicus DEM GLO-90 (90m) via Open-Meteo Elevation API",
                "source_url": OPEN_METEO_DOCS_URL,
                "copernicus_doi_url": COPERNICUS_DOI_URL,
                "license": "CC BY 4.0 (Open-Meteo non-commercial) / Copernicus DEM open licence",
                "attribution": ATTRIBUTION_TEXT,
                "cache_hit": None,
                "data_label": (
                    f"Observed-derived DEM terrain (Copernicus GLO-90 ~90 m digital surface model, "
                    f"{n}x{n} ENU grid)"
                ),
            }
            _mem_cache_put(key, payload)
            _sqlite_cache_put(key, payload)
            return payload
    finally:
        rem = _INFLIGHT_COUNTS.get(key, 1) - 1
        if rem <= 0:
            _INFLIGHT_COUNTS.pop(key, None)
            _INFLIGHT_LOCKS.pop(key, None)
        else:
            _INFLIGHT_COUNTS[key] = rem

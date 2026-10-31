"""
Stage 3A — OpenStreetMap geographic base layer (per-layer Overpass client).

Replaces the single hard-coded Overpass query in `environment.py` with:
  * one query per semantic layer, each with its own timeout and failure isolation
    (a slow road query can no longer discard the water results),
  * explicit *distance semantics* per feature, because "distance to a river" and
    "distance to the centroid of the mapped river way" are different quantities,
  * optional simplified geometry (`include_geometry`) for the Stage 3B MapLibre base layer,
  * an env-configurable Overpass endpoint so a private mirror can be used.

Data class: **mapped** (crowd-sourced). Absence of a feature is never reported as
"no feature exists" - only as "none mapped within the radius".
"""

from __future__ import annotations

import asyncio
import datetime
import math
import os
from typing import Any

import httpx

from .cache import NamespacedCache
from .geo import decimate, enu_km, haversine_km

OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OVERPASS_TIMEOUT_SEC = float(os.getenv("OVERPASS_TIMEOUT_SEC", "15"))
OVERPASS_QUERY_TIMEOUT_SEC = int(os.getenv("OVERPASS_QUERY_TIMEOUT", "12"))
MAX_ELEMENTS_PER_LAYER = int(os.getenv("OVERPASS_MAX_ELEMENTS", "250"))
MAX_GEOMETRY_POINTS = int(os.getenv("OVERPASS_MAX_GEOMETRY_POINTS", "200"))
USER_AGENT = os.getenv(
    "OVERPASS_USER_AGENT",
    "TerraLex-EnvIntelligence/3a (non-commercial screening prototype; contact: project repository)",
)

# Layer id -> (human label, Overpass QL body, geometry-aware, legal trigger keys)
LAYER_SPECS: dict[str, dict[str, Any]] = {
    "water": {
        "label": "Lakes and standing water",
        "selector": 'node["natural"="water"];way["natural"="water"];way["water"];',
        "kind": "water",
        "linear": False,
        "trigger_keys": [],
    },
    "waterway": {
        "label": "Rivers, streams and canals (mapped centrelines)",
        "selector": 'way["waterway"~"^(river|stream|canal|drain)$"];',
        "kind": "water",
        "linear": True,
        "trigger_keys": [],
    },
    "wetland": {
        "label": "Wetlands",
        "selector": 'node["natural"="wetland"];way["natural"="wetland"];',
        "kind": "wetland",
        "linear": False,
        "trigger_keys": ["wetland_proximity"],
    },
    "woodland": {
        "label": "Forest and woodland",
        "selector": 'way["landuse"="forest"];way["natural"="wood"];',
        "kind": "forest",
        "linear": False,
        "trigger_keys": ["forest_proximity"],
    },
    "protected": {
        "label": "Protected areas and nature reserves",
        "selector": (
            'node["leisure"="nature_reserve"];way["leisure"="nature_reserve"];'
            'way["boundary"="protected_area"];way["boundary"="national_park"];'
        ),
        "kind": "protected",
        "linear": False,
        "trigger_keys": ["wetland_proximity"],
    },
    "settlement": {
        "label": "Settlements",
        "selector": 'node["place"~"^(hamlet|village|town|city)$"];',
        "kind": "settlement",
        "linear": False,
        "trigger_keys": [],
    },
    "road": {
        "label": "Major roads (mapped centrelines)",
        "selector": 'way["highway"~"^(motorway|trunk|primary|secondary|tertiary)$"];',
        "kind": "road",
        "linear": True,
        "trigger_keys": [],
    },
    "landuse": {
        "label": "Mapped land use",
        "selector": (
            'way["landuse"~"^(residential|industrial|commercial|retail|farmland|meadow|grass|quarry)$"];'
        ),
        "kind": "landuse",
        "linear": False,
        "trigger_keys": [],
    },
}

DEFAULT_LAYERS: tuple[str, ...] = ("water", "waterway", "wetland", "woodland", "protected", "settlement", "road", "landuse")
# Layers that reproduce the Stage 1/2 `/environment` feature set.
LEGACY_LAYERS: tuple[str, ...] = ("water", "waterway", "wetland", "woodland", "protected", "settlement")

DISTANCE_BASIS_NOTES: dict[str, str] = {
    "point_feature": "Distance to a mapped point feature (OSM node).",
    "geometry_min_distance": "Shortest distance from the site to the mapped geometry (line/area), computed in a local ENU plane.",
    "vertex_min_distance": "Shortest distance from the site to the nearest mapped vertex (exact geometry unavailable; Shapely not installed).",
    "mapped_centroid": (
        "Distance to the centroid of the mapped OSM way/relation, not to the feature itself. "
        "OSM ways are centrelines or outlines, not surveyed surfaces - this is NOT a perpendicular "
        "distance to the carriageway or riverbank."
    ),
}

_TAG_KEEP = ("name", "natural", "water", "waterway", "landuse", "leisure", "boundary", "place", "highway", "intermittent", "seasonal")


# Per-layer cache. A single successful layer is cached on its own so that one failing
# layer can neither discard the good results nor force them to be re-fetched. Failed
# layers are never written to any tier.
_layer_cache = NamespacedCache("osm:v1", table="landscape_cache", max_entries=256)


def clear_osm_cache() -> None:
    """Test/diagnostic helper."""
    _layer_cache.clear()


def layer_cache_key(layer_id: str, lat: float, lon: float, radius_km: float, include_geometry: bool) -> str:
    return _layer_cache.key(
        layer=layer_id,
        lat=f"{round(float(lat), 4):.4f}",
        lon=f"{round(float(lon), 4):.4f}",
        radius=f"{round(float(radius_km), 2):.2f}",
        geom=int(bool(include_geometry)),
    )


def resolve_layers(layers: list[str] | tuple[str, ...] | None) -> list[str]:
    if not layers:
        return list(DEFAULT_LAYERS)
    out: list[str] = []
    for raw in layers:
        lid = str(raw).strip().lower()
        if lid in LAYER_SPECS and lid not in out:
            out.append(lid)
    return out or list(DEFAULT_LAYERS)


def _build_query_body(layer_id: str, lat: float, lon: float, radius_km: float) -> str:
    """Expands a layer selector template into concrete `around:` statements."""
    spec = LAYER_SPECS[layer_id]
    radius_m = int(max(50.0, float(radius_km) * 1000.0))
    centre = f"around:{radius_m},{float(lat):.6f},{float(lon):.6f}"
    # Selector strings are written as bare `node[...]` / `way[...]` statements.
    return spec["selector"].replace("node[", f"node({centre})[").replace("way[", f"way({centre})[")


def build_layer_query(layer_id: str, lat: float, lon: float, radius_km: float, include_geometry: bool = False) -> str:
    out_mode = "geom" if include_geometry else "center"
    return (
        f"[out:json][timeout:{OVERPASS_QUERY_TIMEOUT_SEC}];"
        f"({_build_query_body(layer_id, lat, lon, radius_km)});"
        f"out {out_mode} {MAX_ELEMENTS_PER_LAYER};"
    )


def sort_features_by_distance(features: list[dict]) -> list[dict]:
    """Nearest-first with deterministic tie-breaking (same ordering contract as Stage 1/2)."""
    return sorted(
        features,
        key=lambda f: (
            f.get("distance_km", float("inf")) if f.get("distance_km") is not None else float("inf"),
            f.get("kind", ""),
            f.get("name", ""),
        ),
    )


# ---------------------------------------------------------------------------
# geometry handling
# ---------------------------------------------------------------------------

def _shapely():
    try:
        import shapely.geometry as sg  # noqa: F401

        return sg
    except Exception:  # pragma: no cover - exercised only when Shapely is absent
        return None


def _coords_from_element(el: dict) -> list[list[tuple[float, float]]]:
    """Returns a list of (lat, lon) coordinate sequences present on an Overpass element."""
    seqs: list[list[tuple[float, float]]] = []
    geom = el.get("geometry")
    if isinstance(geom, list) and geom:
        seq = [(p["lat"], p["lon"]) for p in geom if isinstance(p, dict) and p.get("lat") is not None and p.get("lon") is not None]
        if len(seq) >= 1:
            seqs.append(seq)
    members = el.get("members")
    if isinstance(members, list):
        for m in members:
            if not isinstance(m, dict):
                continue
            mgeom = m.get("geometry")
            if isinstance(mgeom, list) and mgeom:
                seq = [(p["lat"], p["lon"]) for p in mgeom if isinstance(p, dict) and p.get("lat") is not None and p.get("lon") is not None]
                if len(seq) >= 1:
                    seqs.append(seq)
    return seqs


def _representative_point(el: dict) -> tuple[float | None, float | None]:
    if el.get("lat") is not None and el.get("lon") is not None:
        return float(el["lat"]), float(el["lon"])
    centre = el.get("center")
    if isinstance(centre, dict) and centre.get("lat") is not None:
        return float(centre["lat"]), float(centre["lon"])
    for seq in _coords_from_element(el):
        return float(seq[0][0]), float(seq[0][1])
    return None, None


def _geometry_distance_km(seqs: list[list[tuple[float, float]]], lat: float, lon: float) -> tuple[float, str] | None:
    """
    Shortest distance (km) from the site to the mapped geometry, or None if the
    element carries no geometry (caller then falls back to the centroid distance).
    """
    if not seqs:
        return None
    sg = _shapely()
    if sg is not None:
        parts = []
        for seq in seqs:
            enu = [enu_km(la, lo, lat, lon) for la, lo in seq]
            if len(enu) == 1:
                parts.append(sg.Point(enu[0][0], enu[0][1]))
            else:
                parts.append(sg.LineString(enu))
        if not parts:
            return None
        origin = sg.Point(0.0, 0.0)
        dist = min(float(p.distance(origin)) for p in parts)
        return max(0.0, dist), "geometry_min_distance"
    # Fallback without Shapely: nearest mapped vertex (labelled as such).
    best = min(math.hypot(*enu_km(la, lo, lat, lon)) for seq in seqs for la, lo in seq)
    return max(0.0, best), "vertex_min_distance"


def _parse_element(el: dict, layer_id: str, lat: float, lon: float, include_geometry: bool) -> dict | None:
    spec = LAYER_SPECS[layer_id]
    rep_lat, rep_lon = _representative_point(el)
    if rep_lat is None or rep_lon is None:
        return None
    tags = el.get("tags", {}) or {}
    seqs = _coords_from_element(el)

    representative_km = haversine_km(lat, lon, rep_lat, rep_lon)
    geom_dist = _geometry_distance_km(seqs, lat, lon)
    if geom_dist is not None:
        distance_km, basis = geom_dist
    else:
        basis = "point_feature" if el.get("type") == "node" else "mapped_centroid"
        distance_km = representative_km
    if el.get("type") == "node":
        rep_kind = "osm_node"
    elif isinstance(el.get("center"), dict):
        rep_kind = "overpass_way_centroid"
    elif seqs:
        rep_kind = "first_mapped_vertex"
    else:
        rep_kind = "unknown"

    feature: dict[str, Any] = {
        "name": tags.get("name") or f"Unnamed {spec['kind']} feature",
        "kind": spec["kind"],
        "layer": layer_id,
        "layer_label": spec["label"],
        "lat": round(float(rep_lat), 6),
        "lon": round(float(rep_lon), 6),
        "distance_km": round(float(distance_km), 3),
        "representative_distance_km": round(float(representative_km), 3),
        "representative_point_kind": rep_kind,
        "distance_basis": basis,
        "distance_note": DISTANCE_BASIS_NOTES[basis],
        "osm_type": el.get("type"),
        "osm_id": el.get("id"),
        "osm_tags": {k: tags[k] for k in _TAG_KEEP if k in tags},
        "source": "OpenStreetMap contributors via Overpass API",
        "data_class": "mapped",
    }
    if include_geometry and seqs:
        flat = [pt for seq in seqs for pt in seq]
        kept, truncated = decimate(flat, MAX_GEOMETRY_POINTS)
        feature["geometry"] = {
            "type": "LineString" if len(kept) >= 2 else "Point",
            "coordinates": [[round(lo, 6), round(la, 6)] for la, lo in kept],
            "sequence_count": len(seqs),
        }
        feature["geometry_truncated"] = truncated
        feature["geometry_points_original"] = len(flat)
    return feature


async def fetch_layer(
    layer_id: str,
    lat: float,
    lon: float,
    radius_km: float,
    include_geometry: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """
    Fetch one OSM layer. Never raises: returns a per-layer status payload so a single
    failing layer cannot silently discard the rest of the geography.
    """
    spec = LAYER_SPECS[layer_id]
    query = build_layer_query(layer_id, lat, lon, radius_km, include_geometry)
    base = {
        "layer": layer_id,
        "label": spec["label"],
        "data_class": "mapped",
        "source": "OpenStreetMap contributors via Overpass API",
        "query": query,
        "features": [],
        # Every failure path inherits this: a failed layer is never cached.
        "cacheable": False,
    }
    owns_client = client is None
    try:
        if owns_client:
            async with httpx.AsyncClient(timeout=OVERPASS_TIMEOUT_SEC, headers={"User-Agent": USER_AGENT}) as cl:
                resp = await cl.post(OVERPASS_URL, data={"data": query})
        else:
            resp = await client.post(OVERPASS_URL, data={"data": query})  # type: ignore[union-attr]

        if resp.status_code == 429:
            return {**base, "status": "unavailable", "reason": "rate_limited",
                    "detail": "Overpass returned HTTP 429 (rate limited). No features are inferred from a failed query."}
        if resp.status_code == 504:
            return {**base, "status": "unavailable", "reason": "gateway_timeout",
                    "detail": "Overpass gateway timeout (HTTP 504). No features are inferred from a failed query."}
        if resp.status_code != 200:
            return {**base, "status": "unavailable", "reason": "http_error",
                    "detail": f"Overpass returned HTTP {resp.status_code}."}
        try:
            body = resp.json()
        except ValueError:
            return {**base, "status": "unavailable", "reason": "malformed_response",
                    "detail": "Overpass returned a non-JSON body."}
    except httpx.TimeoutException:
        return {**base, "status": "unavailable", "reason": "timeout",
                "detail": f"Overpass timed out after {OVERPASS_TIMEOUT_SEC}s."}
    except httpx.HTTPError as exc:
        return {**base, "status": "unavailable", "reason": "network_error",
                "detail": f"Overpass transport error: {exc}"}
    except Exception as exc:  # pragma: no cover - defensive
        return {**base, "status": "unavailable", "reason": "unexpected_error", "detail": str(exc)}

    elements = body.get("elements", []) if isinstance(body, dict) else []
    features = [f for f in (_parse_element(el, layer_id, lat, lon, include_geometry) for el in elements) if f]
    features = sort_features_by_distance(features)
    truncated = bool(body.get("remark")) or len(elements) >= MAX_ELEMENTS_PER_LAYER
    return {
        **base,
        "status": "ok",
        "cacheable": True,
        "reason": None,
        "detail": (
            f"{len(features)} mapped feature(s) returned"
            + ("; result capped/truncated by the element limit" if truncated else "")
        ),
        "element_count": len(elements),
        "result_capped": truncated,
        "features": features,
    }


async def fetch_osm_layers(
    lat: float,
    lon: float,
    radius_km: float,
    layers: list[str] | tuple[str, ...] | None = None,
    include_geometry: bool = False,
    use_cache: bool = True,
) -> dict:
    """
    Fetch all requested layers concurrently with per-layer isolation.

    Each layer is cached independently under its own site/resolution-scoped key. Only
    layers with `status == "ok"` are written to the cache, so after a partial result the
    layers that worked are reused while the failed ones are retried.
    """
    wanted = resolve_layers(layers)

    async def _one(layer_id: str, client: httpx.AsyncClient) -> dict:
        async def _factory() -> dict:
            return await fetch_layer(layer_id, lat, lon, radius_km, include_geometry, client=client)

        if not use_cache:
            result = await _factory()
            result["cache_hit"] = None
            return result
        return await _layer_cache.get_or_fetch(
            layer_cache_key(layer_id, lat, lon, radius_km, include_geometry), _factory
        )

    async with httpx.AsyncClient(timeout=OVERPASS_TIMEOUT_SEC, headers={"User-Agent": USER_AGENT}) as client:
        results = await asyncio.gather(*(_one(lid, client) for lid in wanted))
    per_layer = {r["layer"]: r for r in results}
    ok_layers = [r for r in results if r["status"] == "ok"]
    features = sort_features_by_distance([f for r in ok_layers for f in r["features"]])
    if not ok_layers:
        status, detail = "unavailable", "All Overpass layer queries failed - no geography is inferred."
    elif len(ok_layers) == len(results):
        status, detail = "ok", f"{len(ok_layers)}/{len(results)} layers retrieved; {len(features)} mapped features."
    else:
        status = "partial"
        failed = [r["layer"] for r in results if r["status"] != "ok"]
        detail = f"{len(ok_layers)}/{len(results)} layers retrieved; failed: {', '.join(failed)}."
    partial_coverage_warning = (
        None
        if status == "ok"
        else (
            f"PARTIAL COVERAGE: only {len(ok_layers)}/{len(results)} OSM layers were retrieved. "
            "This is NOT complete geography for the site - absence of a feature in the missing "
            "layers says nothing about the ground."
        )
    )
    failed_reasons = sorted({
        str(r.get("reason")) for r in results if r["status"] != "ok" and r.get("reason")
    })
    return {
        "cacheable": status == "ok",
        "partial_coverage_warning": partial_coverage_warning,
        "status": status,
        "reason": None if status == "ok" else ("all_layers_failed:" + ",".join(failed_reasons) if failed_reasons else "unknown"),
        "detail": detail,
        "data_class": "mapped" if status in ("ok", "partial") else "unavailable",
        "source": "OpenStreetMap contributors via Overpass API",
        "license": "ODbL",
        "attribution": "\u00a9 OpenStreetMap contributors (ODbL)",
        "retrieved_at": datetime.datetime.utcnow().isoformat() + "Z",
        "include_geometry": bool(include_geometry),
        "radius_km": round(float(radius_km), 3),
        "layer_status": {
            lid: {
                "status": per_layer[lid]["status"],
                "detail": per_layer[lid]["detail"],
                "cache_hit": per_layer[lid].get("cache_hit"),
                "cacheable": per_layer[lid].get("cacheable", False),
            }
            for lid in wanted
        },
        "layers": {lid: per_layer[lid] for lid in wanted},
        "features": features,
        "data_label": (
            "UNAVAILABLE (Overpass unreachable) - no geography inferred, nothing fabricated"
            if not ok_layers
            else (
                "PARTIAL mapped geography (OpenStreetMap, crowd-sourced) - "
                f"only {len(ok_layers)}/{len(results)} layers retrieved, so this is NOT complete "
                "coverage of the site"
                if status == "partial"
                else "Mapped geography (OpenStreetMap, crowd-sourced) - absence of a mapped feature "
                     "is not evidence of absence"
            )
        ),
    }


async def legacy_overpass_features(lat: float, lon: float, radius_km: float) -> tuple[list[dict], bool]:
    """
    Stage 1/2 compatibility shim: `(features, ok)` with the original feature keys.

    `ok` stays all-or-nothing (as before) so `/environment` falls back to its labelled
    synthetic demonstration data instead of presenting partial geography as complete.
    """
    result = await fetch_osm_layers(lat, lon, radius_km, layers=LEGACY_LAYERS, include_geometry=False)
    if result["status"] != "ok":
        return [], False
    feats = [
        {
            "name": f["name"],
            "kind": f["kind"],
            "lat": f["lat"],
            "lon": f["lon"],
            "distance_km": round(f["distance_km"], 2),
            "source": f["source"],
            "distance_basis": f["distance_basis"],
            "distance_note": f["distance_note"],
            "layer": f["layer"],
        }
        for f in result["features"]
    ]
    return sort_features_by_distance(feats), True

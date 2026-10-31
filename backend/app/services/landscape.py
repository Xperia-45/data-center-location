"""
Stage 3A — Landscape data service.

One entry point that assembles the two layers TerraLex keeps strictly apart:

  base_layer                geographic reality: terrain (Copernicus DEM), mapped OSM
                            geography, ESA WorldCover land cover. Identical for Present,
                            Development and Future at the same site + resolution +
                            dataset versions.
  development_impact_layer  scenario arithmetic only. This is the only layer that may
                            change between time periods or scenarios.

Both layers carry an explicit `data_class` per number (observation_derived / mapped / estimated /
modelled / user_input / unavailable) and the base layer carries a fingerprint plus a
content digest so a client can *prove* the geography did not move between scenarios.

Nothing here fabricates geography: every layer degrades to `status: "unavailable"` with
a reason, and unavailable layers are never written to the cache.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any

from .cache import NamespacedCache, stable_digest
from .datasets import (
    DATA_CLASS_DEFINITIONS,
    INTEGRITY_RULES,
    NOT_MODELLED_EFFECTS,
    SCHEMA_VERSION,
    dataset_registry,
)
from .elevation import get_elevation_grid, normalize_grid_size
from .landcover import dataset_metadata as worldcover_metadata
from .landcover import landcover_summary
from .osm import DEFAULT_LAYERS, clear_osm_cache, fetch_osm_layers, resolve_layers
from .parcels import ParcelValidationError, parcel_digest, validate_parcel
from .scenario_multipliers import build_impact_layer

LAYER_CHOICES = ("dem", "osm", "landcover")
DEFAULT_REQUEST_LAYERS = ("dem", "osm", "landcover")
LAYER_MODEL_NOTE = (
    "base_layer = geography (terrain, hydrology, land cover, roads, settlements). It does not "
    "change between Present / Development / Future for the same site, resolution and dataset "
    "versions. development_impact_layer = scenario arithmetic only. If the base layer differs "
    "between two temporal views of the same site, that is a bug, not a scenario."
)

DEFAULT_PROJECT = {"footprint_ha": 10.0, "water_mld": 1.0, "power_mw": 20.0, "source": "platform defaults"}


def cached_base_layer_is_current(base: dict) -> bool:
    """
    Read-time guard for the assembled base layer.

    Rows written before the provenance correction classify the Copernicus DEM terrain as
    `measured`. The namespace bump to `landscape:v2` already makes them unreachable; this
    guard is the second guarantee, so such a payload is a cache miss even if an old row
    survives (e.g. a database restored from a Persistent Disk backup).
    """
    if not isinstance(base, dict):
        return False
    terrain = base.get("terrain")
    if not isinstance(terrain, dict):
        return True  # the DEM was not part of this base layer
    if terrain.get("status") != "ok":
        return True  # an explicitly unavailable terrain carries no classification claim
    return terrain.get("data_class") == "observation_derived" and bool(terrain.get("surface_model"))


_cache = NamespacedCache(
    "landscape:v2",
    table="landscape_cache",
    max_entries=64,
    validator=cached_base_layer_is_current,
)
# Independent land-cover tier: a raster read that succeeded must not be repeated just
# because a different layer (e.g. one OSM query) failed on the same request.
_landcover_cache = NamespacedCache("landcover:v1", table="landscape_cache", max_entries=64)

_VOLATILE_KEYS = {"cache_hit", "retrieved_at", "generated_at", "cache", "detail"}


def clear_landscape_cache() -> None:
    """Test/diagnostic helper: clears the assembled-layer, land-cover and OSM layer caches."""
    _cache.clear()
    _landcover_cache.clear()
    clear_osm_cache()


def resolve_request_layers(layers: list[str] | tuple[str, ...] | None) -> list[str]:
    if not layers:
        return list(DEFAULT_REQUEST_LAYERS)
    out = []
    for raw in layers:
        lid = str(raw).strip().lower()
        if lid in LAYER_CHOICES and lid not in out:
            out.append(lid)
    return out or list(DEFAULT_REQUEST_LAYERS)


def _strip_volatile(obj: Any) -> Any:
    """Removes volatile keys so the content digest reflects geography, not bookkeeping."""
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in sorted(obj.items()) if k not in _VOLATILE_KEYS}
    if isinstance(obj, (list, tuple)):
        return [_strip_volatile(v) for v in obj]
    return obj


def base_fingerprint(*, lat: float, lon: float, radius_km: float, grid_size: int,
                     layers: list[str], include_geometry: bool, osm_layers: list[str],
                     parcel: dict | None) -> str:
    """
    Identifies the *geographic request*: site, resolution, layer selection and dataset
    versions. Deliberately excludes scenario/preset, because the base layer must not
    depend on the scenario.
    """
    return "sha256:" + stable_digest(
        {
            "lat": round(float(lat), 4),
            "lon": round(float(lon), 4),
            "radius_km": round(float(radius_km), 2),
            "grid_size": int(grid_size),
            "layers": sorted(layers),
            "include_geometry": bool(include_geometry),
            "osm_layers": sorted(osm_layers),
            "parcel_digest": parcel.get("digest") if parcel else None,
            "dataset_versions": {
                "dem": "copernicus-dem-2021-glo90",
                "osm": "live-overpass",
                "landcover": worldcover_metadata()["dataset_version"],
            },
        }
    )


def cache_key(*, lat: float, lon: float, radius_km: float, grid_size: int, layers: list[str],
              include_geometry: bool, osm_layers: list[str], parcel: dict | None) -> str:
    return _cache.key(
        lat=f"{round(float(lat), 4):.4f}",
        lon=f"{round(float(lon), 4):.4f}",
        radius=f"{round(float(radius_km), 2):.2f}",
        grid=int(grid_size),
        layers=",".join(sorted(layers)),
        geom=int(bool(include_geometry)),
        osm=",".join(sorted(osm_layers)),
        parcel=(parcel or {}).get("digest", "none"),
    )


def _availability(entries: dict[str, tuple[bool, str, str]]) -> dict[str, dict]:
    now = datetime.datetime.utcnow().isoformat() + "Z"
    out = {}
    for dataset_id, (available, status, detail) in entries.items():
        out[dataset_id] = {"available": available, "status": status, "detail": detail, "observed_at": now}
    return out


async def assemble_base_layer(
    *,
    lat: float,
    lon: float,
    radius_km: float,
    grid_size: int,
    layers: list[str],
    include_geometry: bool,
    osm_layers: list[str],
    parcel_geojson: dict | None,
    use_cache: bool = True,
) -> dict:
    """Fetches the geographic base layer concurrently, one task per dataset."""

    async def _landcover() -> dict:
        key = _landcover_cache.key(
            lat=f"{round(float(lat), 4):.4f}",
            lon=f"{round(float(lon), 4):.4f}",
            radius=f"{round(float(radius_km), 2):.2f}",
            parcel=parcel_digest(parcel_geojson) if parcel_geojson else "none",
            dataset=worldcover_metadata()["dataset_version"],
        )
        if not use_cache:
            payload = await landcover_summary(lat, lon, radius_km, parcel_geojson=parcel_geojson)
            payload["cache_hit"] = None
            return payload
        return await _landcover_cache.get_or_fetch(
            key, lambda: landcover_summary(lat, lon, radius_km, parcel_geojson=parcel_geojson)
        )

    tasks: dict[str, asyncio.Task] = {}
    if "dem" in layers:
        tasks["terrain"] = asyncio.ensure_future(get_elevation_grid(lat, lon, radius_km, grid_size=grid_size))
    if "osm" in layers:
        tasks["osm"] = asyncio.ensure_future(
            fetch_osm_layers(
                lat, lon, radius_km, layers=osm_layers,
                include_geometry=include_geometry, use_cache=use_cache,
            )
        )
    if "landcover" in layers:
        tasks["land_cover"] = asyncio.ensure_future(_landcover())

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    resolved: dict[str, dict] = {}
    for name, res in zip(tasks.keys(), results):
        if isinstance(res, Exception):  # pragma: no cover - defensive
            resolved[name] = {
                "status": "unavailable",
                "reason": "unexpected_error",
                "detail": f"{type(res).__name__}: {res}",
                "data_class": "unavailable",
                "cacheable": False,
                "data_label": "UNAVAILABLE - layer raised an unexpected error; nothing fabricated",
            }
        else:
            resolved[name] = res

    terrain = resolved.get("terrain")
    if terrain is not None:
        # Copernicus DEM is a Digital Surface Model - state it in the payload, not only in docs.
        terrain["surface_model"] = (
            "Digital Surface Model (DSM): elevations include buildings, structures and vegetation "
            "canopy. This is NOT bare-earth terrain (DTM)."
        )
        # An unavailable DEM is not measured data; label it as what it is.
        terrain["data_class"] = "observation_derived" if terrain.get("status") == "ok" else "unavailable"
        if terrain.get("status") == "ok":
            terrain.setdefault("resolution_note", (
                f"Native DEM resolution is ~90 m. The {terrain.get('grid_size')}x{terrain.get('grid_size')} "
                "sample grid does not add resolution - interpolation cannot create detail that the "
                "dataset does not contain."
            ))

    osm = resolved.get("osm")
    land_cover = resolved.get("land_cover")

    cacheable = all(
        layer.get("status") == "ok" and layer.get("cacheable", True)
        for layer in (v for v in (terrain, osm, land_cover) if v is not None)
    )

    base: dict[str, Any] = {
        "layer_model": "geographic_base",
        "temporal_invariance": (
            "Identical across Present / Development / Future for the same site, resolution, "
            "layer selection and dataset versions."
        ),
        "radius_km": round(float(radius_km), 3),
        "grid_size": grid_size,
        "layers_requested": layers,
    }
    if terrain is not None:
        base["terrain"] = terrain
    if osm is not None:
        base["osm"] = osm
    if land_cover is not None:
        base["land_cover"] = land_cover
    base["cacheable"] = cacheable
    base["content_digest"] = "sha256:" + stable_digest(_strip_volatile({
        k: v for k, v in base.items() if k not in ("cacheable", "content_digest")
    }))
    return base


async def get_landscape(
    *,
    lat: float,
    lon: float,
    radius_km: float,
    grid_size: int | None = None,
    layers: list[str] | tuple[str, ...] | None = None,
    osm_layers: list[str] | tuple[str, ...] | None = None,
    include_geometry: bool = False,
    parcel_geojson: dict | None = None,
    project: dict | None = None,
    preset: str = "baseline",
    use_cache: bool = True,
) -> dict:
    n = normalize_grid_size(grid_size)
    wanted = resolve_request_layers(layers)
    resolved_osm_layers = resolve_layers(osm_layers) if "osm" in wanted else []

    parcel: dict | None = None
    parcel_error: dict | None = None
    if parcel_geojson is not None:
        try:
            parcel = validate_parcel(parcel_geojson, {"lat": lat, "lon": lon, "radius_km": radius_km})
        except ParcelValidationError as exc:
            parcel_error = {"status": "rejected", "reason": str(exc), "data_class": "user_input"}
        except Exception as exc:  # ParcelValidationUnavailable or unexpected
            parcel_error = {
                "status": "unavailable",
                "reason": f"{type(exc).__name__}: {exc}",
                "data_class": "user_input",
            }

    key = cache_key(
        lat=lat, lon=lon, radius_km=radius_km, grid_size=n, layers=wanted,
        include_geometry=include_geometry, osm_layers=resolved_osm_layers, parcel=parcel,
    )

    # Use the VALIDATED (and, if needed, repaired) parcel geometry - never the raw input -
    # so land cover is masked by exactly the geometry the parcel report describes.
    effective_parcel = parcel["geometry"] if parcel is not None else None

    async def _build() -> dict:
        return await assemble_base_layer(
            lat=lat, lon=lon, radius_km=radius_km, grid_size=n, layers=wanted,
            include_geometry=include_geometry, osm_layers=resolved_osm_layers,
            parcel_geojson=effective_parcel, use_cache=use_cache,
        )

    if use_cache:
        base = await _cache.get_or_fetch(key, _build)
    else:
        base = await _build()
        base.setdefault("cache_hit", None)

    # A rejected/unvalidatable parcel must never mask land cover, so the base layer above
    # was built without it; surface the reason in the response.
    project_inputs = dict(DEFAULT_PROJECT)
    if project:
        for k in ("footprint_ha", "water_mld", "power_mw"):
            if project.get(k) is not None:
                project_inputs[k] = float(project[k])
        project_inputs["source"] = "site record"

    impact = build_impact_layer(
        base_fingerprint=base_fingerprint(
            lat=lat, lon=lon, radius_km=radius_km, grid_size=n, layers=wanted,
            include_geometry=include_geometry, osm_layers=resolved_osm_layers, parcel=parcel,
        ),
        base_content_digest=base["content_digest"],
        base_datasets=[k for k in ("terrain", "osm", "land_cover") if k in base],
        project=project_inputs,
        preset=preset,
        land_cover=base.get("land_cover"),
        parcel=parcel,
    )

    availability = _availability({
        "copernicus_dem_glo90": (
            (base.get("terrain", {}) or {}).get("status") == "ok",
            "integrated" if (base.get("terrain", {}) or {}).get("status") == "ok" else "degraded",
            (base.get("terrain", {}) or {}).get("reason") or "DEM grid retrieved",
        ),
        "openstreetmap_overpass": (
            (base.get("osm", {}) or {}).get("status") == "ok",
            "integrated" if (base.get("osm", {}) or {}).get("status") == "ok" else "degraded",
            (base.get("osm", {}) or {}).get("detail") or "Overpass layers retrieved",
        ),
        "esa_worldcover_2021_v200": (
            (base.get("land_cover", {}) or {}).get("status") == "ok",
            "integrated" if (base.get("land_cover", {}) or {}).get("status") == "ok" else "degraded",
            (base.get("land_cover", {}) or {}).get("reason") or "Land-cover classes retrieved",
        ),
    })

    layers_ok = all(
        base.get(name, {}).get("status") == "ok"
        for name in ("terrain", "osm", "land_cover")
        if name in base
    )
    coverage_warnings = [
        warning
        for warning in (
            (base.get("osm") or {}).get("partial_coverage_warning"),
        )
        if warning
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "layer_model": {"note": LAYER_MODEL_NOTE, "base_layer": "geography", "development_impact_layer": "scenario"},
        "site": {
            "lat": round(float(lat), 6),
            "lon": round(float(lon), 6),
            "radius_km": round(float(radius_km), 3),
        },
        "request": {
            "grid_size": n,
            "layers": wanted,
            "osm_layers": resolved_osm_layers,
            "include_geometry": bool(include_geometry),
            "preset": preset,
            "parcel_supplied": parcel_geojson is not None,
        },
        "base_layer": base,
        "development_impact_layer": impact,
        "parcel": parcel if parcel is not None else parcel_error,
        "datasets": dataset_registry(availability),
        "integrity": {
            "data_class_definitions": DATA_CLASS_DEFINITIONS,
            "rules": INTEGRITY_RULES,
            "not_modelled_effects": NOT_MODELLED_EFFECTS,
        },
        "cache": {"key": key, "hit": base.get("cache_hit"), "cacheable": base.get("cacheable", False)},
        "all_layers_ok": layers_ok,
        "coverage_warnings": coverage_warnings,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "disclaimer": (
            "Screening tool only. Base geography is mapped/measured data with stated limitations; "
            "scenario output is modelled. Not environmental clearance, not a forecast, not legal advice."
        ),
    }


def landscape_json_canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


__all__ = [
    "DEFAULT_LAYERS",
    "DEFAULT_PROJECT",
    "assemble_base_layer",
    "base_fingerprint",
    "cache_key",
    "clear_landscape_cache",
    "get_landscape",
    "landscape_json_canonical",
    "resolve_request_layers",
]

"""
Stage 3A — Dataset provenance & availability registry.

Single source of truth for *what* data TerraLex uses, *where* it comes from,
*what kind of evidence* it is, and *whether it is currently available*.

Integrity rules encoded here (never restate a class more strongly than the data is):
  measured  - physical observation (e.g. interferometric SAR elevation)
  mapped    - classified / digitised representation of reality (OSM, ESA WorldCover)
  estimated - derived arithmetic from mapped/measured inputs (areas, counts, fractions)
  modelled  - assumption-driven scenario output; never a forecast
  user_input- supplied by the user, unverified
  unavailable - no authoritative source connected; shown as UNAVAILABLE, never fabricated
"""

from __future__ import annotations

import datetime

SCHEMA_VERSION = "3a.1"

DATA_CLASS_DEFINITIONS: dict[str, str] = {
    "measured": "Direct physical observation by an instrument (e.g. radar-derived elevation). Still carries sensor error and a native resolution limit.",
    "mapped": "Digitised or classified representation of the world (crowd-sourced vectors, satellite classification). Correctness depends on the mapper/classifier, not on a measurement at your site.",
    "estimated": "Arithmetic derived from mapped/measured inputs (pixel counts x pixel area, feature counts, distances). Precision is inherited from the input, never improved by the calculation.",
    "modelled": "Assumption-driven scenario arithmetic. Illustrative only - not a calibrated model, not a scientific forecast.",
    "user_input": "Provided by the user at submission time. Unverified.",
    "unavailable": "No authoritative source is connected. Reported as UNAVAILABLE and never fabricated.",
}

INTEGRITY_RULES: list[str] = [
    "Copernicus DEM GLO-90 is a Digital Surface Model (DSM): elevations include buildings, structures and vegetation canopy. It is NOT bare-earth terrain (DTM).",
    "Interpolating or re-gridding a DEM does not increase its native resolution. A 20x20 sample grid of a ~90 m DEM is still ~90 m data.",
    "ESA WorldCover is a classified map (76.7% overall accuracy for 2021 v200), not a ground survey. Class areas are estimates from pixel counts.",
    "OpenStreetMap is crowd-sourced and unevenly mapped. Absence of a feature is not evidence that the feature does not exist.",
    "Distances to mapped linear features (roads, rivers) depend on the distance basis: geometry minimum distance vs. mapped centroid are different quantities and are labelled per feature.",
    "Scenario multipliers are configurable planning assumptions, not calibrated coefficients. Scenario output is labelled modelled and is never a forecast.",
    "Environmental effects without a connected authoritative dataset are reported as NOT MODELLED rather than approximated.",
]

# ---------------------------------------------------------------------------
# Registry. `status` is the *declared* integration state; runtime availability
# (retrieval ok / degraded / blocked) is merged in by `dataset_registry()`.
# ---------------------------------------------------------------------------

DATASETS: list[dict] = [
    {
        "id": "copernicus_dem_glo90",
        "name": "Copernicus DEM 2021 release GLO-90",
        "provider": "ESA / DLR e.V. / Airbus Defence and Space, served by Open-Meteo Elevation API",
        "layer": "geographic_base",
        "dataset_version": "2021 release, GLO-90",
        "data_class": "measured",
        "surface_model": "Digital Surface Model (DSM) - includes buildings, structures and vegetation canopy; NOT bare-earth DTM",
        "native_resolution_m": 90,
        "crs": "EPSG:4326 samples, returned as a local East-North-Up grid",
        "temporal_coverage": "2021 release (static product)",
        "accuracy": "GLO-90 global product; absolute vertical accuracy is location-dependent and not published per tile - see Copernicus DEM product documentation",
        "license": "Copernicus DEM open licence / Open-Meteo CC BY 4.0 (non-commercial free tier)",
        "attribution": (
            "Elevation data by Open-Meteo.com (CC BY 4.0, non-commercial free tier) \u2022 "
            "Copernicus DEM 2021 release GLO-90 (90m, \u00a9 DLR e.V. / Airbus Defence and Space GmbH, "
            "funded by the European Union; DOI: 10.5270/ESA-c5d3d65)"
        ),
        "citation_doi": "10.5270/ESA-c5d3d65",
        "citation_doi_url": "https://doi.org/10.5270/ESA-c5d3d65",
        "access_url": "https://open-meteo.com/en/docs/elevation-api",
        "retrieval_mode": "live HTTP (<=100 coordinates per request) + memory/SQLite/browser cache",
        "usage_limits": "Free tier: non-commercial only, <10,000 calls/day, <5,000/hour, <600/minute",
        "limitations": [
            "DSM, not DTM - canopy and structures are included in the elevation",
            "~90 m native resolution: sub-90 m relief detail does not exist in the data",
            "Re-gridding to N x N does not add resolution",
        ],
        "status": "integrated",
    },
    {
        "id": "openstreetmap_overpass",
        "name": "OpenStreetMap geographic features",
        "provider": "OpenStreetMap contributors via the Overpass API",
        "layer": "geographic_base",
        "dataset_version": "live crowd-sourced database (no fixed release)",
        "data_class": "mapped",
        "native_resolution_m": None,
        "crs": "EPSG:4326",
        "temporal_coverage": "continuously edited; reflects the database at query time",
        "accuracy": "Unverified crowd-sourced coverage; completeness varies strongly by region",
        "license": "Open Database License (ODbL)",
        "attribution": "\u00a9 OpenStreetMap contributors (ODbL)",
        "citation_doi": None,
        "citation_doi_url": None,
        "access_url": "https://overpass-api.de",
        "retrieval_mode": "live HTTP POST (Overpass QL), per-layer isolation, cached per site/resolution",
        "usage_limits": "Shared public instance - be conservative; set OVERPASS_URL for a private mirror",
        "limitations": [
            "Absence of a mapped feature is not evidence of absence on the ground",
            "Ways are centrelines/outlines, not surveyed surfaces",
            "Geometry may be incomplete where only centroids are returned",
        ],
        "status": "integrated",
    },
    {
        "id": "esa_worldcover_2021_v200",
        "name": "ESA WorldCover 10 m 2021 v200",
        "provider": "ESA WorldCover consortium (VITO), AWS Open Data public bucket",
        "layer": "geographic_base",
        "dataset_version": "2021 map, algorithm v200",
        "data_class": "mapped",
        "native_resolution_m": 10,
        "crs": "EPSG:4326 (3\u00b0 x 3\u00b0 Cloud-Optimized GeoTIFF tiles)",
        "temporal_coverage": "calendar year 2021",
        "accuracy": "76.7% overall accuracy (WorldCover Product Validation Report V2.0)",
        "license": "CC BY 4.0",
        "attribution": (
            "\u00a9 ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) "
            "processed by ESA WorldCover consortium"
        ),
        "citation_doi": "10.5281/zenodo.7254221",
        "citation_doi_url": "https://doi.org/10.5281/zenodo.7254221",
        "access_url": "https://esa-worldcover.org/en/data-access",
        "retrieval_mode": "live HTTP range requests on Cloud-Optimized GeoTIFF tiles (rasterio/GDAL), cached per site/resolution",
        "usage_limits": "Public bucket; read only the window needed - full tiles are ~100 MB",
        "limitations": [
            "Classified map, not a ground measurement (76.7% overall accuracy)",
            "10 m pixels: class areas are pixel-count estimates, not surveyed areas",
            "2021 snapshot: land cover may have changed since",
            "Terrascope WMTS/WMS layers are RGB previews and are NOT suitable for analysis",
        ],
        "status": "integrated",
    },
    {
        "id": "terralex_scenario_multipliers",
        "name": "TerraLex scenario multipliers",
        "provider": "TerraLex configuration (backend/app/config/scenario_multipliers.json)",
        "layer": "development_impact",
        "dataset_version": SCHEMA_VERSION,
        "data_class": "modelled",
        "native_resolution_m": None,
        "crs": "n/a",
        "temporal_coverage": "scenario-time only",
        "accuracy": "Not applicable - assumptions are not calibrated against observations",
        "license": "Project configuration",
        "attribution": "TerraLex illustrative planning multipliers (configurable, labelled modelled)",
        "citation_doi": None,
        "citation_doi_url": None,
        "access_url": None,
        "retrieval_mode": "local configuration with environment overrides",
        "usage_limits": "Bounds-validated; out-of-bounds values are rejected",
        "limitations": [
            "Illustrative planning assumptions, not calibrated coefficients",
            "Not a scientific forecast and not a compliance statement",
        ],
        "status": "integrated",
    },
    {
        "id": "user_project_inputs",
        "name": "User-provided project inputs",
        "provider": "Submitted by the user",
        "layer": "development_impact",
        "dataset_version": "n/a",
        "data_class": "user_input",
        "native_resolution_m": None,
        "crs": "EPSG:4326 coordinates",
        "temporal_coverage": "at submission",
        "accuracy": "Unverified",
        "license": "n/a",
        "attribution": "User-provided input",
        "citation_doi": None,
        "citation_doi_url": None,
        "access_url": None,
        "retrieval_mode": "request payload",
        "usage_limits": "Server-side validated ranges (Pydantic)",
        "limitations": ["Unverified; a user-drawn parcel is not a surveyed boundary"],
        "status": "integrated",
    },
    {
        "id": "india_code_legal",
        "name": "Central Acts full text (India Code)",
        "provider": "India Code",
        "layer": "legal_reference",
        "dataset_version": "curated seed, verified 2026-10-31",
        "data_class": "mapped",
        "native_resolution_m": None,
        "crs": "n/a",
        "temporal_coverage": "as enacted / as amended at curation time",
        "accuracy": "Abridged excerpts pointing to the official full text",
        "license": "Official source",
        "attribution": "India Code (indiacode.nic.in)",
        "citation_doi": None,
        "citation_doi_url": None,
        "access_url": "https://www.indiacode.nic.in",
        "retrieval_mode": "curated seed import (app/legal_seed.json, app/import_legal.py)",
        "usage_limits": "Manual import pipeline; provenance required",
        "limitations": ["Abridged excerpt only - always read the current text at source"],
        "status": "integrated",
    },
    {
        "id": "moefcc_parivesh",
        "name": "EIA Notification & environmental clearances",
        "provider": "MoEFCC / PARIVESH",
        "layer": "legal_reference",
        "dataset_version": "referenced in legal DB",
        "data_class": "mapped",
        "native_resolution_m": None,
        "crs": "n/a",
        "temporal_coverage": "current notification",
        "accuracy": "Official source",
        "license": "Official source",
        "attribution": "MoEFCC / PARIVESH",
        "citation_doi": None,
        "citation_doi_url": None,
        "access_url": "https://environmentclearance.nic.in",
        "retrieval_mode": "referenced (not machine-queried)",
        "usage_limits": "n/a",
        "limitations": ["Referenced only; clearance status is not queried live"],
        "status": "referenced",
    },
    {
        "id": "cgwa_groundwater",
        "name": "Groundwater NOC guidelines",
        "provider": "Central Ground Water Authority (CGWA)",
        "layer": "legal_reference",
        "dataset_version": "2020 guidelines",
        "data_class": "mapped",
        "native_resolution_m": None,
        "crs": "n/a",
        "temporal_coverage": "2020 guidelines",
        "accuracy": "Official source",
        "license": "Official source",
        "attribution": "CGWA",
        "citation_doi": None,
        "citation_doi_url": None,
        "access_url": "https://cgwa-noc.gov.in",
        "retrieval_mode": "referenced (not machine-queried)",
        "usage_limits": "n/a",
        "limitations": ["Referenced only; NOC status is not queried live"],
        "status": "referenced",
    },
    {
        "id": "hazard_layers_wri_imd_cwc",
        "name": "Flood, heat, drought and water-stress layers",
        "provider": "WRI Aqueduct / IMD / CWC",
        "layer": "geographic_base",
        "dataset_version": "not integrated",
        "data_class": "unavailable",
        "native_resolution_m": None,
        "crs": "n/a",
        "temporal_coverage": "n/a",
        "accuracy": "n/a - no data is served",
        "license": "Check provider terms",
        "attribution": None,
        "citation_doi": None,
        "citation_doi_url": None,
        "access_url": "https://www.wri.org/aqueduct",
        "retrieval_mode": "none",
        "usage_limits": "n/a",
        "limitations": [
            "NOT INTEGRATED - reported as UNAVAILABLE, never fabricated",
            "No flood, heat, drought or water-stress claim is made by TerraLex",
        ],
        "status": "not_integrated",
    },
]

# Effects that TerraLex deliberately does not model. Surfaced verbatim in the API.
NOT_MODELLED_EFFECTS: list[dict] = [
    {"id": "groundwater_drawdown", "effect": "Groundwater drawdown / aquifer response",
     "reason": "Requires hydrogeological survey data and a calibrated groundwater model.", "status": "NOT MODELLED"},
    {"id": "flood_risk_change", "effect": "Change in flood risk / runoff routing",
     "reason": "No authoritative flood-hazard or drainage dataset is connected (WRI Aqueduct / IMD / CWC not integrated).", "status": "NOT MODELLED"},
    {"id": "biodiversity_loss_magnitude", "effect": "Magnitude of biodiversity loss",
     "reason": "Requires species/habitat survey data; land-cover class area alone cannot quantify it.", "status": "NOT MODELLED"},
    {"id": "noise_propagation", "effect": "Noise propagation to receptors",
     "reason": "Requires source power levels, terrain acoustics and a propagation model.", "status": "NOT MODELLED"},
    {"id": "air_quality_dispersion", "effect": "Air-quality dispersion from construction and backup generation",
     "reason": "Requires emission inventories and a dispersion model.", "status": "NOT MODELLED"},
    {"id": "heat_island_magnitude", "effect": "Local heat-island magnitude",
     "reason": "Requires thermal remote sensing and a microclimate model.", "status": "NOT MODELLED"},
    {"id": "ghg_emissions", "effect": "Lifecycle GHG emissions",
     "reason": "Requires grid carbon intensity and lifecycle inventory data.", "status": "NOT MODELLED"},
    {"id": "land_surface_temperature_change", "effect": "Land-surface temperature change",
     "reason": "No thermal time series is connected.", "status": "NOT MODELLED"},
]


def _runtime_status(dataset_id: str, availability: dict[str, dict] | None) -> dict:
    """Merge declared status with observed runtime availability."""
    declared = next((d for d in DATASETS if d["id"] == dataset_id), None)
    declared_status = (declared or {}).get("status", "unknown")
    if not availability or dataset_id not in availability:
        return {"status": declared_status, "available": declared_status == "integrated",
                "observed_at": None, "detail": "Declared integration state; not probed in this request."}
    observed = availability[dataset_id]
    return {
        "status": observed.get("status", declared_status),
        "available": bool(observed.get("available", False)),
        "observed_at": observed.get("observed_at"),
        "detail": observed.get("detail", ""),
    }


def dataset_registry(availability: dict[str, dict] | None = None) -> list[dict]:
    """Full registry with runtime availability merged in."""
    out = []
    for d in DATASETS:
        entry = dict(d)
        entry["availability"] = _runtime_status(d["id"], availability)
        out.append(entry)
    return out


def dataset_by_id(dataset_id: str) -> dict | None:
    for d in DATASETS:
        if d["id"] == dataset_id:
            return dict(d)
    return None


def registry_payload(availability: dict[str, dict] | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "data_class_definitions": DATA_CLASS_DEFINITIONS,
        "integrity_rules": INTEGRITY_RULES,
        "not_modelled_effects": NOT_MODELLED_EFFECTS,
        "datasets": dataset_registry(availability),
    }


# Legacy `/api/v1/data-sources` shape (keys consumed by the frontend):
# provider, dataset, url, license, quality, status  -- DO NOT RENAME.
_LEGACY_VIEW: dict[str, dict] = {
    "copernicus_dem_glo90": {
        "provider": "Open-Meteo Elevation API / Copernicus DEM",
        "dataset": "Copernicus DEM 2021 release GLO-90 (90m Digital Surface Model, DOI: 10.5270/ESA-c5d3d65)",
        "quality": "Measured global DEM (~90m) - DSM, not bare-earth DTM",
    },
    "openstreetmap_overpass": {
        "provider": "OpenStreetMap / Overpass API",
        "dataset": "Water, waterways, wetland, woodland, protected areas, settlements, roads, land use",
        "quality": "Third-party crowd-sourced (mapped)",
    },
    "esa_worldcover_2021_v200": {
        "provider": "ESA WorldCover (VITO) / AWS Open Data",
        "dataset": "ESA WorldCover 10 m 2021 v200 land cover (11 classes, DOI: 10.5281/zenodo.7254221)",
        "quality": "Mapped classification (76.7% overall accuracy); class areas estimated",
    },
    "terralex_scenario_multipliers": {
        "provider": "TerraLex scenario configuration",
        "dataset": "Configurable, bounds-validated scenario multipliers (illustrative planning assumptions)",
        "quality": "Modelled - not calibrated, not a forecast",
    },
    "user_project_inputs": {
        "provider": "User submission",
        "dataset": "Project inputs: footprint, water demand, power, cooling, built-up area, optional parcel geometry",
        "quality": "User input, unverified",
    },
    "india_code_legal": {
        "provider": "India Code", "dataset": "Central Acts full text", "quality": "Verified official (abridged excerpts)",
    },
    "moefcc_parivesh": {
        "provider": "MoEFCC / PARIVESH", "dataset": "EIA Notification & clearances", "quality": "Verified official",
    },
    "cgwa_groundwater": {
        "provider": "CGWA", "dataset": "Groundwater NOC guidelines", "quality": "Verified official",
    },
    "hazard_layers_wri_imd_cwc": {
        "provider": "WRI Aqueduct / IMD / CWC",
        "dataset": "Water stress, flood, climate hazards",
        "quality": "Not integrated",
    },
}

_STATUS_TEXT = {
    "integrated": "Integrated",
    "referenced": "Referenced in legal DB",
    "not_integrated": "Planned - shown as UNAVAILABLE, never fabricated",
}


def data_sources_view(availability: dict[str, dict] | None = None) -> list[dict]:
    """Backwards-compatible `/api/v1/data-sources` list, derived from the registry."""
    registry = {d["id"]: d for d in dataset_registry(availability)}
    out = []
    for dataset_id, view in _LEGACY_VIEW.items():
        entry = registry.get(dataset_id)
        if entry is None:  # pragma: no cover - defensive
            continue
        avail = entry["availability"]
        status = _STATUS_TEXT.get(avail["status"], avail["status"])
        if avail["status"] == "integrated" and avail["observed_at"] and not avail["available"]:
            status = f"Degraded in this request - {avail['detail']}"
        elif avail["status"] == "integrated" and avail["observed_at"]:
            status = "Integrated (live, verified in this request)"
        out.append({
            "provider": view["provider"],
            "dataset": view["dataset"],
            "url": entry.get("access_url") or "https://github.com/Xperia-45/data-center-location",
            "license": entry.get("license") or "n/a",
            "quality": view["quality"],
            "status": status,
        })
    return out

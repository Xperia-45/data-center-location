"""
Environmental findings for a site (Stage 1/2 contract preserved).

Stage 3A moved OSM access into `services/osm.py` (per-layer queries, distance semantics,
optional geometry). This module keeps the exact Stage 1/2 output contract - same keys,
same ordering, same labelled synthetic fallback - and adds nothing that changes the
existing `/api/v1/sites/{id}/analyze` and `/environment` payloads.
"""
import asyncio
from .elevation import get_elevation_grid
from .geo import haversine_km as haversine
from .osm import legacy_overpass_features, sort_features_by_distance

__all__ = ["analyze", "sort_features_by_distance", "overpass_features", "haversine", "classify", "triggers_for"]


async def overpass_features(lat, lon, radius_km):
    """
    Real OSM data via Overpass (Stage 3A: delegated to services/osm.py).

    Returns (features, ok) sorted nearest-first, with the Stage 1/2 feature keys plus the
    new explicit distance-semantics fields. `ok` remains all-or-nothing so a partially
    failed query still falls back to the labelled synthetic demonstration data instead of
    presenting partial geography as complete.
    """
    return await legacy_overpass_features(lat, lon, radius_km)


def classify(count, kind, nearest):
    if kind in ("wetland", "protected") and nearest is not None and nearest < 1:
        return "Critical"
    if nearest is not None and nearest < 1:
        return "High"
    if nearest is not None and nearest < 3:
        return "Moderate"
    if count:
        return "Low"
    return "Low"

async def analyze(site, elevation_grid_size: int | None = None):
    (feats, ok), elevation = await asyncio.gather(
        overpass_features(site["lat"], site["lon"], site["radius_km"]),
        get_elevation_grid(site["lat"], site["lon"], site["radius_km"], grid_size=elevation_grid_size),
    )
    data_label = (
        "Verified real data (OpenStreetMap)"
        if ok
        else "Synthetic demonstration data (OSM unavailable - labelled, do not use for decisions)"
    )
    if not ok:
        # deterministic labelled synthetic fallback scaled within the requested analysis radius
        scale = min(1.0, max(0.1, site["radius_km"] / 5.0))
        raw_fallback = [
            ("Demo water body (synthetic)", "water", site["lat"] + 0.02 * scale, site["lon"] + 0.02 * scale),
            ("Demo settlement (synthetic)", "settlement", site["lat"] - 0.015 * scale, site["lon"] + 0.01 * scale),
        ]
        feats = [
            {
                "name": name,
                "kind": kind,
                "lat": round(flat, 6),
                "lon": round(flon, 6),
                "distance_km": round(haversine(site["lat"], site["lon"], flat, flon), 2),
                "source": "Synthetic demo fallback",
            }
            for name, kind, flat, flon in raw_fallback
        ]
    feats = sort_features_by_distance(feats)
    by = {}
    for f in feats:
        by.setdefault(f["kind"], []).append(f)

    def finding(cat, key, expl):
        lst = by.get(key, [])
        nearest = min([x["distance_km"] for x in lst], default=None)
        risk = classify(len(lst), key, nearest)
        return {
            "category": cat,
            "finding": f"{len(lst)} {key} feature(s) within {site['radius_km']} km"
            + (f"; nearest {nearest} km" if nearest is not None else ""),
            "risk": risk,
            "evidence": lst[:10],
            "source": "OpenStreetMap contributors via Overpass API" if ok else "Synthetic demo fallback",
            "data_quality": "Third-party crowd-sourced; verify on ground" if ok else "Synthetic - not a measurement",
            "last_updated": "Live query at analysis time" if ok else "n/a",
            "confidence": "Medium" if ok else "Low - synthetic",
            "explanation": expl,
            "data_label": data_label,
            "trigger_keys": triggers_for(key, site),
        }

    findings = [
        finding("Water", "water", "Proximity to surface water affects cooling-water options, flood review and discharge consenting."),
        finding("Land and ecology", "wetland", "Wetlands trigger Wetlands Rules 2017 review; proximity raises ecological sensitivity."),
        finding("Land and ecology", "forest", "Forest proximity triggers forest-diversion and biodiversity review."),
        finding("Land and ecology", "protected", "Protected/nature-reserve proximity raises EIA and clearance sensitivity."),
        finding("Community", "settlement", "Nearby settlements indicate community exposure to noise, water demand and traffic."),
    ]
    # climate: explicitly unavailable, not fabricated
    findings.append(
        {
            "category": "Climate and hazards",
            "finding": "Authoritative flood/heat/drought layer not connected in prototype",
            "risk": "UNKNOWN",
            "evidence": [],
            "source": "WRI Aqueduct / IMD / CWC - not yet integrated (see Data Sources)",
            "data_quality": "Unavailable",
            "last_updated": "n/a",
            "confidence": "Unknown",
            "explanation": "No hazard claim is made without a connected authoritative dataset.",
            "data_label": "Unavailable data",
            "trigger_keys": [],
        }
    )
    if site["water_mld"] >= 0.5:
        findings.append(
            {
                "category": "Water",
                "finding": f"Project water demand {site['water_mld']} MLD - groundwater/surface abstraction approval screening required",
                "risk": "Moderate" if site["water_mld"] < 3 else "High",
                "evidence": [],
                "source": "User-provided project input",
                "data_quality": "User input, unverified",
                "last_updated": "At submission",
                "confidence": "Medium",
                "explanation": "Cooling water demand is a key consenting and community-conflict driver in water-stressed regions.",
                "data_label": "User-provided input",
                "trigger_keys": ["water_extraction"],
            }
        )
    return {
        "site_id": site["id"],
        "findings": findings,
        "features": feats,
        "elevation": elevation,
        "data_label": data_label,
        "disclaimer": "Screening tool only. Not environmental clearance or legal advice.",
    }

def triggers_for(kind, site):
    m = {"wetland": ["wetland_proximity"], "forest": ["forest_proximity"], "protected": ["wetland_proximity"], "water": [], "settlement": [], "water_body": []}
    return m.get(kind, [])

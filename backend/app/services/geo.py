"""
Stage 3A — shared local East-North-Up (ENU) geometry helpers.

The constants deliberately match Stage 1/2 (`services/elevation.py`,
`frontend/src/lib/timescapeMath.ts`) so backend distances, DEM grids and the 3D
scene stay in the same local metric frame.
"""

from __future__ import annotations

import math

# Project-wide ENU convention (must stay in sync with elevation.py and timescapeMath.ts)
KM_PER_DEG_LAT = 111.32
METRES_PER_KM = 1000.0

ORIGIN_LAT_TOLERANCE_DEG = 0.002


def clamp_lat(lat: float) -> float:
    return max(-90.0, min(90.0, float(lat)))


def wrap_lon(lon: float) -> float:
    return ((float(lon) + 180.0) % 360.0) - 180.0


def km_per_deg_lon(lat: float) -> float:
    return KM_PER_DEG_LAT * max(1e-6, math.cos(math.radians(float(lat))))


def enu_km(lat: float, lon: float, origin_lat: float, origin_lon: float) -> tuple[float, float]:
    """Local tangent-plane offset in km: (east_km, north_km)."""
    east = (float(lon) - float(origin_lon)) * km_per_deg_lon(origin_lat)
    north = (float(lat) - float(origin_lat)) * KM_PER_DEG_LAT
    return east, north


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km (identical formula to Stage 1/2 environment.py)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bbox_for_circle(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) bounding box of a circle in EPSG:4326."""
    r = abs(float(radius_km))
    d_lat = r / KM_PER_DEG_LAT
    d_lon = r / km_per_deg_lon(lat)
    return (
        wrap_lon(float(lon) - d_lon),
        clamp_lat(float(lat) - d_lat),
        wrap_lon(float(lon) + d_lon),
        clamp_lat(float(lat) + d_lat),
    )


def pixel_area_ha(res_lat_deg: float, res_lon_deg: float, lat: float) -> float:
    """
    Approximate ground area of one raster pixel in hectares at a given latitude,
    using the project ENU degree scale. Labelled `estimated` wherever it is used:
    a degree-grid pixel is not exactly rectangular on the ellipsoid.
    """
    north_m = abs(float(res_lat_deg)) * KM_PER_DEG_LAT * METRES_PER_KM
    east_m = abs(float(res_lon_deg)) * km_per_deg_lon(lat) * METRES_PER_KM
    return (north_m * east_m) / 10_000.0


def decimate(coords: list[tuple[float, float]], max_points: int) -> tuple[list[tuple[float, float]], bool]:
    """Stride-decimate a coordinate sequence, always keeping the first and last point."""
    n = len(coords)
    if max_points < 2 or n <= max_points:
        return list(coords), False
    stride = math.ceil((n - 1) / (max_points - 1))
    out = coords[::stride]
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out, True

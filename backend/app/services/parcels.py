"""
Stage 3A — optional parcel geometry validation (Shapely).

A user-drawn parcel is an OPTIONAL input. When it is supplied it is validated before it
is used to mask land cover or to size the development footprint. When Shapely is not
installed the endpoint reports that validation is unavailable and the parcel is NOT
accepted silently - an unvalidated geometry must never drive a land-cover statistic.

Integrity: a parcel is `user_input`, not a surveyed boundary. Areas are computed in a
local ENU tangent plane (documented approximation), never presented as cadastral area.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any

from .geo import KM_PER_DEG_LAT, enu_km, haversine_km, km_per_deg_lon

MAX_PARCEL_VERTICES = int(os.getenv("MAX_PARCEL_VERTICES", "2000"))
PARCEL_AREA_EPSILON_KM = 1e-9
# Circular buffer segments per quadrant used for the polygon-to-circle containment test.
CIRCLE_QUAD_SEGS = int(os.getenv("PARCEL_CIRCLE_QUAD_SEGS", "64"))

ALLOWED_GEOMETRY_TYPES = ("Polygon", "MultiPolygon")
POLYGONAL_TYPES = ("Polygon", "MultiPolygon")


class ParcelValidationError(ValueError):
    """Parcel rejected: the payload is not a usable polygon."""


class ParcelValidationUnavailable(RuntimeError):
    """Shapely is not installed, so the parcel cannot be validated."""


def shapely_available() -> bool:
    try:
        import shapely  # noqa: F401

        return True
    except Exception:
        return False


def _shapely_or_raise():
    try:
        import shapely
        from shapely.geometry import shape
        from shapely.validation import explain_validity, make_valid
    except Exception as exc:  # pragma: no cover - depends on the deployment image
        raise ParcelValidationUnavailable(
            "Shapely is not installed, so parcel geometry cannot be validated. "
            f"Install with: pip install shapely (import error: {exc})"
        ) from exc
    return shapely, shape, explain_validity, make_valid


def _extract_geometry(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ParcelValidationError("Parcel payload must be a GeoJSON object.")
    ptype = payload.get("type")
    if ptype == "Feature":
        geom = payload.get("geometry")
        if not isinstance(geom, dict):
            raise ParcelValidationError("GeoJSON Feature has no geometry object.")
        ptype = geom.get("type")
        payload = geom
    if ptype == "FeatureCollection":
        raise ParcelValidationError("FeatureCollection is not accepted - send a single Polygon or MultiPolygon.")
    if ptype == "GeometryCollection":
        raise ParcelValidationError("GeometryCollection is not accepted - send a single Polygon or MultiPolygon.")
    if ptype not in ALLOWED_GEOMETRY_TYPES:
        raise ParcelValidationError(
            f"Geometry type '{ptype}' is not accepted; a data-centre parcel must be a Polygon or MultiPolygon."
        )
    coords = payload.get("coordinates")
    # `shapely.geometry.mapping()` (and many GeoJSON producers) use tuples, not lists.
    if not isinstance(coords, (list, tuple)) or not coords:
        raise ParcelValidationError("Parcel geometry has no coordinates.")
    return {"type": ptype, "coordinates": coords}


def _iter_positions(coords: Any, depth: int):
    if depth == 0:
        yield coords
        return
    for c in coords:
        yield from _iter_positions(c, depth - 1)


def _positions(geom: dict) -> list[tuple[float, float]]:
    # Polygon.coordinates = [ring][position][lon, lat]  -> descend 2 levels to reach positions
    # MultiPolygon.coordinates = [polygon][ring][position][lon, lat] -> descend 3 levels
    depth = 2 if geom["type"] == "Polygon" else 3
    out = []
    for pos in _iter_positions(geom["coordinates"], depth):
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            raise ParcelValidationError("Parcel coordinates must be [lon, lat] pairs.")
        lon, lat = pos[0], pos[1]
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            raise ParcelValidationError("Parcel coordinates must be numeric [lon, lat] pairs.")
        if isinstance(lon, bool) or isinstance(lat, bool):
            raise ParcelValidationError("Parcel coordinates must be numeric [lon, lat] pairs.")
        if not (math.isfinite(float(lon)) and math.isfinite(float(lat))):
            raise ParcelValidationError("Parcel coordinates must be finite numbers.")
        if not -90.0 <= float(lat) <= 90.0:
            raise ParcelValidationError(f"Parcel latitude {lat} is outside [-90, 90].")
        if not -180.0 <= float(lon) <= 180.0:
            raise ParcelValidationError(f"Parcel longitude {lon} is outside [-180, 180].")
        out.append((float(lat), float(lon)))
    if not out:
        raise ParcelValidationError("Parcel geometry contains no positions.")
    return out


def extract_polygonal(shp):
    """
    Returns (polygonal_geometry, discarded_geometry_types).

    `make_valid()` on a self-touching or spiked polygon can return a GeometryCollection
    holding polygons *and* zero-area leftovers (LineString / Point). A parcel must be a
    polygon: the polygonal components are kept (unioned) and everything else is discarded
    and reported. If nothing polygonal survives, the caller rejects the parcel.
    """
    if shp.geom_type in POLYGONAL_TYPES:
        return shp, []
    parts = list(getattr(shp, "geoms", []) or [])
    kept = [g for g in parts if g.geom_type in POLYGONAL_TYPES and not g.is_empty]
    discarded: list[str] = [g.geom_type for g in parts if g.geom_type not in POLYGONAL_TYPES]
    if not kept:
        return None, discarded
    if len(kept) == 1:
        return kept[0], discarded
    try:
        from shapely.ops import unary_union

        return unary_union(kept), discarded
    except Exception:  # pragma: no cover - defensive
        return kept[0], discarded


def parcel_bbox(geojson: dict) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat). Pure Python - works without Shapely."""
    geom = _extract_geometry(geojson)
    pts = _positions(geom)
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    return (min(lons), min(lats), max(lons), max(lats))


def parcel_digest(geojson: dict, length: int = 16) -> str:
    payload = json.dumps(geojson, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def validate_parcel(geojson: dict, site: dict | None = None) -> dict:
    """
    Validates a parcel polygon and measures it in a local ENU plane centred on the site.
    Raises ParcelValidationError (reject) or ParcelValidationUnavailable (503).
    """
    geom = _extract_geometry(geojson)
    pts = _positions(geom)
    if len(pts) > MAX_PARCEL_VERTICES:
        raise ParcelValidationError(
            f"Parcel has {len(pts)} vertices; the limit is {MAX_PARCEL_VERTICES}. Simplify the geometry."
        )
    shapely, shape, explain_validity, make_valid = _shapely_or_raise()

    origin_lat = float(site["lat"]) if site and site.get("lat") is not None else pts[0][0]
    origin_lon = float(site["lon"]) if site and site.get("lon") is not None else pts[0][1]

    try:
        shp = shape(geom)
    except Exception as exc:
        raise ParcelValidationError(f"GeoJSON could not be parsed as a geometry: {exc}") from exc

    repaired = False
    discarded_components: list[str] = []
    extracted_from_collection = False
    validity_error = None if shp.is_valid else explain_validity(shp)
    if not shp.is_valid:
        shp = make_valid(shp)
        repaired = True
        if shp.is_empty:
            raise ParcelValidationError("Parcel geometry is not valid and could not be repaired.")
        if shp.geom_type not in POLYGONAL_TYPES:
            # A repaired GeometryCollection (or a bare line/point) is NOT an acceptable parcel.
            shp, discarded_components = extract_polygonal(shp)
            extracted_from_collection = True
            if shp is None:
                raise ParcelValidationError(
                    "Parcel geometry is not valid and repairing it produced no polygonal area "
                    f"(discarded components: {', '.join(discarded_components) or 'none'}). "
                    "A parcel must be a Polygon or MultiPolygon."
                )
        if not shp.is_valid:  # pragma: no cover - defensive
            raise ParcelValidationError("Parcel geometry is still not valid after repair.")

    # Local ENU projection (km) so area/perimeter are metric, not degrees.
    def _to_enu(coords):
        return [enu_km(y, x, origin_lat, origin_lon) for x, y in coords]

    if hasattr(shapely, "transform"):  # Shapely >= 2.0
        projected = shapely.transform(shp, _to_enu)
    else:  # pragma: no cover - legacy Shapely 1.x
        from shapely.ops import transform as _legacy_transform

        projected = _legacy_transform(lambda x, y, z=None: enu_km(y, x, origin_lat, origin_lon), shp)
    if projected.is_empty or projected.area < PARCEL_AREA_EPSILON_KM:
        raise ParcelValidationError("Parcel has no measurable area after projection.")

    area_km2 = float(projected.area)
    perimeter_km = float(projected.length)
    min_lon, min_lat, max_lon, max_lat = shp.bounds  # bounds of the geometry actually used
    centroid_lon, centroid_lat = shp.centroid.x, shp.centroid.y

    out: dict[str, Any] = {
        "status": "ok",
        "valid": True,
        "was_repaired": repaired,
        "validity_error": validity_error,
        "repair_extracted_polygonal_components": extracted_from_collection,
        "discarded_non_polygonal_components": discarded_components,
        "geometry_type": shp.geom_type,
        "vertex_count": len(pts),
        "area_ha": round(area_km2 * 100.0, 4),
        "area_km2": round(area_km2, 6),
        "perimeter_km": round(perimeter_km, 4),
        "area_method": "Local ENU tangent-plane projection around the site (approximation, not cadastral area)",
        "bbox": {"min_lon": round(min_lon, 6), "min_lat": round(min_lat, 6),
                 "max_lon": round(max_lon, 6), "max_lat": round(max_lat, 6)},
        "bbox_size_km": {
            "east_west": round((max_lon - min_lon) * km_per_deg_lon(origin_lat), 4),
            "north_south": round((max_lat - min_lat) * KM_PER_DEG_LAT, 4),
        },
        "centroid": {"lat": round(float(centroid_lat), 6), "lon": round(float(centroid_lon), 6)},
        "digest": parcel_digest(geom),
        "data_class": "user_input",
        "data_label": "User-supplied parcel geometry - not a surveyed or cadastral boundary",
        # `geometry` is the geometry TerraLex will actually use: the repaired/extracted one
        # when a repair happened, otherwise the validated input.
        "geometry": _mapping(shp),
        "input_geometry": {"type": geom["type"], "coordinates": geom["coordinates"]},
        "geometry_modified_by_repair": repaired,
    }

    if site is not None and site.get("lat") is not None:
        site_point = shapely.geometry.Point(0.0, 0.0)
        radius_km = float(site.get("radius_km") or 0.0)
        min_distance_km = float(projected.distance(site_point))
        max_vertex_km = max(math.hypot(*enu_km(la, lo, origin_lat, origin_lon)) for la, lo in pts)
        max_geodesic_km = max(haversine_km(origin_lat, origin_lon, la, lo) for la, lo in pts)

        if radius_km > 0:
            # Real polygon-to-circle containment: the whole (possibly concave, possibly
            # repaired) polygon must be covered by the site disc - not just its vertices.
            disc = site_point.buffer(radius_km, quad_segs=CIRCLE_QUAD_SEGS)
            within_site_radius = bool(disc.covers(projected))
            containment_method = (
                "Polygon-to-circle containment: the parcel polygon is tested with "
                f"Shapely `covers` against a {CIRCLE_QUAD_SEGS * 4}-segment circular buffer of "
                "radius_km around the site, in the local ENU plane. `covers` counts a parcel "
                "touching the boundary as contained."
            )
        else:
            within_site_radius = False
            containment_method = "radius_km must be > 0 to test containment."

        out["site"] = {
            "lat": round(origin_lat, 6),
            "lon": round(origin_lon, 6),
            "radius_km": round(radius_km, 3),
            "min_distance_km": round(min_distance_km, 4),
            "max_distance_km": round(max_vertex_km, 4),
            "max_vertex_distance_km": round(max_vertex_km, 4),
            "max_geodesic_vertex_distance_km": round(max_geodesic_km, 4),
            "centroid_distance_km": round(
                math.hypot(*enu_km(float(centroid_lat), float(centroid_lon), origin_lat, origin_lon)), 4
            ),
            "within_site_radius": within_site_radius,
            "all_vertices_within_site_radius": bool(radius_km > 0 and max_vertex_km <= radius_km),
            "containment_method": containment_method,
            "containment_note": (
                "`within_site_radius` is a polygon-to-circle test and is the field to rely on. "
                "`all_vertices_within_site_radius` is a vertex-only check and by itself does NOT "
                "prove that every edge lies inside the site circle. Distances are computed in a "
                "local ENU tangent plane; `max_geodesic_vertex_distance_km` reports the same "
                "vertices by great-circle distance so the planar approximation is visible. The "
                "circular buffer is a polygonal approximation of the disc (inscribed, so very "
                "slightly conservative)."
            ),
        }
    return out


def _mapping(shp) -> dict:
    """
    Plain-list GeoJSON mapping of the geometry TerraLex will actually use.

    `shapely.geometry.mapping()` returns tuples; normalising to lists keeps the API
    payload (and anything cached from it) plain JSON-compatible.
    """
    import json as _json

    from shapely.geometry import mapping

    return _json.loads(_json.dumps(mapping(shp)))

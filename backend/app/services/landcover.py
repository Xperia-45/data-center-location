"""
Stage 3A — ESA WorldCover 2021 v200 land-cover layer.

Reads only the pixels needed from the public AWS Cloud-Optimized GeoTIFF tiles using
HTTP range requests (rasterio/GDAL `/vsicurl`). The whole 3\u00b0 tile is ~100 MB, so a
full-tile download is never performed.

Integrity rules enforced here:
  * The product is a **classified map** (76.7% overall accuracy), not a ground survey.
    Every class area is labelled `estimated`, the layer `mapped`.
  * Native resolution is 10 m. When the requested AOI holds more pixels than the
    sampling budget, pixels are **systematically subsampled** (a documented, unbiased
    stride) - never interpolated, because interpolation cannot add resolution.
  * NoData pixels are counted and reported, never silently dropped.
  * If rasterio is missing, the network fails, or the AOI is too large, the layer is
    reported `unavailable` with a reason. No class fraction is ever invented.

`rasterio` is an OPTIONAL runtime dependency: the backend imports fine without it and
reports `reason = "rasterio_unavailable"`.
"""

from __future__ import annotations

import asyncio
import datetime
import math
import os
from typing import Any

from .geo import bbox_for_circle, pixel_area_ha

WORLDCOVER_ENABLED = os.getenv("WORLDCOVER_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
WORLDCOVER_URL_PREFIX = os.getenv(
    "WORLDCOVER_URL_PREFIX", "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
).rstrip("/")
WORLDCOVER_YEAR = int(os.getenv("WORLDCOVER_YEAR", "2021"))
WORLDCOVER_VERSION = os.getenv("WORLDCOVER_VERSION", "v200")
WORLDCOVER_TILE_DEG = 3.0
WORLDCOVER_NODATA = 0
WORLDCOVER_TIMEOUT_SEC = float(os.getenv("WORLDCOVER_TIMEOUT_SEC", "30"))
WORLDCOVER_MAX_WINDOW_PIXELS = int(os.getenv("WORLDCOVER_MAX_WINDOW_PIXELS", str(16_000_000)))
WORLDCOVER_MAX_SAMPLED_PIXELS = int(os.getenv("WORLDCOVER_MAX_SAMPLED_PIXELS", str(2_000_000)))
WORLDCOVER_READ_CHUNK_ROWS = int(os.getenv("WORLDCOVER_READ_CHUNK_ROWS", "1024"))
WORLDCOVER_MAX_TILES = int(os.getenv("WORLDCOVER_MAX_TILES", "4"))

WORLDCOVER_DOI = "10.5281/zenodo.7254221"
WORLDCOVER_DOI_URL = f"https://doi.org/{WORLDCOVER_DOI}"
WORLDCOVER_ACCESS_URL = "https://esa-worldcover.org/en/data-access"
WORLDCOVER_LICENSE = "CC BY 4.0"
WORLDCOVER_ATTRIBUTION = (
    "\u00a9 ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) "
    "processed by ESA WorldCover consortium"
)
WORLDCOVER_CITATION = (
    "Zanaga, D., Van De Kerchove, R., Daems, D., De Keersmaecker, W., Brockmann, C., Kirches, G., "
    "Wevers, J., Cartus, O., Santoro, M., Fritz, S., Herold, M., Tsendbazar, N.E., Xu, P., "
    "Ramoino, F., Arino, O., 2022. ESA WorldCover 10 m 2021 v200. https://doi.org/10.5281/zenodo.7254221"
)
ACCURACY_NOTE = (
    "ESA WorldCover 2021 v200 has 76.7% overall accuracy (Product Validation Report V2.0). "
    "It is a satellite-derived classification, not a ground survey."
)

# Official v200 class table (11 classes). NOTE: v100 swaps 70/95 - the version is part of
# every payload so the legend can never be applied to the wrong release.
WORLDCOVER_CLASSES: list[dict[str, Any]] = [
    {"value": 10, "label": "Tree cover", "color_hex": "#006400", "group": "vegetated"},
    {"value": 20, "label": "Shrubland", "color_hex": "#ffbb22", "group": "vegetated"},
    {"value": 30, "label": "Grassland", "color_hex": "#ffff4c", "group": "vegetated"},
    {"value": 40, "label": "Cropland", "color_hex": "#f096ff", "group": "agricultural"},
    {"value": 50, "label": "Built-up", "color_hex": "#fa0000", "group": "built_up"},
    {"value": 60, "label": "Bare / sparse vegetation", "color_hex": "#b4b4b4", "group": "bare"},
    {"value": 70, "label": "Snow and ice", "color_hex": "#f0f0f0", "group": "snow_ice"},
    {"value": 80, "label": "Permanent water bodies", "color_hex": "#0064c8", "group": "water"},
    {"value": 90, "label": "Herbaceous wetland", "color_hex": "#0096a0", "group": "wetland"},
    {"value": 95, "label": "Mangroves", "color_hex": "#00cf75", "group": "wetland"},
    {"value": 100, "label": "Moss and lichen", "color_hex": "#fae6a0", "group": "vegetated"},
]
CLASS_BY_VALUE = {c["value"]: c for c in WORLDCOVER_CLASSES}


def tile_id_for(lat: float, lon: float) -> str:
    """WorldCover 3\u00b0 tile id from the lower-left corner, e.g. (30.901, 75.857) -> 'N30E075'."""
    lat_i = int(math.floor(float(lat) / WORLDCOVER_TILE_DEG) * WORLDCOVER_TILE_DEG)
    lon_i = int(math.floor(float(lon) / WORLDCOVER_TILE_DEG) * WORLDCOVER_TILE_DEG)
    return f"{'N' if lat_i >= 0 else 'S'}{abs(lat_i):02d}{'E' if lon_i >= 0 else 'W'}{abs(lon_i):03d}"


def tiles_for_bbox(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> list[str]:
    ids: list[str] = []
    lat = math.floor(min_lat / WORLDCOVER_TILE_DEG) * WORLDCOVER_TILE_DEG
    while lat <= max_lat + 1e-9:
        lon = math.floor(min_lon / WORLDCOVER_TILE_DEG) * WORLDCOVER_TILE_DEG
        while lon <= max_lon + 1e-9:
            tid = tile_id_for(lat + WORLDCOVER_TILE_DEG / 2, lon + WORLDCOVER_TILE_DEG / 2)
            if tid not in ids:
                ids.append(tid)
            lon += WORLDCOVER_TILE_DEG
        lat += WORLDCOVER_TILE_DEG
    return ids


def tile_url(tile_id: str) -> str:
    return (
        f"{WORLDCOVER_URL_PREFIX}/{WORLDCOVER_VERSION}/{WORLDCOVER_YEAR}/map/"
        f"ESA_WorldCover_10m_{WORLDCOVER_YEAR}_{WORLDCOVER_VERSION}_{tile_id}_Map.tif"
    )


def dataset_metadata() -> dict:
    return {
        "id": "esa_worldcover_2021_v200",
        "name": f"ESA WorldCover 10 m {WORLDCOVER_YEAR} {WORLDCOVER_VERSION}",
        "dataset_version": f"{WORLDCOVER_YEAR} map, algorithm {WORLDCOVER_VERSION}",
        "provider": "ESA WorldCover consortium (VITO)",
        "data_class": "mapped",
        "native_resolution_m": 10,
        "crs": "EPSG:4326",
        "tile_size_deg": WORLDCOVER_TILE_DEG,
        "nodata_value": WORLDCOVER_NODATA,
        "class_table": WORLDCOVER_CLASSES,
        "accuracy": ACCURACY_NOTE,
        "license": WORLDCOVER_LICENSE,
        "attribution": WORLDCOVER_ATTRIBUTION,
        "citation": WORLDCOVER_CITATION,
        "citation_doi": WORLDCOVER_DOI,
        "citation_doi_url": WORLDCOVER_DOI_URL,
        "access_url": WORLDCOVER_ACCESS_URL,
    }


def unavailable(reason: str, detail: str, **extra: Any) -> dict:
    payload = {
        "status": "unavailable",
        "reason": reason,
        "detail": detail,
        "data_class": "unavailable",
        "classes": None,
        "cacheable": False,
        "dataset": dataset_metadata(),
        "data_label": "Land cover UNAVAILABLE - no class fraction is estimated or invented",
    }
    payload.update(extra)
    return payload


def _rasterio_source(url: str) -> str:
    return f"/vsicurl/{url}" if url.startswith(("http://", "https://")) else url


def _parse_parcel(polygon_coords: Any):
    """Builds a Shapely geometry for a parcel mask. Returns (geom, error_reason)."""
    try:
        from shapely.geometry import shape
    except Exception:
        return None, "shapely_unavailable"
    try:
        geom = shape(polygon_coords)
    except Exception:
        return None, "invalid_parcel_geometry"
    if geom.is_empty:
        return None, "empty_parcel_geometry"
    return geom, None


def _read_worldcover(
    lat: float,
    lon: float,
    radius_km: float,
    aoi: dict,
    max_window_pixels: int = WORLDCOVER_MAX_WINDOW_PIXELS,
    max_sampled_pixels: int = WORLDCOVER_MAX_SAMPLED_PIXELS,
) -> dict:
    """
    Synchronous raster read; executed in a worker thread by `landcover_summary`.

    The stride is decided ONCE from the total AOI pixel count (measured on the first
    tile's geotransform) so every tile is sampled identically and class areas stay
    comparable across a multi-tile AOI.
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.windows import Window
    except Exception as exc:
        return unavailable(
            "rasterio_unavailable",
            "rasterio/GDAL is not installed, so ESA WorldCover tiles cannot be read. "
            "The backend continues to run without a land-cover layer. "
            f"Install with: pip install rasterio (import error: {exc})",
        )

    parcel_geom = None
    if aoi["kind"] == "parcel":
        parcel_geom, err = _parse_parcel(aoi["geometry"])
        if err:
            return unavailable(err, "A valid parcel polygon is required to mask land-cover pixels to the parcel.")

    min_lon, min_lat, max_lon, max_lat = aoi["bbox"]
    tile_ids = tiles_for_bbox(min_lon, min_lat, max_lon, max_lat)
    if len(tile_ids) > WORLDCOVER_MAX_TILES:
        return unavailable(
            "too_many_tiles",
            f"The AOI spans {len(tile_ids)} WorldCover tiles (limit {WORLDCOVER_MAX_TILES}). "
            "Reduce the radius or supply a smaller parcel.",
        )

    env_opts = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "GDAL_HTTP_TIMEOUT": str(int(max(5.0, WORLDCOVER_TIMEOUT_SEC))),
        "GDAL_HTTP_MAX_RETRY": "2",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    }
    counts: dict[int, int] = {}
    total_sampled = 0
    tiles_used: list[dict] = []
    window_pixels_total = 0
    stride = 1
    pixel_area = None
    res_deg = None
    nodata_val = WORLDCOVER_NODATA

    try:
        with rasterio.Env(**env_opts):
            # -- pass 1: pixel grid resolution, then one stride for the whole AOI -------
            with rasterio.open(_rasterio_source(tile_url(tile_ids[0]))) as probe:
                res_x, res_y = abs(probe.transform.a), abs(probe.transform.e)
            res_deg = (res_y, res_x)
            pixel_area = pixel_area_ha(res_y, res_x, lat)
            aoi_pixels = int(
                math.ceil((max_lon - min_lon) / res_x) * math.ceil((max_lat - min_lat) / res_y)
            )
            if aoi_pixels > max_window_pixels:
                return unavailable(
                    "aoi_exceeds_landcover_budget",
                    f"The AOI covers about {aoi_pixels:,} WorldCover pixels "
                    f"(budget {max_window_pixels:,}). Reduce the radius/parcel rather than "
                    "downsampling the map: coarser resampling would misrepresent a 10 m classification.",
                )
            stride = max(1, int(math.ceil(math.sqrt(aoi_pixels / max(1, max_sampled_pixels)))))

            # -- pass 2: read each tile window in row chunks --------------------------
            for tid in tile_ids:
                url = tile_url(tid)
                with rasterio.open(_rasterio_source(url)) as ds:
                    transform = ds.transform
                    left, top = transform.c, transform.f
                    if ds.nodata is not None:
                        nodata_val = int(ds.nodata)
                    col0 = max(0, int(math.floor((min_lon - left) / res_x)))
                    row0 = max(0, int(math.floor((top - max_lat) / res_y)))
                    col1 = min(ds.width, int(math.ceil((max_lon - left) / res_x)))
                    row1 = min(ds.height, int(math.ceil((top - min_lat) / res_y)))
                    w, h = col1 - col0, row1 - row0
                    if w <= 0 or h <= 0:
                        continue
                    window_pixels_total += w * h

                    cols = np.arange(col0, col1, stride)
                    lons = left + (cols + 0.5) * res_x
                    if parcel_geom is not None:
                        try:
                            import shapely
                        except Exception:
                            return unavailable(
                                "shapely_unavailable",
                                "Shapely is required to mask land cover to a parcel polygon.",
                            )

                    for r0 in range(row0, row1, WORLDCOVER_READ_CHUNK_ROWS):
                        ch = min(WORLDCOVER_READ_CHUNK_ROWS, row1 - r0)
                        arr = ds.read(1, window=Window(col0, r0, w, ch))
                        block = arr[::stride, ::stride]
                        rows = np.arange(r0, r0 + ch, stride)
                        if block.shape[1] != cols.size or block.shape[0] != rows.size:
                            # Defensive: never count a misaligned block.
                            continue
                        lats = top - (rows + 0.5) * res_y
                        if parcel_geom is not None:
                            lon_grid, lat_grid = np.meshgrid(lons, lats)
                            mask = shapely.contains_xy(parcel_geom, lon_grid.ravel(), lat_grid.ravel())
                            mask = mask.reshape(lat_grid.shape)
                        else:
                            east = (lons[None, :] - lon) * (111.32 * math.cos(math.radians(lat)))
                            north = (lats[:, None] - lat) * 111.32
                            mask = np.hypot(east, north) <= radius_km
                        vals = block[mask]
                        if vals.size == 0:
                            continue
                        total_sampled += int(vals.size)
                        binc = np.bincount(vals.astype("int64"), minlength=101)
                        for value, count in enumerate(binc):
                            if count:
                                counts[value] = counts.get(value, 0) + int(count)
                    tiles_used.append({
                        "tile_id": tid,
                        "url": url,
                        "window": {"col_off": col0, "row_off": row0, "width": w, "height": h},
                        "stride_pixels": stride,
                    })
    except Exception as exc:
        name = type(exc).__name__
        return unavailable(
            "raster_read_failed",
            f"ESA WorldCover tile read failed ({name}: {exc}). No class fraction is estimated.",
            tiles_attempted=[{"tile_id": t, "url": tile_url(t)} for t in tile_ids],
        )

    if total_sampled == 0 or not tiles_used:
        return unavailable(
            "no_pixels_in_aoi",
            "No WorldCover pixels intersected the AOI (outside product coverage, or entirely NoData).",
            tiles_attempted=[{"tile_id": t, "url": tile_url(t)} for t in tile_ids],
        )

    nodata_pixels = counts.get(nodata_val, 0)
    valid_pixels = total_sampled - nodata_pixels
    area_per_sample = stride * stride * (pixel_area or 0.0)
    classes = []
    for spec in WORLDCOVER_CLASSES:
        c = counts.get(spec["value"], 0)
        classes.append({
            **spec,
            "sampled_pixels": c,
            "fraction": round(c / valid_pixels, 6) if valid_pixels else 0.0,
            "area_ha": round(c * area_per_sample, 3),
            "data_class": "mapped",
            "area_data_class": "estimated",
        })
    unknown_values = sorted(v for v in counts if v not in CLASS_BY_VALUE)

    return {
        "status": "ok",
        "reason": None,
        "detail": (
            f"{total_sampled:,} pixels sampled ({valid_pixels:,} valid, {nodata_pixels:,} NoData) "
            f"across {len(tiles_used)} tile(s); stride {stride} px."
        ),
        "dataset": dataset_metadata(),
        "aoi": aoi,
        "tiles": tiles_used,
        "native_resolution_m": 10,
        "pixel_size_deg": {"lat": res_deg[0], "lon": res_deg[1]} if res_deg else None,
        "pixel_area_ha_at_site_latitude": round(pixel_area, 8) if pixel_area else None,
        "window_pixels": window_pixels_total,
        "sampled_pixels": total_sampled,
        "valid_pixels": valid_pixels,
        "nodata_pixels": nodata_pixels,
        "nodata_fraction": round(nodata_pixels / total_sampled, 6),
        "stride_pixels": stride,
        "sampling_method": "full_window" if stride == 1 else "systematic_subsample",
        "sampling_note": (
            "Every pixel inside the AOI window was counted."
            if stride == 1
            else f"Pixels were systematically subsampled every {stride} pixel(s) in x and y to stay "
                 "within the read budget. Systematic subsampling estimates class proportions; it "
                 "does not change or improve the 10 m native resolution."
        ),
        "sampled_area_ha": round(valid_pixels * area_per_sample, 3),
        "classes": classes,
        "unexpected_class_values": unknown_values,
        "accuracy_note": ACCURACY_NOTE,
        "license": WORLDCOVER_LICENSE,
        "attribution": WORLDCOVER_ATTRIBUTION,
        "citation_doi": WORLDCOVER_DOI,
        "citation_doi_url": WORLDCOVER_DOI_URL,
        "cacheable": True,
        "data_label": (
            f"Mapped land cover (ESA WorldCover {WORLDCOVER_YEAR} {WORLDCOVER_VERSION}, 10 m "
            "classified; class areas estimated from pixel counts)"
        ),
    }


async def landcover_summary(
    lat: float,
    lon: float,
    radius_km: float,
    parcel_geojson: dict | None = None,
    max_window_pixels: int = WORLDCOVER_MAX_WINDOW_PIXELS,
    max_sampled_pixels: int = WORLDCOVER_MAX_SAMPLED_PIXELS,
) -> dict:
    """
    Land-cover class fractions for the site circle (default) or a parcel polygon.
    Never raises; returns an explicit `unavailable` payload instead.
    """
    if not WORLDCOVER_ENABLED:
        return unavailable(
            "disabled_by_configuration",
            "WORLDCOVER_ENABLED is off, so no land-cover layer is served.",
        )

    if parcel_geojson is not None:
        from .parcels import parcel_bbox

        try:
            bbox = parcel_bbox(parcel_geojson)
        except Exception as exc:
            return unavailable("invalid_parcel_geometry", f"Parcel geometry could not be read: {exc}")
        aoi = {"kind": "parcel", "bbox": bbox, "geometry": parcel_geojson, "radius_km": None}
    else:
        aoi = {
            "kind": "circle",
            "bbox": bbox_for_circle(lat, lon, radius_km),
            "radius_km": round(float(radius_km), 3),
            "centre": {"lat": round(float(lat), 6), "lon": round(float(lon), 6)},
        }

    try:
        payload = await asyncio.wait_for(
            asyncio.to_thread(
                _read_worldcover, lat, lon, radius_km, aoi, max_window_pixels, max_sampled_pixels
            ),
            timeout=WORLDCOVER_TIMEOUT_SEC + 5.0,
        )
    except asyncio.TimeoutError:
        return unavailable(
            "timeout",
            f"ESA WorldCover read exceeded {WORLDCOVER_TIMEOUT_SEC + 5.0:.0f}s.",
            aoi=aoi,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return unavailable("unexpected_error", f"{type(exc).__name__}: {exc}", aoi=aoi)

    payload.setdefault("retrieved_at", datetime.datetime.utcnow().isoformat() + "Z")
    return payload

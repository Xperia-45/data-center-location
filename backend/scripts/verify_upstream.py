#!/usr/bin/env python3
"""
Live upstream verification for TerraLex Stage 3A.

    cd backend && ../.venv/bin/python scripts/verify_upstream.py

Contacts the REAL providers - Open-Meteo (Copernicus DEM GLO-90), the public Overpass
API (OpenStreetMap) and the public ESA WorldCover S3 bucket - and prints what each one
actually returned. Use it on any machine with outbound internet; the CI sandbox has none,
which is exactly why this script exists separately from the test suite.

Exit code 0 = every probed source answered. 1 = at least one source failed (the reason
is printed). Nothing here is stubbed and nothing here writes to the database.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.services import landcover  # noqa: E402
from app.services.elevation import get_elevation_grid  # noqa: E402
from app.services.osm import DEFAULT_LAYERS, fetch_layer, fetch_osm_layers  # noqa: E402

SITES = {
    "Ludhiana (IN-PB)": (30.901, 75.8573),
    "Mohali (IN-PB)": (30.7046, 76.7179),
    "Hyderabad (IN-TG)": (17.385, 78.4867),
}
RADIUS_KM = 3.0

results: list[tuple[str, str, str]] = []


def record(check: str, ok: bool, detail: str) -> None:
    results.append((check, "PASS" if ok else "FAIL", detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {check}: {detail}")


async def check_open_meteo() -> None:
    print("\n== Copernicus DEM GLO-90 via Open-Meteo (measured elevation) ==")
    for name, (lat, lon) in SITES.items():
        grid = await get_elevation_grid(lat, lon, RADIUS_KM, grid_size=8)
        if grid["status"] == "ok":
            record(
                f"open-meteo/{name}", True,
                f"center {grid['center_m']} m AMSL, range {grid['min_m']}-{grid['max_m']} m, "
                f"relief {grid['relief_m']} m, {grid['grid_size']}x{grid['grid_size']} grid, "
                f"cache={grid['cache_hit']}",
            )
        else:
            record(f"open-meteo/{name}", False, f"{grid['reason']}: {grid['detail']}")


async def check_overpass() -> None:
    print("\n== OpenStreetMap via Overpass (mapped geography) ==")
    lat, lon = SITES["Ludhiana (IN-PB)"]
    result = await fetch_osm_layers(lat, lon, RADIUS_KM)
    record("overpass/overall", result["status"] == "ok", result["detail"])
    for layer_id in DEFAULT_LAYERS:
        info = result["layer_status"].get(layer_id, {})
        count = len(result["layers"].get(layer_id, {}).get("features", []))
        record(
            f"overpass/{layer_id}", info.get("status") == "ok",
            f"{count} feature(s); {info.get('detail', '')}",
        )
    single = await fetch_layer("road", lat, lon, RADIUS_KM, include_geometry=True)
    if single["status"] == "ok" and single["features"]:
        feature = single["features"][0]
        record(
            "overpass/road-geometry", True,
            f"nearest road {feature['distance_km']} km ({feature['distance_basis']}), "
            f"representative point {feature['representative_distance_km']} km "
            f"({feature['representative_point_kind']})",
        )
    else:
        record("overpass/road-geometry", single["status"] == "ok",
               f"no road geometry returned ({single.get('reason', 'empty')})")


async def check_worldcover() -> None:
    print("\n== ESA WorldCover 2021 v200 (mapped land cover) ==")
    try:
        import rasterio  # noqa: F401
    except Exception as exc:
        record("worldcover/rasterio", False, f"rasterio unavailable: {exc}")
        return
    lat, lon = SITES["Ludhiana (IN-PB)"]
    tile_id = landcover.tile_id_for(lat, lon)
    url = landcover.tile_url(tile_id)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            head = await client.head(url)
        record(
            "worldcover/tile-head", head.status_code == 200,
            f"{tile_id} HTTP {head.status_code}, "
            f"content-length={head.headers.get('content-length')}, "
            f"accept-ranges={head.headers.get('accept-ranges')}",
        )
    except Exception as exc:
        record("worldcover/tile-head", False, f"{type(exc).__name__}: {exc}")

    summary = await landcover.landcover_summary(lat, lon, 2.0)
    if summary["status"] == "ok":
        present = sorted(
            ((cl["label"], round(cl["fraction"] * 100, 1), cl["area_ha"])
             for cl in summary["classes"] if cl["sampled_pixels"] > 0),
            key=lambda row: -row[1],
        )
        record(
            "worldcover/class-fractions", True,
            f"{summary['sampled_pixels']:,} px sampled (stride {summary['stride_pixels']}, "
            f"nodata {summary['nodata_fraction']:.3f}); "
            + ", ".join(f"{label} {pct}% ({ha} ha)" for label, pct, ha in present[:5]),
        )
    else:
        record("worldcover/class-fractions", False, f"{summary['reason']}: {summary['detail']}")


async def main() -> int:
    print(f"TerraLex Stage 3A upstream verification - {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Overpass endpoint: {os.getenv('OVERPASS_URL', 'https://overpass-api.de/api/interpreter')}")
    print(f"WorldCover prefix: {landcover.WORLDCOVER_URL_PREFIX}")
    await check_open_meteo()
    await check_overpass()
    await check_worldcover()

    print("\n== Summary ==")
    for check, status, detail in results:
        print(f"{status:5} {check}")
    failures = [r for r in results if r[1] == "FAIL"]
    print(f"\n{len(results) - len(failures)}/{len(results)} checks passed.")
    if failures:
        print("Failing sources are reported as UNAVAILABLE by the API - never fabricated.")
        print(json.dumps([{"check": c, "detail": d} for c, _, d in failures], indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

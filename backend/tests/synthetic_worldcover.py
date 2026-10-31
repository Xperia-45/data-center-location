"""
Builder for a SYNTHETIC ESA WorldCover-shaped Cloud-Optimized GeoTIFF.

*** THIS IS NOT ESA DATA ***
The pixels are generated locally so the production read path (tile URL construction,
/vsicurl range requests, window maths, class counting, NoData accounting, subsampling)
can be executed and asserted inside a sandbox with no outbound internet. Every payload
produced from these files is labelled synthetic in the tests and in reports.

The container is deliberately faithful to the real product: EPSG:4326, uint8, NoData = 0,
the official v200 class values, 10 m pixels and a 3-degree tile naming scheme.
"""

from __future__ import annotations

import os
import pathlib

WORLDCOVER_RES_DEG = 1.0 / 12000.0  # ~10 m
WORLDCOVER_NODATA = 0

# Official ESA WorldCover v200 class values (v100 swaps 70/95 - do not reuse blindly).
CLASS_TREE = 10
CLASS_SHRUBLAND = 20
CLASS_GRASSLAND = 30
CLASS_CROPLAND = 40
CLASS_BUILTUP = 50
CLASS_BARE = 60
CLASS_WATER = 80
CLASS_WETLAND = 90


def tile_relative_path(tile_id: str, year: int = 2021, version: str = "v200") -> str:
    return f"{version}/{year}/map/ESA_WorldCover_10m_{year}_{version}_{tile_id}_Map.tif"


def write_tile(
    root: str | os.PathLike,
    array,
    tile_id: str = "N30E075",
    left: float = 75.0,
    top: float = 30.2,
    res: float = WORLDCOVER_RES_DEG,
    year: int = 2021,
    version: str = "v200",
) -> pathlib.Path:
    """Writes `array` (uint8, [row, col]) as a WorldCover-shaped COG under `root`."""
    import rasterio
    from rasterio.transform import from_origin

    rel = tile_relative_path(tile_id, year, version)
    path = pathlib.Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width = array.shape
    with rasterio.open(
        path,
        "w",
        driver="COG",
        height=height,
        width=width,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(left, top, res, res),
        nodata=WORLDCOVER_NODATA,
        compress="DEFLATE",
    ) as dst:
        dst.write(array.astype("uint8"), 1)
    return path


def uniform_grid(width: int, height: int, value: int):
    import numpy as np

    return np.full((height, width), int(value), dtype="uint8")


def split_vertical_grid(width: int, height: int, west_value: int, east_value: int, split_col: int | None = None):
    """Left/right halves - a symmetric AOI centred on the split must give ~50/50."""
    import numpy as np

    split = width // 2 if split_col is None else split_col
    grid = np.empty((height, width), dtype="uint8")
    grid[:, :split] = west_value
    grid[:, split:] = east_value
    return grid


def pixel_centre_latlon(row: int, col: int, left: float, top: float, res: float) -> tuple[float, float]:
    return (top - (row + 0.5) * res, left + (col + 0.5) * res)

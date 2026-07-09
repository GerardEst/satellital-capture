#!/usr/bin/env python3
"""satellital-dem — extract elevation data from AWS Terrain tiles.

Usage:
  python3 dem.py --coords \"lat,lon lat,lon lat,lon lat,lon\" --output dem.tif

Same coordinate format as straighten_sat.py. Output is a single-band
GeoTIFF with elevation in metres (Float32).
"""

import argparse
import math
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import numpy as np
import requests
from PIL import Image

from straighten_sat import (
    parse_coords, reproject_coords, haversine_m, optimal_zoom,
    latlon_to_tile, tile_to_latlon, detect_crs,
)

# ── DEM tile source ────────────────────────────────────────────────────

DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
DEM_MAX_ZOOM = 15
HEADERS = {
    "User-Agent": "satellital-dem/1.0 (github.com/GerardEst/satellital-capture)",
}


# ── DEM tile decoding ──────────────────────────────────────────────────

def dem_decode(pil_image: Image.Image) -> np.ndarray:
    """Convert a Terrarium RGB tile to elevation (metres).
    Terrarium encoding: height = (R * 256 + G + B / 256) - 32768
    Returns float32 array of shape (256, 256).
    """
    arr = np.array(pil_image, dtype=np.uint8).astype(np.float32)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    return r * 256.0 + g + b / 256.0 - 32768.0


def _download_tile(url: str) -> np.ndarray | None:
    """Download and decode a single DEM tile; return float32 (256,256) or None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return dem_decode(Image.open(BytesIO(resp.content)).convert("RGB"))
    except Exception:
        return None


# ── DEM pipeline ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract elevation data from satellite tiles"
    )
    parser.add_argument("--coords", required=True,
                        help='4 corners as "x,y x,y x,y x,y" clockwise')
    parser.add_argument("--output", required=True)
    parser.add_argument("--width", type=int, default=1200)
    parser.add_argument("--crs", default=None)
    args = parser.parse_args()

    coords = parse_coords(args.coords)

    if args.crs is None:
        crs = detect_crs(coords)
        print(f"  Auto-detected CRS: {crs}")
    else:
        crs = args.crs

    coords = reproject_coords(coords, crs)

    # ── Compute dimensions ──────────────────────────────────────────
    width_m = haversine_m(coords[0], coords[1])
    height_m = haversine_m(coords[1], coords[2])
    avg_lat = sum(c[0] for c in coords) / len(coords)
    out_w = args.width
    out_h = max(int(out_w * (height_m / width_m)), 1)
    z = optimal_zoom(width_m, out_w, avg_lat)
    z = min(z, DEM_MAX_ZOOM)
    # Always use max available zoom for best detail — width controls output px only
    z = DEM_MAX_ZOOM

    print(f"  Rectangle: ~{width_m:.1f}m × ~{height_m:.1f}m")
    print(f"  Output:    {out_w}×{out_h} px, zoom {z}, source AWS Terrain")
    print(f"  Pixel res: ~{width_m / out_w:.2f} m/px")

    # ── Tile range ─────────────────────────────────────────────────
    lats = [c[0] for c in coords]; lons = [c[1] for c in coords]
    x0, y0 = latlon_to_tile(max(lats), min(lons), z)
    x1, y1 = latlon_to_tile(min(lats), max(lons), z)
    if x0 > x1: x0, x1 = x1, x0
    if y0 > y1: y0, y1 = y1, y0

    nx = x1 - x0 + 1; ny = y1 - y0 + 1
    total_tiles = nx * ny
    print(f"  Tiles:     {nx}×{ny} ({total_tiles} total)")

    # ── Build URL list ─────────────────────────────────────────────
    jobs = []
    for j, ty in enumerate(range(y0, y1 + 1)):
        for i, tx in enumerate(range(x0, x1 + 1)):
            url = DEM_URL.format(z=z, x=tx, y=ty)
            jobs.append((url, i, j))

    # ── Download tiles (disk-backed memmap) ─────────────────────────
    tile_size = 256
    print(f"\n[1/3] Downloading {len(jobs)} tiles "
          f"(parallel, {min(8, len(jobs))} workers)...", flush=True)

    mosaic_dir = tempfile.TemporaryDirectory()
    mosaic_file = os.path.join(mosaic_dir.name, "dem.dat")
    mosaic = np.memmap(mosaic_file, dtype="float32", mode="w+",
                        shape=(ny * tile_size, nx * tile_size))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_download_tile, url): (i, j) for url, i, j in jobs}
        done = 0
        for f in as_completed(futures):
            i, j = futures[f]
            tile = f.result()
            if tile is not None:
                y0p, y1p = j * tile_size, (j + 1) * tile_size
                x0p, x1p = i * tile_size, (i + 1) * tile_size
                mosaic[y0p:y1p, x0p:x1p] = tile
            done += 1
            pct = int(done / len(jobs) * 100)
            print(f"\r[1/3] Downloading... {pct}%", end="", flush=True)
    print(" done.")

    # ── Perspective warp ────────────────────────────────────────────
    print(f"\n[2/3] Warping...", flush=True)

    lon_min, lat_min, _, _ = tile_to_latlon(x0, y1, z)
    _, _, lon_max, lat_max = tile_to_latlon(x1, y0, z)
    src_w = nx * tile_size; src_h = ny * tile_size

    dst_corners = [
        (0, out_h - 1), (out_w - 1, out_h - 1),
        (out_w - 1, 0), (0, 0),
    ]

    src_pixels = []
    for lat, lon in coords:
        px = (lon - lon_min) / (lon_max - lon_min) * src_w
        py = (lat_max - lat) / (lat_max - lat_min) * src_h
        src_pixels.append((px, py))

    A_rows, b_rows = [], []
    for (sx, sy), (dx, dy) in zip(src_pixels, dst_corners):
        A_rows.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy])
        A_rows.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy])
        b_rows.extend([sx, sy])

    A = np.array(A_rows, dtype=np.float64)
    b = np.array(b_rows, dtype=np.float64)
    coeffs = np.linalg.lstsq(A, b, rcond=None)[0].tolist()

    mosaic_img = Image.fromarray(mosaic, "F")
    warped = mosaic_img.transform(
        (out_w, out_h), Image.PERSPECTIVE, coeffs, Image.NEAREST
    )

    # ── Write GeoTIFF ───────────────────────────────────────────────
    print(f"\n[3/3] Writing {args.output}...", flush=True)
    with tempfile.TemporaryDirectory() as outtmp:
        raw = os.path.join(outtmp, "raw.tif")
        warped.save(raw)

        subprocess.run([
            "gdal_translate", "-q",
            "-of", "GTiff", "-co", "COMPRESS=LZW",
            "-a_srs", "EPSG:4326",
            "-a_ullr", str(lon_min), str(lat_max),
                       str(lon_max), str(lat_min),
            "-outsize", str(out_w), str(out_h),
            raw, args.output,
        ], check=True)

    size_mb = os.path.getsize(args.output) / (1024 * 1024)
    print(f"\n✓  {args.output}  ({size_mb:.1f} MB, {out_w}×{out_h} px, Float32)")

    # ── Grayscale PNG preview ───────────────────────────────────────
    png_out = args.output.rsplit(".", 1)[0] + ".png"
    subprocess.run([
        "gdal_translate", "-q",
        "-of", "PNG",
        "-ot", "Byte",
        "-scale",
        args.output, png_out,
    ], check=True)
    png_mb = os.path.getsize(png_out) / (1024 * 1024)
    print(f"✓  {png_out}  ({png_mb:.1f} MB, grayscale)")


if __name__ == "__main__":
    main()

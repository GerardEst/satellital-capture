#!/usr/bin/env python3
"""
One-shot tool: 4 rotated rectangle corners → straight satellite image.

Give it 4 coordinates in order around the rectangle, and it:
  1. Downloads map tiles (Google Satellite by default) covering the area
  2. Stitches them into a single image
  3. Warps with GCPs so the rectangle becomes axis-aligned
  4. Writes a GeoTIFF or PNG

Usage:
  python3 straighten_sat.py \\
    --coords "lat1,lon1 lat2,lon2 lat3,lon3 lat4,lon4" \\
    --output result.tif

  python3 straighten_sat.py \\
    --coords "lat1,lon1 lat2,lon2 lat3,lon3 lat4,lon4" \\
    --output result.png --zoom 20 --width 800

The 4 coordinate pairs should go in order around the rectangle
(e.g., SW, NW, NE, SE — any start corner is fine as long as they go around).
"""

import argparse
import math
import os
import subprocess
import sys
import tempfile
from io import BytesIO

import requests
from PIL import Image

# ── Tile sources ────────────────────────────────────────────────────────────

TILE_SOURCES = {
    "google": {
        "name": "Google Satellite",
        "url": "https://mt0.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
        "max_zoom": 22,
    },
    "esri": {
        "name": "ESRI World Imagery",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "max_zoom": 19,
    },
    "bing": {
        "name": "Bing Aerial",
        "url": "https://ecn.t0.tiles.virtualearth.net/tiles/a{quad}.jpeg?g=1",
        "max_zoom": 21,
        "quadkey": True,
    },
    "osm": {
        "name": "OpenStreetMap",
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "max_zoom": 19,
        "headers": {
            "User-Agent": "satellital-capture/1.0 (github.com/GerardEst/satellital-capture)",
        },
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://www.google.com/",
}


# ── Helpers ─────────────────────────────────────────────────────────────────

def parse_coords(coords_str: str) -> list[tuple[float, float]]:
    """Parse 'lat1,lon1 lat2,lon2 lat3,lon3 lat4,lon4' into pairs."""
    parts = coords_str.strip().split()
    if len(parts) != 4:
        raise ValueError(f"Need exactly 4 pairs, got {len(parts)}")
    result = []
    for p in parts:
        lat_s, lon_s = p.split(",")
        result.append((float(lat_s.strip()), float(lon_s.strip())))
    return result


def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Convert lat/lon to tile (x, y) at the given zoom level (Web Mercator)."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return (x, y)


def tile_to_latlon(tx: int, ty: int, zoom: int) -> tuple[float, float, float, float]:
    """Return (min_lon, min_lat, max_lon, max_lat) for a tile."""
    n = 2 ** zoom
    lon_min = tx / n * 360.0 - 180.0
    lon_max = (tx + 1) / n * 360.0 - 180.0
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
    return (lon_min, lat_min, lon_max, lat_max)


def haversine_m(p1: tuple, p2: tuple) -> float:
    """Haversine distance in metres between (lat, lon) pairs."""
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6371000 * 2 * math.asin(math.sqrt(a))


def quadkey(tx: int, ty: int, zoom: int) -> str:
    """Encode tile x,y,zoom into a Bing quadkey."""
    key = ""
    for i in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if tx & mask:
            digit += 1
        if ty & mask:
            digit += 2
        key += str(digit)
    return key


def run_cmd(cmd: list[str], desc: str):
    """Print and run a command, exit on failure."""
    print(f"  [{desc}] {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ERROR: {r.stderr}", file=sys.stderr)
        sys.exit(1)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Straighten satellite imagery from 4 rotated corner coordinates"
    )
    parser.add_argument(
        "--coords", required=True,
        help='4 corners as "lat,lon lat,lon lat,lon lat,lon" going around the rectangle'
    )
    parser.add_argument("--output", required=True, help="Output file (.tif or .png)")
    parser.add_argument("--zoom", type=int, default=19,
                        help="Tile zoom level (default 19)")
    parser.add_argument("--width", type=int, default=None,
                        help="Output width in pixels (auto if not set)")
    parser.add_argument("--source", choices=list(TILE_SOURCES), default="google",
                        help="Tile source (default: google)")
    args = parser.parse_args()

    coords = parse_coords(args.coords)
    source = TILE_SOURCES[args.source]
    max_z = min(args.zoom, source["max_zoom"])
    z = max_z

    # ── Compute output dimensions ───────────────────────────────────────
    width_m = max(haversine_m(coords[0], coords[3]),
                  haversine_m(coords[1], coords[2]))
    height_m = max(haversine_m(coords[0], coords[1]),
                   haversine_m(coords[2], coords[3]))
    mpp = 156543.0 / (2 ** z)  # metres per pixel at equator (rough)

    if args.width:
        out_w = args.width
        out_h = int(out_w * (height_m / width_m))
    else:
        out_w = int(width_m / mpp)
        out_h = int(height_m / mpp)

    out_w = max(out_w, 1)
    out_h = max(out_h, 1)

    print(f"  Rectangle: ~{width_m:.1f}m × ~{height_m:.1f}m")
    print(f"  Output:    {out_w}×{out_h} px, zoom {z}, source {source['name']}")
    print(f"  Pixel res: ~{width_m/out_w:.2f} m/px")

    # ── Tile range ──────────────────────────────────────────────────────
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    x0, y0 = latlon_to_tile(max(lats), min(lons), z)
    x1, y1 = latlon_to_tile(min(lats), max(lons), z)

    # x0/y0 = top-left tile, x1/y1 = bottom-right tile (Web Mercator)
    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0

    nx = x1 - x0 + 1
    ny = y1 - y0 + 1
    print(f"  Tiles:     {nx}×{ny} ({nx*ny} total) from ({x0},{y0}) to ({x1},{y1})")

    # ── Download & stitch tiles ─────────────────────────────────────────
    tile_size = 256
    mosaic = Image.new("RGB", (nx * tile_size, ny * tile_size))
    session = requests.Session()

    print(f"\n[1/3] Downloading {nx*ny} tiles...", end="", flush=True)
    for j, ty in enumerate(range(y0, y1 + 1)):
        for i, tx in enumerate(range(x0, x1 + 1)):
            if source.get("quadkey"):
                url = source["url"].format(quad=quadkey(tx, ty, z), z=z, x=tx, y=ty)
            else:
                url = source["url"].format(z=z, x=tx, y=ty)

            try:
                # Use source-specific headers if available, else default
                hdrs = source.get("headers", HEADERS)
                resp = session.get(url, headers=hdrs, timeout=15)
                resp.raise_for_status()
                tile = Image.open(BytesIO(resp.content)).convert("RGB")
                mosaic.paste(tile, (i * tile_size, j * tile_size))
            except Exception as e:
                print(f"\n  WARNING: tile ({tx},{ty}) failed: {e}")

        pct = int((j + 1) / ny * 100)
        print(f"\r[1/3] Downloading tiles... {pct}%", end="", flush=True)
    print(" done.")

    session.close()

    if sum(mosaic.getextrema()[0]) == 0:
        print("  ERROR: all tiles are black — download likely failed.", file=sys.stderr)
        sys.exit(1)

    # ── Compute mosaic geo-bounds ───────────────────────────────────────
    lon_min, lat_min, _, _ = tile_to_latlon(x0, y1, z)   # bottom-left
    _, _, lon_max, lat_max = tile_to_latlon(x1, y0, z)   # top-right
    src_w, src_h = mosaic.size
    print(f"  Geo bounds: {lon_min:.6f},{lat_min:.6f} → {lon_max:.6f},{lat_max:.6f}")

    # ── Auto-detect which corner is which (NW, NE, SE, SW) ────────────
    # Sort coords by latitude to find northern vs southern corners
    by_lat = sorted(enumerate(coords), key=lambda x: x[1][0], reverse=True)
    north = [by_lat[0], by_lat[1]]   # top 2 by latitude
    south = [by_lat[2], by_lat[3]]   # bottom 2 by latitude

    # Among northern corners, westernmost = NW, easternmost = NE
    nw_idx, nw = min(north, key=lambda x: x[1][1])
    ne_idx, ne = max(north, key=lambda x: x[1][1])

    # Among southern corners, westernmost = SW, easternmost = SE
    sw_idx, sw = min(south, key=lambda x: x[1][1])
    se_idx, se = max(south, key=lambda x: x[1][1])

    # Reorder coords to: NW, NE, SE, SW (matching output corners)
    ordered_coords = [nw, ne, se, sw]

    # ── Compute source pixel coords for each rectangle corner ──────────
    # Source image: [lon_min, lat_min] (bottom-left) to [lon_max, lat_max] (top-right)
    # Pixel mapping: px = (lon - lon_min)/(lon_max - lon_min) * src_w
    #                py = (lat_max - lat)/(lat_max - lat_min) * src_h
    src_pixels = []
    for lat, lon in ordered_coords:
        px = (lon - lon_min) / (lon_max - lon_min) * src_w
        py = (lat_max - lat) / (lat_max - lat_min) * src_h
        src_pixels.append((px, py))

    # Output corners in image space (0,0 is top-left)
    dst_corners = [
        (0,           0),            # NW
        (out_w - 1,   0),            # NE
        (out_w - 1,   out_h - 1),    # SE
        (0,           out_h - 1),    # SW
    ]

    # Compute perspective coefficients using least squares
    # We solve for the 3x3 homography matrix that maps src → dst
    # PIL.Image.PERSPECTIVE expects: [a, b, c, d, e, f, g, h] where:
    #   x' = (a*x + b*y + c) / (g*x + h*y + 1)
    #   y' = (d*x + e*y + f) / (g*x + h*y + 1)

    import numpy as np

    print(f"\n[2/3] Perspective-warping to straighten...")

    # Build the linear system for each dst→src pair
    # PIL.PERSPECTIVE maps output coords → source coords, so we solve
    # for coefficients that map dst → src (not src → dst)
    A_rows = []
    b_rows = []
    for (sx, sy), (dx, dy) in zip(src_pixels, dst_corners):
        A_rows.append([dx, dy, 1, 0,  0,  0, -sx*dx, -sx*dy])
        A_rows.append([0,  0,  0, dx, dy, 1, -sy*dx, -sy*dy])
        b_rows.extend([sx, sy])

    A = np.array(A_rows, dtype=np.float64)
    b = np.array(b_rows, dtype=np.float64)
    coeffs = np.linalg.lstsq(A, b, rcond=None)[0]
    coeffs = coeffs.tolist()  # [a, b, c, d, e, f, g, h]

    warped = mosaic.transform(
        (out_w, out_h),
        Image.PERSPECTIVE,
        coeffs,
        Image.BICUBIC,
    )

    # ── Geo-reference and save ─────────────────────────────────────────
    print(f"\n[3/3] Writing {args.output}...")
    with tempfile.TemporaryDirectory() as tmp:
        raw_tif = os.path.join(tmp, "raw.tif")
        warped.save(raw_tif)

        # Compute output geo-bounds: the 4 corners of the output image
        # correspond to the original rectangle corners in geographic space.
        # Output top-left = coords[1] (NW), bottom-right = coords[3] (SE)
        # ... but after warping, the corners ARE the rectangle corners.
        # We just need the lat/lon range of the rectangle for georeferencing.
        out_lats = [c[0] for c in coords]
        out_lons = [c[1] for c in coords]
        out_lon_min = min(out_lons)
        out_lon_max = max(out_lons)
        out_lat_min = min(out_lats)
        out_lat_max = max(out_lats)

        if args.output.endswith(".png"):
            run_cmd([
                "gdal_translate", "-of", "PNG",
                "-a_srs", "EPSG:4326",
                "-a_ullr", str(out_lon_min), str(out_lat_max),
                            str(out_lon_max), str(out_lat_min),
                "-outsize", str(out_w), str(out_h),
                raw_tif, args.output,
            ], "to PNG")
        else:
            run_cmd([
                "gdal_translate", "-of", "GTiff",
                "-co", "COMPRESS=LZW",
                "-a_srs", "EPSG:4326",
                "-a_ullr", str(out_lon_min), str(out_lat_max),
                            str(out_lon_max), str(out_lat_min),
                "-outsize", str(out_w), str(out_h),
                raw_tif, args.output,
            ], "to GeoTIFF")

    size_mb = os.path.getsize(args.output) / (1024*1024)
    print(f"\n✓  {args.output}  ({size_mb:.1f} MB, {out_w}×{out_h} px)")


if __name__ == "__main__":
    main()

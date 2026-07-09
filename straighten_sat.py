#!/usr/bin/env python3
"""
One-shot tool: 4 rotated rectangle corners → straight satellite image.

Give it 4 coordinates in order around the rectangle, and it:
  1. Downloads map tiles (Google Satellite by default) covering the area
  2. Stitches them into a single image
  3. Warps with GCPs so the rectangle becomes axis-aligned
  4. Writes a GeoTIFF or PNG

Usage:
  python3 straighten_sat.py \
    --coords "lat1,lon1 lat2,lon2 lat3,lon3 lat4,lon4" \
    --output result.tif

  python3 straighten_sat.py \
    --coords "lat1,lon1 lat2,lon2 lat3,lon3 lat4,lon4" \
    --output result.png --width 800

The 4 coordinate pairs should go in order around the rectangle
(e.g., SW, NW, NE, SE — any start corner is fine as long as they go around).
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
from osgeo import osr
osr.UseExceptions()  # suppress GDAL 4.0 FutureWarning

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


def reproject_coords(coords: list[tuple[float, float]], src_crs: str) -> list[tuple[float, float]]:
    """Reproject coordinates from src_crs to EPSG:4326 via osgeo.osr.

    coords are (x, y) pairs in the source CRS axis order.
    Returns (lat, lon) in EPSG:4326.
    """
    if src_crs.upper() in ("EPSG:4326", "WGS84", "WGS 84"):
        return coords  # already (lat, lon)

    src = osr.SpatialReference()
    if src.SetFromUserInput(src_crs) != 0:
        print(f"  ERROR: unknown CRS '{src_crs}'", file=sys.stderr)
        sys.exit(1)

    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    transform = osr.CoordinateTransformation(src, dst)

    out = []
    for x, y in coords:
        lon, lat, _ = transform.TransformPoint(x, y)
        out.append((lat, lon))
    return out


def detect_crs(coords: list[tuple[float, float]]) -> str:
    """Auto-detect CRS from coordinate value ranges."""
    x_vals = [c[0] for c in coords]
    y_vals = [c[1] for c in coords]

    if all(-90 <= x <= 90 for x in x_vals) and \
       all(-180 <= y <= 180 for y in y_vals):
        return "EPSG:4326"

    print("ERROR: Could not auto-detect coordinate system.", file=sys.stderr)
    print("", file=sys.stderr)
    print("Coordinates don't look like WGS 84 lat/lon.", file=sys.stderr)
    print("Pass --crs to specify the input coordinate system:", file=sys.stderr)
    print("  --crs EPSG:3857    Web Mercator", file=sys.stderr)
    print("  --crs EPSG:32631   UTM zone 31N (adjust zone number)", file=sys.stderr)
    print("  --crs EPSG:25831   ETRS89 / UTM 31N (Spain, Catalonia)", file=sys.stderr)
    sys.exit(1)


def haversine_m(p1: tuple, p2: tuple) -> float:
    """Haversine distance in metres between (lat, lon) pairs."""
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = min(1.0, max(0.0,
        math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2))
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


def _download_tile(url: str, headers: dict) -> Image.Image | None:
    """Download a single tile, return PIL Image or None on failure."""
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception:
        return None


def optimal_zoom(width_m: float, out_w: int, avg_lat: float) -> int:
    """Compute zoom level with oversampling for better quality.

    Multiplies target resolution by 2× so tiles are downloaded at
    higher detail than output, then downscaled for cleaner results.
    """
    target_mpp = width_m / out_w
    equator_mpp = 156543.0 * math.cos(math.radians(avg_lat)) * 2  # 2× oversample
    if target_mpp <= 0:
        return 22
    z = int(math.log2(equator_mpp / target_mpp))
    return max(1, min(z, 22))


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
        help='4 corners as "x,y x,y x,y x,y" going around the rectangle. '
             'For EPSG:4326: lat,lon. For UTM: easting,northing.'
    )
    parser.add_argument("--output", required=True, help="Output file (.tif or .png)")
    parser.add_argument("--zoom", type=int, default=None,
                        help="Tile zoom level (auto-computed from area and width)")
    parser.add_argument("--width", type=int, default=None,
                        help="Output width in pixels (default 1200 if zoom is auto)")
    parser.add_argument("--source", choices=list(TILE_SOURCES), default="esri",
                        help="Tile source (default: esri)")
    parser.add_argument("--crs", default=None,
                        help="Input CRS (auto-detected if omitted). "
                             "Examples: EPSG:32631 (UTM 31N), EPSG:3857, EPSG:25831")
    args = parser.parse_args()

    coords = parse_coords(args.coords)

    # Auto-detect CRS or use explicit flag
    if args.crs is None:
        crs = detect_crs(coords)
        print(f"  Auto-detected CRS: {crs}")
    else:
        crs = args.crs

    # Reproject to WGS 84 if needed
    coords = reproject_coords(coords, crs)
    source = TILE_SOURCES[args.source]

    # ── Compute output dimensions and zoom ────────────────────────────────
    width_m = haversine_m(coords[0], coords[1])
    height_m = haversine_m(coords[1], coords[2])
    avg_lat = sum(c[0] for c in coords) / len(coords)

    if args.zoom is not None:
        z = min(args.zoom, source["max_zoom"])
        mpp = 156543.0 * math.cos(math.radians(avg_lat)) / (2 ** z)
        if args.width:
            out_w = args.width
            out_h = int(out_w * (height_m / width_m))
        else:
            out_w = int(width_m / mpp)
            out_h = int(height_m / mpp)
    else:
        # Auto zoom: match tile resolution to output resolution
        out_w = args.width if args.width else 1200
        out_h = int(out_w * (height_m / width_m))
        z = optimal_zoom(width_m, out_w, avg_lat)
        z = min(z, source["max_zoom"])

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

    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0

    nx = x1 - x0 + 1
    ny = y1 - y0 + 1
    total_tiles = nx * ny
    print(f"  Tiles:     {nx}×{ny} ({total_tiles} total) from ({x0},{y0}) to ({x1},{y1})")

    MAX_TILES = 1000
    if total_tiles > MAX_TILES and args.zoom is None:
        print(f"  ERROR: {total_tiles} tiles exceeds limit of {MAX_TILES}. "
              f"Reduce the area or specify --width manually.", file=sys.stderr)
        sys.exit(1)
    if total_tiles > MAX_TILES:
        print(f"  Warning: {total_tiles} tiles may take a while (--zoom {z} is explicit)")

    # ── Download & stitch tiles ─────────────────────────────────────────
    tile_size = 256
    mosaic = Image.new("RGB", (nx * tile_size, ny * tile_size))

    # Build list of (url, x, y) for all tiles
    jobs = []
    for j, ty in enumerate(range(y0, y1 + 1)):
        for i, tx in enumerate(range(x0, x1 + 1)):
            if source.get("quadkey"):
                url = source["url"].format(quad=quadkey(tx, ty, z), z=z, x=tx, y=ty)
            else:
                url = source["url"].format(z=z, x=tx, y=ty)
            jobs.append((url, i, j))

    hdrs = source.get("headers", HEADERS)
    print(f"\n[1/3] Downloading {len(jobs)} tiles "
          f"(parallel, {min(8, len(jobs))} workers)...", flush=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_download_tile, url, hdrs): (i, j)
                   for url, i, j in jobs}
        done = 0
        for f in as_completed(futures):
            i, j = futures[f]
            tile = f.result()
            if tile is not None:
                mosaic.paste(tile, (i * tile_size, j * tile_size))
            else:
                print(f"\n  WARNING: tile at ({i},{j}) failed")
            done += 1
            pct = int(done / len(jobs) * 100)
            print(f"\r[1/3] Downloading... {pct}%", end="", flush=True)

    print(" done.")

    if sum(mosaic.getextrema()[0]) == 0:
        print("  ERROR: all tiles are black — download likely failed.", file=sys.stderr)
        sys.exit(1)

    # ── Compute mosaic geo-bounds ───────────────────────────────────────
    lon_min, lat_min, _, _ = tile_to_latlon(x0, y1, z)
    _, _, lon_max, lat_max = tile_to_latlon(x1, y0, z)
    src_w, src_h = mosaic.size
    print(f"  Geo bounds: {lon_min:.6f},{lat_min:.6f} → {lon_max:.6f},{lat_max:.6f}")

    # ── Output corners in image space ────────────────────────────────────
    dst_corners = [
        (0,           out_h - 1),
        (out_w - 1,   out_h - 1),
        (out_w - 1,   0),
        (0,           0),
    ]

    src_pixels = []
    for lat, lon in coords:
        px = (lon - lon_min) / (lon_max - lon_min) * src_w
        py = (lat_max - lat) / (lat_max - lat_min) * src_h
        src_pixels.append((px, py))

    import numpy as np

    print(f"\n[2/3] Perspective-warping to straighten...")

    A_rows = []
    b_rows = []
    for (sx, sy), (dx, dy) in zip(src_pixels, dst_corners):
        A_rows.append([dx, dy, 1, 0,  0,  0, -sx*dx, -sx*dy])
        A_rows.append([0,  0,  0, dx, dy, 1, -sy*dx, -sy*dy])
        b_rows.extend([sx, sy])

    A = np.array(A_rows, dtype=np.float64)
    b = np.array(b_rows, dtype=np.float64)
    coeffs = np.linalg.lstsq(A, b, rcond=None)[0]
    coeffs = coeffs.tolist()

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

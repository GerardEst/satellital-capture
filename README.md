# satellital-capture

One-shot tool: give it 4 rotated rectangle corner coordinates, get back a straight satellite image.

Downloads tiles (Google Satellite by default), stitches them, and affine-warps using the 4 corners as Ground Control Points so the rectangle's edges become the image axes.

## Usage

```bash
python3 straighten_sat.py \
  --coords "lat1,lon1 lat2,lon2 lat3,lon3 lat4,lon4" \
  --output result.tif
```

The 4 coordinate pairs go in order around the rectangle (e.g., SW → NW → NE → SE).

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--coords` | *(required)* | 4 pairs: `"lat,lon lat,lon lat,lon lat,lon"` |
| `--output` | *(required)* | `.tif` for GeoTIFF or `.png` |
| `--zoom` | `19` | Tile detail (higher = sharper, max 22 for Google) |
| `--width` | auto | Output width in px (height auto-calculated) |
| `--source` | `google` | `google`, `esri`, `bing`, or `osm` |
| `--crs` | `EPSG:4326` | Input CRS (anything `gdaltransform` accepts: `EPSG:32631`, `EPSG:3857`, etc.) |

## Requirements

```bash
sudo apt-get install gdal-bin python3-gdal python3-pil python3-requests python3-numpy
```

## Example

```bash
# WGS 84 (default) — coordinates from Google Maps etc.
python3 straighten_sat.py \
  --coords "51.506,-0.128 51.507,-0.128 51.507,-0.126 51.506,-0.126" \
  --output whitehall.tif \
  --zoom 19 --width 800

# UTM zone 31N — coordinates from a drone flight plan or GPS field survey
python3 straighten_sat.py \
  --coords "431200,4582345 431400,4582345 431400,4582145 431200,4582145" \
  --output field.tif \
  --crs EPSG:32631

# Web Mercator — coordinates from a tile server or web map export
python3 straighten_sat.py \
  --coords "242676,5070038 243790,5070038 243790,5068554 242676,5068554" \
  --output area.png \
  --crs EPSG:3857

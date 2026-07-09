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

## Requirements

```bash
sudo apt-get install gdal-bin python3-pil python3-requests
```

## Example

```bash
python3 straighten_sat.py \
  --coords "51.506,-0.128 51.507,-0.128 51.507,-0.126 51.506,-0.126" \
  --output whitehall.tif \
  --zoom 19 --width 800
```

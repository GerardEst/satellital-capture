"""Tests for satellital-capture: CRS reprojection and end-to-end pipeline."""

import os
import subprocess
import tempfile

import pytest

# Import the module-under-test (repo root on sys.path at test time)
import straighten_sat


# ── Unit: reproject_coords ────────────────────────────────────────────────

def test_wgs84_passthrough():
    """EPSG:4326 input should be returned unchanged."""
    coords = [(41.0, 2.0), (41.0, 2.001), (41.001, 2.001), (41.001, 2.0)]
    result = straighten_sat.reproject_coords(coords, "EPSG:4326")
    assert result == coords

    # Also accept alias "WGS84"
    result = straighten_sat.reproject_coords(coords, "WGS84")
    assert result == coords


def test_utm31n_to_wgs84():
    """UTM zone 31N coords should map to ~41.39°N, ~2.18°E."""
    utm = [(431200.0, 4582345.0), (431400.0, 4582345.0),
           (431400.0, 4582145.0), (431200.0, 4582145.0)]
    result = straighten_sat.reproject_coords(utm, "EPSG:32631")
    assert len(result) == 4
    for lat, lon in result:
        assert 41.3 < lat < 41.5, f"lat {lat} out of range"
        assert 2.1 < lon < 2.3, f"lon {lon} out of range"


def test_web_mercator_to_wgs84():
    """EPSG:3857 → WGS 84: small area near 41.39°N, 2.18°E."""
    # Web Mercator coords for ~41.39°N, ~2.18°E
    merc = [
        (242676.0, 5070038.0),
        (243790.0, 5070038.0),
        (243790.0, 5068554.0),
        (242676.0, 5068554.0),
    ]
    result = straighten_sat.reproject_coords(merc, "EPSG:3857")
    assert len(result) == 4
    for lat, lon in result:
        assert 41.3 < lat < 41.5, f"lat {lat} out of range"
        assert 2.1 < lon < 2.3, f"lon {lon} out of range"


def test_bad_crs_exits():
    """Unknown CRS should raise SystemExit."""
    coords = [(41.0, 2.0)] * 4
    with pytest.raises(SystemExit):
        straighten_sat.reproject_coords(coords, "EPSG:99999")


# ── Unit: parse_coords ─────────────────────────────────────────────────────

def test_parse_coords_valid():
    result = straighten_sat.parse_coords("41.0,2.0 41.1,2.1 41.2,2.2 41.3,2.3")
    assert result == [(41.0, 2.0), (41.1, 2.1), (41.2, 2.2), (41.3, 2.3)]


def test_parse_coords_wrong_count():
    with pytest.raises(ValueError, match="exactly 4 pairs"):
        straighten_sat.parse_coords("41.0,2.0 41.1,2.1")


# ── Integration: full CLI pipeline ─────────────────────────────────────────


@pytest.mark.integration
def test_full_pipeline_wgs84():
    """End-to-end: WGS 84 coords → PNG."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        out = f.name
    try:
        r = subprocess.run(
            [
                "python3",
                os.path.join(os.path.dirname(__file__), "..", "straighten_sat.py"),
                "--coords", "41.39,2.17 41.39,2.18 41.38,2.18 41.38,2.17",
                "--output", out,
                "--zoom", "16",
                "--width", "200",
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, f"CLI failed: {r.stderr}"
        assert os.path.getsize(out) > 0, "Output file empty"
    finally:
        os.unlink(out)


@pytest.mark.integration
def test_full_pipeline_utm():
    """End-to-end: UTM 31N → PNG (exercises --crs)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        out = f.name
    try:
        r = subprocess.run(
            [
                "python3",
                os.path.join(os.path.dirname(__file__), "..", "straighten_sat.py"),
                "--coords", "431200,4582345 431400,4582345 431400,4582145 431200,4582145",
                "--crs", "EPSG:32631",
                "--output", out,
                "--zoom", "16",
                "--width", "200",
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, f"CLI failed: {r.stderr}"
        assert os.path.getsize(out) > 0, "Output file empty"
    finally:
        os.unlink(out)


@pytest.mark.integration
def test_cli_help_shows_crs():
    """--crs should appear in --help output."""
    r = subprocess.run(
        [
            "python3",
            os.path.join(os.path.dirname(__file__), "..", "straighten_sat.py"),
            "--help",
        ],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "--crs" in r.stdout

#!/usr/bin/env python3
"""Minimal HTTP server for the satellital-capture web UI.

Serves ui/index.html and handles POST /capture to run straighten_sat.py.
"""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse

import straighten_sat

HOST = "0.0.0.0"
PORT = 8080
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(SCRIPT_DIR, "ui")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Quiet logging
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == "":
            path = "/index.html"
        filepath = os.path.join(UI_DIR, path.lstrip("/"))
        if os.path.isfile(filepath) and filepath.startswith(UI_DIR):
            self._serve_file(filepath)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/bounds":
            self._handle_bounds()
        elif self.path == "/capture":
            self._handle_capture()
        else:
            self.send_error(404)

    def _handle_bounds(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        coords = data.get("coords", "")
        crs = data.get("crs")
        out_w = data.get("width", 1200)
        if not coords:
            self.send_error(400, "Missing coords")
            return

        parsed = straighten_sat.parse_coords(coords)
        if crs:
            parsed = straighten_sat.reproject_coords(parsed, crs)

        lats = [p[0] for p in parsed]
        lons = [p[1] for p in parsed]
        width_m = straighten_sat.haversine_m(parsed[0], parsed[1])
        height_m = straighten_sat.haversine_m(parsed[1], parsed[2])
        avg_lat = sum(lats) / len(lats)
        zoom = straighten_sat.optimal_zoom(width_m, out_w, avg_lat)
        out_h = int(out_w * (height_m / width_m)) if width_m > 0 else 0
        result = {
            "south": min(lats), "north": max(lats),
            "west": min(lons), "east": max(lons),
            "corners": [[p[0], p[1]] for p in parsed],
            "zoom": zoom,
            "width_m": round(width_m, 1),
            "height_m": round(height_m, 1),
            "height_px": max(out_h, 1),
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def _handle_capture(self):

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        coords = data.get("coords", "")
        width = str(data.get("width", 1200))
        source = data.get("source", "google")
        filename = data.get("filename", "capture.tif")
        crs = data.get("crs")  # None = auto-detect

        if not coords:
            self.send_error(400, "Missing coords")
            return

        with tempfile.TemporaryDirectory() as tmp:
            outfile = os.path.join(tmp, filename)
            cmd = [
                sys.executable,
                os.path.join(SCRIPT_DIR, "straighten_sat.py"),
                "--coords", coords,
                "--width", width,
                "--source", source,
                "--output", outfile,
            ]
            if crs:
                cmd.extend(["--crs", crs])
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            if r.returncode != 0:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(r.stderr.encode() or r.stdout.encode())
                return

            if not os.path.exists(outfile):
                self.send_error(500, "Output file not created")
                return

            with open(outfile, "rb") as f:
                data = f.read()

        self.send_response(200)
        ct = "image/tiff" if outfile.endswith(".tif") else "image/png"
        self.send_header("Content-Type", ct)
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, path):
        ct = "text/html"
        if path.endswith(".css"):
            ct = "text/css"
        elif path.endswith(".js"):
            ct = "application/javascript"
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    os.chdir(SCRIPT_DIR)
    server = http.server.HTTPServer((HOST, PORT), Handler)
    print(f"  UI: http://{HOST}:{PORT}")
    print("  Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()

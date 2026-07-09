#!/usr/bin/env python3
"""HTTP server for the satellital-capture web UI with job queue.

POST /capture    → enqueue job, returns {job_id}
GET  /job/<id>   → returns {status, progress, filename, error}
GET  /download/<id> → returns the TIFF/PNG file
GET  /           → serves ui/index.html
"""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import straighten_sat

HOST = "0.0.0.0"
PORT = 8080
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(SCRIPT_DIR, "ui")

# Job queue — max 2 concurrent captures
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2)

# Output cache — keep files for 1 hour
_file_ttl = 3600


def _cleanup_old_files():
    """Remove cached output files older than _file_ttl."""
    now = time.time()
    with _jobs_lock:
        expired = [
            jid for jid, j in _jobs.items()
            if j["status"] in ("done", "error") and now - j.get("_finished", 0) > _file_ttl
        ]
        for jid in expired:
            path = _jobs[jid].get("_file")
            if path and os.path.exists(path):
                os.unlink(path)
            del _jobs[jid]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/job/"):
            self._handle_job_status(path.split("/job/")[1])
        elif path.startswith("/download/"):
            self._handle_download(path.split("/download/")[1])
        elif path in ("/", ""):
            self._serve_file(os.path.join(UI_DIR, "index.html"))
        else:
            filepath = os.path.join(UI_DIR, path.lstrip("/"))
            if os.path.isfile(filepath) and filepath.startswith(UI_DIR):
                self._serve_file(filepath)
            else:
                self.send_error(404)

    def do_POST(self):
        try:
            if self.path == "/bounds":
                self._handle_bounds()
            elif self.path == "/capture":
                self._handle_capture()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_error(500, str(e))
            except Exception:
                pass

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
        source = data.get("source", "esri")
        filename = data.get("filename", "capture.tif")
        crs = data.get("crs")

        if not coords:
            self.send_error(400, "Missing coords")
            return

        job_id = uuid.uuid4().hex[:12]
        with _jobs_lock:
            _jobs[job_id] = {"status": "queued", "progress": 0, "filename": filename}
            _cleanup_old_files()

        _executor.submit(_process_job, job_id, coords, width, source, filename, crs)

        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"job_id": job_id}).encode())

    def _handle_job_status(self, job_id: str):
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job:
            self.send_error(404, "Job not found")
            return

        resp = {
            "status": job["status"],
            "progress": job.get("progress", 0),
            "filename": job.get("filename", ""),
        }
        if job["status"] == "error":
            resp["error"] = job.get("error", "Unknown error")
        elif job["status"] == "done":
            resp["filename"] = job.get("filename", "")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode())

    def _handle_download(self, job_id: str):
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job or job["status"] != "done":
            self.send_error(404, "File not available")
            return

        path = job.get("_file")
        if not path or not os.path.exists(path):
            self.send_error(404, "File expired or missing")
            return

        filename = job.get("filename", "capture.tif")
        ct = "image/tiff" if filename.endswith(".tif") else "image/png"
        with open(path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _process_job(job_id, coords, width, source, filename, crs):
    """Run the capture in a background thread."""
    with _jobs_lock:
        _jobs[job_id]["status"] = "processing"
        _jobs[job_id]["progress"] = 5

    tmpdir = None
    outfile = None
    try:
        tmpdir = tempfile.mkdtemp()
        outfile = os.path.join(tmpdir, filename)
        cmd = [
            sys.executable, "-u",
            os.path.join(SCRIPT_DIR, "straighten_sat.py"),
            "--coords", coords,
            "--width", width,
            "--source", source,
            "--output", outfile,
        ]
        if crs:
            cmd.extend(["--crs", crs])

        # Run with unbuffered output for progress, 300s timeout
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if r.returncode != 0:
            raise RuntimeError(r.stderr or r.stdout or "Capture failed")

        if not os.path.exists(outfile):
            raise RuntimeError("Output file not created")

        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["progress"] = 100
            _jobs[job_id]["_file"] = outfile
            _jobs[job_id]["_finished"] = time.time()

    except subprocess.TimeoutExpired:
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = "Capture timed out — reduce area or width"
            _jobs[job_id]["_finished"] = time.time()
        if outfile and os.path.exists(outfile):
            os.unlink(outfile)
        if tmpdir:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)
            _jobs[job_id]["_finished"] = time.time()
        if outfile and os.path.exists(outfile):
            os.unlink(outfile)
        if tmpdir:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    os.chdir(SCRIPT_DIR)
    server = http.server.HTTPServer((HOST, PORT), Handler)
    print(f"  UI: http://{HOST}:{PORT}")
    print("  Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        _executor.shutdown(wait=False)
        server.shutdown()


if __name__ == "__main__":
    main()

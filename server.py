#!/usr/bin/env python3
"""HTTP server for satellital-capture with job queue.

POST /capture  → returns {job_id}
GET  /job/<id> → returns {status, progress, error}
GET  /download/<id> → returns the file
"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, Future

import straighten_sat

HOST = "0.0.0.0"
PORT = 8080
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(SCRIPT_DIR, "ui")

_jobs: dict = {}
_jobs_lock = threading.Lock()

# Run captures in background — 2 max concurrent
_executor = ThreadPoolExecutor(max_workers=2)


def _cleanup_expired():
    now = time.time()
    with _jobs_lock:
        expired = [
            jid for jid, j in _jobs.items()
            if j["status"] in ("done", "error")
            and now - j.get("_finished", 0) > 3600
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
            self._job_status(path.split("/job/", 1)[1])
        elif path.startswith("/download/"):
            self._download(path.split("/download/", 1)[1])
        elif path in ("/", ""):
            self._serve(os.path.join(UI_DIR, "index.html"))
        else:
            fp = os.path.join(UI_DIR, path.lstrip("/"))
            if os.path.isfile(fp) and fp.startswith(UI_DIR):
                self._serve(fp)
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
        data = json.loads(body)
        coords = data.get("coords", "")
        crs = data.get("crs")
        out_w = data.get("width", 1200)
        if not coords:
            self.send_error(400, "Missing coords"); return

        parsed = straighten_sat.parse_coords(coords)
        if crs:
            parsed = straighten_sat.reproject_coords(parsed, crs)

        lats = [p[0] for p in parsed]; lons = [p[1] for p in parsed]
        w = straighten_sat.haversine_m(parsed[0], parsed[1])
        h = straighten_sat.haversine_m(parsed[1], parsed[2])
        zoom = straighten_sat.optimal_zoom(w, out_w, sum(lats)/len(lats))
        oh = int(out_w * (h / w)) if w > 0 else 0
        self._json(200, {
            "south": min(lats), "north": max(lats),
            "west": min(lons), "east": max(lons),
            "corners": [[p[0], p[1]] for p in parsed],
            "zoom": zoom, "width_m": round(w, 1),
            "height_m": round(h, 1), "height_px": max(oh, 1),
        })

    def _handle_capture(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        data = json.loads(body)

        coords = data.get("coords", "")
        width = data.get("width", 1200)
        source = data.get("source", "esri")
        filename = data.get("filename", "capture.tif")
        crs = data.get("crs")

        if not coords:
            self.send_error(400, "Missing coords"); return

        jid = uuid.uuid4().hex[:12]
        with _jobs_lock:
            _jobs[jid] = {"status": "queued", "progress": 0, "filename": filename}
            _cleanup_expired()

        _executor.submit(_run_capture, jid, coords, width, source, filename, crs)
        self._json(202, {"job_id": jid})

    def _job_status(self, jid):
        with _jobs_lock:
            j = _jobs.get(jid)
        if not j:
            self.send_error(404); return
        self._json(200, {
            "status": j["status"], "progress": j.get("progress", 0),
            "filename": j.get("filename", ""),
            "error": j.get("error", "") if j["status"] == "error" else "",
        })

    def _download(self, jid):
        with _jobs_lock:
            j = _jobs.get(jid)
        if not j or j["status"] != "done":
            self.send_error(404); return
        path = j.get("_file")
        if not path or not os.path.exists(path):
            self.send_error(404); return
        ct = "image/tiff" if path.endswith(".tif") else "image/png"
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Disposition", f"attachment; filename=\"{j['filename']}\"")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _serve(self, path):
        ct = "text/html"
        if path.endswith(".css"): ct = "text/css"
        elif path.endswith(".js"): ct = "application/javascript"
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _run_capture(jid, coords, width, source, filename, crs):
    with _jobs_lock:
        _jobs[jid]["status"] = "processing"
        _jobs[jid]["progress"] = 5

    tmpdir = tempfile.mkdtemp()
    outfile = os.path.join(tmpdir, filename)
    try:
        cmd = [
            sys.executable, "-u",
            os.path.join(SCRIPT_DIR, "straighten_sat.py"),
            "--coords", coords, "--width", str(width),
            "--source", source, "--output", outfile,
        ]
        if crs:
            cmd.extend(["--crs", crs])

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if r.returncode != 0:
            raise RuntimeError(r.stderr or r.stdout or "Capture failed")

        if not os.path.exists(outfile):
            raise RuntimeError("Output file not created")

        with _jobs_lock:
            _jobs[jid]["status"] = "done"
            _jobs[jid]["progress"] = 100
            _jobs[jid]["_file"] = outfile
            _jobs[jid]["_finished"] = time.time()

    except subprocess.TimeoutExpired:
        with _jobs_lock:
            _jobs[jid]["status"] = "error"
            _jobs[jid]["error"] = "Capture timed out — reduce area or width"
            _jobs[jid]["_finished"] = time.time()
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:
        with _jobs_lock:
            _jobs[jid]["status"] = "error"
            _jobs[jid]["error"] = str(e)
            _jobs[jid]["_finished"] = time.time()
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    os.chdir(SCRIPT_DIR)
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"  UI: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        _executor.shutdown(wait=False)
        server.shutdown()


if __name__ == "__main__":
    main()

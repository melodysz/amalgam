"""AMALGAM kiosk server.

Serves the shop like `python3 -m http.server`, plus a tiny archive so every
customer who checks out on the exhibition computer stays "in stock":

  POST /api/inventory   save one checkout's warped selfies as image files
  GET  /api/inventory   list everyone saved so far (newest first)

Files land in inventory/ next to this script (kept out of git). Only the
warped products are saved: never the original photo, never the email.

Run:  python3 kiosk_server.py          then open http://localhost:8400
"""

import base64
import json
import re
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INV = ROOT / "inventory"
INDEX = INV / "inventory.json"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8400
MAX_BODY = 8 * 1024 * 1024  # three ~100 KB JPEGs fit with lots of room
lock = threading.Lock()


def read_index():
    try:
        return json.loads(INDEX.read_text())
    except (FileNotFoundError, ValueError):
        return []


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:30] or "customer"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/inventory":
            with lock:
                entries = read_index()
            return self.send_json(200, list(reversed(entries)))
        if self.headers.get("Range"):
            return self.send_range()
        return super().do_GET()

    def send_range(self):
        # Partial file requests, so the browser can jump around inside a song (the music "skip" glitch needs this)
        path = Path(self.translate_path(self.path))
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers["Range"].strip())
        if not path.is_file() or not m or m.groups() == ("", ""):
            return super().do_GET()
        size = path.stat().st_size
        first, last = m.groups()
        if first == "":  # "the last N bytes"
            start, end = max(0, size - int(last)), size - 1
        else:
            start, end = int(first), min(int(last) if last else size - 1, size - 1)
        if start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser often cancels a song request partway through; that's normal

    def do_POST(self):
        if self.path.split("?")[0] != "/api/inventory":
            return self.send_json(404, {"error": "not found"})
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= MAX_BODY:
            return self.send_json(413, {"error": "too big"})
        try:
            data = json.loads(self.rfile.read(size))
            name = str(data.get("name") or "a customer")[:60]
            pick = str(data.get("pick") or "")
            variants = data.get("variants") or {}
            assert isinstance(variants, dict) and variants
        except Exception:
            return self.send_json(400, {"error": "bad request"})

        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        entry_id = f"{stamp}_{slug(name)}"
        files = {}
        INV.mkdir(exist_ok=True)
        for kind, url in variants.items():
            m = re.fullmatch(r"data:image/(jpeg|png);base64,(.+)", str(url), re.S)
            kind = re.sub(r"[^a-z]", "", str(kind).lower())[:12]
            if not m or not kind:
                continue
            ext = "jpg" if m.group(1) == "jpeg" else "png"
            path = INV / f"{entry_id}_{kind}.{ext}"
            path.write_bytes(base64.b64decode(m.group(2)))
            files[kind] = f"inventory/{path.name}"
        if not files:
            return self.send_json(400, {"error": "no images"})

        entry = {"id": entry_id, "name": name, "pick": pick if pick in files else next(iter(files)),
                 "ts": int(time.time() * 1000), "files": files}
        with lock:
            entries = read_index()
            entries.append(entry)
            INDEX.write_text(json.dumps(entries, indent=1))
        print(f"  saved {name!r} → {len(files)} images in inventory/")
        return self.send_json(200, {"id": entry_id})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # this computer only
    print(f"AMALGAM kiosk on http://localhost:{PORT}  (saving customers to {INV})")
    server.serve_forever()

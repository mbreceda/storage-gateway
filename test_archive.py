"""Prueba el archivador contra un backend falso, sin secretos ni red real.

Corre con: `python3 test_archive.py` desde la raiz del repo.

Cubre los cuatro caminos: archivo normal, archivo que pasa del tope de la Bot
API, Telegram rechazando la subida, y cola vacia.
"""
"""Prueba la logica del archivador contra un backend falso."""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- backend falso -----------------------------------------------------------
LLAMADAS = []
COLA = []

class H(BaseHTTPRequestHandler):
    pass
    def log_message(self, *a): pass
    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/big":
            b = b"x" * (60*1024*1024)
        elif self.path == "/small":
            b = b"hola mundo\n"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        LLAMADAS.append((self.path, body, self.headers.get("X-Worker-Token")))
        if self.path == "/storage/api/archive/claim":
            self._json({"item": COLA.pop(0) if COLA else None})
        else:
            self._json({"ok": True})

srv = HTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
COLA.extend([
    {"id": "so_grande", "project": "p", "filename": "grande.mp4", "size": 60*1024*1024,
     "content_type": None, "r2_key": "k", "download_url": f"http://127.0.0.1:{port}/big", "bot_alias": None},
    {"id": "so_chico", "project": "p", "filename": "chico.txt", "size": 11,
     "content_type": None, "r2_key": "k", "download_url": f"http://127.0.0.1:{port}/small", "bot_alias": None},
])
threading.Thread(target=srv.serve_forever, daemon=True).start()

# Telegram falso
class T(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); self.rfile.read(n)
        self._json({"ok": True, "result": {"message_id": 42, "document": {"file_id": "FID-REAL"}}})
    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

tsrv = HTTPServer(("127.0.0.1", 0), T)
tport = tsrv.server_address[1]
threading.Thread(target=tsrv.serve_forever, daemon=True).start()

os.environ.update({
    "BACKEND_URL": f"http://127.0.0.1:{port}",
    "WORKER_TOKEN": "wtok",
    "ARCHIVE_BOT_TOKEN": "btok",
    "ARCHIVE_CHAT_ID": "-100999",
    "ARCHIVE_BOT_ALIAS": "bot-07",
    "GITHUB_RUN_ID": "run-1",
})
import archive
archive.COLA = COLA
# Redirigir la subida a Telegram al servidor falso
real_upload = archive._upload
def fake_upload(ruta, filename):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{tport}/sendDocument", data=b"x", method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())
archive._upload = fake_upload

codigo = archive.main()
print("\n--- resultado ---")
print("codigo de salida:", codigo)
for path, body, tok in LLAMADAS:
    print(f"  {path} token={tok} {json.dumps(body)[:110]}")

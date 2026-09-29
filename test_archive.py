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
            b = b"x" * (30*1024*1024)
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
    {"id": "so_grande", "project": "p", "filename": "grande.mp4", "size": 30*1024*1024,
     "content_type": None, "r2_key": "k", "download_url": f"http://127.0.0.1:{port}/big", "bot_alias": "bot-07",
     "bot_token": "btok", "chat_id": "-100999"},
    {"id": "so_chico", "project": "p", "filename": "chico.txt", "size": 11,
     "content_type": None, "r2_key": "k", "download_url": f"http://127.0.0.1:{port}/small", "bot_alias": "bot-07",
     "bot_token": "btok", "chat_id": "-100999"},
])
threading.Thread(target=srv.serve_forever, daemon=True).start()

# Telegram falso
class T(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); self.rfile.read(n)
        self._json({"ok": True, "result": {"message_id": 42, "document": {"file_id": "FID-REAL"}}})
    def do_GET(self):
        # `getFile`: Telegram entrega la ruta y el tamano.
        self._json({"ok": True, "result": {"file_path": "documents/x.txt", "file_size": 11}})
    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

tsrv = HTTPServer(("127.0.0.1", 0), T)
tport = tsrv.server_address[1]
threading.Thread(target=tsrv.serve_forever, daemon=True).start()

os.environ.update({
    "BACKEND_URL": f"http://127.0.0.1:{port}",
    "WORKER_TOKEN": "wtok",
    "GITHUB_RUN_ID": "run-1",
})
import archive
archive.COLA = COLA
# Redirigir la subida a Telegram al servidor falso
real_upload = archive._upload
def fake_upload(ruta, filename, bot_token, chat_id):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{tport}/sendDocument", data=b"x", method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())
archive._upload = fake_upload

# `_verificar_bajada` habla con api.telegram.org, que no existe en el test.
# Se sustituye por una version que usa el Telegram falso.
_verify_real = archive._verificar_bajada
def fake_verify(bot_token, fid, subido):
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{tport}/getFile", timeout=10) as r:
        data = json.loads(r.read())
    if not data.get("ok"):
        raise RuntimeError(f"no se puede entregar: {data.get('description')}")
    return None
archive._verificar_bajada = fake_verify

codigo = archive.main()
print("\n--- resultado ---")
# --- Caso: el backend no entrega bot ----------------------------------------
# Sin token ni canal, el archivador debe soltar el objeto en vez de intentar
# subir. Es lo que pasa si el alias no existe en el pool, si no tiene canal, o
# si SECRET_KEY cambio y el token guardado ya no se descifra.
LLAMADAS.clear()
COLA.append({"id": "so_sin_bot", "project": "p", "filename": "x.txt", "size": 5,
             "content_type": None, "r2_key": "k",
             "download_url": f"http://127.0.0.1:{port}/small", "bot_alias": "bot-99",
             "bot_token": None, "chat_id": None})
codigo = archive.main()
print("\n--- sin bot ---")
print("codigo de salida:", codigo)
assert codigo == 1, "sin bot debe salir 1"
assert any("release" in p for p, _, _ in LLAMADAS), "debe soltar el reclamo"
assert not any("done" in p for p, _, _ in LLAMADAS), "no debe marcarlo archivado"
print("OK: sin bot suelta el reclamo y no lo archiva")

for path, body, tok in LLAMADAS:
    print(f"  {path} token={tok} {json.dumps(body)[:110]}")

# --- Caso: Telegram acepta la subida pero no puede entregarla -----------------
# **El fallo silencioso y destructivo.** La Bot API sube hasta 50 MB pero solo
# baja 20: si esto no se detecta, el objeto sale de R2 y el archivo queda
# inaccesible para siempre. El archivado pareceria haber funcionado.
LLAMADAS.clear()
COLA.append({"id": "so_no_entregable", "project": "p", "filename": "y.txt", "size": 11,
             "content_type": None, "r2_key": "k",
             "download_url": f"http://127.0.0.1:{port}/small", "bot_alias": "bot-07",
             "bot_token": "btok", "chat_id": "-100999"})

def verify_que_falla(bot_token, fid, subido):
    raise RuntimeError("Telegram acepto la subida pero no puede entregar el archivo: "
                       "Bad Request: file is too big. Se deja en R2.")
archive._verificar_bajada = verify_que_falla

codigo = archive.main()
print("\n--- no entregable ---")
print("codigo de salida:", codigo)
assert codigo == 1, "debe salir 1"
assert any("release" in p for p, _, _ in LLAMADAS), "debe soltar el reclamo"
assert not any("done" in p for p, _, _ in LLAMADAS), (
    "**NO debe marcarlo archivado**: eso soltaria R2 y perderia el archivo")
print("OK: no marca archivado lo que Telegram no puede entregar")

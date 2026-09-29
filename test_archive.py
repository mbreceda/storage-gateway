"""Prueba el archivador contra un backend falso, sin secretos ni red real.

Corre con: `python3 test_archive.py` desde la raiz del repo.

Cubre los seis caminos:
- archivo chico, por la Bot API
- archivo de mas de 20 MB, por MTProto
- archivo que pasa el tope de MTProto
- el backend no entrega bot
- Telegram acepta la subida pero no puede entregarla
- cola vacia

**Cada caso es independiente**: limpia `LLAMADAS`, la cola y los dobles antes de
correr. Los casos que comparten estado dan falsos verdes, y estos tests existen
justo para atrapar un fallo que solo se ve mirando el resultado final.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- Estado compartido del backend falso -------------------------------------
LLAMADAS = []
COLA = []
SUBIDAS_BOT_API = []
SUBIDAS_MTPROTO = []


class BackendFalso(BaseHTTPRequestHandler):
    """Backend y R2 falsos: sirve el archivo y registra las llamadas."""

    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/grande":
            b = b"x" * (30 * 1024 * 1024)
        elif self.path == "/chico":
            b = b"hola mundo\n"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        LLAMADAS.append((self.path, body, self.headers.get("X-Worker-Token")))
        if self.path == "/storage/api/archive/claim":
            self._json({"item": COLA.pop(0) if COLA else None})
        else:
            self._json({"ok": True})


def _arrancar(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


backend_srv, port = _arrancar(BackendFalso)

os.environ.update({
    "BACKEND_URL": f"http://127.0.0.1:{port}",
    "WORKER_TOKEN": "wtok",
    "GITHUB_RUN_ID": "run-1",
})

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import archive  # noqa: E402


def _item(oid, *, tam, url, bot_token="btok", chat_id="-100999", alias="bot-07"):
    return {
        "id": oid, "project": "p", "filename": f"{oid}.bin", "size": tam,
        "content_type": None, "r2_key": "k",
        "download_url": f"http://127.0.0.1:{port}/{url}",
        "bot_alias": alias, "bot_token": bot_token, "chat_id": chat_id,
        # Destino de los grandes: la cuenta de usuario no es miembro del canal
        # del bot, asi que MTProto publica en el canal principal.
        "big_chat_id": "-1004395494685",
    }


def _reset():
    """Deja el mundo limpio para el caso siguiente."""
    LLAMADAS.clear()
    COLA.clear()
    SUBIDAS_BOT_API.clear()
    SUBIDAS_MTPROTO.clear()
    # Los dobles se reinstalan en cada caso: uno de ellos sustituye la
    # verificacion de bajada y no debe filtrarse al siguiente.
    _instalar_dobles()


def _instalar_dobles():
    """Sustituye los dos caminos de subida por dobles que registran."""
    def fake_bot_api(ruta, filename, bot_token, chat_id):
        SUBIDAS_BOT_API.append((filename, chat_id))
        return {"ok": True, "result": {"message_id": 42, "document": {"file_id": "FID-REAL"}}}

    async def fake_mtproto(ruta, filename, chat_id, **kw):
        SUBIDAS_MTPROTO.append((filename, chat_id))
        return {"message_id": 77, "size": os.path.getsize(ruta), "bot_alias": None}

    async def fake_mtproto_verify(chat_id, message_id, esperado):
        return None

    def fake_verificar(bot_token, fid, subido):
        return None

    archive._upload = fake_bot_api
    archive._verificar_bajada = fake_verificar
    archive.mtproto_upload.upload = fake_mtproto
    archive.mtproto_upload.verify_downloadable = fake_mtproto_verify


def _paths():
    return [p for p, _, _ in LLAMADAS]


def _done_bodies():
    return [b for p, b, _ in LLAMADAS if p.endswith("/done")]


def caso_archivo_chico_por_bot_api():
    _reset()
    COLA.append(_item("so_chico", tam=11, url="chico"))
    codigo = archive.main()
    assert codigo == 0, f"salida {codigo}"
    assert SUBIDAS_BOT_API, "debe subir por la Bot API"
    assert not SUBIDAS_MTPROTO, "no debe usar MTProto para 11 bytes"
    assert _done_bodies(), "debe marcarlo archivado"
    print("OK: 11 bytes van por la Bot API")


def caso_archivo_grande_por_mtproto():
    """**No es una optimizacion.** La Bot API solo entrega hasta 20 MB, asi que
    un archivo de 30 MB subido por ahi queda inaccesible."""
    _reset()
    COLA.append(_item("so_grande", tam=30 * 1024 * 1024, url="grande"))
    codigo = archive.main()
    assert codigo == 0, f"salida {codigo}"
    assert SUBIDAS_MTPROTO, "debe usar MTProto, no soltar el archivo"
    assert not SUBIDAS_BOT_API, "no debe usar la Bot API para 30 MB"
    cuerpo = _done_bodies()[0]
    assert cuerpo["file_id"] is None, (
        "el file_id de MTProto no sirve por la Bot API: no debe guardarse uno falso"
    )
    assert SUBIDAS_MTPROTO[0][1] == "-1004395494685", (
        f"debe publicar en el canal de los grandes, no en {SUBIDAS_MTPROTO[0][1]}")
    assert cuerpo["chat_id"] == "-1004395494685", (
        "el done debe guardar el canal real, o el message_id apuntaria a otro sitio")
    print("OK: 30 MB va por MTProto al canal de los grandes, sin file_id falso")


def caso_pasa_el_tope_de_mtproto():
    _reset()
    COLA.append(_item("so_enorme", tam=3 * 1024 * 1024 * 1024, url="grande"))
    codigo = archive.main()
    assert codigo == 0, "no tener donde archivarlo no es un fallo"
    assert not SUBIDAS_MTPROTO and not SUBIDAS_BOT_API, "no debe intentar subirlo"
    assert any("release" in p for p in _paths()), "debe soltarlo"
    assert not _done_bodies(), "no debe marcarlo archivado"
    print("OK: mas de 2 GB se queda en R2")


def caso_sin_bot():
    """Sin token ni canal el archivo no se puede subir. Es lo que pasa si el
    alias no existe, no tiene canal, o SECRET_KEY cambio."""
    _reset()
    COLA.append(_item("so_sin_bot", tam=5, url="chico", bot_token=None, chat_id=None,
                      alias="bot-99"))
    codigo = archive.main()
    assert codigo == 1, f"salida {codigo}"
    assert any("release" in p for p in _paths()), "debe soltar el reclamo"
    assert not _done_bodies(), "no debe marcarlo archivado"
    print("OK: sin bot suelta el reclamo y no lo archiva")


def caso_telegram_no_entrega():
    """**El fallo silencioso y destructivo.** Subir bien no garantiza poder
    bajar: si no se detecta, el objeto sale de R2 y el archivo se pierde."""
    _reset()

    def verify_que_falla(bot_token, fid, subido):
        raise RuntimeError("Telegram acepto la subida pero no puede entregar el archivo")

    archive._verificar_bajada = verify_que_falla
    COLA.append(_item("so_no_entregable", tam=11, url="chico"))
    codigo = archive.main()
    assert codigo == 1, f"salida {codigo}"
    assert any("release" in p for p in _paths()), "debe soltar el reclamo"
    assert not _done_bodies(), (
        "**NO debe marcarlo archivado**: eso soltaria R2 y perderia el archivo"
    )
    print("OK: no marca archivado lo que Telegram no puede entregar")


def caso_cola_vacia():
    _reset()
    codigo = archive.main()
    assert codigo == 0, f"salida {codigo}"
    assert _paths() and "claim" in _paths()[0], "debe preguntar por trabajo"
    print("OK: cola vacia no es un fallo")


def main():
    for caso in (
        caso_archivo_chico_por_bot_api,
        caso_archivo_grande_por_mtproto,
        caso_pasa_el_tope_de_mtproto,
        caso_sin_bot,
        caso_telegram_no_entrega,
        caso_cola_vacia,
    ):
        caso()
    print("\nTODOS LOS CAMINOS OK")


if __name__ == "__main__":
    main()

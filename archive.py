"""Archivador: baja un objeto de R2 y lo sube a Telegram.

Que hace
--------
Toma un objeto reclamado del backend, lo baja de R2, lo sube a Telegram con la
Bot API del bot del proyecto, y confirma. El backend entonces borra la copia de
R2.

Por que corre aqui y no en el backend
------------------------------------
El backend vive en Render con 0.1 CPU y 512 MB. Mover 2 GB ahi dentro lo deja
bloqueado durante minutos. Este runner tiene 4 vCPU, 15 GB de disco y ffmpeg,
y su trabajo es exactamente mover bytes.

**Los bytes no pasan por el backend en ningun momento.** El backend firma una
URL de R2 y este script habla directo con R2 y con Telegram.

Limite duro que condiciona el diseño
------------------------------------
La Bot API **no sube mas de 50 MB por `sendDocument`**. Para archivos mayores
hay que trocear o usar MTProto. Trocear un archivo y subir las partes es peor
que no archivar: deja el objeto partido en Telegram y obliga a recomponerlo en
cada descarga.

**Este script no trocea.** Si el archivo pasa del limite, suelta el reclamo con
el motivo y el objeto se queda en R2. Es correcto: R2 tiene 10 GB gratis y sin
coste de salida, y un archivo grande es justo el que mas conviene tener ahi.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BACKEND = os.environ["BACKEND_URL"].rstrip("/")
WORKER_TOKEN = os.environ["WORKER_TOKEN"]

# **Se comprueba que la URL sea una URL.** Sin esto, una variable vacia -o el
# nombre equivocado en el workflow- produce `unknown url type:
# '/storage/api/archive/claim'`, que no dice en ningun momento que falto
# `BACKEND_URL`. Se perdio una corrida averiguando que el workflow leia `vars`
# y el valor estuviera dado de alta como `secrets`.
if not BACKEND.startswith("http"):
    print(
        f"FALLO: BACKEND_URL no es una URL valida: {BACKEND!r}.\n"
        "Revisa que exista en el repo Y que el workflow la lea del sitio "
        "correcto (secrets o vars).",
        file=sys.stderr,
    )
    sys.exit(1)

# **El token del bot y su canal NO viven aqui.** Los entrega `/claim`, resueltos
# desde la base del backend. Duplicarlos en Infisical crearia dos fuentes de
# verdad: rotar una no rotaria la otra, y el fallo apareceria como
# `chat not found` sin pista del motivo.

# Tope real de la Bot API, y **el que manda es el de bajada**.
#
# Subir acepta 50 MB -probado con 51-, pero bajar por `getFile` corta en 20:
# medido, 19 MB pasa y 21 MB responde `Bad Request: file is too big`.
#
# **Archivar por encima de 20 MB seria perder el archivo.** Subiria bien, se
# borraria de R2, y despues nadie podria bajarlo: el archivado pareceria
# funcionar y el archivo quedaria inaccesible. Es un fallo silencioso y
# destructivo, y por eso el tope se pone por el lado conservador.
#
# Los archivos mayores se quedan en R2. No es un problema: 10 GB gratis y cero
# coste de salida, y un video grande es justo el que mas conviene tener ahi.
MAX_BOT_API_BYTES = 20 * 1024 * 1024

# Cuantos objetos procesa una corrida.
#
# **Mas de uno, y con tope.** El cron corre cada 30 minutos: si solo se
# procesara uno, un lote de 20 archivos tardaria 10 horas. Y sin tope, una
# corrida se acercaria al limite de 350 minutos y dejaria el siguiente lote
# esperando a que termine.
#
# Se para al llegar al tope o al primer fallo, lo que pase antes.
MAX_POR_CORRIDA = int(os.environ.get("ARCHIVE_MAX_PER_RUN", "10"))

WORKER_ID = os.environ.get("GITHUB_RUN_ID", "local")
API = f"{BACKEND}/storage/api/archive"


def _call(path: str, payload: dict) -> dict:
    """Llamada al backend, con el token de worker.

    **Traduce los fallos de HTTP a mensajes.** `urlopen` lanza `HTTPError` en
    cualquier respuesta >= 400, y sin atraparlo una credencial mal puesta sale
    como un traceback de treinta lineas que no dice cual fue el codigo ni que
    contesto el servidor. Los tres casos que importan -token de worker malo,
    backend dormido, y error del servidor- se distinguen de un vistazo.
    """
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Worker-Token": WORKER_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as exc:
        cuerpo = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 403:
            raise RuntimeError(
                f"el backend rechazo el token de worker (403). Comprueba que "
                f"WORKER_TOKEN sea el mismo en el workflow y en Infisical. {cuerpo}"
            ) from exc
        if exc.code == 503:
            raise RuntimeError(
                f"el backend responde 503: {cuerpo}. Normalmente es que Render "
                "esta despertando, o que falta SERVER_PASSWORD."
            ) from exc
        raise RuntimeError(f"el backend devolvio {exc.code}: {cuerpo}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"no se pudo hablar con {API}: {exc.reason}. Si Render esta dormido, "
            "la primera peticion tarda hasta un minuto en despertarlo."
        ) from exc


def _download(url: str, destino: str) -> int:
    """Baja de R2 por la URL firmada. Devuelve los bytes escritos.

    Se escribe a un `.part` y se renombra al final: si la descarga se corta, no
    queda un archivo a medias con el nombre bueno que luego se subiria a
    Telegram como si estuviera completo.
    """
    parcial = destino + ".part"
    total = 0
    with urllib.request.urlopen(url, timeout=300) as r, open(parcial, "wb") as fh:
        while True:
            trozo = r.read(1024 * 1024)
            if not trozo:
                break
            fh.write(trozo)
            total += len(trozo)
    os.replace(parcial, destino)
    return total


def _upload(ruta: str, filename: str, bot_token: str, chat_id: str) -> dict:
    """Sube a Telegram con `sendDocument`. Devuelve la respuesta de Telegram.

    El token y el canal vienen de `/claim`, no del entorno: el backend los
    resuelve desde su base, que es donde vive el bot.

    Se usa `sendDocument` y no `sendVideo` aunque sea un video: Telegram solo
    previsualiza los formatos que conoce, y con `sendVideo` un archivo que no
    cumple sus reglas -codec, contenedor- se rechaza. `sendDocument` acepta
    cualquier cosa y el archivo se puede bajar igual.
    """
    boundary = "----tgmarchive"
    with open(ruta, "rb") as fh:
        contenido = fh.read()

    cuerpo = b""
    for nombre, valor in (("chat_id", chat_id), ("caption", filename)):
        cuerpo += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{nombre}"\r\n\r\n'
            f"{valor}\r\n"
        ).encode()
    cuerpo += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    cuerpo += contenido + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendDocument",
        data=cuerpo,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def _file_id(resultado: dict) -> str:
    """Saca el `file_id` del documento de la respuesta de Telegram."""
    doc = resultado.get("result", {}).get("document") or {}
    return doc.get("file_id") or ""


def _verificar_bajada(bot_token: str, file_id: str, subido: int) -> None:
    """Confirma que Telegram puede entregar el archivo. Lanza si no.

    **Existe por un fallo real.** La Bot API sube hasta 50 MB pero solo baja 20:
    medido, 19 MB pasa y 21 MB responde `file is too big`. Sin esta comprobacion,
    un archivo de 30 MB subiria bien, el backend soltaria la copia de R2, y el
    archivo quedaria inaccesible. El archivado pareceria haber funcionado.

    `getFile` no baja el archivo: pide su ruta y su tamano, y es justo ahi donde
    Telegram aplica el tope. Una llamada barata que atrapa el caso peor.

    Se compara tambien el tamano: un `file_size` distinto del subido significa
    que Telegram guardo otra cosa, y bajar ese archivo daria un truncado.
    """
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/getFile?file_id={urllib.parse.quote(file_id)}"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())

    if not data.get("ok"):
        raise RuntimeError(
            f"Telegram acepto la subida pero no puede entregar el archivo: "
            f"{data.get('description')}. Se deja en R2."
        )
    tam = data.get("result", {}).get("file_size")
    if tam is not None and tam != subido:
        raise RuntimeError(
            f"Telegram guardo {tam} bytes y se subieron {subido}: "
            "el archivo quedaria truncado. Se deja en R2."
        )
    print(f"verificado: Telegram puede entregarlo ({tam} bytes)")


def _claim() -> dict | None:
    """Toma el siguiente objeto, o `None` si la cola esta vacia."""
    return _call("/claim", {"worker_id": WORKER_ID, "lease_seconds": 3600}).get("item")


def _archive_one(item: dict) -> int:
    """Archiva un objeto ya reclamado. 0 si termino bien, 1 si fallo."""
    oid = item["id"]
    print(f"reclamado {oid} ({item['size']} bytes) de {item['project']}")

    # **Se suelta antes de bajar, no despues.** Si se empezara a bajar un
    # archivo de 2 GB para luego descubrir que no cabe, se habrian gastado los
    # minutos y el ancho de banda para nada.
    if item["size"] > MAX_BOT_API_BYTES:
        motivo = (
            f"archivo de {item['size']} bytes: pasa el tope de la Bot API "
            f"({MAX_BOT_API_BYTES}); se queda en R2"
        )
        print(motivo)
        _call(f"/{oid}/release", {"error": motivo})
        return 0

    if not item.get("download_url"):
        _call(f"/{oid}/release", {"error": "el backend no pudo firmar la bajada"})
        return 1

    # **Sin bot no se puede subir, y se dice por que.** Los tres motivos posibles
    # -el proyecto no tiene alias, el alias no existe en el pool, o `SECRET_KEY`
    # cambio y el token guardado ya no se descifra- dan el mismo `None` aqui. El
    # mensaje los enumera en vez de elegir uno, porque desde el runner no se
    # pueden distinguir y adivinar mal cuesta una tarde.
    if not item.get("bot_token") or not item.get("chat_id"):
        motivo = (
            f"el backend no entrego bot para el alias {item.get('bot_alias')!r}: "
            "revisa que el alias exista en el pool y tenga canal, y que SECRET_KEY "
            "no haya cambiado"
        )
        print(motivo, file=sys.stderr)
        _call(f"/{oid}/release", {"error": motivo})
        return 1

    destino = f"/tmp/{oid}_{item['filename']}"
    try:
        escrito = _download(item["download_url"], destino)
        print(f"bajados {escrito} bytes")

        resultado = _upload(destino, item["filename"], item["bot_token"], item["chat_id"])
        if not resultado.get("ok"):
            raise RuntimeError(f"Telegram rechazo la subida: {resultado}")

        fid = _file_id(resultado)
        if not fid:
            raise RuntimeError(f"Telegram no devolvio file_id: {resultado}")

        # **Se comprueba que se puede bajar ANTES de soltar R2.** Este paso es la
        # red que atrapa el fallo silencioso: si Telegram acepta la subida pero
        # despues no entrega el archivo, soltar R2 perderia el archivo para
        # siempre. `getFile` es la unica forma de saberlo sin bajarlo entero.
        #
        # Cuesta una llamada y salva el caso peor: archivar algo inaccesible.
        _verificar_bajada(item["bot_token"], fid, item["size"])

        _call(
            f"/{oid}/done",
            {
                "chat_id": item["chat_id"],
                "message_id": resultado["result"]["message_id"],
                "file_id": fid,
                "bot_alias": item.get("bot_alias"),
            },
        )
        print(f"archivado {oid}")
        return 0
    except (urllib.error.URLError, OSError, RuntimeError, KeyError) as exc:
        # **El reclamo SIEMPRE se suelta.** Si el runner muere con el reclamo
        # puesto, la fila se queda en `archiving` hasta que venza el lease, y
        # mientras tanto nadie reintenta el archivo.
        print(f"fallo archivando {oid}: {exc}", file=sys.stderr)
        try:
            _call(f"/{oid}/release", {"error": str(exc)[:500]})
        except Exception as exc2:  # noqa: BLE001 - el release no puede tapar el fallo original
            print(f"ademas fallo el release: {exc2}", file=sys.stderr)
        return 1
    finally:
        # El disco del runner es efimero, pero un archivo de 2 GB que se quede
        # puede llenarlo y tumbar el siguiente objeto de la misma corrida.
        if os.path.exists(destino):
            os.unlink(destino)


def main() -> int:
    """Procesa hasta `MAX_POR_CORRIDA` objetos.

    **Se para al primer fallo**, y devuelve 1. Seguir con el siguiente cuando
    Telegram acaba de rechazar uno suele significar repetir el mismo error diez
    veces y gastar diez veces el tiempo; el cron vuelve en 30 minutos.
    """
    procesados = 0
    while procesados < MAX_POR_CORRIDA:
        item = _claim()
        if item is None:
            break
        if _archive_one(item) != 0:
            return 1
        procesados += 1

    print(f"procesados {procesados} objetos" if procesados else "nada que archivar")
    return 0


if __name__ == "__main__":
    sys.exit(main())

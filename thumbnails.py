"""Genera las miniaturas de video que faltan, por lotes.

Por que existe
--------------
El worker local genera miniaturas a demanda y las sube a R2, pero **se para
cuando no hace falta**: dejarlo corriendo sondea cada pocos segundos y mantiene
Render despierto 24/7, que son las 750 horas gratuitas del mes.

Con el worker parado, cada video nuevo se queda con un placeholder. Este script
es la red: pide los pendientes al backend y los procesa.

Que hace, por archivo
---------------------
1. Baja **el inicio** del video de Telegram por MTProto.
2. Saca un frame con ffmpeg, del segundo 1 y si falla del 0.
3. Lo redimensiona con PIL y lo sube a `thumbs/<uuid>.jpg`.
4. Avisa al backend, que cuenta los bytes y marca el archivo.

Por que MTProto y no el backend
-------------------------------
El worker de casa baja por un Bot API local que resuelve el `file_id` sin el
tope de 20 MB del publico. **Un runner de GitHub no tiene eso.** La alternativa
seria bajar por el backend, con 0.1 CPU en medio: 25 MB por video, que es justo
lo que el trabajo de miniaturas existe para evitar.

Por que una SESION PROPIA y no la del servidor
----------------------------------------------
**Una sesion de Telegram no admite dos IPs a la vez.** Comprobado: usar la misma
clave desde Render y desde local hace que Telegram la mate con
`AuthKeyDuplicatedError`, y **el servidor se queda sin sesion**.

Este cron corre en la IP de GitHub, distinta cada vez. Por eso usa
`CRON_TG_SESSION`, una sesion propia: la misma cuenta puede tener varias si cada
una tiene su clave -la cuenta ya tenia tres activas-.

Por que no baja el video entero
-------------------------------
Un frame del segundo 1 vive en la cabecera. Se piden los primeros MB y se corta:
bajar 2 GB para quedarse con un frame es lo que la cache en R2 existe para
evitar. Los videos cuyo indice esta al final no se pueden muestrear asi, y se
marcan como descartados para no reintentarlos cada media hora.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import httpx
from PIL import Image

BACKEND = os.environ["BACKEND_URL"].rstrip("/")
WORKER_TOKEN = os.environ["WORKER_TOKEN"]

# Cuantos procesa una corrida.
#
# Cada video tarda entre 5 y 30 segundos entre bajar, extraer y subir. Con 50 son
# menos de 25 minutos, muy por debajo de los 120 del timeout. Mas alto arriesga
# acercarse al limite sin ganar nada: el cron vuelve en 30 minutos.
MAX_POR_CORRIDA = int(os.environ.get("THUMBS_MAX_PER_RUN", "50"))

# Cuanto se baja de cada video, en orden. Un frame del segundo 1 suele estar en
# los primeros megabytes; los MP4 con `moov` al final necesitan mas.
INTENTOS_MB = (5, 20)

ANCHO_MAX = 400

if not BACKEND.startswith("http"):
    print(
        f"FALLO: BACKEND_URL no es una URL valida: {BACKEND!r}.\n"
        "Revisa que exista en el repo Y que el workflow la lea del sitio "
        "correcto (secrets o vars).",
        file=sys.stderr,
    )
    sys.exit(1)


class _SinCamino(Exception):
    """Un fallo del que no hay salida: reintentarlo no cambia nada.

    Se distingue de un fallo transitorio -un bot caido, un backend sin
    desplegar- porque el descarte es permanente. Confundirlos deja archivos sin
    miniatura para siempre, o hace que el cron reintente 40 archivos cada 30
    minutos sin poder avanzar.
    """


def _limpiar(exc: Exception) -> str:
    """El mensaje de error sin el nombre del archivo.

    **Los nombres son el titulo del video y los logs son publicos.** Un error de
    ffmpeg o de R2 arrastra la ruta completa -`/tmp/<uuid>_<titulo>.mp4`-, asi
    que recortar el print no basta: hay que limpiar el mensaje.

    Se deja el resto del texto: la causa -`Invalid data found`, `403`- es lo que
    sirve para diagnosticar, y eso no identifica a nadie.
    """
    import re

    texto = str(exc)
    # **Hasta el final de la ruta, no hasta el primer espacio.** Un nombre con
    # espacios -`/tmp/abc12345_Mi Video Secreto.mkv`- dejaba el resto a la vista
    # si se cortaba en el espacio. Se para en lo que delimita una ruta: dos
    # puntos, comilla, parentesis, o el final de la cadena.
    texto = re.sub(r"/tmp/[0-9a-f]{8,}[^\s:)\]}]*(\s+[^\s:)\]}]+)*", "/tmp/<archivo>", texto)
    # Y por si el nombre viaja suelto, se recorta a lo que aporta.
    return texto[:160]


def _headers() -> dict:
    return {"X-Worker-Token": WORKER_TOKEN}


def _pendientes(limite: int, intentos: int = 4) -> list[dict]:
    """Los videos sin miniatura, segun el backend. **Reintenta.**

    **Render se duerme a los 15 minutos sin trafico.** La primera peticion lo
    despierta y puede tardar hasta un minuto; si el timeout se agota a mitad, la
    lectura lanza `TimeoutError` -que **no** es `URLError`-, y el script moria
    con un traceback de treinta lineas en vez de reintentar.

    Se reintenta con espera creciente: 5, 10, 20 segundos. Es lo que tarda en
    despertar, y el cron corre cada 30 minutos: esperar un minuto no molesta.
    """
    url = f"{BACKEND}/jobs/worker/thumbnails/pending?limit={limite}"
    for intento in range(1, intentos + 1):
        req = urllib.request.Request(url, headers=_headers())
        try:
            # **Timeout generoso.** Despertar Render tarda hasta 60 s, y con 60
            # justos cualquier lentitud lo dejaba fuera.
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read()).get("items", [])
        except urllib.error.HTTPError as exc:
            cuerpo = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 403:
                # Un token malo no se arregla reintentando.
                raise RuntimeError(
                    f"el backend rechazo el token de worker (403). Comprueba que "
                    f"WORKER_TOKEN sea el mismo aqui y en Infisical. {cuerpo}"
                ) from exc
            if exc.code < 500 or intento == intentos:
                raise RuntimeError(f"el backend devolvio {exc.code}: {cuerpo}") from exc
            print(f"  el backend devolvio {exc.code}: reintento {intento}/{intentos}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # **`TimeoutError` incluido.** Un timeout al leer la respuesta NO es
            # `URLError`: escapa del manejo y mata el script con un traceback.
            if intento == intentos:
                raise RuntimeError(
                    f"no se pudo hablar con {BACKEND}: {exc}. Si Render esta "
                    "dormido, la primera peticion tarda hasta un minuto en despertarlo."
                ) from exc
            print(f"  sin respuesta ({type(exc).__name__}): reintento {intento}/{intentos}")
        espera = 5 * (2 ** (intento - 1))
        print(f"  esperando {espera}s antes de reintentar...")
        time.sleep(espera)
    return []


def _bajar_inicio(item: dict, largo: int, destino: str) -> int:
    """Baja los primeros `largo` bytes del video, directo de Telegram. MTProto.

    **Se corta al llegar al largo.** Un frame del segundo 1 vive en la cabecera:
    bajarlo entero serian 2 GB para quedarse con 50 KB. Telethon no sabe "dame
    solo el principio", asi que la economia esta en **dejar de escribir y salir**,
    no en pedir menos.
    """
    import asyncio

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    chat_id, message_id = item.get("chat_id"), item.get("message_id")
    if not chat_id or not message_id:
        # **Tambien es un sin camino.** MTProto necesita el canal y el mensaje
        # para pedirle el archivo a Telegram; sin ellos no hay nada que
        # reintentar.
        raise _SinCamino("sin chat_id/message_id y MTProto no ve el canal")

    escrito = 0

    async def _bajar():
        nonlocal escrito
        cliente = TelegramClient(
            StringSession(os.environ["CRON_TG_SESSION"]),
            int(os.environ["TG_API_ID"]),
            os.environ["TG_API_HASH"],
        )
        await cliente.connect()
        try:
            if not await cliente.is_user_authorized():
                raise RuntimeError("CRON_TG_SESSION no esta autorizada")
            entidad = await cliente.get_entity(int(chat_id))
            mensaje = await cliente.get_messages(entidad, ids=int(message_id))
            if mensaje is None or not mensaje.file:
                raise RuntimeError(f"el mensaje {message_id} no tiene archivo")
            with open(destino, "wb") as fh:
                async for trozo in cliente.iter_download(mensaje, request_size=1024 * 1024):
                    fh.write(trozo)
                    escrito += len(trozo)
                    if escrito >= largo:
                        break
        finally:
            await cliente.disconnect()

    asyncio.run(_bajar())
    return escrito


def _bajar_bot_api(item: dict, destino: str) -> int:
    """Baja el archivo por la Bot API del backend. Respaldo de MTProto.

    **Existe porque la sesion del cron no es miembro de todos los canales.** El
    canal del bot `primary` es donde vive la mayor parte de la biblioteca, y la
    cuenta de usuario no esta dentro: MTProto responde "Could not find the input
    entity" y no hay nada que hacer desde aqui.

    La Bot API si funciona en ese caso, con su tope de 20 MB. Es justo el tope
    que el camino MTProto existe para superar, asi que este respaldo cubre los
    archivos chicos y deja los grandes al worker de casa.

    **El `file_id` solo sirve con el bot que subio el archivo**, y el backend
    entrega su token resuelto por canal en `bot_token`.
    """
    file_id = item.get("file_id")
    if not file_id:
        # **Sin `file_id` no hay camino, y es definitivo.** La Bot API necesita
        # ese identificador y MTProto necesita el canal: si falta, y la sesion no
        # ve el canal, no hay nada que reintentar. Marcarlo como transitorio
        # hacia que 40 archivos volvieran en cada corrida sin poder avanzar.
        raise _SinCamino("sin file_id y MTProto no ve el canal: ningun camino alcanza")

    # **Se prueba el token del canal y, si no hay, todos los bots activos.** Un
    # `file_id` solo responde con el bot que subio el archivo, y las 119 filas
    # sin canal no dicen cual fue. El que no sirve contesta `wrong file_id`.
    candidatos = [item["bot_token"]] if item.get("bot_token") else item.get("bot_candidates") or []
    if not candidatos:
        raise RuntimeError("sin bot_token ni candidatos: no se puede usar la Bot API")

    token = None
    ultimo = ""
    for cand in candidatos:
        try:
            info = urllib.request.urlopen(
                f"https://api.telegram.org/bot{cand}/getFile?file_id={urllib.parse.quote(file_id)}",
                timeout=60,
            )
            datos = json.loads(info.read())
            if datos.get("ok"):
                token = cand
                break
            ultimo = str(datos.get("description") or "")[:80]
        except urllib.error.HTTPError as exc:
            ultimo = str(json.loads(exc.read()).get("description") or "")[:80]
    if token is None:
        raise RuntimeError(f"ningun bot pudo resolver el file_id ({ultimo})")

    ruta_remota = datos["result"]["file_path"]
    with urllib.request.urlopen(
        f"https://api.telegram.org/file/bot{token}/{ruta_remota}", timeout=300
    ) as r, open(destino, "wb") as fh:
        while True:
            trozo = r.read(1024 * 1024)
            if not trozo:
                break
            fh.write(trozo)
    return os.path.getsize(destino)


def _frame(src: str) -> str | None:
    """Saca un frame con ffmpeg. El segundo 1, o el 0 si el video es corto.

    **El segundo 1 y no el 0.** El primer frame suele ser negro o una cortinilla:
    una miniatura negra no dice nada de lo que hay dentro.
    """
    for seek in (1, 0):
        dst = f"{src}.{seek}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(seek), "-i", src,
             "-frames:v", "1", "-vf", "scale=320:-2", "-f", "mjpeg", dst],
            capture_output=True, timeout=120,
        )
        if r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst
    return None


def _jpeg(frame_path: str) -> bytes:
    img: Image.Image = Image.open(frame_path)
    img.thumbnail((ANCHO_MAX, ANCHO_MAX))
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _subir(uuid: str, jpeg: bytes) -> None:
    """Sube el JPEG a R2 y avisa al backend."""
    import boto3

    key = f"thumbs/{uuid}.jpg"
    boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    ).put_object(
        Bucket=os.environ["R2_BUCKET"],
        Key=key,
        Body=jpeg,
        ContentLength=len(jpeg),
        ContentType="image/jpeg",
    )

    # **El aviso es lo que apaga el placeholder.** Sin el, el backend no sabe
    # que existe y cada visita vuelve a encolar el mismo trabajo.
    r = httpx.post(
        f"{BACKEND}/jobs/worker/files/{uuid}/thumbnail-ready",
        headers=_headers(),
        json={"r2_key": key, "bytes": len(jpeg)},
        timeout=60,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"el backend rechazo el aviso ({r.status_code}): {r.text[:200]}")


def _una(item: dict) -> bool:
    """Genera la miniatura de un video. `False` si no se pudo."""
    uuid = item["uuid"]
    tam = int(item.get("size") or 0)
    # **El nombre NO se imprime.** Los logs de GitHub Actions de un repo publico
    # son publicos, y los nombres de estos archivos son el titulo del video.
    # El uuid y el tamano bastan para diagnosticar.
    print(f"  {uuid[:12]} ({tam // (1024 * 1024)} MB)")

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg no esta instalado en el runner")

    # **Primero por la Bot API si MTProto no puede con ese canal.** Se prueba el
    # respaldo de entrada cuando sabemos que la sesion no es miembro, en vez de
    # gastar los intentos de MTProto fallando.
    if item.get("sin_mtproto"):
        src = tempfile.mktemp(suffix=".bin")
        frame = None
        try:
            _bajar_bot_api(item, src)
            frame = _frame(src)
            if frame:
                jpeg = _jpeg(frame)
                _subir(uuid, jpeg)
                print(f"    ok por Bot API, {len(jpeg)} bytes")
                return True
        except _SinCamino as exc:
            print(f"    sin camino: {exc}", file=sys.stderr)
            _descartar(uuid, tam, str(exc))
            return False
        except Exception as exc:  # noqa: BLE001 - se reporta; puede ser transitorio
            motivo = f"{type(exc).__name__}: {_limpiar(exc)}"
            print(f"    fallo por Bot API: {motivo}", file=sys.stderr)
            # **Solo se descarta lo DEFinitivo.** El tope de 20 MB no cambia: un
            # archivo que lo pasa nunca se podra bajar por aqui, y reintentarlo
            # cada 30 minutos es un bucle.
            #
            # Un fallo al resolver el bot, en cambio, puede ser transitorio -un
            # bot caido, un backend sin desplegar-. Marcarlo como definitivo
            # dejaria el archivo sin miniatura para siempre por un problema de
            # un minuto. Se comprobo: 4 archivos de 3 a 18 MB quedaron
            # descartados por eso.
            if "too big" in motivo:
                # **Mensaje limpio, sin repetir el error crudo.** El motivo ya
                # dice lo que pasa; pegarle el `RuntimeError: ...` detras lo
                # hacia ilegible y ocupaba dos lineas en el log.
                _descartar(
                    uuid, tam,
                    f"pasa el tope de 20 MB de la Bot API y la sesion no ve su canal "
                    f"({tam // (1024 * 1024)} MB)",
                )
                return False
            print("    (no se descarta: el fallo puede ser transitorio)", file=sys.stderr)
            return False
        finally:
            for ruta in (src, frame):
                if ruta and os.path.exists(ruta):
                    os.unlink(ruta)
        # Se llego aqui sin frame y sin excepcion: el archivo no dio imagen.
        _descartar(uuid, tam, "la Bot API bajo el archivo pero no dio frame")
        return False

    for mb in INTENTOS_MB:
        src = tempfile.mktemp(suffix=".bin")
        frame = None
        try:
            largo = min(mb * 1024 * 1024, tam) if tam else mb * 1024 * 1024
            try:
                _bajar_inicio(item, largo, src)
            except _SinCamino as exc:
                print(f"    sin camino: {exc}", file=sys.stderr)
                _descartar(uuid, tam, str(exc))
                return False
            frame = _frame(src)
            if not frame:
                continue  # Se reintenta con mas bytes: `moov` puede estar al final.
            jpeg = _jpeg(frame)
            _subir(uuid, jpeg)
            print(f"    ok, {len(jpeg)} bytes")
            return True
        except Exception as exc:  # noqa: BLE001 - un video malo no corta el lote
            print(f"    fallo: {type(exc).__name__}: {_limpiar(exc)}", file=sys.stderr)
            return False
        finally:
            for ruta in (src, frame):
                if ruta and os.path.exists(ruta):
                    os.unlink(ruta)

    # **Se avisa al backend para no reintentarlo para siempre.** Sin esto, el
    # video vuelve en cada corrida: el cron baja 25 MB, falla, y empieza de nuevo
    # en 30 minutos. Con unos pocos asi, el cron se convierte en un bucle que
    # gasta ancho de banda sin avanzar nunca.
    #
    # **Se dice que se intento y con cuanto.** Este mensaje es lo que distingue
    # "el indice esta al final" de "ffmpeg esta roto": sin los tamanos probados,
    # los dos casos se ven igual y ninguno se puede diagnosticar.
    probados = ", ".join(f"{mb} MB" for mb in INTENTOS_MB)
    # **Solo se descarta si el video es chico.** Con 20 MB probados, un archivo
    # de 25 MB puede perfectamente tener el indice mas alla: descartarlo seria
    # rendirse antes de intentarlo. Los grandes se dejan pendientes para el
    # worker de casa, que tiene el video entero.
    if tam <= max(INTENTOS_MB) * 1024 * 1024:
        _descartar(
            uuid, tam,
            f"sin frame con {probados} en un archivo de {tam // (1024*1024)} MB: "
            "el indice esta al final o el video no es muestreable",
        )
    else:
        print(
            f"    sin frame con {probados}, pero el archivo tiene "
            f"{tam // (1024*1024)} MB: se deja pendiente para el worker de casa",
            file=sys.stderr,
        )
    return False


# Motivos de descarte por uuid, para el resumen final. Se usa en vez de un
# contador global porque el bucle no sabe por que fallo cada uno.
_DESCARTES: dict[str, str] = {}


def _descartar(uuid: str, tam: int, motivo: str) -> None:
    """Avisa al backend de que no se pudo. **Es lo que evita el bucle.**

    Sin esto el video vuelve en cada corrida: el cron baja 25 MB, falla, y
    empieza de nuevo en 30 minutos. Con unos pocos asi, el cron se convierte en
    un bucle que gasta ancho de banda sin avanzar nunca.
    """
    print(f"    {motivo}", file=sys.stderr)
    _DESCARTES[uuid] = motivo
    try:
        httpx.post(
            f"{BACKEND}/jobs/worker/files/{uuid}/thumbnail-failed",
            headers=_headers(), json={"error": motivo[:200]}, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 - no poder avisar no cambia el resultado
        print(f"    (no se pudo marcar como descartada: {exc})", file=sys.stderr)


def _canales_visibles(pendientes: list[dict]) -> None:
    """Marca que items tienen que ir por la Bot API.

    **Se comprueba una vez por canal, no por archivo.** La sesion de MTProto no
    es miembro de todos los canales -el del bot `primary` es el caso claro-, y
    descubrirlo fallando cuesta intentos y ruido en el log. Con 235 archivos en
    ese canal, serian 235 fallos identicos.
    """
    import asyncio

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    canales = sorted({i["chat_id"] for i in pendientes if i.get("chat_id")})
    if not canales:
        return

    visibles: dict[str, bool] = {}

    async def _comprobar():
        cliente = TelegramClient(
            StringSession(os.environ["CRON_TG_SESSION"]),
            int(os.environ["TG_API_ID"]),
            os.environ["TG_API_HASH"],
        )
        await cliente.connect()
        try:
            for canal in canales:
                try:
                    await cliente.get_entity(int(canal))
                    visibles[canal] = True
                except Exception:  # noqa: BLE001 - no verlo es el caso normal aqui
                    visibles[canal] = False
        finally:
            await cliente.disconnect()

    asyncio.run(_comprobar())

    for item in pendientes:
        canal = item.get("chat_id")
        # **Sin canal tambien va por la Bot API.** MTProto necesita
        # `chat_id` + `message_id` para pedir el mensaje; sin canal no puede,
        # aunque tenga `file_id`.
        if not canal or not visibles.get(canal, False):
            item["sin_mtproto"] = True

    ciegos = sum(1 for i in pendientes if i.get("sin_mtproto"))
    if ciegos:
        print(f"{ciegos} de {len(pendientes)} van por la Bot API: la sesion no ve su canal")


def main() -> int:
    pendientes = _pendientes(MAX_POR_CORRIDA)
    if not pendientes:
        print("no hay miniaturas pendientes")
        return 0

    print(f"generando {len(pendientes)} miniaturas...")
    _canales_visibles(pendientes)
    hechas = 0
    for item in pendientes:
        if _una(item):
            hechas += 1

    # Cuantos se descartaron por no tener camino. **Se cuentan por el motivo y
    # no restando**, porque un fallo transitorio tampoco es "hecho" y restarlo
    # daria un numero que no significa nada.
    sin_camino = sum(1 for m in _DESCARTES.values() if m.startswith("sin "))
    print(
        f"\n{hechas} de {len(pendientes)} listas"
        + (f" ({sin_camino} sin camino)" if sin_camino else "")
    )

    # **Fallar en TODAS es un fallo sistemico, y debe verse rojo.** Un video
    # corrupto no debe marcar la corrida: el cron vuelve en 30 minutos. Pero si
    # fallan las 50, algo esta roto -ffmpeg sin instalar, R2 sin permisos- y una
    # corrida verde lo esconderria: se ve igual que "no habia nada que hacer".
    #
    # **Salvo que ninguna tuviera camino.** "Sin file_id y MTProto no ve el
    # canal" no es un fallo del cron: es trabajo que ya no existe. Marcarlo rojo
    # haria que cada corrida parezca rota mientras se drenan los pendientes
    # inalcanzables, y un rojo que no significa nada se deja de mirar.
    if pendientes and hechas == 0 and sin_camino < len(pendientes):
        print(
            f"FALLO: ninguna de las {len(pendientes)} miniaturas se pudo generar. "
            "Con una sola seria un archivo raro; con todas, algo esta roto.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

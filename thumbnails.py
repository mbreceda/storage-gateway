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
1. Pide al backend los primeros MB del video (`Range`, no el archivo entero).
   Un frame del segundo 1 vive en la cabecera: bajar 2 GB para quedarse con 5 MB
   es justo lo que la cache en R2 existe para evitar.
2. Saca un frame con ffmpeg, del segundo 1 y si falla del 0.
3. Lo redimensiona con PIL y lo sube a `thumbs/<uuid>.jpg`.
4. Avisa al backend, que cuenta los bytes y marca el archivo.

**No usa el worker ni la base.** El backend expone lo que hace falta por HTTP,
asi que este script no comparte codigo con el repo del servidor: es un runner
que sabe hablar los dos protocolos -HTTP y S3-.

Por que no toca los fragmentados
--------------------------------
Un archivo partido en trozos de 512 MB no se puede muestrear con un `Range` de
los primeros megabytes: el primer trozo no contiene la cabecera del video
completo. El backend ya los excluye de la lista.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


def _headers() -> dict:
    return {"X-Worker-Token": WORKER_TOKEN}


def _pendientes(limite: int) -> list[dict]:
    """Los videos sin miniatura, segun el backend."""
    url = f"{BACKEND}/jobs/worker/thumbnails/pending?limit={limite}"
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read()).get("items", [])
    except urllib.error.HTTPError as exc:
        cuerpo = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 403:
            raise RuntimeError(
                f"el backend rechazo el token de worker (403). Comprueba que "
                f"WORKER_TOKEN sea el mismo aqui y en Infisical. {cuerpo}"
            ) from exc
        raise RuntimeError(f"el backend devolvio {exc.code}: {cuerpo}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"no se pudo hablar con {BACKEND}: {exc.reason}. Si Render esta "
            "dormido, la primera peticion tarda hasta un minuto en despertarlo."
        ) from exc


def _bajar_inicio(uuid: str, largo: int, destino: str) -> int:
    """Baja los primeros `largo` bytes del video. Devuelve los bytes escritos.

    **`Range`, no el archivo entero.** El backend lo soporta, y sin el esta
    funcion bajaria 2 GB para sacar un frame de la cabecera.
    """
    url = f"{BACKEND}/jobs/worker/files/{uuid}/download"
    req = urllib.request.Request(url, headers={**_headers(), "Range": f"bytes=0-{largo - 1}"})
    with urllib.request.urlopen(req, timeout=300) as r, open(destino, "wb") as fh:
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
    nombre = item.get("name") or uuid
    tam = int(item.get("size") or 0)
    print(f"  {uuid[:12]} {nombre[:45]} ({tam // (1024 * 1024)} MB)")

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg no esta instalado en el runner")

    for mb in INTENTOS_MB:
        src = tempfile.mktemp(suffix=".bin")
        frame = None
        try:
            largo = min(mb * 1024 * 1024, tam) if tam else mb * 1024 * 1024
            escrito = _bajar_inicio(uuid, largo, src)
            frame = _frame(src)
            if not frame:
                continue  # Se reintenta con mas bytes: `moov` puede estar al final.
            jpeg = _jpeg(frame)
            _subir(uuid, jpeg)
            print(f"    ok, {len(jpeg)} bytes")
            return True
        except Exception as exc:  # noqa: BLE001 - un video malo no corta el lote
            print(f"    fallo: {type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr)
            return False
        finally:
            for ruta in (src, frame):
                if ruta and os.path.exists(ruta):
                    os.unlink(ruta)

    print("    no se pudo sacar un frame con ningun tamano", file=sys.stderr)
    return False


def main() -> int:
    pendientes = _pendientes(MAX_POR_CORRIDA)
    if not pendientes:
        print("no hay miniaturas pendientes")
        return 0

    print(f"generando {len(pendientes)} miniaturas...")
    hechas = 0
    for item in pendientes:
        if _una(item):
            hechas += 1

    print(f"\n{hechas} de {len(pendientes)} listas")
    # **Sale 0 aunque fallen algunas.** Un video corrupto no debe marcar la
    # corrida como roja: el cron vuelve en 30 minutos y reintenta el resto. Un
    # fallo duro -token malo, backend caido- ya lanzo excepcion antes.
    return 0


if __name__ == "__main__":
    sys.exit(main())

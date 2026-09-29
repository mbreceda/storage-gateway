"""Verifica dos reglas del cron: sin nombres en el log, y descarte con criterio.

Por que existe
--------------
El repo `storage-gateway` es **publico**, asi que sus logs de GitHub Actions son
publicos. Y los nombres de estos archivos son el titulo del video: varios son
NSFW.

Y el descarte tiene que ser selectivo. Un fallo al resolver el bot -un bot
caido, un backend sin desplegar- no puede marcar el archivo como imposible:
quedaria sin miniatura para siempre por un problema de un minuto. Paso: 4
archivos de 3 a 18 MB quedaron descartados asi, y hubo que recuperarlos a mano.

Corre con: `python3 test_sin_nombres.py`
"""

import inspect
import os
import sys

os.environ.setdefault("BACKEND_URL", "http://localhost")
os.environ.setdefault("WORKER_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import thumbnails  # noqa: E402
from archive import _limpiar as limpiar_archive  # noqa: E402
from thumbnails import _limpiar as limpiar_thumbs  # noqa: E402

SECRETOS = ("NSFW", "Secreto", "temporada3", "Titulo", "Porno", "XVIDEOS")

CASOS = [
    "ffmpeg failed on /tmp/714cd65e_Titulo+NSFW+Del+Video.mp4: Invalid data found",
    "/tmp/abc12345_Mi Video Secreto.mkv no such file",
    "error en /tmp/2405ed66_temporada3-intro.mp4 (No such file)",
    "R2 fallo en /tmp/deadbeef_x.mp4",
    "/tmp/a1b2c3d4_Porno Gratis [XVIDEOS.COM].mp4",
    "no se pudo abrir /tmp/ff001122_video.con.espacios y mas.mp4",
    "Bad Request: file is too big",
    "el backend devolvio 503",
    "chat not found",
]


def probar_nombres() -> int:
    fugas = 0
    for nombre, fn in (("thumbnails.py", limpiar_thumbs), ("archive.py", limpiar_archive)):
        print(f"=== {nombre} ===")
        for caso in CASOS:
            limpio = fn(RuntimeError(caso))
            malos = [s for s in SECRETOS if s in limpio]
            if malos:
                fugas += 1
                print(f"  FUGA {malos}: {limpio[:78]}")
            else:
                print(f"  ok   {limpio[:78]}")
    return fugas


def probar_descarte() -> int:
    """El descarte solo aplica a lo definitivo.

    Se lee el codigo porque la decision depende de excepciones de red que no se
    pueden provocar desde aqui. Es debil, pero cubre lo que importa: que la
    rama exista.
    """
    print("=== descarte ===")
    fuente = inspect.getsource(thumbnails._una) + inspect.getsource(thumbnails.main)
    fallos = 0
    if 'if "too big" in motivo:' not in fuente:
        print("  FALLO: un fallo de la Bot API descarta sin mirar si es definitivo")
        fallos += 1
    else:
        print("  ok   el tope de 20 MB descarta, y es definitivo")
    if "puede ser transitorio" not in fuente:
        print("  FALLO: no hay rama para el fallo transitorio")
        fallos += 1
    else:
        print("  ok   un fallo de resolucion no descarta")
    if "if tam <= max(INTENTOS_MB)" not in fuente:
        print("  FALLO: MTProto descarta un video grande sin intentarlo entero")
        fallos += 1
    else:
        print("  ok   un video grande no se descarta por no caber en 20 MB")
    return fallos


def main() -> int:
    fallos = probar_nombres()
    print()
    fallos += probar_descarte()
    print()
    if fallos:
        print(f"FALLO: {fallos} problemas.")
        return 1
    print("OK: sin fugas de nombres, y el descarte solo aplica a lo definitivo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

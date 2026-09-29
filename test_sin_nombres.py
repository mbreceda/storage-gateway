"""Verifica que los logs del cron no filtren nombres de archivo.

Por que existe
--------------
El repo `storage-gateway` es **publico**, asi que sus logs de GitHub Actions son
publicos. Y los nombres de estos archivos son el titulo del video: varios son
NSFW.

No basta con no imprimir el nombre a proposito. Un error de ffmpeg o de R2
arrastra la ruta temporal completa -`/tmp/<uuid>_<titulo>.mp4`-, asi que el
nombre se filtra por un camino que nadie mira.

Corre con: `python3 test_sin_nombres.py`
"""

import os
import sys

os.environ.setdefault("BACKEND_URL", "http://localhost")
os.environ.setdefault("WORKER_TOKEN", "x")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from archive import _limpiar as limpiar_archive  # noqa: E402
from thumbnails import _limpiar as limpiar_thumbs  # noqa: E402

# Fragmentos que NO deben sobrevivir a la limpieza.
SECRETOS = ("NSFW", "Secreto", "temporada3", "Titulo", "Porno", "XVIDEOS")

CASOS = [
    # El nombre va pegado al uuid: es lo que genera el codigo.
    "ffmpeg failed on /tmp/714cd65e_Titulo+NSFW+Del+Video.mp4: Invalid data found",
    "/tmp/abc12345_Mi Video Secreto.mkv no such file",
    "error en /tmp/2405ed66_temporada3-intro.mp4 (No such file)",
    "R2 fallo en /tmp/deadbeef_x.mp4",
    # Con caracteres raros, que es lo normal en estos titulos.
    "/tmp/a1b2c3d4_Porno Gratis [XVIDEOS.COM].mp4",
    "no se pudo abrir /tmp/ff001122_video.con.espacios y mas.mp4",
    # Mensajes sin ruta: no se tocan.
    "Bad Request: file is too big",
    "el backend devolvio 503",
    "chat not found",
]


def main() -> int:
    fallos = 0
    for nombre, fn in (("thumbnails.py", limpiar_thumbs), ("archive.py", limpiar_archive)):
        print(f"=== {nombre} ===")
        for caso in CASOS:
            limpio = fn(RuntimeError(caso))
            fugas = [s for s in SECRETOS if s in limpio]
            if fugas:
                fallos += 1
                print(f"  FUGA {fugas}: {limpio[:80]}")
            else:
                print(f"  ok   {limpio[:80]}")

    print()
    if fallos:
        print(f"FALLO: {fallos} fugas. El nombre del archivo llega al log.")
        return 1
    print("OK: ningun caso filtra el nombre del archivo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

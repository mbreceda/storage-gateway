"""Subida por MTProto, para los archivos que la Bot API no puede entregar.

Por que existe
--------------
La Bot API tiene dos topes distintos y el que manda es el de bajada:

| Operacion | Tope |
|---|---|
| `sendDocument` (subir) | 50 MB |
| `getFile` (bajar) | **20 MB** |

Medido: 19 MB baja, 21 MB no. Asi que un archivo de 30 MB subido por la Bot API
**queda inaccesible**: se sube, el backend suelta R2, y despues nadie lo puede
bajar.

MTProto sube **y baja** hasta 2 GB, porque usa la API de cliente y no la de bot.
Es la unica forma de archivar videos de verdad.

Por que una sesion de usuario y no un bot
-----------------------------------------
Un bot no puede usar MTProto para subir archivos grandes: los bots estan
limitados a la Bot API. Hace falta una sesion de usuario, que es lo que
`TG_USER_SESSION` guarda.

**Consecuencia que hay que tener presente:** los archivos subidos por MTProto
aparecen en Telegram como enviados por **esa cuenta de usuario**, no por el bot.
El `file_id` de la Bot API y el `file_id` de MTProto **no son intercambiables**:
para volver a bajarlos hay que usar la misma sesion y el mismo `message_id`.
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)

# Tope de MTProto. Muy por encima del de la Bot API, y es el que permite
# archivar videos.
MAX_MTPROTO_BYTES = 2000 * 1024 * 1024

# Tope de la Bot API para BAJAR, que es el que limita que se puede archivar sin
# MTProto. Ver el docstring del modulo.
MAX_BOT_API_BYTES = 20 * 1024 * 1024


def _credenciales() -> tuple[int, str, str]:
    """`api_id`, `api_hash` y la sesion. Lanza si falta alguna.

    Se leen del entorno y no de `settings` a proposito: este script corre en
    GitHub Actions, donde no existe el resto de la configuracion del backend.
    """
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    sesion = os.environ.get("TG_USER_SESSION")
    faltan = [
        nombre
        for nombre, valor in (
            ("TG_API_ID", api_id),
            ("TG_API_HASH", api_hash),
            ("TG_USER_SESSION", sesion),
        )
        if not valor
    ]
    if faltan:
        raise RuntimeError(
            f"faltan credenciales de MTProto: {', '.join(faltan)}. "
            "Sin ellas no se pueden archivar archivos de mas de 20 MB."
        )
    return int(api_id), api_hash, sesion


async def upload(
    ruta: str, filename: str, chat_id: str, *, timeout: int = 1800
) -> dict:
    """Sube un archivo por MTProto. Devuelve `message_id` y datos del archivo.

    Se usa `send_file` con `force_document` para que no dependa del formato: un
    video que no cumple las reglas de Telegram -codec, contenedor- se rechazaria
    con `video=True`, y aqui lo que importa es que el archivo llegue y se pueda
    bajar, no que se previsualice.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id, api_hash, sesion = _credenciales()

    cliente = TelegramClient(StringSession(sesion), api_id, api_hash)
    await cliente.connect()
    try:
        if not await cliente.is_user_authorized():
            raise RuntimeError(
                "TG_USER_SESSION no esta autorizada: la sesion fue revocada o "
                "pertenece a otra cuenta. Genera una nueva con scripts/."
            )

        # **La entidad hay que traerla primero.** Telethon no puede resolver un
        # `-100...` que no haya visto antes en esta sesion: lanza `Could not
        # find the input entity`. Por eso se pide explicitamente y se comprueba
        # el acceso antes de subir nada.
        entidad = await cliente.get_entity(int(chat_id))
        logger.info("canal resuelto: %s", getattr(entidad, "title", chat_id))

        tam = os.path.getsize(ruta)
        logger.info("subiendo %s (%s bytes) por MTProto", filename, tam)

        mensaje = await cliente.send_file(
            entity=entidad,
            file=ruta,
            file_name=filename,
            caption=filename,
            # Sin esto Telegram intenta tratarlo como video y rechaza los
            # formatos que no reconoce.
            force_document=True,
        )
        if not mensaje or not mensaje.file:
            raise RuntimeError("Telegram acepto el envio pero no devolvio archivo")

        return {
            "message_id": mensaje.id,
            "size": getattr(mensaje.file, "size", None),
            "bot_alias": None,  # Lo subio un usuario, no un bot del pool.
        }
    finally:
        await cliente.disconnect()


async def verify_downloadable(chat_id: str, message_id: int, esperado: int) -> None:
    """Comprueba que la sesion puede volver a bajar el archivo. Lanza si no.

    **La red que atrapa el fallo silencioso.** Igual que con la Bot API, subir
    bien no garantiza poder bajar: el archivo puede haber quedado en un canal
    del que la sesion ya no es miembro, o con un tamano distinto.

    Se pide el mensaje -no el archivo entero- y se mira su tamano. Una llamada
    barata en vez de bajar 2 GB para comprobarlo.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id, api_hash, sesion = _credenciales()
    cliente = TelegramClient(StringSession(sesion), api_id, api_hash)
    await cliente.connect()
    try:
        entidad = await cliente.get_entity(int(chat_id))
        mensaje = await cliente.get_messages(entidad, ids=message_id)
        if mensaje is None or not mensaje.file:
            raise RuntimeError(
                f"el mensaje {message_id} no existe o no tiene archivo. "
                "El archivo no se podria recuperar."
            )
        real = mensaje.file.size
        if real != esperado:
            raise RuntimeError(
                f"Telegram guardo {real} bytes y se subieron {esperado}: "
                "el archivo quedaria truncado. Se deja en R2."
            )
        logger.info("verificado por MTProto: %s bytes recuperables", real)
    finally:
        await cliente.disconnect()

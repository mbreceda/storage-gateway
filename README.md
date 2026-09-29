# Archivador: de R2 a Telegram

## Qué es esto

Los proyectos suben archivos a R2 por una URL firmada. R2 da 10 GB gratis, y el
plan es usarlos **como sala de espera, no como almacén**: en cuanto el archivo
está subido, este workflow lo baja, lo sube a Telegram, y el backend borra la
copia de R2.

Vive en su propio repo público (`mbreceda/storage-gateway`) y **no comparte
código con ningún proyecto**. Es un script y un workflow.

## Por qué no corre en el backend

El backend vive en Render con **0.1 CPU y 512 MB**. Mover 2 GB ahí dentro lo
deja bloqueado durante minutos, y mientras tanto el servidor no atiende a nadie.

El runner de GitHub tiene **4 vCPU, 15 GB y ffmpeg**, y su trabajo es exactamente
mover bytes. El backend solo coordina: firma la bajada y anota el resultado.

**Los bytes no pasan por el backend en ningún momento.**

## El ciclo

```
proyecto ──PUT──> R2
                    │
       backend ─────┤ POST /storage/api/archive/claim   (token de worker)
                    │   ← devuelve la clave y una URL firmada de R2
                    ▼
      runner ──GET──> R2          (baja el archivo)
      runner ──POST─> Telegram    (sendDocument)
                    │
       backend <────┤ POST /storage/api/archive/{id}/done
                    │   ← borra la copia de R2 y descuenta los bytes
                    ▼
              objeto archivado
```

## El límite que condiciona todo: 20 MB

**La Bot API no sube y baja los mismos tamaños:**

| Operación | Tope |
|---|---|
| `sendDocument` (subir) | 50 MB |
| `getFile` (bajar) | **20 MB** |

Medido: 19 MB baja, 21 MB responde `Bad Request: file is too big`. Y 45 MB sube
sin queja.

**El tope que manda es el de bajada.** Diseñar con el de 50 MB haría esto con un
archivo de 30 MB:

1. Sube a Telegram. OK.
2. El backend borra la copia de R2. **Se pierde el original.**
3. Un usuario pide el archivo. `getFile` dice `file is too big`.
4. El archivo ya no existe en ningún sitio accesible.

Ni un paso falla, y el log dice `archivado`. Por eso `MAX_BOT_API_BYTES` es
**20 MB** y no 50.

Los archivos mayores se quedan en R2: 10 GB gratis y cero coste de salida, y un
video grande es justo el que más conviene tener ahí.

### La red que atrapa el fallo

El tope es una constante, y las constantes cambian: Telegram ha subido sus
límites con los años. Así que además se **comprueba la bajada antes de soltar
R2**.

`_verificar_bajada()` llama a `getFile` —que no baja el archivo, solo pide su
ruta y tamaño, y es donde Telegram aplica el tope— y compara el `file_size`. Si
falla, el objeto se suelta en vez de marcarse archivado.

Cuesta una llamada por archivo. Sin ella, un cambio de límite en Telegram
significaría perder archivos en silencio.

### Para archivos grandes de verdad

Habría que usar MTProto, que sube **y baja** hasta 2 GB. Necesita una sesión de
usuario (`TG_USER_SESSION`), no un bot. Es otro trabajo, y el worker local ya lo
hace.

## Secretos

Todo viene de Infisical por OIDC. **Nada vive en GitHub.**

| Variable | Qué es |
|---|---|
| `WORKER_TOKEN` | El mismo del backend. Autentica las llamadas de coordinación |

**El token del bot y su canal no son variables.** Los entrega `/claim`, resueltos
desde la base del backend: `bot_token` y `chat_id`.

Duplicarlos en Infisical crearía dos fuentes de verdad. Rotar el bot en la base
no rotaría la copia, y el fallo aparecería como `chat not found` —que suena a
permisos— en vez de como lo que es.

Y una **variable de repo** en GitHub, no un secreto:

| Variable | Qué es |
|---|---|
| `BACKEND_URL` | La URL del backend en Render |

### Cómo se elige el bot

El backend resuelve el bot así:

1. El token del proyecto (`storage_tokens.bot_alias`), si tiene uno.
2. Si no, el `bot_alias` del propio objeto.
3. Con ese alias busca en `bots` y descifra `token_encrypted`.

Sale del pool existente. **No hay que crear nada en BotFather.**

Estado verificado: `bot-07` → `@store_cool_agent_6_bot`, canal `storage-07`,
administrador con permiso de publicar.

Si el backend no logra resolver el bot —el alias no existe, no tiene canal, o
`SECRET_KEY` cambió y el token guardado ya no se descifra— `/claim` devuelve
`bot_token: null`. El archivador entonces **suelta el objeto** en vez de
intentar subir, y dice cuáles son los tres motivos posibles.

### El canal tiene que ser el del bot

En la base el canal vive como lo devuelve MTProto -`4059354648`- y la Bot API
contesta `chat not found` con esa forma: necesita `-1004059354648`. El backend
hace la conversión y entrega el id ya listo.

El bot tiene que ser miembro de ese canal con permiso para publicar.

Y el `file_id` **solo sirve con el bot que subió el archivo**. Cambiar el bot de
un proyecto después deja los archivos ya archivados sin poder leerse.

## Cuándo corre

| Disparador | Cuándo |
|---|---|
| `repository_dispatch` | Justo después de un `complete`. El camino normal |
| `schedule` | Cada 30 minutos. La red de seguridad |
| `workflow_dispatch` | A mano |

El dispatch es una **optimización, no el mecanismo**. Si el token falta o GitHub
está caído, el cron recoge lo pendiente igual; solo tarda más.

El cron va cada 30 min y no cada 15 por el cupo de minutos de GitHub (2000/mes
en repo privado; este es público y no gasta).

## Cuántos procesa

`ARCHIVE_MAX_PER_RUN`, por defecto **10**.

Sin tope, una corrida se acercaría al límite de 350 minutos y dejaría el
siguiente lote esperando. Con el cron cada 30 minutos, 10 por corrida son 480
archivos al día.

**Se para al primer fallo.** Seguir cuando Telegram acaba de rechazar uno suele
significar repetir el mismo error diez veces.

## Dos runners a la vez

El reclamo es un **`UPDATE` condicional** en el backend, no un `SELECT` seguido
de `UPDATE`. Con dos consultas, los dos runners leen la misma fila y los dos
archivan el mismo archivo: se paga el ancho de banda dos veces y quedan dos
copias en Telegram.

El segundo ve cero filas y se va con las manos vacías.

Y `claimed_at` con lease de una hora resuelve el otro caso: un runner que muere
a mitad deja la fila marcada, y sin el lease se quedaría atascada para siempre.

## Probar sin secretos

El script se puede correr contra un backend falso. Es lo que se hizo para
verificarlo antes del primer despliegue: un `HTTPServer` que devuelve un objeto
en `/claim`, un archivo en la URL de bajada, y un `sendDocument` falso.

Comprueba los cinco caminos: archivo normal, archivo que pasa de 20 MB,
Telegram rechazando, cola vacía, y sin bot.

## Errores conocidos

| Síntoma | Causa |
|---|---|
| `401 OIDC audience not allowed` | Falta `oidc-audience: infisical` en el action |
| `chat not found` | El bot no es miembro del canal, o falta el prefijo `-100` |
| `el backend no entrego bot para el alias` | El alias no existe en el pool, no tiene canal, o cambió `SECRET_KEY` |
| `file is too big` en `getFile` | El archivo pasa de 20 MB: no se puede bajar |
| `Request Entity Too Large` | El archivo pasa de 50 MB y llegó a Telegram |
| Nada se archiva y el log dice "nada que archivar" | Los objetos están en `pending`: nunca se llamó a `/complete` |

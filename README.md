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

## El límite que condiciona todo

**La Bot API no sube más de 50 MB por `sendDocument`.** Verificado: 51 MB falla.

El script **no trocea**. Un archivo troceado queda partido en Telegram y hay que
recomponerlo en cada descarga, que es peor que no archivarlo.

Si el archivo pasa de 50 MB, se suelta el reclamo con el motivo y **se queda en
R2**. Es lo correcto: R2 tiene 10 GB gratis y **cero coste de salida**, y un
archivo grande es justo el que más conviene tener ahí.

Para archivos grandes de verdad habría que usar MTProto -que sube hasta 2 GB-,
pero eso necesita una sesión de usuario, no un bot, y es otro trabajo.

## Secretos

Todo viene de Infisical por OIDC. **Nada vive en GitHub.**

| Variable | Qué es |
|---|---|
| `WORKER_TOKEN` | El mismo del backend. Autentica las llamadas de coordinación |
| `ARCHIVE_BOT_TOKEN` | Token del bot que sube |
| `ARCHIVE_CHAT_ID` | Canal **de ese bot**, donde publica |
| `ARCHIVE_BOT_ALIAS` | Alias del bot en el pool (`bot-07`) |

Y una **variable de repo** en GitHub, no un secreto:

| Variable | Qué es |
|---|---|
| `BACKEND_URL` | La URL del backend en Render |

### El canal tiene que ser el del bot

`ARCHIVE_CHAT_ID` **no es `TG_FILES_CHAT`**. El bot tiene que ser miembro de ese
canal con permiso para publicar. Con el canal de otro bot, Telegram contesta
`Bad Request: chat not found` aunque el canal exista.

Y el `file_id` **solo sirve con el bot que subió el archivo**. Cambiar
`ARCHIVE_BOT_TOKEN` después deja los archivos ya archivados sin poder leerse.

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

Comprueba los cuatro caminos: archivo normal, archivo que pasa de 50 MB,
Telegram rechazando, y cola vacía.

## Errores conocidos

| Síntoma | Causa |
|---|---|
| `401 OIDC audience not allowed` | Falta `oidc-audience: infisical` en el action |
| `chat not found` | El bot no es miembro del canal, o es el canal de otro bot |
| `Request Entity Too Large` | El archivo pasa de 50 MB y llegó a Telegram |
| Nada se archiva y el log dice "nada que archivar" | Los objetos están en `pending`: nunca se llamó a `/complete` |

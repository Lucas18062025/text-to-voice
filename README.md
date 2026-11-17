# Texto a voz · `text-to-voice`

![Radio DJ hablando al micrófono](Radio_DJ_speaking_into_microphone_fbadaeea_2K.jpg)

**Deploy:** https://text-to-voice-api-ila7.onrender.com/ 
Convertí texto en voz. Hoy hay dos caminos en el repo:

| Modo | Qué hace | Cuándo usarlo |
| --- | --- | --- |
| **Fase 1 — App local** | UI web: escribís, elegís voz (femenina/masculina), escuchás y bajás el MP3 | Demo, uso personal, validar la experiencia |
| **Fase 2 — Serverless** | Subís un `.txt` a S3 y una Lambda lo convierte con Polly y avisa por SNS | Procesamiento por lotes en AWS |

Worker en
Cloudflare (UI + proxy) y la síntesis en Python detrás
([cómo está armado](#deploy-cloudflare-ui--proxy--backend-python)).


---

## Fase 1 — App local (UI web)

### Requisitos

- Python 3.11+
- Conexión a internet (el motor `edge` sintetiza contra los servidores de Microsoft)

### Instalación

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Uso

```powershell
.\run.ps1
```

Abre <http://127.0.0.1:8000> automáticamente. O a mano:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

En la interfaz: escribís el texto → elegís voz (agrupadas en **femeninas** y
**masculinas**) → ajustás la velocidad → **Generar audio**. El reproductor
aparece con el botón **Descargar MP3**. Atajo: `Ctrl` + `Enter`.

### Cómo está armado

```
web/index.html   # UI (HTML/CSS/JS, sin dependencias ni CDN)
app.py           # FastAPI: /, /api/voices, /api/tts, /api/health
tts_engine.py    # catálogo de voces + motores (edge / polly)
run.ps1          # levanta el server y abre el navegador
worker.js        # Worker de Cloudflare: assets + proxy /api/*
demo.ps1         # backend + túnel + deploy + verificación (una sola orden)
```

La UI no sabe qué motor hay detrás: pide `/api/voices` y publica
`POST /api/tts`. Cambiar de motor es cambiar una variable de entorno, no la
interfaz.

### Motores

| Motor | Credenciales | Voces | Uso |
| --- | --- | --- | --- |
| `edge` (default) | Ninguna | 19 (10 F / 9 M), 9 países | Desarrollo y demo |
| `polly` | AWS | 9 (neural, standard y generative) | Producción en AWS |

```powershell
$env:TTS_PROVIDER = "polly"   # cambia el motor activo
```

> **Ojo con `edge` en producción:** usa un endpoint no oficial de Microsoft.
> Está perfecto para desarrollo, pero no es un contrato de servicio: puede
> cambiar o cortarse sin aviso. Para un producto, usá un motor con API oficial.

### API

| Método | Ruta | Descripción |
| --- | --- | --- |
| `GET` | `/` | Interfaz |
| `GET` | `/api/health` | Estado + motor activo + límite de caracteres |
| `GET` | `/api/voices` | Voces del motor activo (con `gender`, `locale`, `country`) |
| `POST` | `/api/tts` | `{text, voice, rate}` → `audio/mpeg` |

Errores: `400` texto vacío o voz inválida, `403` falta el `X-Proxy-Token`
cuando está configurado, `413` texto demasiado largo (>3000 caracteres) o
cuerpo mayor al tope, `422` velocidad fuera de rango, `429` límite de
frecuencia o de síntesis simultáneas, `502` falla del motor.

---

## Deploy: Cloudflare (UI + proxy) + backend Python

```
[Browser] -> Worker (assets + proxy /api/*) -> FastAPI (edge-tts) -> Microsoft
```

El Worker **no ejecuta Python**: sirve `web/` como assets y reenvía `/api/*`
al backend. Como todo pasa por el mismo origen, la UI no cambia y no hace
falta CORS. La URL del backend es `API_ORIGIN` (en `wrangler.toml`, con el
valor de producción por defecto); es una URL pública, no un secreto.

Sitio: <https://text-to-voice.lucaslean1806.workers.dev>

### Modo actual — backend 24/7 en Render (free)

```
[Browser] -> Worker (assets + proxy /api/*) -> Render (FastAPI + edge-tts) -> Microsoft
```

Backend en producción: **<https://text-to-voice-api-ila7.onrender.com>**
(servicio `text-to-voice-api`, Blueprint `text-to-voice` en Render).

1. Render → **New** → **Blueprint** → repo `text-to-voice` (usa `render.yaml`).
2. Copiar la URL del servicio. Render agrega un sufijo si el nombre está tomado.
3. `.\deploy-cloudflare.ps1 -ApiOrigin https://<esa-url>`

Así el sitio funciona sin depender de tu PC: verificado con el túnel y el
backend local apagados (19 voces y un MP3 real salieron igual). El free tier
duerme a los 15 min de inactividad y el primer request tarda ~40 s; el cron del
Worker (`*/10 * * * *`) le pega a `/api/health` cada 10 minutos y lo mantiene
despierto.

> **Caveat honesto:** el cron cubre el letargo, no un cold start provocado por
> una caída de Render. Si el servicio duerme igual, la primera visita puede
> tardar ~40 s (la UI muestra el aviso de "no disponible" y alcanza con
> reintentar).

### Opción B — Túnel a tu PC (solo desarrollo local)

Para probar cambios sin desplegar, o si no querés ningún host externo:

```powershell
.\demo.ps1          # backend + túnel + deploy del Worker + verificación
.\demo.ps1 -Stop    # apaga el backend y el túnel
```

`demo.ps1` es idempotente:

1. Si `127.0.0.1:8000` ya responde `/api/health`, reusa ese backend.
2. Abre un *quick tunnel* de `cloudflared` y extrae la URL `*.trycloudflare.com`.
3. Verifica `/api/health` a través del túnel.
4. Redeploya el Worker con esa URL (`deploy-cloudflare.ps1 -ApiOrigin <url>`).
5. Verifica el sitio público y muestra las tres URLs.

Requiere `cloudflared.exe` en `%USERPROFILE%\.local\bin` (binario portable,
sin instalador ni permisos de administrador).

| Limitación | Detalle |
| --- | --- |
| **No es 24/7** | Si el proceso `cloudflared` muere (PC apagada, suspensión, sesión cerrada), el hostname desaparece |
| La URL **cambia en cada arranque** | Por eso redeployar es parte del arranque |
| `/api/*` responde **503** con mensaje claro | El Worker traduce el `530 / 1016` de Cloudflare a JSON legible |
| No es producción | Demo sin SLA, sin auth, con el endpoint no oficial de `edge` |

### Deploy del Worker

```powershell
npx wrangler login      # una sola vez, abre el navegador
.\deploy-cloudflare.ps1
```

`API_ORIGIN` tiene el backend de producción como default en `wrangler.toml`,
así que **un deploy a secas funciona**. Para apuntar a otro backend:

```powershell
.\deploy-cloudflare.ps1 -ApiOrigin https://<otro-backend>
```

El script ya no termina en el deploy: verifica el origen antes de publicar y
hace un smoke test real después (`/api/health`, `/api/voices` y una síntesis
de punta a punta). Si cualquiera falla, termina con error. Un deploy sin
verificación fue exactamente lo que dejó el sitio en 503.

### Límites y defensas

Todo se configura por entorno, con defaults seguros. No hace falta tocar
código para ajustarlos.

| Variable | Default | Qué hace |
| --- | --- | --- |
| `TTS_MAX_BODY_BYTES` | `32768` | Corta el request por `Content-Length` antes de parsear. Alcanza para 3000 caracteres con el peor escape JSON |
| `TTS_MAX_CONCURRENT` | `4` | Síntesis simultáneas. Pasado el tope responde `429` en vez de encolar |
| `TTS_RATE_LIMIT_PER_MIN` | `60` | Peticiones por cliente y minuto (`0` desactiva) |
| `TTS_DOCS` | apagado | `1` publica `/docs`, `/redoc` y `/openapi.json` |
| `PROXY_TOKEN` | vacío | Si está seteado, `POST /api/tts` exige `X-Proxy-Token`. El Worker lo inyecta solo |

Con `PROXY_TOKEN` configurado en Render **y** en el Worker, el backend deja de
ser un relay público: quien le pegue directo al origen recibe `403`. Es
opcional a propósito — si se setea en un solo lado, la síntesis se rompe, así
que se activa en los dos o en ninguno.

`/api/health` nunca pide token: lo usan el health check de Render y el cron
del Worker. Si pidiera token, Render marcaría el servicio como caído.

El rate limit es por instancia y en memoria: frena abuso desde una IP, no
reemplaza un WAF. El control que no depende de la IP es `TTS_MAX_CONCURRENT`.

### Seguridad

El backend es público por diseño (es una demo), pero acotado. Cada control
tiene una verificación concreta:

| Vector | Control | Cómo se comprueba |
| --- | --- | --- |
| Body gigante | Tope por `Content-Length` antes de parsear | `413` en ~200 ms, sin mencionar el tamaño del texto |
| Ráfaga de síntesis | `TTS_MAX_CONCURRENT` | el request de más devuelve `429` + `Retry-After` |
| Scraping por IP | `TTS_RATE_LIMIT_PER_MIN` | pasado el cupo, `429` |
| Health check saturado | `/api/health` es `async` | responde en milisegundos con todos los slots ocupados |
| Acceso directo al origen | `PROXY_TOKEN` (opcional) | `POST /api/tts` sin el header devuelve `403` |
| Fuga de internals | `502` genérico y `/docs` apagado | sin `str(exc)` y sin Swagger público |

La auditoría completa (código y superficie desplegada, con evidencia) está en
`Portafolio/Auditorías/text-to-voice` del vault de Obsidian.

### Verificación

Las siguientes solicitudes son solo de lectura (`GET`):

```powershell
$baseUrl = 'https://text-to-voice.lucaslean1806.workers.dev'
$origin  = 'https://text-to-voice-api-ila7.onrender.com'

Invoke-RestMethod -Method Get -Uri "$baseUrl/api/health"    # 200 status=ok
Invoke-RestMethod -Method Get -Uri "$baseUrl/api/voices"    # 200, 19 voces
(Invoke-WebRequest "$origin/docs" -SkipHttpErrorCheck).StatusCode   # 404
```

Las dos primeras deben dar 200. Si `/api/health` da **503**, el backend no
responde (servicio dormido, túnel cerrado, `API_ORIGIN` viejo) o está en cold
start: el cuerpo trae `code: upstream_timeout` o `upstream_unreachable`.

La tercera es el control de que el backend desplegado es el actual: **si
`/docs` da 200, el origen quedó con código viejo** y hay que redesplegar
(Render → **Manual Deploy** → **Deploy latest commit**). `autoDeploy` depende
de cómo se creó el servicio; no siempre está activo.

### Otros hosts

`Dockerfile` incluido para Fly.io, Koyeb o cualquier host de contenedores:
`docker build -t text-to-voice . && docker run -p 8000:8000 text-to-voice`.

---

## Fase 2 — Serverless (S3 + Lambda + Polly + SNS)

Subís un `.txt` a un bucket de S3 y, sin que nadie llame a nada a mano, una
Lambda lo convierte en audio con Amazon Polly, guarda el `.mp3` en otro
bucket y avisa por mail con SNS.

```
[.txt] -> S3 (input) --ObjectCreated--> Lambda --> Polly --> S3 (output, .mp3)
                                           \--> SNS --> mail "Audio listo"
```

- **Orientada a eventos:** S3 emite `ObjectCreated` y dispara la Lambda.
- **Asincrónica:** S3 invoca la Lambda con `InvocationType=Event` y no espera
  la respuesta. Si falla, Lambda reintenta 2 veces.
- **IA como servicio:** Polly (voz neural `Lupe`, es-US) hace la síntesis.

### Decisiones de seguridad y costo

| Riesgo | Defensa |
| --- | --- |
| Loop infinito (la Lambda escribe en el bucket que la dispara) | Buckets separados, filtro `.txt` en el trigger, validación en código y límite de concurrencia |
| Permisos excesivos | Rol con menor privilegio: lee `input`, escribe `output`, Polly y un topic SNS |
| Secretos en el repo | Nada de keys: perfil de CLI y variables de entorno |
| Costos | Alarma de billing + limpieza al final |

### Despliegue (PowerShell)

1. Buckets `tts-input-<account>` y `tts-output-<account>`.
2. Topic SNS `tts-audio-ready` y suscripción por mail (confirmar el link).
3. Rol IAM `tts-lambda-role` con política de menor privilegio.
4. Lambda `tts-lambda` (python3.12, timeout 30 s) con variables
   `OUTPUT_BUCKET`, `TOPIC_ARN`, `VOICE_ID`.
5. Permiso para que S3 invoque la Lambda y notificación con filtro `.txt`.
6. Concurrencia reservada:
   `aws lambda put-function-concurrency --function-name tts-lambda --reserved-concurrent-executions 2`

---

## Escalado: de demo a producto

| Etapa | Arquitectura | Límite práctico | Costo |
| --- | --- | --- | --- |
| **0 · hoy** | FastAPI local + `edge` | 1 usuario, tu máquina | $0 |
| **1 · compartir** | El mismo FastAPI en Fly.io/Render/VPS + Cloudflare delante | decenas de usuarios | ~$5–10/mes |
| **2 · serverless** | API Gateway + Lambda (`app.py` ya es compatible) + motor oficial + S3/CloudFront | miles, escala a cero | por caracteres |
| **3 · producto** | + cola para textos largos, cache por hash, cuotas por usuario, auth | ilimitado | + storage/egress |

### Decisiones que importan al escalar

1. **Motor con API oficial.** Las voces `es-AR-Elena` y `es-AR-Tomas` que usa
   la app son de Microsoft: **Azure Speech** las expone por API oficial (capa
gratis mensual) y es el salto más directo desde `edge` sin perder calidad.
   Amazon **Polly** es la opción si querés quedarte en AWS. `generative` de
   Polly y ElevenLabs suben la calidad y el precio.
2. **Cache por hash.** Guardá el MP3 con clave `sha256(texto|voz|rate)`. En un
   producto real la mayoría de los pedidos se repiten: es el ahorro más grande
   y es una tarde de trabajo.
3. **Textos largos.** Polly corta en ~3000 caracteres. Para más, chunking por
   párrafos concatenando MP3, o `start_speech_synthesis_task` (asincrónico,
   escribe directo a S3).
4. **Cola en vez de request largo.** Sintetizar 10 minutos de audio no entra en
   un request HTTP cómodo: encolás (SQS), devolvés `job_id`, y el cliente
   consulta o recibe webhook.
5. **Auth y cuotas antes de abrir la API.** Sin cuota por usuario, un script
   ajeno te consume el presupuesto en horas.
6. **IaC.** SAM o Terraform para no repetir la consola a mano.

---

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

- `test_handler.py` — Lambda (S3/Polly/SNS mockeados).
- `test_app.py` — API y validaciones (motor mockeado, sin red), incluidos los
  límites anti-abuso: body máximo (también en bodies chunked), concurrencia,
  rate limit, token de proxy, `/docs` apagado y que el `502` no filtre el
  detalle interno.

Casos manuales de la Lambda:

1. Camino feliz: `.txt` corto → mp3 + mail.
2. Nombre con espacios o acentos (valida `unquote_plus`).
3. Texto de más de 3000 caracteres → se rechaza y queda en logs.
4. Archivo que no es `.txt` → no dispara la Lambda.

## Limpieza (AWS)

Borrar Lambda, buckets (`--force`), topic SNS y rol IAM para no pagar de más.

# Texto a voz · `text-to-voice`

Convertí texto en voz. Hoy hay dos caminos en el repo:

| Modo | Qué hace | Cuándo usarlo |
| --- | --- | --- |
| **Fase 1 — App local** | UI web: escribís, elegís voz (femenina/masculina), escuchás y bajás el MP3 | Demo, uso personal, validar la experiencia |
| **Fase 2 — Serverless** | Subís un `.txt` a S3 y una Lambda lo convierte con Polly y avisa por SNS | Procesamiento por lotes en AWS |

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

Errores: `400` texto vacío o voz inválida, `413` texto demasiado largo
(>3000 caracteres), `422` velocidad fuera de rango, `502` falla del motor.

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
| Loop infinito (la Lambda escribe en el bucket que la dispara) | 1. Dos buckets separados. 2. Filtro por sufijo `.txt` en el trigger. 3. Guarda en el código (`should_process`). 4. Concurrencia reservada = 2 |
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
   gratuita mensual) y es el salto más directo desde `edge` sin perder calidad.
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
- `test_app.py` — API y validaciones (motor mockeado, sin red).

Casos manuales de la Lambda:

1. Camino feliz: `.txt` corto → mp3 + mail.
2. Nombre con espacios o acentos (valida `unquote_plus`).
3. Texto de más de 3000 caracteres → se rechaza y queda en logs.
4. Archivo que no es `.txt` → no dispara la Lambda.

## Limpieza (AWS)

Borrar Lambda, buckets (`--force`), topic SNS y rol IAM para no pagar de más.

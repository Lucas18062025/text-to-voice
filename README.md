# Texto a audio con Lambda asincrónico (S3 + Lambda + Polly + SNS)

Subís un `.txt` a un bucket de S3 y, sin que nadie llame a nada a mano, una
Lambda lo convierte en audio con Amazon Polly, guarda el `.mp3` en otro
bucket y avisa por mail con SNS.

## Arquitectura

```
[.txt] -> S3 (input) --ObjectCreated--> Lambda --> Polly --> S3 (output, .mp3)
                                           \--> SNS --> mail "Audio listo"
```

- **Orientada a eventos:** S3 emite `ObjectCreated` y dispara la Lambda.
- **Asincrónica:** S3 invoca la Lambda con `InvocationType=Event` y no espera
  la respuesta. Si falla, Lambda reintenta 2 veces.
- **IA como servicio:** Polly (voz neural `Lupe`, es-US) hace la síntesis.

## Decisiones de seguridad y costo

| Riesgo | Defensa |
| --- | --- |
| Loop infinito (la Lambda escribe en el bucket que la dispara) | 1. Dos buckets separados. 2. Filtro por sufijo `.txt` en el trigger. 3. Guarda en el código (`should_process`). 4. Concurrencia reservada = 2 |
| Permisos excesivos | Rol con menor privilegio: lee `input`, escribe `output`, Polly y un topic SNS |
| Secretos en el repo | Nada de keys: perfil de CLI y variables de entorno |
| Costos | Alarma de billing + limpieza al final |

## Estructura

```
handler.py        # código de la Lambda
test_handler.py   # tests unitarios (mocks, sin AWS real)
```

## Despliegue (PowerShell)

Los pasos numerados son los mismos que se hicieron en la consola y el CLI:

1. Buckets `tts-input-<account>` y `tts-output-<account>`.
2. Topic SNS `tts-audio-ready` y suscripción por mail (confirmar el link).
3. Rol IAM `tts-lambda-role` con política de menor privilegio.
4. Lambda `tts-lambda` (python3.12, timeout 30 s) con variables
   `OUTPUT_BUCKET`, `TOPIC_ARN`, `VOICE_ID`.
5. Permiso para que S3 invoque la Lambda y notificación con filtro `.txt`.
6. Concurrencia reservada:
   `aws lambda put-function-concurrency --function-name tts-lambda --reserved-concurrent-executions 2`

## Pruebas

```powershell
python -m pytest -q
```

Casos manuales a documentar con captura:

1. Camino feliz: `.txt` corto -> mp3 + mail.
2. Nombre con espacios o acentos (valida `unquote_plus`).
3. Texto de más de 3000 caracteres -> se rechaza y queda en logs.
4. Archivo que no es `.txt` -> no dispara la Lambda.

## Limitaciones y v2

- `synthesize_speech` acepta ~3000 caracteres; para textos largos usar
  `start_speech_synthesis_task` (asincrónico, escribe directo a S3).
- Pasar todo a IaC (AWS SAM o Terraform).
- Tests de integración con `moto`.

## Limpieza

Borrar Lambda, buckets (`--force`), topic SNS y rol IAM para no pagar de más.

"""App web: escribís texto, elegís voz y bajás el MP3.

Levantar con:
    .\\.venv\\Scripts\\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000

Defensas contra abuso (todas por variable de entorno, con default seguro):

======================  =====================================================
``TTS_MAX_BODY_BYTES``  Cuerpo máximo aceptado. Default 32 KB: alcanza para
                        3000 caracteres con el peor escape JSON posible
                        (3000 x 6 bytes) y evita que un body de megabytes
                        llegue siquiera a parsearse.
``TTS_MAX_CONCURRENT``  Síntesis simultáneas antes de responder 429.
``TTS_RATE_LIMIT_PER_MIN``  Peticiones por cliente y minuto. ``0`` desactiva.
``TTS_DOCS``            ``1`` publica /docs, /redoc y /openapi.json. Default
                        apagado: un backend público no necesita Swagger.
``PROXY_TOKEN``         Si está seteado, ``POST /api/tts`` exige el header
                        ``X-Proxy-Token``. Cierra el abuso directo al origen
                        cuando el Worker es el único front door. No afecta
                        ``/api/health``, así el health check de la plataforma
                        y el cron del Worker siguen funcionando.
======================  =====================================================
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

import tts_engine

logger = logging.getLogger("tts.app")

WEB_DIR = Path(__file__).parent / "web"


def _env_int(name: str, default: int) -> int:
    """Lee un entero del entorno sin romper el arranque si viene mal."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


MAX_BODY_BYTES = _env_int("TTS_MAX_BODY_BYTES", 32 * 1024)
MAX_CONCURRENT_SYNTH = _env_int("TTS_MAX_CONCURRENT", 4)
RATE_LIMIT_PER_MIN = _env_int("TTS_RATE_LIMIT_PER_MIN", 60)
DOCS_ENABLED = os.environ.get("TTS_DOCS", "0") == "1"
PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "").strip()

PROTECTED_PATH = "/api/tts"


class RateLimiter:
    """Ventana fija por cliente, en memoria.

    Es una sola instancia: el estado no se comparte entre procesos ni se
    reinicia con el servidor. Frena abuso desde una IP, no es un sistema
    de facturación.
    """

    MAX_TRACKED_CLIENTS = 10_000

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, tuple[float, int]] = {}

    def allow(self, client: str) -> bool:
        """Consume una unidad del cupo del cliente."""
        if self.limit <= 0:
            return True

        now = time.monotonic()
        with self._lock:
            entry = self._hits.get(client)
            if entry is None and len(self._hits) >= self.MAX_TRACKED_CLIENTS:
                self._purge_locked(now)
                if len(self._hits) >= self.MAX_TRACKED_CLIENTS:
                    # Techo de memoria alcanzado: se rechaza lo nuevo en vez
                    # de crecer sin límite con IPs rotadas por el atacante.
                    return False

            start, count = entry if entry is not None else (now, 0)
            if now - start >= self.window_seconds:
                self._hits[client] = (now, 1)
                return True
            if count >= self.limit:
                return False
            self._hits[client] = (start, count + 1)
            return True

    def _purge_locked(self, now: float) -> None:
        stale = [
            key
            for key, (start, _count) in self._hits.items()
            if now - start >= self.window_seconds
        ]
        for key in stale:
            del self._hits[key]


class ConcurrencyGate:
    """Tope de síntesis en vuelo, independiente de la IP.

    Es el control que de verdad protege el threadpool: un atacante con mil
    IPs sigue limitado por este número, no por el rate limit.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        """Reserva un slot. ``False`` si no queda ninguno libre."""
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        """Libera un slot reservado."""
        with self._lock:
            self._active = max(0, self._active - 1)


def _client_key(scope: dict) -> str:
    """Identifica al cliente para el rate limit.

    Detrás de Cloudflare ``cf-connecting-ip`` lo escribe el edge y no se
    puede falsificar. Sin Cloudflare se usa el último salto de
    ``x-forwarded-for`` (el que agregó el proxy más cercano).
    """
    headers: dict[bytes, bytes] = dict(scope.get("headers") or [])
    cf_ip = headers.get(b"cf-connecting-ip")
    if cf_ip:
        return cf_ip.decode("latin-1").strip()

    forwarded = headers.get(b"x-forwarded-for")
    if forwarded:
        return forwarded.decode("latin-1").split(",")[-1].strip()

    client = scope.get("client")
    return client[0] if client else "desconocido"


async def _send_json(
    send: Callable, status: int, payload: dict, headers: dict[str, str] | None = None
) -> None:
    """Responde JSON sin pasar por FastAPI (corta antes de parsear)."""
    body = json.dumps(payload).encode("utf-8")
    raw_headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))

    await send({"type": "http.response.start", "status": status, "headers": raw_headers})
    await send({"type": "http.response.body", "body": body})


class RequestGuard:
    """ASGI puro: valida token, tamaño y frecuencia antes de parsear nada.

    Se aplica solo a ``POST /api/tts``, el único endpoint que consume CPU
    y servicios externos. ``/api/health`` queda libre a propósito: lo usan
    el health check de la plataforma y el cron del Worker.
    """

    def __init__(
        self,
        app: Callable,
        *,
        max_body_bytes: int = MAX_BODY_BYTES,
        limiter: RateLimiter | None = None,
        proxy_token: str = PROXY_TOKEN,
        protected_path: str = PROTECTED_PATH,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.limiter = limiter if limiter is not None else RateLimiter(RATE_LIMIT_PER_MIN)
        self.proxy_token = proxy_token
        self.protected_path = protected_path

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http" or not self._is_protected(scope):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])

        if self.proxy_token:
            supplied = headers.get(b"x-proxy-token", b"").decode("latin-1").strip()
            if supplied != self.proxy_token:
                await _send_json(
                    send,
                    403,
                    {"detail": "Esta API solo acepta tráfico del proxy autorizado."},
                )
                return

        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                declared_size = 0
            if declared_size > self.max_body_bytes:
                await self._reject_too_large(send)
                return

        if not self.limiter.allow(_client_key(scope)):
            await _send_json(
                send,
                429,
                {"detail": "Demasiadas peticiones. Esperá un minuto y reintentá."},
                headers={"Retry-After": "60"},
            )
            return

        # Sin Content-Length (chunked) el tope se aplica mientras se lee.
        # Se lanza HTTPException y no una excepción propia: FastAPI tiene un
        # ``except Exception`` genérico en el parseo del body que convierte
        # cualquier otra cosa en un 400 sin explicación.
        received = 0
        max_bytes = self.max_body_bytes

        async def counting_receive() -> dict:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise HTTPException(status_code=413, detail=self._too_large_detail())
            return message

        await self.app(scope, receive=counting_receive, send=send)

    def _is_protected(self, scope: dict) -> bool:
        return (
            scope.get("method") == "POST"
            and scope.get("path") == self.protected_path
        )

    def _too_large_detail(self) -> str:
        """Mensaje único para los dos caminos de rechazo por tamaño."""
        return (
            "El cuerpo de la petición supera el máximo permitido "
            f"({self.max_body_bytes // 1024} KB)."
        )

    async def _reject_too_large(self, send: Callable) -> None:
        await _send_json(send, 413, {"detail": self._too_large_detail()})


api = FastAPI(
    title="Texto a audio",
    version="1.0.0",
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)

gate = ConcurrencyGate(MAX_CONCURRENT_SYNTH)


class TtsRequest(BaseModel):
    """Cuerpo de POST /api/tts."""

    text: str = Field(..., description="Texto a sintetizar")
    voice: str = Field(..., description="ID de voz (ver /api/voices)")
    rate: int = Field(100, ge=tts_engine.MIN_RATE, le=tts_engine.MAX_RATE)


@api.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Sirve la interfaz."""
    return FileResponse(WEB_DIR / "index.html")


@api.get("/api/health")
async def health() -> dict:
    """Estado del servicio y motor activo.

    Es ``async`` a propósito: si fuera síncrono competiría por el threadpool
    con las síntesis y el health check de la plataforma fallaría justo
    cuando el servicio está saturado.
    """
    return {
        "status": "ok",
        "provider": tts_engine.active_provider(),
        "max_chars": tts_engine.MAX_CHARS,
    }


@api.get("/api/voices")
async def list_voices() -> dict:
    """Voces del motor activo, agrupables por género en el cliente."""
    provider = tts_engine.active_provider()
    return {
        "provider": provider,
        "max_chars": tts_engine.MAX_CHARS,
        "voices": [voice.as_dict() for voice in tts_engine.voices(provider)],
    }


@api.post("/api/tts")
async def text_to_speech(req: TtsRequest) -> Response:
    """Devuelve el MP3 del texto pedido."""
    if not gate.try_acquire():
        raise HTTPException(
            status_code=429,
            detail="El servicio está procesando otras síntesis. Reintentá en unos segundos.",
            headers={"Retry-After": "5"},
        )

    try:
        # La síntesis es bloqueante (edge-tts usa asyncio.run adentro y Polly
        # es HTTP síncrono): va a un hilo para no congelar el event loop.
        audio = await asyncio.to_thread(
            tts_engine.synthesize, req.text, req.voice, req.rate
        )
    except tts_engine.TextTooLongError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (tts_engine.EmptyTextError, tts_engine.UnknownVoiceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except tts_engine.TtsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception:
        # El detalle de la excepción queda en el log, no en la respuesta:
        # puede incluir rutas, hostnames o credenciales del proveedor.
        logger.exception("La síntesis falló")
        raise HTTPException(
            status_code=502, detail="El motor de voz falló. Reintentá en unos segundos."
        ) from None
    finally:
        gate.release()

    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": 'inline; filename="audio.mp3"',
            "Cache-Control": "no-store",
            "X-Audio-Bytes": str(len(audio)),
        },
    )


# `app` es el ASGI que se sirve: FastAPI envuelto por las defensas.
# Se envuelve a mano (en vez de add_middleware) para que los tests puedan
# ajustar límites sobre la instancia real.
app = RequestGuard(api)

"""App web local: escribís texto, elegís voz y bajás el MP3.

Levantar con:
    .\\.venv\\Scripts\\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

import tts_engine

logger = logging.getLogger("tts.app")

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="Texto a audio", version="1.0.0")


class TtsRequest(BaseModel):
    """Cuerpo de POST /api/tts."""

    text: str = Field(..., description="Texto a sintetizar")
    voice: str = Field(..., description="ID de voz (ver /api/voices)")
    rate: int = Field(100, ge=tts_engine.MIN_RATE, le=tts_engine.MAX_RATE)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Sirve la interfaz."""
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    """Estado del servicio y motor activo."""
    return {
        "status": "ok",
        "provider": tts_engine.active_provider(),
        "max_chars": tts_engine.MAX_CHARS,
    }


@app.get("/api/voices")
def list_voices() -> dict:
    """Voces del motor activo, agrupables por género en el cliente."""
    provider = tts_engine.active_provider()
    return {
        "provider": provider,
        "max_chars": tts_engine.MAX_CHARS,
        "voices": [voice.as_dict() for voice in tts_engine.voices(provider)],
    }


@app.post("/api/tts")
def text_to_speech(req: TtsRequest) -> Response:
    """Devuelve el MP3 del texto pedido."""
    try:
        audio = tts_engine.synthesize(req.text, req.voice, req.rate)
    except tts_engine.TextTooLongError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (tts_engine.EmptyTextError, tts_engine.UnknownVoiceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except tts_engine.TtsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - se traduce a un 502 legible
        logger.exception("La síntesis falló")
        raise HTTPException(
            status_code=502, detail=f"El motor de voz falló: {exc}"
        ) from exc

    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": 'inline; filename="audio.mp3"',
            "Cache-Control": "no-store",
            "X-Audio-Bytes": str(len(audio)),
        },
    )

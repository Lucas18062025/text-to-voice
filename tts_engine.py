"""Capa de síntesis de voz: dos motores detrás de una sola interfaz.

- ``edge``: voces neuronales de Microsoft Edge. Sin credenciales, ideal para
  desarrollo y demo local.
- ``polly``: Amazon Polly. El motor de producción (requiere credenciales AWS).

El motor activo se elige con la variable de entorno ``TTS_PROVIDER``
(``edge`` por defecto).
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from xml.sax.saxutils import escape

# Límite de Polly ``synthesize_speech``; el mismo tope se aplica a ambos
# motores para que el comportamiento sea predecible.
MAX_CHARS = 3000

MIN_RATE = 75
MAX_RATE = 200


class TtsError(Exception):
    """Fallo esperado y mostrable al usuario."""


class EmptyTextError(TtsError):
    """El texto está vacío."""


class TextTooLongError(TtsError):
    """El texto supera ``MAX_CHARS``."""


class UnknownVoiceError(TtsError):
    """La voz no existe o no pertenece al motor activo."""


@dataclass(frozen=True)
class Voice:
    """Una voz disponible en un motor."""

    id: str
    name: str
    gender: str  # "F" o "M"
    locale: str
    country: str
    provider: str  # "edge" o "polly"
    engine: str = "neural"

    def as_dict(self) -> dict:
        """Serializa la voz para la API."""
        return {
            "id": self.id,
            "name": self.name,
            "gender": self.gender,
            "locale": self.locale,
            "country": self.country,
            "provider": self.provider,
            "engine": self.engine,
        }


def _edge(voice_id: str, name: str, gender: str, locale: str, country: str) -> Voice:
    return Voice(voice_id, name, gender, locale, country, "edge")


def _polly(voice_id: str, name: str, gender: str, locale: str, country: str, engine: str) -> Voice:
    return Voice(voice_id, name, gender, locale, country, "polly", engine)


VOICES: tuple[Voice, ...] = (
    # --- Microsoft Edge (desarrollo / demo, sin credenciales) ---
    _edge("es-AR-ElenaNeural", "Elena", "F", "es-AR", "Argentina"),
    _edge("es-AR-TomasNeural", "Tomás", "M", "es-AR", "Argentina"),
    _edge("es-MX-DaliaNeural", "Dalia", "F", "es-MX", "México"),
    _edge("es-MX-JorgeNeural", "Jorge", "M", "es-MX", "México"),
    _edge("es-ES-ElviraNeural", "Elvira", "F", "es-ES", "España"),
    _edge("es-ES-AlvaroNeural", "Álvaro", "M", "es-ES", "España"),
    _edge("es-ES-XimenaNeural", "Ximena", "F", "es-ES", "España"),
    _edge("es-CO-SalomeNeural", "Salomé", "F", "es-CO", "Colombia"),
    _edge("es-CO-GonzaloNeural", "Gonzalo", "M", "es-CO", "Colombia"),
    _edge("es-CL-CatalinaNeural", "Catalina", "F", "es-CL", "Chile"),
    _edge("es-CL-LorenzoNeural", "Lorenzo", "M", "es-CL", "Chile"),
    _edge("es-PE-CamilaNeural", "Camila", "F", "es-PE", "Perú"),
    _edge("es-PE-AlexNeural", "Álex", "M", "es-PE", "Perú"),
    _edge("es-UY-ValentinaNeural", "Valentina", "F", "es-UY", "Uruguay"),
    _edge("es-UY-MateoNeural", "Mateo", "M", "es-UY", "Uruguay"),
    _edge("es-VE-PaolaNeural", "Paola", "F", "es-VE", "Venezuela"),
    _edge("es-VE-SebastianNeural", "Sebastián", "M", "es-VE", "Venezuela"),
    _edge("es-US-PalomaNeural", "Paloma", "F", "es-US", "EE. UU."),
    _edge("es-US-AlonsoNeural", "Alonso", "M", "es-US", "EE. UU."),
    # --- Amazon Polly (producción) ---
    _polly("Lupe", "Lupe", "F", "es-US", "EE. UU.", "neural"),
    _polly("Penelope", "Penélope", "F", "es-US", "EE. UU.", "neural"),
    _polly("Miguel", "Miguel", "M", "es-US", "EE. UU.", "neural"),
    _polly("Mia", "Mía", "F", "es-MX", "México", "neural"),
    _polly("Lucia", "Lucía", "F", "es-ES", "España", "neural"),
    _polly("Sergio", "Sergio", "M", "es-ES", "España", "neural"),
    _polly("Andres", "Andrés", "M", "es-MX", "México", "standard"),
    _polly("Alba", "Alba", "F", "es-ES", "España", "generative"),
    _polly("Raul", "Raúl", "M", "es-ES", "España", "generative"),
)

_BY_ID: dict[str, Voice] = {v.id: v for v in VOICES}


def active_provider() -> str:
    """Motor en uso según ``TTS_PROVIDER``."""
    return os.environ.get("TTS_PROVIDER", "edge").strip().lower()


def voices(provider: str | None = None) -> list[Voice]:
    """Voces del motor indicado (o del activo)."""
    target = provider or active_provider()
    return [v for v in VOICES if v.provider == target]


def get_voice(voice_id: str) -> Voice | None:
    """Busca una voz por id."""
    return _BY_ID.get(voice_id)


def _validate(text: str, voice_id: str) -> tuple[Voice, str]:
    voice = _BY_ID.get(voice_id)
    if voice is None or voice.provider != active_provider():
        raise UnknownVoiceError(
            f"La voz '{voice_id}' no está disponible en el motor "
            f"'{active_provider()}'."
        )
    clean = text.strip()
    if not clean:
        raise EmptyTextError("Escribí algo de texto para convertir.")
    if len(text) > MAX_CHARS:
        raise TextTooLongError(
            f"El texto tiene {len(text)} caracteres y el máximo es {MAX_CHARS}."
        )
    return voice, text


def _clamp_rate(rate_percent: int) -> int:
    return max(MIN_RATE, min(MAX_RATE, int(rate_percent)))


def _edge_synthesize(text: str, voice: Voice, rate_percent: int) -> bytes:
    import edge_tts

    rate = f"{rate_percent - 100:+d}%"

    async def collect() -> bytes:
        communicate = edge_tts.Communicate(text, voice=voice.id, rate=rate)
        buffer = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buffer.extend(chunk["data"])
        return bytes(buffer)

    audio = asyncio.run(collect())
    if not audio:
        raise TtsError("El motor no devolvió audio para ese texto.")
    return audio


def _polly_synthesize(text: str, voice: Voice, rate_percent: int) -> bytes:
    import boto3

    client = boto3.client("polly")
    params: dict = {"OutputFormat": "mp3", "VoiceId": voice.id, "Engine": voice.engine}
    if rate_percent == 100:
        params["Text"] = text
        params["TextType"] = "text"
    else:
        params["Text"] = (
            f'<speak><prosody rate="{rate_percent}%">{escape(text)}</prosody></speak>'
        )
        params["TextType"] = "ssml"

    response = client.synthesize_speech(**params)
    return response["AudioStream"].read()


def synthesize(text: str, voice_id: str, rate_percent: int = 100) -> bytes:
    """Convierte ``text`` a MP3 usando la voz pedida.

    Raises:
        EmptyTextError: si el texto está vacío.
        TextTooLongError: si supera ``MAX_CHARS``.
        UnknownVoiceError: si la voz no existe o es de otro motor.
        TtsError: si el motor falla.
    """
    voice, clean = _validate(text, voice_id)
    rate = _clamp_rate(rate_percent)
    if voice.provider == "edge":
        return _edge_synthesize(clean, voice, rate)
    return _polly_synthesize(clean, voice, rate)

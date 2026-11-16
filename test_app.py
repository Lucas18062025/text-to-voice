"""Tests de la app web (el motor de voz se mockea, sin red ni AWS)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import tts_engine
from app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    """Fija el motor en edge para que los tests sean deterministas."""
    monkeypatch.setenv("TTS_PROVIDER", "edge")


def test_health_reports_provider():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "provider": "edge", "max_chars": 3000}


def test_index_serves_html():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_voices_include_both_genders_and_only_active_provider():
    data = client.get("/api/voices").json()
    assert data["provider"] == "edge"
    genders = {voice["gender"] for voice in data["voices"]}
    assert genders == {"F", "M"}
    assert all(voice["provider"] == "edge" for voice in data["voices"])


def test_tts_rejects_empty_text():
    response = client.post("/api/tts", json={"text": "   ", "voice": "es-AR-ElenaNeural"})
    assert response.status_code == 400


def test_tts_rejects_unknown_voice():
    response = client.post("/api/tts", json={"text": "hola", "voice": "no-existe"})
    assert response.status_code == 400


def test_tts_rejects_voice_from_other_provider():
    response = client.post("/api/tts", json={"text": "hola", "voice": "Lupe"})
    assert response.status_code == 400


def test_tts_rejects_long_text():
    payload = {"text": "x" * (tts_engine.MAX_CHARS + 1), "voice": "es-AR-ElenaNeural"}
    assert client.post("/api/tts", json=payload).status_code == 413


def test_tts_rejects_out_of_range_rate():
    payload = {"text": "hola", "voice": "es-AR-ElenaNeural", "rate": 500}
    assert client.post("/api/tts", json=payload).status_code == 422


def test_tts_returns_mpeg_audio(monkeypatch):
    monkeypatch.setattr(tts_engine, "synthesize", lambda text, voice, rate=100: b"ID3-fake")
    response = client.post(
        "/api/tts",
        json={"text": "Hola ñandú", "voice": "es-AR-ElenaNeural", "rate": 110},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["x-audio-bytes"] == "8"
    assert response.content == b"ID3-fake"


def test_tts_maps_engine_failure_to_502(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("sin red")

    monkeypatch.setattr(tts_engine, "synthesize", boom)
    response = client.post("/api/tts", json={"text": "hola", "voice": "es-AR-ElenaNeural"})
    assert response.status_code == 502


def test_engine_validation_helpers():
    with pytest.raises(tts_engine.EmptyTextError):
        tts_engine.synthesize("  ", "es-AR-ElenaNeural")
    with pytest.raises(tts_engine.TextTooLongError):
        tts_engine.synthesize("x" * (tts_engine.MAX_CHARS + 1), "es-AR-ElenaNeural")
    with pytest.raises(tts_engine.UnknownVoiceError):
        tts_engine.synthesize("hola", "voz-inventada")


def test_engine_clamps_rate():
    assert tts_engine._clamp_rate(10) == tts_engine.MIN_RATE
    assert tts_engine._clamp_rate(999) == tts_engine.MAX_RATE
    assert tts_engine._clamp_rate(120) == 120

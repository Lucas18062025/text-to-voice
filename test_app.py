"""Tests de la app web (el motor de voz se mockea, sin red ni AWS)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module
import tts_engine
from app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    """Fija el motor en edge y deja las defensas en modo permisivo.

    Los tests de límites los ajustan explícitamente; el resto corre sin
    cupo para que el orden de ejecución no los haga intermitentes.
    """
    monkeypatch.setenv("TTS_PROVIDER", "edge")
    monkeypatch.setattr(app_module.app.limiter, "limit", 0)
    monkeypatch.setattr(app_module.app.limiter, "_hits", {})
    monkeypatch.setattr(app_module.gate, "limit", 4)
    monkeypatch.setattr(app_module.app, "proxy_token", "")
    monkeypatch.setattr(app_module.gate, "_active", 0)


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


def test_tts_502_does_not_leak_exception_detail(monkeypatch):
    """El detalle interno va al log, nunca al cliente."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("credenciales de AWS invalidas en arn:aws:iam::123")

    monkeypatch.setattr(tts_engine, "synthesize", boom)
    response = client.post("/api/tts", json={"text": "hola", "voice": "es-AR-ElenaNeural"})
    assert response.status_code == 502
    assert "arn:aws" not in response.text
    assert "credenciales" not in response.text


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


# --- Defensas contra abuso -------------------------------------------------


def test_docs_disabled_by_default():
    """Sin TTS_DOCS=1 no se publica Swagger ni el schema."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_body_limit_rejects_before_parsing():
    """Un body gigante se corta por Content-Length, sin parsearlo.

    La prueba de que no se parseó: el mensaje no menciona cuántos
    caracteres traía el texto (eso solo se sabe después de leerlo).
    """
    payload = {"text": "x" * 100_000, "voice": "es-AR-ElenaNeural"}
    response = client.post("/api/tts", json=payload)

    assert response.status_code == 413
    assert "100000" not in response.text
    assert "caracteres" not in response.text
    assert "KB" in response.text


def test_body_limit_caps_chunked_bodies():
    """Sin Content-Length el tope se aplica mientras se lee."""
    chunk = b"x" * 8192
    response = client.post(
        "/api/tts",
        content=iter([chunk] * 8),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_concurrency_gate_returns_429(monkeypatch):
    """Con todos los slots ocupados responde 429 en vez de encolar."""
    monkeypatch.setattr(app_module.gate, "limit", 1)
    assert app_module.gate.try_acquire() is True

    response = client.post("/api/tts", json={"text": "hola", "voice": "es-AR-ElenaNeural"})

    app_module.gate.release()
    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"


def test_concurrency_gate_releases_after_success(monkeypatch):
    monkeypatch.setattr(tts_engine, "synthesize", lambda text, voice, rate=100: b"ID3")
    for _ in range(5):
        assert client.post(
            "/api/tts", json={"text": "hola", "voice": "es-AR-ElenaNeural"}
        ).status_code == 200
    assert app_module.gate._active == 0


def test_health_responds_while_all_slots_are_busy(monkeypatch):
    """El health check no compite por los slots de síntesis."""
    monkeypatch.setattr(app_module.gate, "limit", 2)
    while app_module.gate.try_acquire():
        pass

    response = client.get("/api/health")

    for _ in range(2):
        app_module.gate.release()
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_rate_limit_returns_429(monkeypatch):
    monkeypatch.setattr(app_module.app.limiter, "limit", 2)
    monkeypatch.setattr(app_module.app.limiter, "_hits", {})
    monkeypatch.setattr(tts_engine, "synthesize", lambda text, voice, rate=100: b"ID3")

    body = {"text": "hola", "voice": "es-AR-ElenaNeural"}
    assert client.post("/api/tts", json=body).status_code == 200
    assert client.post("/api/tts", json=body).status_code == 200
    blocked = client.post("/api/tts", json=body)

    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"


def test_proxy_token_blocks_direct_access(monkeypatch):
    """Con PROXY_TOKEN seteado, el origen rechaza al que no viene del Worker."""
    monkeypatch.setattr(app_module.app, "proxy_token", "secreto-de-prueba")
    body = {"text": "hola", "voice": "es-AR-ElenaNeural"}

    direct = client.post("/api/tts", json=body)
    assert direct.status_code == 403

    spoofed = client.post("/api/tts", json=body, headers={"X-Proxy-Token": "otro"})
    assert spoofed.status_code == 403


def test_proxy_token_allows_the_worker(monkeypatch):
    monkeypatch.setattr(app_module.app, "proxy_token", "secreto-de-prueba")
    monkeypatch.setattr(tts_engine, "synthesize", lambda text, voice, rate=100: b"ID3")

    response = client.post(
        "/api/tts",
        json={"text": "hola", "voice": "es-AR-ElenaNeural"},
        headers={"X-Proxy-Token": "secreto-de-prueba"},
    )
    assert response.status_code == 200


def test_proxy_token_never_blocks_health(monkeypatch):
    """El health check de la plataforma y el cron del Worker no llevan token."""
    monkeypatch.setattr(app_module.app, "proxy_token", "secreto-de-prueba")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/voices").status_code == 200

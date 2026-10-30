"""Unit tests for handler.py (AWS calls are mocked, no real AWS needed)."""
from __future__ import annotations

import io
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import handler  # noqa: E402

OUT = "tts-output-123456789012"
TOPIC = "arn:aws:sns:us-east-1:123456789012:tts-audio-ready"


def make_event(bucket: str, key: str) -> dict:
    """Build a minimal S3 ObjectCreated event."""
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OUTPUT_BUCKET", OUT)
    monkeypatch.setenv("TOPIC_ARN", TOPIC)


def test_should_process_accepts_txt_from_input_bucket():
    assert handler.should_process("tts-input-1", "a.txt", OUT)


def test_should_process_rejects_output_bucket():
    assert not handler.should_process(OUT, "a.txt", OUT)


def test_should_process_rejects_non_txt():
    assert not handler.should_process("tts-input-1", "a.mp3", OUT)


def test_synthesize_rejects_long_text():
    with pytest.raises(ValueError):
        handler.synthesize("x" * (handler.MAX_CHARS + 1), "Lupe")


def test_synthesize_rejects_empty_text():
    with pytest.raises(ValueError):
        handler.synthesize("   ", "Lupe")


def test_handler_happy_path_decodes_key_and_notifies():
    with patch.object(handler, "s3") as s3, patch.object(
        handler, "polly"
    ) as polly, patch.object(handler, "sns") as sns:
        s3.get_object.return_value = {"Body": io.BytesIO("Hola ñandú".encode())}
        polly.synthesize_speech.return_value = {"AudioStream": io.BytesIO(b"MP3")}
        result = handler.handler(make_event("tts-input-1", "mi+texto.txt"), None)

    assert result["processed"] == 1
    s3.get_object.assert_called_once_with(Bucket="tts-input-1", Key="mi texto.txt")
    assert s3.put_object.call_args.kwargs["Key"] == "mi texto.mp3"
    assert s3.put_object.call_args.kwargs["Bucket"] == OUT
    sns.publish.assert_called_once()


def test_handler_ignores_events_from_output_bucket():
    """Loop guard: an event from the output bucket must do nothing."""
    with patch.object(handler, "s3") as s3, patch.object(
        handler, "polly"
    ) as polly, patch.object(handler, "sns") as sns:
        result = handler.handler(make_event(OUT, "a.txt"), None)

    assert result["processed"] == 0
    s3.get_object.assert_not_called()
    polly.synthesize_speech.assert_not_called()
    sns.publish.assert_not_called()


def test_handler_skips_too_long_text_without_crashing():
    with patch.object(handler, "s3") as s3, patch.object(
        handler, "polly"
    ) as polly, patch.object(handler, "sns") as sns:
        s3.get_object.return_value = {"Body": io.BytesIO(b"x" * 4000)}
        result = handler.handler(make_event("tts-input-1", "big.txt"), None)

    assert result["processed"] == 0
    polly.synthesize_speech.assert_not_called()
    sns.publish.assert_not_called()
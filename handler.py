"""Lambda: convert uploaded .txt files to MP3 with Amazon Polly.

Flow: S3 (input) --ObjectCreated--> Lambda --> Polly --> S3 (output) --> SNS.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
polly = boto3.client("polly")
sns = boto3.client("sns")

# Polly synthesize_speech limit; use start_speech_synthesis_task above this.
MAX_CHARS = 3000


@dataclass(frozen=True)
class Config:
    """Runtime configuration loaded from environment variables."""

    output_bucket: str
    topic_arn: str
    voice_id: str = "Lupe"

    @classmethod
    def from_env(cls) -> "Config":
        """Build the config from OUTPUT_BUCKET, TOPIC_ARN and VOICE_ID."""
        return cls(
            output_bucket=os.environ["OUTPUT_BUCKET"],
            topic_arn=os.environ["TOPIC_ARN"],
            voice_id=os.environ.get("VOICE_ID", "Lupe"),
        )


def should_process(bucket: str, key: str, output_bucket: str) -> bool:
    """Decide whether an S3 event must be processed.

    Second defense against an infinite loop: never react to the output
    bucket, and only accept .txt files.

    Args:
        bucket: Bucket that emitted the event.
        key: Object key from the event.
        output_bucket: Bucket where audio files are written.

    Returns:
        True if the object is a .txt file coming from a non-output bucket.
    """
    if bucket == output_bucket:
        return False
    return key.lower().endswith(".txt")


def read_text(bucket: str, key: str) -> str:
    """Download a UTF-8 text object from S3."""
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return body.decode("utf-8")


def synthesize(text: str, voice_id: str) -> bytes:
    """Convert text to MP3 bytes with Polly (neural engine).

    Raises:
        ValueError: If the text is empty or longer than MAX_CHARS.
    """
    if not text.strip():
        raise ValueError("Empty text")
    if len(text) > MAX_CHARS:
        raise ValueError(f"Text too long ({len(text)} > {MAX_CHARS})")
    resp = polly.synthesize_speech(
        Text=text, OutputFormat="mp3", VoiceId=voice_id, Engine="neural"
    )
    return resp["AudioStream"].read()


def handler(event: dict, context) -> dict:
    """Entry point: triggered by S3 ObjectCreated events."""
    cfg = Config.from_env()
    processed = 0
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        # S3 keys arrive URL-encoded (spaces become '+').
        key = unquote_plus(record["s3"]["object"]["key"])

        if not should_process(bucket, key, cfg.output_bucket):
            logger.info("Skipping s3://%s/%s", bucket, key)
            continue

        logger.info("Processing s3://%s/%s", bucket, key)
        try:
            audio = synthesize(read_text(bucket, key), cfg.voice_id)
        except ValueError as exc:
            # Bad input: retrying will not fix it, so log and move on.
            logger.error("Rejected %s: %s", key, exc)
            continue

        out_key = f"{os.path.splitext(key)[0]}.mp3"
        s3.put_object(
            Bucket=cfg.output_bucket,
            Key=out_key,
            Body=audio,
            ContentType="audio/mpeg",
        )
        sns.publish(
            TopicArn=cfg.topic_arn,
            Subject="Audio listo",
            Message=f"Tu audio esta en s3://{cfg.output_bucket}/{out_key}",
        )
        processed += 1
    return {"status": "ok", "processed": processed}

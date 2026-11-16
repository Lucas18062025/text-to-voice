FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py tts_engine.py ./
COPY web ./web

ENV TTS_PROVIDER=edge
ENV PORT=8000
EXPOSE 8000

# Usa $PORT si el host lo inyecta (Render/Fly), si no 8000.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]

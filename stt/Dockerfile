# Speech-to-text microservice.
# Owns the Whisper model and exposes POST /transcribe (see scripts/stt-service.py).
# Kept deliberately small: ffmpeg (for Whisper's audio decoding) + faster-whisper.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --break-system-packages --no-cache-dir faster-whisper

WORKDIR /app
COPY scripts/stt-service.py /app/stt-service.py

CMD ["python3", "/app/stt-service.py"]

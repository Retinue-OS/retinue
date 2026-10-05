# Speech-to-text microservice.
# Owns the Whisper model and exposes POST /transcribe (see scripts/stt-service.py).
# Kept deliberately small: ffmpeg (for Whisper's audio decoding) + faster-whisper.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Pinned: an unpinned install takes whatever PyAV is current at build time.
# PyAV 19 dropped av.open(metadata_errors=...), which faster-whisper's
# decode_audio() still passes, so every transcription failed. Naming the
# versions also invalidates this layer, so a bump actually reaches the image.
RUN python3 -m pip install --break-system-packages --no-cache-dir \
    faster-whisper==1.2.1 av==18.1.0

WORKDIR /app
COPY scripts/stt-service.py /app/stt-service.py

CMD ["python3", "/app/stt-service.py"]

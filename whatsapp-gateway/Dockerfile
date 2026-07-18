# WhatsApp gateway — owns the linked-device session and exposes the thin /send
# API (see scripts/whatsapp-gateway.py). The WhatsApp Web protocol is handled by
# neonize, a Python binding over the whatsmeow Go library; the compiled bridge
# ships inside the neonize wheel, so no Go toolchain is needed here.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# ffmpeg/libmagic are pulled in for media handling and mime detection; ca-certs
# for outbound TLS to the retinue and STT services.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# neonize bundles the platform-native whatsmeow shared library in its wheel.
# requests: HTTP client to the retinue/STT services. langdetect: answer-language
# hint for control-mode replies.
RUN pip install --no-cache-dir neonize requests langdetect

# Session store persists on the whatsapp-data volume (see docker-compose.yml).
RUN mkdir -p /root/.local/share/whatsapp

WORKDIR /app
COPY scripts/whatsapp-gateway.py /app/whatsapp-gateway.py
COPY scripts/requester_identity.py /app/requester_identity.py

CMD ["python3", "/app/whatsapp-gateway.py"]

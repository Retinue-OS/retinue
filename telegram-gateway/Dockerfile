# Telegram gateway — logs in as the user's own Telegram account (an MTProto user
# client via Telethon) and exposes the thin /send API (see
# scripts/telegram-gateway.py). Acting as the user (not a bot) is what lets it
# message the user's contacts as them, read their own DMs, and enumerate the
# real contact directory. The api_id/api_hash and login session live only in
# this container.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# telethon: MTProto user client (pure Python, includes its own crypto).
# requests: HTTP client to the retinue/STT services.
# langdetect: answer-language hint for control-mode replies.
# segno: renders the QR re-pairing code served at GET /qr (pure Python, no deps).
RUN pip install --no-cache-dir telethon requests langdetect segno

# Login session + pending sends + recent chats persist on the telegram-data
# volume (see docker-compose.yml).
RUN mkdir -p /root/.local/share/telegram

WORKDIR /app
COPY scripts/telegram-gateway.py /app/telegram-gateway.py
COPY scripts/requester_identity.py /app/requester_identity.py
COPY scripts/reply_tokens.py /app/reply_tokens.py
COPY scripts/triage_policy.py /app/triage_policy.py
COPY scripts/inbound_store.py /app/inbound_store.py
COPY scripts/job_delivery.py /app/job_delivery.py
COPY scripts/news_ingest.py /app/news_ingest.py
COPY scripts/chat_ingest.py /app/chat_ingest.py
COPY scripts/build_stamp.py /app/build_stamp.py

# The commit this image was built from, when the build passes one (compose
# forwards $RETINUE_BUILD_SHA; the updater's default recipe sets it after the
# pull). Unset is fine and honest — /health then reports sha: null, and the
# framework digest identifies the build either way. See scripts/build_stamp.py.
#
# Last, deliberately: the sha changes on every commit, so an earlier ENV would
# invalidate the COPY layers above for a change that touches none of them.
ARG RETINUE_BUILD_SHA=""
ENV RETINUE_BUILD_SHA=${RETINUE_BUILD_SHA}

CMD ["python3", "/app/telegram-gateway.py"]

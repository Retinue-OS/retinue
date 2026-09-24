# SMS gateway — the channel gateway for SMS (see scripts/sms-gateway.py). Talks
# to the self-hosted android-sms-gateway server (the `sms-server` service) that
# the user's Android phone is paired with, receives the phone's signed
# sms:received webhooks, and exposes the thin /send API. The server's API
# credentials live only in this container.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# requests: HTTP client to the SMS server and the retinue services.
RUN pip install --no-cache-dir requests

# Pending sends + recent chats + reply tokens + seen webhook ids persist on the
# sms-data volume (see docker-compose.yml).
RUN mkdir -p /root/.local/share/sms

WORKDIR /app
COPY scripts/sms-gateway.py /app/sms-gateway.py
COPY scripts/requester_identity.py /app/requester_identity.py
COPY scripts/reply_tokens.py /app/reply_tokens.py
COPY scripts/triage_policy.py /app/triage_policy.py
COPY scripts/inbound_store.py /app/inbound_store.py
COPY scripts/job_delivery.py /app/job_delivery.py
COPY scripts/news_ingest.py /app/news_ingest.py
COPY scripts/chat_ingest.py /app/chat_ingest.py
COPY scripts/build_stamp.py /app/build_stamp.py

# Last, deliberately: see telegram-gateway/Dockerfile and scripts/build_stamp.py.
ARG RETINUE_BUILD_SHA=""
ENV RETINUE_BUILD_SHA=${RETINUE_BUILD_SHA}

CMD ["python3", "/app/sms-gateway.py"]

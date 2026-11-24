# Mail gateway — the channel gateway for e-mail (see scripts/mail-gateway.py and
# docs/mail-chats.md). Mirrors one mailbox over IMAP into the per-message
# ledger the messenger gateways write, so its threads show as chats, and serves
# the stored parts token-gated. The mailbox credentials it reads are the ones
# scripts/email_client.py reads; email_client.py is its IMAP/MIME library.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# requests: job_delivery.py's HTTP client for the rails to the retinue
# services. IMAP, SMTP and MIME are the standard library's.
RUN pip install --no-cache-dir requests

# Thread index, mailbox index and outbound notes persist on the mail-data
# volume (see docker-compose.yml).
RUN mkdir -p /root/.local/share/mail

WORKDIR /app
COPY scripts/mail-gateway.py /app/mail-gateway.py
COPY scripts/email_client.py /app/email_client.py
COPY scripts/mail_threads.py /app/mail_threads.py
COPY scripts/mail_trim.py /app/mail_trim.py
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

CMD ["python3", "/app/mail-gateway.py"]

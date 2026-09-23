FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl ffmpeg libespeak-ng1 openjdk-21-jre-headless python3 python3-pip wget \
    && rm -rf /var/lib/apt/lists/*

ARG SIGNAL_CLI_VERSION=0.14.8
RUN wget -qO /tmp/signal-cli.tar.gz \
      "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}-Linux-native.tar.gz" \
    && tar xf /tmp/signal-cli.tar.gz -C /opt/ \
    && if [ -x /opt/signal-cli ]; then \
         install -m 0755 /opt/signal-cli /usr/local/bin/signal-cli; \
       elif [ -x "/opt/signal-cli-${SIGNAL_CLI_VERSION}-Linux-native/bin/signal-cli" ]; then \
         ln -s "/opt/signal-cli-${SIGNAL_CLI_VERSION}-Linux-native/bin/signal-cli" /usr/local/bin/signal-cli; \
       else \
         echo "signal-cli binary not found after extraction" >&2; \
         exit 1; \
       fi \
    && rm /tmp/signal-cli.tar.gz

# segno renders the re-link pairing QR served at GET /qr (pure Python, no deps).
RUN python3 -m pip install --break-system-packages --no-cache-dir piper-tts requests langdetect segno

RUN mkdir -p /models

WORKDIR /app
COPY scripts/signal-gateway.py /app/signal-gateway.py
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

CMD ["python3", "/app/signal-gateway.py"]

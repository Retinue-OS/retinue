FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl ffmpeg libespeak-ng1 openjdk-21-jre-headless python3 python3-pip wget \
    && rm -rf /var/lib/apt/lists/*

ARG SIGNAL_CLI_VERSION=0.14.5
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

RUN python3 -m pip install --break-system-packages --no-cache-dir piper-tts requests langdetect

RUN mkdir -p /models

WORKDIR /app
COPY scripts/signal-gateway.py /app/signal-gateway.py
COPY scripts/requester_identity.py /app/requester_identity.py

CMD ["python3", "/app/signal-gateway.py"]

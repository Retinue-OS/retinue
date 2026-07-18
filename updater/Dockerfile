# Updater sidecar — tiny image with the Docker CLI + compose plugin + git,
# driving the host's Docker daemon via the mounted socket (docker-out-of-docker,
# not `privileged`). See update-server.py for the fixed rebuild/restart recipe
# it exposes over HTTP.
FROM docker:cli

RUN apk add --no-cache docker-cli-compose git python3

WORKDIR /repo
COPY update-server.py /app/update-server.py

ENV PROJECT_DIR=/repo
ENV UPDATER_PORT=9000
EXPOSE 9000

ENTRYPOINT ["python3", "/app/update-server.py"]

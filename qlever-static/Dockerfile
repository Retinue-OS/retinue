FROM docker.io/adfreiburg/qlever:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

USER root
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
RUN mkdir -p /index

ENV INPUT_FILE=/data/genetics.nt
ENV PORT=7001

EXPOSE 7001
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

FROM mitmproxy/mitmproxy:latest

USER root
RUN mkdir -p /var/log/retinue/egress /home/mitmproxy/.mitmproxy /scripts
COPY scripts/egress-audit-addon.py /scripts/
COPY egress-audit/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV EGRESS_AUDIT_LOG_DIR=/var/log/retinue/egress
ENV EGRESS_AUDIT_BODY_LIMIT=65536
ENV EGRESS_AUDIT_STREAM_THRESHOLD=4m

ENTRYPOINT ["/entrypoint.sh"]

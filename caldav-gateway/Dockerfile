# CalDAV gateway — writes calendar events into the user's real calendar (see
# scripts/caldav-gateway.py). Provider-agnostic: the backend (Zoho, or any
# other RFC 4791 CalDAV server) is selected purely by configuration
# (CALDAV_SERVER_URL/CALDAV_USERNAME/CALDAV_PASSWORD), never by a
# provider-specific code path.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# caldav is a generic RFC 4791 client library (pulls in icalendar/vobject/lxml).
# Pinned, not left to resolve "latest": this layer depends on no repo file, so
# an unpinned install is cached forever at whatever was current on first build.
# 3.2.1 in particular silently returns no events from a server that answers a
# calendar-query with hrefs only (no inline calendar-data): the objects it then
# loads keep a stale cached "no data" state and are discarded as empty. Fixed
# in 3.3.0. Bumping this version is what rebuilds the layer.
RUN pip install --no-cache-dir caldav==3.3.0

WORKDIR /app
COPY scripts/caldav-gateway.py /app/caldav-gateway.py

CMD ["python3", "/app/caldav-gateway.py"]

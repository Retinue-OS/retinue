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
RUN pip install --no-cache-dir caldav

WORKDIR /app
COPY scripts/caldav-gateway.py /app/caldav-gateway.py

CMD ["python3", "/app/caldav-gateway.py"]

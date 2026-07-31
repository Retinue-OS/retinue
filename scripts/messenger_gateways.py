#!/usr/bin/env python3
"""Shared discovery of the deployment's messenger channel gateways.

One registry, two consumers: the web-gateway (which aggregates pending sends on
/sends and renders the /gateways status page) and the gateway-monitor (which
polls each gateway's /health). Both must see exactly the same set of gateways,
so the discovery lives here instead of being duplicated.

The three built-in channels enrol when their ``*_GATEWAY_BASE_URL`` is set; a
deployment adds any further gateways (extra accounts, extra channels) via
``MESSENGER_GATEWAYS`` — a JSON array of ``{base_url, token?, label?, slug?}``
objects. The slug (the ``/sends/<slug>/<id>`` URL segment, also used on
``/gateways``) is taken verbatim from an explicit ``slug``, else derived from
the Docker service hostname in ``base_url`` with the redundant ``-gateway``
infix dropped (``signal-gateway-personal`` → ``signal-personal``). Config flows
deployment → framework: the framework never names a specific deployment's
services.
"""
import json
import os
import urllib.parse


def slug_from_base_url(base_url: str) -> str:
    """Derive a URL-safe slug from a gateway's base URL.

    Uses the Docker service hostname (``http://<host>:<port>`` -> ``<host>``)
    and drops a redundant ``-gateway`` infix so ``signal-gateway-personal``
    reads as ``signal-personal`` in URLs, not the raw internal hostname."""
    host = urllib.parse.urlsplit(base_url).hostname or ""
    return host.replace("-gateway-", "-").removesuffix("-gateway")


def _extra_channel_gateways(log_prefix: str) -> dict:
    """Deployment-declared channel gateways beyond the three built-ins.

    Malformed entries are skipped with a log line rather than crashing boot.
    """
    raw = os.environ.get("MESSENGER_GATEWAYS", "").strip()
    if not raw:
        return {}
    try:
        entries = json.loads(raw)
    except ValueError as exc:
        print(f"{log_prefix} MESSENGER_GATEWAYS is not valid JSON, ignoring: {exc}", flush=True)
        return {}
    if not isinstance(entries, list):
        print(f"{log_prefix} MESSENGER_GATEWAYS must be a JSON array, ignoring", flush=True)
        return {}
    out: dict = {}
    for entry in entries:
        if not isinstance(entry, dict):
            print(f"{log_prefix} MESSENGER_GATEWAYS entry is not an object, skipping: {entry!r}", flush=True)
            continue
        base_url = str(entry.get("base_url", "")).rstrip("/")
        if not base_url:
            print(f"{log_prefix} MESSENGER_GATEWAYS entry has no base_url, skipping: {entry!r}", flush=True)
            continue
        slug = str(entry.get("slug", "")).strip() or slug_from_base_url(base_url)
        if not slug:
            print(f"{log_prefix} could not derive a slug for {base_url}, skipping", flush=True)
            continue
        out[slug] = {
            "base_url": base_url,
            "token": str(entry.get("token", "")).strip(),
            "label": str(entry.get("label", "")).strip() or slug.replace("-", " ").title(),
        }
    return out


def channel_gateways(log_prefix: str = "[messenger-gateways]") -> dict:
    """Return the registry of configured channel gateways, keyed by slug.

    Reads the environment at call time (the three built-ins plus any
    MESSENGER_GATEWAYS extras). Deployment-declared extras win on slug
    collision — a deployment can override a built-in's target if it needs to.
    """
    registry = {
        slug: {"base_url": base_url, "token": token, "label": label}
        for slug, base_url, token, label in (
            ("signal",
             os.environ.get("SIGNAL_GATEWAY_BASE_URL", "").rstrip("/"),
             os.environ.get("SIGNAL_GATEWAY_TOKEN", "").strip(),
             "Signal"),
            ("whatsapp",
             os.environ.get("WHATSAPP_GATEWAY_BASE_URL", "").rstrip("/"),
             os.environ.get("WHATSAPP_GATEWAY_TOKEN", "").strip(),
             "WhatsApp"),
            ("telegram",
             os.environ.get("TELEGRAM_GATEWAY_BASE_URL", "").rstrip("/"),
             os.environ.get("TELEGRAM_GATEWAY_TOKEN", "").strip(),
             "Telegram"),
        )
        if base_url
    }
    registry.update(_extra_channel_gateways(log_prefix))
    return registry

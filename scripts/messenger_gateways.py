#!/usr/bin/env python3
"""Shared discovery of the deployment's messenger channel gateways.

One registry, two consumers: the web-gateway (which aggregates pending sends on
/sends and renders the /gateways status page) and the gateway-monitor (which
polls each gateway's /health). Both must see exactly the same set of gateways,
so the discovery lives here instead of being duplicated.

The three built-in channels enrol when their ``*_GATEWAY_BASE_URL`` is set
**and** their name is listed in ``MESSENGER_BUILTIN_CHANNELS`` (comma-separated
subset of ``signal``, ``whatsapp``, ``telegram`` — defaults to all three, i.e.
today's behaviour, unchanged). ``docker-compose.yml`` wires all three
``*_GATEWAY_BASE_URL`` vars unconditionally, so a deployment that never runs
one of the built-in gateway containers at all (not even unpaired) would
otherwise still enrol a gateway pointed at a host that doesn't exist —
indistinguishable, by URL alone, from that same container having crashed. Such
a deployment names only the channels it actually runs, e.g.
``MESSENGER_BUILTIN_CHANNELS=signal``, and the other two drop out of the
registry entirely — same as a chamber that was never mounted — with no need to
separately blank their base URLs. A deployment adds any further gateways
(extra accounts, extra channels) via ``MESSENGER_GATEWAYS`` — a JSON array of
``{base_url, token?, label?}`` objects. The slug (the ``/sends/<slug>/<id>``
URL segment, also used on ``/gateways``) is the **Docker service hostname**
from ``base_url``, verbatim (``http://signal-gateway-personal:8090`` →
``signal-gateway-personal``). The
gateways derive the same slug from the ``Host`` header of the ``/send`` request
that queued the message, so approval links resolve with no slug configuration
on either side — any account a deployment adds gets a working ``verify`` flow
by construction. Config flows deployment → framework: the framework never
names a specific deployment's services.

Older links used shortened slugs (``signal``, ``signal-personal`` — the
hostname with the redundant ``-gateway`` infix dropped); ``resolve()`` still
accepts those as aliases so pre-upgrade approval links keep working.
"""
import json
import os
import urllib.parse


def slug_from_base_url(base_url: str) -> str:
    """Derive the slug from a gateway's base URL: the Docker service hostname.

    ``http://signal-gateway-personal:8090`` → ``signal-gateway-personal``. The
    hostname is what the gateway itself sees in the ``Host`` header of a
    ``/send`` request, so using it verbatim keeps the approval link a gateway
    emits and the registry key the web-gateway resolves identical with no
    configuration."""
    return urllib.parse.urlsplit(base_url).hostname or ""


def legacy_slug(slug: str) -> str:
    """The pre-service-name shortening of a slug, kept for old approval links.

    Drops the redundant ``-gateway`` infix/suffix: ``signal-gateway`` →
    ``signal``, ``signal-gateway-personal`` → ``signal-personal``."""
    return slug.replace("-gateway-", "-").removesuffix("-gateway")


def resolve(registry: dict, slug: str):
    """Resolve a /sends or /gateways URL slug to ``(canonical_slug, gateway)``.

    Exact registry keys win; otherwise a slug matching a key's legacy
    shortening (see ``legacy_slug``) resolves to that key, so approval links
    queued before the service-name slugs keep working. Returns ``(None, None)``
    for anything else (e.g. an e-mail account segment)."""
    if slug in registry:
        return slug, registry[slug]
    for key in sorted(registry):
        if legacy_slug(key) == slug:
            return key, registry[key]
    return None, None


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
        if entry.get("slug"):
            print(f"{log_prefix} 'slug' is no longer read; ignoring {entry['slug']!r} for {base_url} — the slug is now the service hostname, verbatim", flush=True)
        slug = slug_from_base_url(base_url)
        if not slug:
            print(f"{log_prefix} could not derive a slug for {base_url}, skipping", flush=True)
            continue
        out[slug] = {
            "base_url": base_url,
            "token": str(entry.get("token", "")).strip(),
            "label": str(entry.get("label", "")).strip() or slug.replace("-", " ").title(),
        }
    return out


# The built-in channels this deployment actually runs. Comma-separated subset
# of "signal", "whatsapp", "telegram"; unset means all three (today's
# behaviour). Lets a deployment that never starts one of the built-in gateway
# containers drop it from the registry with one variable, instead of having to
# blank that channel's *_GATEWAY_BASE_URL to the same effect.
_ALL_BUILTIN_CHANNELS = ("signal", "whatsapp", "telegram")


def _enabled_builtin_channels() -> set:
    raw = os.environ.get("MESSENGER_BUILTIN_CHANNELS")
    if raw is None:
        return set(_ALL_BUILTIN_CHANNELS)
    return {name.strip() for name in raw.split(",") if name.strip()}


def channel_gateways(log_prefix: str = "[messenger-gateways]") -> dict:
    """Return the registry of configured channel gateways, keyed by slug.

    Reads the environment at call time (the enabled built-ins plus any
    MESSENGER_GATEWAYS extras). Every gateway — built-in or extra — is keyed
    by the service hostname of its base URL. Deployment-declared extras win on
    slug collision — a deployment can override a built-in's target if it needs
    to.
    """
    enabled = _enabled_builtin_channels()
    registry = {
        slug_from_base_url(base_url): {"base_url": base_url, "token": token, "label": label}
        for name, base_url, token, label in (
            ("signal", os.environ.get("SIGNAL_GATEWAY_BASE_URL", "").rstrip("/"),
             os.environ.get("SIGNAL_GATEWAY_TOKEN", "").strip(),
             "Signal"),
            ("whatsapp", os.environ.get("WHATSAPP_GATEWAY_BASE_URL", "").rstrip("/"),
             os.environ.get("WHATSAPP_GATEWAY_TOKEN", "").strip(),
             "WhatsApp"),
            ("telegram", os.environ.get("TELEGRAM_GATEWAY_BASE_URL", "").rstrip("/"),
             os.environ.get("TELEGRAM_GATEWAY_TOKEN", "").strip(),
             "Telegram"),
        )
        if name in enabled and base_url and slug_from_base_url(base_url)
    }
    registry.update(_extra_channel_gateways(log_prefix))
    return registry

# Client-certificate auth for the gateway (Traefik side)

The public gateway accepts **either** a TLS
client certificate **or** HTTP basic auth. Basic auth is enforced by the
gateway's `/auth` endpoint (Traefik `forwardAuth`); the certificate half needs a
small amount of Traefik configuration that **cannot** be expressed as Docker
labels, because Traefik TLS options are only settable through its *file
provider*. That config lives here.

This directory contains:

| File | Purpose |
|------|---------|
| `dynamic/retinue-mtls.yml` | Defines the `retinue-mtls` TLS option: optional client-cert verification (`VerifyClientCertIfGiven`) against our client CA. |
| `dynamic/retinue-client-ca.crt` | The client **CA public certificate** Traefik trusts. Browsers install a client cert *signed by this CA* (delivered separately as a `.p12`). The CA private key is never stored in this repo. |

## One-time wiring into your Traefik

Traefik runs as a separate stack (it owns the external `web` network and the
Let's Encrypt resolver `myresolver`). Do this **once** there:

1. **Enable the file provider** (if not already), pointing at a watched
   directory. In Traefik's static config (`traefik.yml`) or flags:

   ```yaml
   providers:
     file:
       directory: /etc/traefik/dynamic
       watch: true
   ```

2. **Mount this directory** into the Traefik container at that path. In
   Traefik's `docker-compose.yml`:

   ```yaml
   services:
     traefik:
       volumes:
         - /path/to/retinue/deploy/traefik/dynamic:/etc/traefik/dynamic:ro
   ```

   The CA file then lands at `/etc/traefik/dynamic/retinue-client-ca.crt`, which
   is exactly the `caFiles` path in `retinue-mtls.yml`. (If you mount it
   elsewhere, edit that path.)

3. **Restart Traefik.** With `watch: true`, future edits to these files are
   picked up live; the first mount needs a restart.

That's it on the Traefik side. The `retinue` service's labels already reference
`retinue-mtls@file` and add the `passTLSClientCert` + `forwardAuth` middlewares,
so rebuilding/restarting the retinue stack completes the wiring.

## Issuing client certificates

Use `scripts/gen-client-cert.sh` (run it on a trusted machine that holds the CA
key, **not** in the agent container). It creates the CA on first run and issues a
browser-installable `.p12` for each device:

```bash
scripts/gen-client-cert.sh --name "reto-laptop" --out ./certs
# -> ./certs/reto-laptop.p12   (import into the browser; a passphrase is printed)
# -> ./certs/ca.crt            (this file; copy to dynamic/retinue-client-ca.crt)
```

Installing more devices later does **not** require touching Traefik again — every
cert signed by the same CA is trusted. Keep `ca.key` offline and safe.

> **Careful:** the script creates a CA when `--out` holds no `ca.key`, and the new CA
> carries the *same subject name* as the old one (`CN = Retinue Client CA`). Running it
> against a fresh directory therefore mints a **second** CA that Traefik does not trust,
> and every certificate issued from it is rejected at the TLS handshake with `unknown ca`
> — the browser then re-prompts for a certificate in a loop. Because `clientAuthType` is
> `VerifyClientCertIfGiven`, *declining* the prompt still works (basic auth), which makes
> this easy to misread as a front-end bug.
>
> Always point `--out` at the directory that already holds `ca.key`. If a new CA was
> minted anyway, copy its `ca.crt` over `dynamic/retinue-client-ca.crt` (the file
> provider picks it up live) and check the chain before blaming anything else:
>
> ```bash
> openssl x509 -in dynamic/retinue-client-ca.crt -noout -fingerprint -sha256
> openssl verify -CAfile dynamic/retinue-client-ca.crt <device>.crt
> ```

## How the request flows

```
Browser ──TLS──▶ Traefik (terminates TLS, LE server cert)
                  │  retinue-mtls TLS option: requests a client cert,
                  │  verifies it against retinue-client-ca.crt if given
                  ├─ passTLSClientCert: forwards the verified cert as a header
                  └─ forwardAuth ▶ gateway GET /auth
                                     cert header present  → 200 (allow)
                                     else valid basic auth → 200 (allow)
                                     else                  → 401 (password prompt)
```

Internal container-to-container traffic never passes through Traefik, so it is
unaffected by any of this.

## Security note

The gateway trusts the *presence* of the forwarded client-cert header as proof of
a valid certificate. Two properties make that safe and **must** hold:

1. **Traefik strips spoofed headers.** `passTLSClientCert` removes any
   client-supplied `X-Forwarded-Tls-Client-Cert(-Info)` and re-adds it only from
   the real TLS handshake. Keep this middleware ahead of `forwardAuth` (the
   labels already do).
2. **`/auth` is never published.** It is reachable only via Traefik on the
   internal Docker network. Do not expose port 8080 directly to the internet, or
   a client could inject the header and bypass the certificate check.

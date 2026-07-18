# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Report it privately through GitHub's [private vulnerability
reporting](https://github.com/retinue-os/retinue/security/advisories/new) on
this repository. If that is unavailable to you, open a public issue containing
only the words "security contact requested" and nothing else, and a maintainer
will arrange a private channel.

Expect an acknowledgement within a week. This is a small project with a single
maintainer; please factor that into your disclosure timeline rather than
assuming silence is indifference.

## What we consider in scope

Retinue is a self-hosted system, so "in scope" means the software's own
behaviour, not a given operator's configuration:

- Anything that lets untrusted input (an inbound e-mail, Signal/WhatsApp/
  Telegram message, or web request) reach a capability it should not have.
- Bypasses of the outbound send-control model — in particular anything that
  lets an agent approve its own send, or send as an identity whose policy
  category is `verify`.
- Bypasses of the account trust boundaries: an `inbox`-mode account driving the
  system, or a sender not on the allowlist reaching `control` mode.
- Credential exposure: anything that puts messaging or mail credentials into the
  agent's context or environment, which the sidecar-gateway architecture exists
  to prevent.
- Authentication and path-traversal issues in the web gateway, including
  attachment and static-file serving.

## Known limitations — please don't report these as vulnerabilities

These are documented design gaps, not undiscovered bugs. They are on the roadmap
and we would welcome help fixing them; a report telling us they exist tells us
nothing new.

- **The egress audit observes; it does not enforce.** It works through
  `HTTP_PROXY`/`HTTPS_PROXY` environment variables, which are advisory: a
  process can unset them, use a raw socket, or speak a non-HTTP protocol. The
  layer is telemetry. Making it a boundary requires an `internal: true` network
  or in-container firewall rules, and is tracked as a roadmap item.
- **The main session runs with broad tool permissions while processing
  untrusted input.** A hostile message cannot steal credentials and cannot
  silently send messages, but it can induce the agent to read across mounted
  chambers or write to them. Reduced-privilege triage is a roadmap item.
- **Chambers are not compartmentalized from each other within a session.**
- **The updater's Docker socket is root-equivalent on the host.** This is
  inherent to what the updater does and is documented in `docker-compose.yml`.

If you have found a way to make one of these *materially worse* than described —
for example, a way to defeat the send-control model rather than merely bypass
the egress proxy — that is very much in scope.

## Operator responsibilities

Some of the security of a Retinue deployment is yours, not ours:

- **Generate your own client CA.** Never reuse one shipped in a repository.
  `deploy/traefik/dynamic/retinue-client-ca.crt` is `.gitignore`d for this
  reason — whoever holds the CA private key can mint client certificates your
  gateway will accept.
- **Set an explicit send policy per account.** Undeclared accounts fail safe to
  `verify`, but relying on the default is not the same as deciding.
- **Keep `UPDATER_TOKEN` and gateway tokens out of any environment you don't
  control.**
- **Back up the named volumes that are not git** — messaging account keys,
  conversations, scheduler state. Losing `signal-data` means re-linking every
  account.

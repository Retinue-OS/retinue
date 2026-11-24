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
- Anything that lets content from outside become a page, a script or a link
  of another scheme on the dashboard's origin. The gateway's escaping and its
  media rules are the first line there; the second is the
  Content-Security-Policy every page carries (docs/dashboard.md, "The
  Content-Security-Policy"), which runs scripts only from the dashboard's own
  files, together with the rule that no route serves bytes from outside under
  a script type. A way around either is in scope. Note that the policy ships
  in report mode (`RETINUE_CSP=report`): until the operator enforces it, it
  reports and blocks nothing. The gateway's policy covers only what the
  gateway answers; another service published on the dashboard's host shares
  its origin and must carry a policy of its own. The egress log viewer, which
  the deployment example can publish at `/egress` there, sends a fixed one
  that runs no script, always enforced.

## Known limitations — please don't report these as vulnerabilities

These are documented design gaps, not undiscovered bugs. They are on the roadmap
and we would welcome help fixing them; a report telling us they exist tells us
nothing new.

- **The egress audit observes; it does not enforce.** It works through
  `HTTP_PROXY`/`HTTPS_PROXY` environment variables, which are advisory: a
  process can unset them, use a raw socket, or speak a non-HTTP protocol. The
  layer is telemetry. Making it a boundary requires an `internal: true` network
  or in-container firewall rules, and is tracked as a roadmap item.
- **Sessions run with broad tool permissions while processing untrusted
  input.** The messenger credentials (Signal keys, the WhatsApp and Telegram
  sessions) live in their own containers, so a hostile message cannot read
  them. Nor can it read a mailbox, gateway or service credential out of a
  session's environment: every `claude -p` the framework spawns — dashboard
  turns, scheduled jobs, messenger triage, Ask-Ara answers — gets its environment from
  the allowlist in `scripts/session_env.py` (capability tokens and the
  framework's own settings) and never inherits the spawning daemon's, so
  mailbox passwords, the LiteLLM master and picker keys, the repo token and
  any secret added to `.env` later are absent by construction, and e-mail
  reaches the mailbox only through the web gateway's backend, where the send
  policy is applied. One credential passes on purpose, plus what a deployment names, and a
  session can read them: the model credential (`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`,
  and `ANTHROPIC_CUSTOM_HEADERS` when it carries a LiteLLM virtual key),
  without which a spawned session cannot run at all, and whatever a
  deployment names in `RETINUE_SESSION_ENV_EXTRA` for its chambers' own
  scripts (a data source's login, say, because `refresh.py --ensure` runs a
  chamber's fetch inside the agent's own process). The `retinue` service also no longer loads
  `.env` wholesale, so a secret meant for another service never enters the
  agent container at all. The remote-control main session is not spawned
  this way and keeps the entrypoint's narrower scrub (mail credentials and
  the API key), so it still sees the LiteLLM master and picker keys and the
  repo token.
  What the allowlist does *not* do: every process in the container runs as
  the same user, so a session can still read a daemon's `/proc/<pid>/environ`
  (the web gateway's, for one, holds the mailbox credentials for its e-mail
  backend), and the repository token sits in `~/.git-credentials`.
  **The mailbox credentials are now held by two containers**: the `retinue`
  container (for `email_client.py`'s backend and the approval of e-mail
  drafts, as before) and the `mail-gateway` container,
  which mirrors the mailbox into the chat ledger, sends mail and moves it
  between folders, and is given only the variables it reads, no `.env`
  wholesale — the send policy (`EMAIL_SEND_POLICY`) and the chat-erase token
  since it sends and erases, and the two ingest tokens of the rails it offers
  mail to: `CHATS_INGEST_TOKEN` (the chats rail, where every arriving mail is
  offered — without that token it presents its own `MAIL_GATEWAY_TOKEN`
  there, see "The chats rail is a second way in" below) and
  `NEWS_INGEST_TOKEN` (the news rail, where it files the mail of a news
  sender or list itself). Compose hands
  it the credentials whether or not it mirrors; a deployment that wants them
  in one container only gives the service a profile nobody enables
  (`docs/messaging.md`). The mail gateway is the sidecar
  that can close this gap for e-mail, but only once the credentials can leave
  the `retinue` service (its `email_client.py` backend still
  uses them); until then it adds a second place that holds them rather than
  taking one away. A named account
  mirrored as chats is held by its own mail gateway service as well. The mail
  gateway runs no session itself and has no public route (what it offers the
  chats rail may start one in the `retinue` service: below). It mirrors nothing and
  serves nothing but its `/health` until `MAIL_GATEWAY_TOKEN` is set — the
  operator's opt-in, so an upgrade that sets nothing mirrors nothing and no
  sidecar on the internal network can read stored mail without the token.
  Once set, what it serves — stored mail and its parts — is behind that
  token, which a session does inherit, like every gateway token, so a session
  can read the mirrored mail as it can already read the mailbox through the
  backend. What the gateway can now do to the mailbox, and what a session
  holding `MAIL_GATEWAY_TOKEN` can ask of it: it **sends** under
  `EMAIL_SEND_POLICY` (`verify` queues, `trust` sends only with
  `user_approved`, `allow` sends — the request's `author` grants nothing), it
  **sets `\Seen`** on a thread's mails, **moves** a thread's INBOX mails to
  the archive folder and, on the user's delete, a thread's mails in every
  mirrored folder to the Trash (a Trash folder it never creates); it never
  expunges a folder. A send may carry an idempotency key, so a repeat of the
  same press sends nothing twice, and `POST /reply-preview` only reads. A
  session can, through the API, send under the policy and mark a thread read or archive it, and —
  as on every gateway — approve, reject or retract a pending send, since
  those share the bearer token, which is why send approval is a workflow gate
  rather than a boundary. It **cannot erase**: moving mail to the
  Trash and erasing the mirror need `CHAT_ERASE_TOKEN`
  (`X-Chat-Erase-Token`), which the gateway demands and which no session
  inherits, so only the user's own delete reaches it; unset, the gateway
  refuses with 503. On the web gateway, marking a mail chat read, archiving it
  and flagging it write to the mailbox, so those endpoints are the user's own
  act like send, compose and delete: same-origin only. Mail sent through the
  gateway writes no policy. Closing
  that means holding each secret in its own sidecar or under a separate uid,
  which is a separate roadmap item. An outbound send still has to clear the
  sending identity's policy, but a pending `verify` approval is not bound to
  the requester or a distinct authorization identity, so treat send approval
  as a workflow gate rather than a hard boundary. A hostile message can also
  induce the agent to read across mounted chambers or write to them.
  Reduced-privilege triage is another roadmap item.
- **A mail's sender is not authenticated.** Who the gate takes a mail for is
  narrow: a mail has a gate sender only when it carries exactly one `From`
  header holding exactly one address the mail gateway's own grammar accepts,
  lowercased by its ASCII letters alone — and none at all where a character
  of that address stands in for an ASCII one on its own (its compatibility
  form is ASCII, or its lowercase or case fold is one ASCII character: a
  Kelvin sign, a long s, a fullwidth letter), so no Unicode folding, on
  either side, can make it equal to an ASCII address the address book holds;
  nor where its header block was cut at its bound, since a second `From`
  past the cut is never seen. The `Sender` header, and the first of
  several `From` addresses, are shown as who a mail is from and decide
  nothing. That address, or a domain a contact files, is what the gate
  matches against the VIP and instructed senders, what the instruction is
  looked up by and what the attention item is judged as (the person's
  importance prior and permits); a mail with no gate sender is nobody's — it
  reaches its chat, buys no turn and rings as nobody's, never as the
  thread's earlier correspondent. What is not narrow is the `From` itself:
  nothing in the mail gateway or the web gateway checks SPF, DKIM, DMARC or
  an `Authentication-Results` header, and the mailbox provider's own
  filtering is the only thing that stands before a mail (which helps where
  the forged domain publishes a DMARC policy the provider enforces, and not
  otherwise). A mail that forges one `From` of a VIP or of an instructed
  sender therefore still gets that sender's treatment: it may ring at once,
  in every Focus mode for a VIP, and a model turn with tools runs on it,
  briefed by the sender's instruction — which can file the documents it
  carries, archive the thread (moving its mails out of the inbox) and send a
  reply under the mailbox's send policy, at once under `allow` where the
  instruction approves that in so many words. Archiving is all such a turn
  can do with its own thread's flags while it runs: the web gateway takes a
  flags call on that thread as the turn's — its mute an archive, its unmute,
  unarchive and star nothing — whether or not the call carries the turn's
  token, unless it carries the proof a dashboard thread's turn is handed
  (the user's word); the user's own flags endpoint, which needs no token,
  refuses a caller inside the container where `EDGE_PROXY_PEERS` names the
  reverse proxy (any peer it does not name is refused). Without that
  setting it refuses only a loopback peer and a peer that is the very
  address the caller dialled, so a caller inside the container that comes
  in from another of the container's addresses, or through a sibling that
  relays (the egress-audit proxy), is taken for the dashboard — and where
  the proxy arrives on loopback (host networking) the two cannot be told
  apart at all.
  That holds no further. The turn's session holds the backend token every
  agent session holds, so it can set **another** chat's flags — mute it —
  with the user's meaning, and a process it leaves behind can do so on its
  own thread once the turn has ended; and while a dashboard turn the user
  started runs, its proof sits in that turn's process environment, which
  another session of the same uid can read — sessions are not sandboxed from
  one another. What keeps a turn to its brief there is the brief. One that forges a contact with
  an importance prior or a permit rings as that contact would. The turn
  reads the mail as data: everything the mail says of itself — its sender's
  name, its subject, its id, its text, and the thread's earlier mails in the
  note that follows — stands escaped inside delimited elements it cannot
  close, bounded in length, and none of it in a sentence that tells the turn
  what to do; the instruction is the user's, not the sender's. But the turn
  can do what the instruction lets it do. An instruction that covers a whole
  domain is as wide as everyone who can write a `From` at that domain. This
  is known and not mitigated in code; give an instruction to a sender, and
  above all to a domain, only where a forged mail from them would do little
  harm, and keep the mailbox's send policy on `verify` for a mailbox whose
  instructions send.
- **The chats rail is a second way in.** `POST /internal/chats/inbound` (and
  `/merge`, `/removed`) is how the gateways announce messages, and the web
  gateway trusts what an event says — whose message it is, what the gate
  decided, the files it hands over. An **e-mail event** is never taken
  without a token: `CHATS_INGEST_TOKEN` where it is set, else
  `MAIL_GATEWAY_TOKEN`, compared in constant time, and refused with neither
  set; `/merge` and `/removed` are authorized the same way. A **messenger
  event** is open unless `CHATS_INGEST_TOKEN` is set (the gateways' forwards
  are fire-and-forget, so a fail-closed default would fail silently), so anything that can reach the
  `retinue` container on the internal network can post one that rings the
  phone in every Focus mode (an event calling its sender a VIP) or starts a
  turn briefed by a person's standing instruction, which acts as far as the
  instruction's words go — a calendar entry or a message it approves in so
  many words goes out unseen. The web gateway says so at startup. Every
  `/internal/chats/*` and `/internal/news` call that a browser marks as
  cross-site (`Sec-Fetch-Site` other than `none` or `same-origin`, else an
  `Origin` of another site) is refused, so a page the user visits cannot
  post to the rail through the dashboard's ambient credential; no gateway
  sends either header. Both tokens are in a session's environment (like
  every gateway token), so a session a hostile message steers can post an
  event as well — no more than it can already do with the gateway's send
  token.
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
- **Enforce the dashboard's Content-Security-Policy.** It ships report-only;
  after about a week of normal use, read `GET /csp-report` and set
  `RETINUE_CSP=enforce` (docs/dashboard.md, "The Content-Security-Policy").
- **Publish nothing on the dashboard's host that lacks a policy of its own.**
  A service routed to a path of that host serves pages on the dashboard's
  origin, where a script it lets run can do what the dashboard can. Give such
  a service a host of its own unless it sends a Content-Security-Policy that
  runs no script from outside, as the egress log viewer does.
- **Set an explicit send policy per account.** Undeclared accounts fail safe to
  `verify`, but relying on the default is not the same as deciding.
- **Set `CHATS_INGEST_TOKEN`** on the `retinue` service and on every gateway
  once a messenger contact is a VIP or carries an instruction: without it,
  the chats rail takes a messenger event from anything on the internal
  network ("The chats rail is a second way in").
- **Keep `UPDATER_TOKEN` and gateway tokens out of any environment you don't
  control.**
- **Back up the named volumes that are not git** — messaging account keys,
  conversations, scheduler state. Losing `signal-data` means re-linking every
  account.

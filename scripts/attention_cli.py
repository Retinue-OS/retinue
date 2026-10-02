"""The gateway call the attention CLIs share (attention-week.py,
attention-spheres.py): JSON in, JSON out, and a failure ends the script with
the gateway's own reason. Also the sphere check conversation-push.py and
attention-set.py run before they declare a sphere or tag.

Configuration (environment): ATTENTION_URL, else the web-gateway on
localhost:WEB_GATEWAY_PORT (8080).
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("ATTENTION_URL", f"http://localhost:{os.environ.get('WEB_GATEWAY_PORT', '8080')}").rstrip("/")
TIMEOUT = 30


def call(prog: str, method: str, path: str, body: dict | None = None) -> dict:
    """``method`` ``path`` on the gateway; exits with ``prog: <reason>`` on
    an error answer or when the gateway does not answer."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            answer = json.loads(exc.read().decode("utf-8"))
        except ValueError:
            answer = None
        reason = (answer.get("error") if isinstance(answer, dict) else None) or exc.reason
        sys.exit(f"{prog}: {reason}")
    except urllib.error.URLError as exc:
        sys.exit(f"{prog}: the gateway at {BASE} did not answer ({exc.reason})")


def spheres() -> list[str] | None:
    """The deployment's sphere vocabulary as the gateway holds it (focus.json),
    or None when the gateway does not answer — a check that cannot see the
    vocabulary must not block the declaration it guards."""
    try:
        with urllib.request.urlopen(BASE + "/attention/profile", timeout=10) as resp:
            focus = json.loads(resp.read().decode("utf-8")).get("focus") or {}
    except (OSError, ValueError, AttributeError):
        return None
    words = focus.get("spheres")
    return list(words) if isinstance(words, list) and words else None


def unknown_spheres(prog: str, sphere: str | None, tags: list[str]) -> str | None:
    """Why a declared sphere or tag is not a word of the vocabulary, or None.

    A tag is a further sphere (docs/attention-model.md), so both are checked
    against the same list. Without the check an agent guesses — a word from
    the shipped defaults that does not fit, or a new one in its own language
    beside the one the user named (``assistenz`` beside ``assistance``) — and
    the item lands in a sphere no mode was told about."""
    vocabulary = spheres()
    if vocabulary is None:
        return None
    import attention as policy  # the CLIs that never check spheres skip the import
    known = set(vocabulary)
    bad = [w for w in dict.fromkeys([sphere, *tags]) if w and policy.sphere_id(w) not in known]
    if not bad:
        return None
    return (f"{prog}: not a sphere of this deployment: {', '.join(bad)}. "
            f"The spheres are: {', '.join(vocabulary)}. Pick from these, or leave "
            f"--sphere/--tag out when none fits; only the user adds a sphere.")

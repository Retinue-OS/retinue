#!/usr/bin/env python3
"""Show the sphere vocabulary, or put one sphere within another.

Spheres are flat words (docs/attention-model.md), but a subject can belong to
a larger one: one client’s work is still customer work. A sphere put *within*
another is admitted wherever the outer one is — Focused on customers lets
Acme items ring — while a mode or scope on the inner sphere admits only it.
This is how an agent makes the change the user asks for:

    attention-spheres.py                                # the spheres and their nesting
    attention-spheres.py within acme customers         # acme lies within customers
    attention-spheres.py within acme --none            # acme stands on its own again

The gateway checks every change — both spheres exist, the nesting does not
loop — and answers what it now holds or why not.

Configuration (environment): ATTENTION_URL, else the web-gateway on
localhost:WEB_GATEWAY_PORT (8080).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import attention as policy  # noqa: E402
import attention_cli  # noqa: E402


def _call(method: str, path: str, body: dict | None = None) -> dict:
    return attention_cli.call("attention-spheres", method, path, body)


def show() -> int:
    focus = _call("GET", "/attention/profile")["focus"]
    within = focus.get("within") or {}
    for sphere in focus.get("spheres") or []:
        chain = policy.enclosing(sphere, within)
        print(sphere + (f"  (within {' → '.join(chain[1:])})" if len(chain) > 1 else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Show the spheres, or put one within another.")
    sub = ap.add_subparsers(dest="cmd")
    nest = sub.add_parser("within", help="put a sphere within another, or (--none) let it stand alone")
    nest.add_argument("sphere")
    nest.add_argument("outer", nargs="?")
    nest.add_argument("--none", action="store_true", help="the sphere lies within nothing")
    args = ap.parse_args()

    if args.cmd is None:
        return show()
    if bool(args.outer) == args.none:
        ap.error("name the outer sphere, or --none")
    out = _call("POST", "/attention/spheres", {"sphere": args.sphere, "within": None if args.none else args.outer})
    outer = (out.get("within") or {}).get(out["nested"])
    print(f"{out['nested']} lies within {outer}" if outer else f"{out['nested']} stands on its own")
    return 0


if __name__ == "__main__":
    sys.exit(main())

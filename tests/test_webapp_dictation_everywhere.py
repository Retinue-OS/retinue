#!/usr/bin/env python3
"""Every text field in the dashboard takes dictation (docs/dashboard.md,
"Every text field takes dictation").

Scans the webapp's markup — the components' template strings and the HTML
pages — for every field that takes words (a <textarea>, an <input> of a text
kind) and requires it to say how it is dictated into:

  data-dictate-field="<key>"   the shared 🎤 of components/dictate-field.js;
  data-dictate-own             a composer with its own recording row
                               (conversation, chat, project command bar);
  data-dictate-exempt="<why>"  a value with a machine format that speech does
                               not produce (a handle, an e-mail domain).

A new field without one of them fails here, so the rule cannot be forgotten.
Standalone like the rest of the suite.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEBAPP = os.path.join(HERE, "..", "webapp")

# Input types that take no free words.
NON_TEXT = {"number", "range", "color", "date", "datetime-local", "time", "month", "week",
            "checkbox", "radio", "file", "hidden", "submit", "button", "reset", "image", "password"}
MARKS = ("data-dictate-field", "data-dictate-own", "data-dictate-exempt")


def tags(source):
    """Each <textarea …> / <input …> opening tag, with the template-string
    seams (`' +\\n '`) between its attribute pieces folded away."""
    for m in re.finditer(r"<(textarea|input)\b", source):
        i = m.end()
        depth = 0
        while i < len(source):
            ch = source[i]
            if source.startswith("${", i):
                depth += 1
                i += 2
                continue
            if ch == "}" and depth:
                depth -= 1
            elif ch == ">" and not depth and source[i - 1] != "=":
                break
            i += 1
        yield m.group(1), source[m.start():i + 1], source.count("\n", 0, m.start()) + 1


def text_field(kind, tag):
    if kind == "textarea":
        return True
    t = re.search(r"""\btype=["']?([\w-]+)""", tag)
    return (t.group(1).lower() if t else "text") not in NON_TEXT


def main():
    files = []
    for base, _dirs, names in os.walk(WEBAPP):
        for n in names:
            if n.endswith((".js", ".html")) and n != "sw.js":
                files.append(os.path.join(base, n))
    failures, seen = [], 0
    for path in sorted(files):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for kind, tag, line in tags(src):
            if not text_field(kind, tag):
                continue
            seen += 1
            if not any(mark in tag for mark in MARKS):
                rel = os.path.relpath(path, os.path.join(HERE, ".."))
                failures.append(f"{rel}:{line}: <{kind}> takes words but no dictation "
                                f"(add data-dictate-field + dictateButtonHtml, or say why not)")
    if seen < 10:
        failures.append(f"only {seen} text fields found — the scan is broken")
    for f in failures:
        print("FAIL", f)
    if failures:
        return 1
    print(f"ok  all {seen} text fields take dictation")
    return 0


if __name__ == "__main__":
    sys.exit(main())

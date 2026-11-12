#!/usr/bin/env python3
"""Bare URLs in the session log page's answers (web-gateway.py, _render_answer).

GET /conversation/<date> renders each logged answer with markdown-it (raw
HTML off) and then turns bare http(s) URLs into links. That second step works
on the rendered HTML, so it must only ever touch text between tags: a URL in
an attribute (an image's src, alt or title, a link's href) is part of the
tag, and rewriting it there broke the tag apart. This pins:

- an image is shown as a link to it, never loaded (the page's
  Content-Security-Policy would block it), and a URL in its src, alt or
  title gains no link of its own;
- a bare URL in text is still linked, with the trailing punctuation of the
  sentence left outside the link, and the URL escaped for the attribute;
- a URL inside a code span, a code block or a link is left alone;
- the answer's own text never becomes markup.

Standalone:

    python3 tests/test_conversation_log_links.py
"""
import html.parser
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load_gateway(tmp: Path):
    os.environ["CHAT_STATE_DIR"] = str(tmp / "chat-state")
    os.environ["CONVERSATIONS_DIR"] = str(tmp / "convs")
    os.environ["CONVERSATION_DIR"] = str(tmp / "convlog")
    os.environ["CHAMBERS_DIR"] = str(tmp / "chambers")
    os.environ["WEB_GATEWAY_STATE"] = str(tmp / "state.json")
    os.environ["PUSH_DIR"] = str(tmp / "push")
    (tmp / "chambers").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "web_gateway_log_links_under_test", SCRIPTS_DIR / "web-gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Tags(html.parser.HTMLParser):
    """Every start tag with its attributes, and the text inside each <a>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.links: list[tuple[str, str]] = []
        self._href = None
        self._text = ""

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), ""

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        if self._href is not None:
            self._text += data

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, self._text))
            self._href = None


def _parse(markup: str) -> _Tags:
    p = _Tags()
    p.feed(markup)
    p.close()
    return p


def test_image_is_a_link_with_its_attributes(wg):
    out = wg._render_answer(
        '![a https://alt.example/x](https://img.example/p.png "see https://t.example")')
    p = _parse(out)
    # The image is a link to it, carrying its URL and title exactly; the URLs
    # in its alt text and title gain no link of their own.
    assert [t for t, _ in p.tags] == ["p", "a"], out
    assert p.tags[1][1] == {"href": "https://img.example/p.png",
                            "title": "see https://t.example"}, out
    assert p.links == [("https://img.example/p.png", "a https://alt.example/x")], out
    print("PASS test_image_is_a_link_with_its_attributes")


def test_no_image_is_loaded(wg):
    """The session log page loads no image an answer names: its
    Content-Security-Policy (img-src) would block one from another origin or
    a data: URL, so the page shows a link to it instead, as the dashboard's
    Markdown renderer does (webapp/components/markdown.js)."""
    answer = ("![remote](https://example.org/x.png) "
              "![](https://example.org/q.png?a=1&b=2) "
              "![inline](data:image/png;base64,iVBORw0KGgo=) "
              "![here](/data/x.png) ![other](//elsewhere.example/x.png) "
              "[![nested](https://example.org/n.png)](https://target.example/)")
    out = wg._render_answer(answer)
    p = _parse(out)
    assert not [a for t, a in p.tags if t == "img"], out
    assert p.links == [("https://example.org/x.png", "remote"),
                       ("https://example.org/q.png?a=1&b=2", "https://example.org/q.png?a=1&b=2"),
                       ("/data/x.png", "here"),
                       ("https://target.example/", "nested")], out
    # A data: image and one on another origin's relative form are their alt text.
    assert "inline" in out and "other" in out and "data:" not in out and "//elsewhere" not in out
    # The whole page, as GET /conversation/<date> builds it.
    page = wg._render_day_html(
        [{"anchor": "a1", "timestamp": "2026-09-02T10:00:00", "question": "Show me",
          "answer": answer}], "2026-09-02", ["2026-09-02"])
    assert not [a for t, a in _parse(page).tags if t == "img"], page
    print("PASS test_no_image_is_loaded")


def test_bare_url_in_text_is_linked(wg):
    out = wg._render_answer("See https://x.example/a?b=1&c=2. Or (https://y.example/z), done")
    p = _parse(out)
    assert p.links == [("https://x.example/a?b=1&c=2", "https://x.example/a?b=1&c=2"),
                       ("https://y.example/z", "https://y.example/z")], out
    # The sentence keeps its punctuation, outside the links.
    assert "</a>. Or (" in out and "</a>), done" in out, out
    # A quote or an angle bracket from the text never ends the attribute.
    out = wg._render_answer('Open https://q.example/"x\'y<b>z')
    p = _parse(out)
    assert [t for t, _ in p.tags] == ["p", "a"], out
    assert p.links[0][0] == "https://q.example/\"x'y", out
    print("PASS test_bare_url_in_text_is_linked")


def test_code_and_links_are_left_alone(wg):
    out = wg._render_answer(
        "`https://code.example/x` and [label](https://l.example/) and "
        "<https://auto.example/> and [https://inner.example](https://outer.example)\n\n"
        "```\nhttps://pre.example/x\n```\n\n    https://indented.example/x\n")
    p = _parse(out)
    assert p.links == [("https://l.example/", "label"),
                       ("https://auto.example/", "https://auto.example/"),
                       ("https://outer.example", "https://inner.example")], out
    assert "<code>https://code.example/x</code>" in out, out
    assert "<pre><code>https://pre.example/x\n</code></pre>" in out, out
    assert "<pre><code>https://indented.example/x\n</code></pre>" in out, out
    print("PASS test_code_and_links_are_left_alone")


def test_text_stays_text(wg):
    out = wg._render_answer('<img src=x onerror=alert(1)> https://e.example/<script>')
    p = _parse(out)
    assert [t for t, _ in p.tags] == ["p", "a"], out
    assert p.links == [("https://e.example/", "https://e.example/")], out
    assert "&lt;script&gt;" in out and "&lt;img" in out, out
    # A table cell and a heading are text like any other.
    out = wg._render_answer("# https://h.example\n\n| a |\n|:-:|\n| https://cell.example |\n")
    assert [h for h, _ in _parse(out).links] == ["https://h.example", "https://cell.example"], out
    assert '<th style="text-align:center">' in out, out
    print("PASS test_text_stays_text")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        gw = _load_gateway(Path(tmp))
        test_image_is_a_link_with_its_attributes(gw)
        test_no_image_is_loaded(gw)
        test_bare_url_in_text_is_linked(gw)
        test_code_and_links_are_left_alone(gw)
        test_text_stays_text(gw)
    print("all conversation log link tests passed")

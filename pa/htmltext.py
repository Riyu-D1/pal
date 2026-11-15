"""Tiny HTML-to-text extraction on top of html.parser (no dependency).

Drops script/style/template/noscript, keeps a rough reading order, and
collapses whitespace. Good enough to feed a fetched page to the model as
bounded untrusted text.
"""

import re
from html.parser import HTMLParser

_SKIP = {"script", "style", "template", "noscript", "svg", "head"}
_BLOCK = {"p", "div", "section", "article", "header", "footer", "main",
          "aside", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3",
          "h4", "h5", "h6", "blockquote", "pre", "br", "hr", "nav"}


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str, max_chars: int = 60_000) -> str:
    ex = _Extractor()
    try:
        ex.feed(html[:2_000_000])
    except Exception:
        pass
    text = " ".join("".join(ex.parts).split())
    return text[:max_chars]

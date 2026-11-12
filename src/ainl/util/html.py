"""Canvas descriptions are HTML; chat wants text. Keep links, drop the rest."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "blockquote", "pre"}


class _Text(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.out: list[str] = []
        self.links: list[tuple[str, str]] = []      # (label, absolute url)
        self._href: str | None = None
        self._label: list[str] = []
        self._skip = 0
        self._link_end = -1          # len(out) right after the last link was written

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a" and a.get("href"):
            self._href = urljoin(self.base, a["href"])
            self._label = []
            if len(self.out) == self._link_end:
                # Two links with nothing between them (PHYSICS247 Problem Set 3:
                # "…files/55159102?wrap=1)https://…/54999348") ran together into
                # one unreadable, unclickable string.
                self.out.append("\n")
        elif tag == "li":
            self.out.append("\n• ")
        elif tag in _BLOCK:
            self.out.append("\n")
        elif tag == "img" and a.get("alt"):
            self.out.append(f"[image: {a['alt']}]")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href:
            label = "".join(self._label).strip() or self._href
            self.links.append((label, self._href))
            self.out.append(f"{label} ({self._href})" if label != self._href else self._href)
            self._link_end = len(self.out)
            self._href = None
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._href is not None:
            self._label.append(data)
        else:
            self.out.append(data)


def html_to_text(html: str, base_url: str = "") -> tuple[str, list[tuple[str, str]]]:
    """Returns (plain_text, [(label, url), …])."""
    p = _Text(base_url)
    p.feed(html or "")
    text = "".join(p.out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), p.links


_FILE_RE = re.compile(r"/courses/(\d+)/files/(\d+)")


def canvas_file_ids(links: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """(label, course_id, file_id) for every Canvas file link."""
    out = []
    for label, url in links:
        m = _FILE_RE.search(url)
        if m:
            out.append((label, m.group(1), m.group(2)))
    return out

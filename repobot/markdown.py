"""Sanitized GitHub-flavored markdown rendering for the PR drawer.

`markdown-it-py` handles CommonMark + GFM-style extensions; `bleach`
scrubs the output so user-controlled content (PR bodies, comments)
can't inject arbitrary HTML/JS. GitHub-style autolinking of issue /
PR references is added as a post-processing step — it's cheap and
keeps the library surface minimal.
"""
import re

import bleach
from markdown_it import MarkdownIt

_md = (MarkdownIt("gfm-like", {"breaks": True, "linkify": True})
       .enable(["table", "strikethrough"]))

ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "em", "del", "code", "pre", "blockquote",
    "ul", "ol", "li", "a", "img",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
    "input",  # task lists produce <input type="checkbox" disabled>
    "details", "summary",
    "span", "div",
}

ALLOWED_ATTRS = {
    "a": ["href", "title", "rel", "target"],
    "img": ["src", "alt", "title", "width", "height"],
    "input": ["type", "disabled", "checked"],
    "code": ["class"],
    "pre": ["class"],
    "span": ["class"],
    "div": ["class"],
    "th": ["align"],
    "td": ["align"],
}

ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def _autolink_issue_refs(html: str, owner: str, name: str) -> str:
    """Turn `#NNNN` and `owner/repo#NNNN` into links. Operates on
    rendered HTML but only outside existing anchor tags."""
    base = f"https://github.com/{owner}/{name}"
    def repl_same(m):
        num = m.group(1)
        return f'<a href="{base}/issues/{num}">#{num}</a>'
    def repl_cross(m):
        o, n, num = m.group(1), m.group(2), m.group(3)
        return f'<a href="https://github.com/{o}/{n}/issues/{num}">{o}/{n}#{num}</a>'
    # Split on anchor tags so we don't rewrite inside existing links.
    parts = re.split(r'(<a\b[^>]*>.*?</a>)', html, flags=re.DOTALL | re.IGNORECASE)
    for i in range(0, len(parts), 2):  # even indices are outside <a>
        parts[i] = re.sub(r'\b([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)#(\d+)\b',
                          repl_cross, parts[i])
        parts[i] = re.sub(r'(?<![\w/])#(\d+)\b', repl_same, parts[i])
    return "".join(parts)


def render(body: str | None, *, owner: str = "", name: str = "") -> str:
    """Render markdown → sanitized HTML string. Empty / None → ''."""
    if not body:
        return ""
    html = _md.render(body)
    if owner and name:
        html = _autolink_issue_refs(html, owner, name)
    cleaned = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    # All outbound links open in a new tab with noopener/noreferrer.
    cleaned = re.sub(
        r'<a\b([^>]*?)>',
        lambda m: '<a' + m.group(1) + ' target="_blank" rel="noopener noreferrer">',
        cleaned,
    )
    return cleaned

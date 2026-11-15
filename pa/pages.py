"""Shareable Markdown pages (docs/PARITY.md #25).

`page_publish` stores owner-approved Markdown under an unguessable
slug; `GET /pages/<slug>` renders it — the URL itself is the
capability, so no session is required. All stored text is escaped
before the (tiny) Markdown transform — a page can never inject
markup or scripts.
"""

import html
import re
import secrets

from .db import audit
from .util import now_iso

MAX_MD = 200_000
_SLUG = r"[a-z0-9-]{1,64}"


def new_slug() -> str:
    return secrets.token_urlsafe(8).replace("_", "-").lower()


def publish(conn, title: str, markdown: str, slug: str = None) -> dict:
    title = (title or "untitled").strip()[:200]
    md = (markdown or "")[:MAX_MD]
    if not md.strip():
        return {"error": "bad_arguments", "message": "empty page"}
    if slug:
        slug = re.sub(r"[^a-z0-9-]", "-", slug.lower())[:64] \
            .strip("-") or new_slug()
    else:
        slug = new_slug()
    now = now_iso()
    conn.execute(
        "INSERT INTO pages(slug, title, markdown, enabled, created_at,"
        " updated_at) VALUES(?,?,?,1,?,?)"
        " ON CONFLICT(slug) DO UPDATE SET title=excluded.title,"
        " markdown=excluded.markdown, enabled=1,"
        " updated_at=excluded.updated_at",
        (slug, title, md, now, now))
    audit(conn, "assistant", "page_publish",
          f"{slug} ({len(md)}B)")
    return {"slug": slug, "title": title}


def unpublish(conn, slug: str) -> bool:
    cur = conn.execute(
        "UPDATE pages SET enabled=0, updated_at=? WHERE slug=?",
        (now_iso(), slug))
    if cur.rowcount:
        audit(conn, "owner", "page_unpublish", slug)
    return cur.rowcount > 0


def get(conn, slug: str):
    return conn.execute(
        "SELECT slug, title, markdown FROM pages"
        " WHERE slug=? AND enabled=1", (slug,)).fetchone()


def list_pages(conn, limit=50):
    return conn.execute(
        "SELECT slug, title, updated_at FROM pages WHERE enabled=1"
        " ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()


# -- tiny safe renderer -----------------------------------------------------
_INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"\*([^*]+)\*"), r"<em>\1</em>"),
    (re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)"),
     r'<a href="\2" rel="noopener noreferrer">\1</a>'),
]


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    for rx, rep in _INLINE:
        out = rx.sub(rep, out)
    return out


def render(markdown: str, title: str) -> str:
    """Markdown -> a self-contained HTML page. Input is escaped
    FIRST: no stored string can become live markup."""
    blocks, buf = [], []

    def flush():
        if buf:
            blocks.append("<p>" + "<br>\n".join(buf) + "</p>")
            buf.clear()

    in_code, in_list = False, False
    for line in (markdown or "").splitlines():
        if line.strip().startswith("```"):
            if in_code:
                blocks.append("</code></pre>")
            else:
                flush()
                blocks.append("<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            blocks.append(html.escape(line) + "\n")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush()
            lvl = len(m.group(1))
            blocks.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            continue
        if re.match(r"^\s*[-*]\s+", line):
            if not in_list:
                flush()
                blocks.append("<ul>")
                in_list = True
            blocks.append("<li>" + _inline(
                re.sub(r"^\s*[-*]\s+", "", line)) + "</li>")
            continue
        if in_list:
            blocks.append("</ul>")
            in_list = False
        if not line.strip():
            flush()
        else:
            buf.append(_inline(line.strip()))
    if in_code:
        blocks.append("</code></pre>")
    if in_list:
        blocks.append("</ul>")
    flush()
    t = html.escape(title or "page")
    return ("<!doctype html><html><head><meta charset=utf-8>"
            f"<title>{t}</title>"
            "<meta name=viewport content='width=device-width'>"
            "<style>body{max-width:44rem;margin:2rem auto;padding:0 1rem;"
            "font:16px/1.6 system-ui,sans-serif;color:#1a1a1a}"
            "pre{background:#f4f4f4;padding:1em;overflow-x:auto}"
            "code{font-family:ui-monospace,monospace;font-size:.92em}"
            "h1{border-bottom:2px solid #eee;padding-bottom:.3em}"
            "</style></head><body><h1>" + t + "</h1>"
            + "".join(blocks) + "</body></html>")


def valid_slug(slug: str) -> bool:
    return bool(re.fullmatch(_SLUG, slug or ""))

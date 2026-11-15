"""incremental read-only indexing of the owner's Notion into
recall FTS + the knowledge graph (PA_NOTION_INDEX, default
off; 'notion-index' sysjob seeded disabled, hourly, migration 46).

Reads pages through the existing 'notion' connection (MCP or Composio
— tool slugs probed at runtime, never hard-required). Each tick pulls
at most _PAGES_PER_TICK pages, resuming from a cursor in pa_settings;
page title + excerpt land in recall_fts with source='notion' and the
same sha manifest as local sources, so unchanged pages cost nothing
and recall.reindex leaves our rows alone (it skips source='notion').

Changed pages also feed kg.build as virtual 'notion://<id>' notes —
same code path as vault notes.

An expired/revoked token stops the tick and sends ONE deduped
"reconnect Notion" card pointing at the existing /connect flow.
Nothing ever writes to Notion.
"""

import logging

from . import db as dbmod
from .util import now_iso

log = logging.getLogger("pa.notionindex")

_PAGES_PER_TICK = 25
_CURSOR = "notion_cursor"
_SEARCH_SLUGS = ("search", "notion-search", "NOTION_SEARCH",
                 "NOTION_SEARCH_PAGES", "NOTION_SEARCH_DATABASE")
_PAGE_SLUGS = ("get-page", "fetch-page", "retrieve-page",
               "NOTION_GET_PAGE", "NOTION_FETCH_PAGE",
               "NOTION_RETRIEVE_PAGE")


def _row(conn):
    from . import connectflows
    row = connectflows.get(conn, "notion")
    if row and row.get("status") == "active":
        return row
    return None


def _call(bot, conn, row, slugs, args):
    """First slug the connection accepts. ConnectError('reconnect')
    propagates — the caller turns it into the reconnect card."""
    from . import connectflows
    last = None
    for slug in slugs:
        try:
            return connectflows.call_tool(
                conn, bot.s, bot.env, row, slug, args)
        except connectflows.ConnectError:
            raise
        except Exception as e:
            last = e
    if last:
        raise last
    return {}


def _pages(out: dict) -> list:
    res = out.get("result", out)
    for key in ("results", "pages", "items", "data"):
        v = res.get(key) if isinstance(res, dict) else None
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _title(p: dict) -> str:
    t = p.get("title") or p.get("name") or ""
    if isinstance(t, list):                    # notion rich-text shape
        t = "".join(x.get("plain_text", "") for x in t
                    if isinstance(x, dict))
    if not t:
        props = p.get("properties") or {}
        for v in props.values():
            if isinstance(v, dict) and v.get("type") == "title":
                t = "".join(x.get("plain_text", "") for x in
                            (v.get("title") or [])
                            if isinstance(x, dict))
    return str(t or "(untitled)")[:160]


def _body(bot, conn, row, pid: str) -> str:
    try:
        out = _call(bot, conn, row, _PAGE_SLUGS, {"page_id": pid})
    except Exception:
        return ""
    res = out.get("result", out)
    if isinstance(res, dict):
        for key in ("content", "text", "markdown", "body"):
            v = res.get(key)
            if isinstance(v, str) and v.strip():
                return v[:1500]
        blocks = res.get("blocks") or res.get("children")
        if isinstance(blocks, list):
            parts = []
            for b in blocks[:80]:
                if isinstance(b, dict):
                    rt = (b.get(b.get("type", ""), {}) or {}).get(
                        "rich_text") or []
                    parts.append("".join(
                        x.get("plain_text", "") for x in rt
                        if isinstance(x, dict)))
            return "\n".join(p for p in parts if p)[:1500]
    if isinstance(res, str):
        return res[:1500]
    return ""


def _upsert(conn, ref_id: str, title: str, body: str, day: str) -> bool:
    """-> True when the row changed (new or re-hashed)."""
    from . import recall
    sha = recall._sha(title + "\n" + body)
    old = conn.execute(
        "SELECT rowid_, sha FROM recall_meta WHERE source='notion'"
        " AND ref_id=?", (ref_id,)).fetchone()
    if old and old["sha"] == sha:
        return False
    if old:
        conn.execute("DELETE FROM recall_fts WHERE rowid=?",
                     (old["rowid_"],))
    cur = conn.execute(
        "INSERT INTO recall_fts(source, ref_id, title, body, day)"
        " VALUES('notion',?,?,?,?)",
        (ref_id, title, body[:recall.EXCERPT], day))
    conn.execute(
        "INSERT OR REPLACE INTO recall_meta(source, ref_id, rowid_,"
        " sha, indexed_at) VALUES('notion',?,?,?,?)",
        (ref_id, cur.lastrowid, sha, now_iso()))
    return True


def _reconnect_card(bot, conn) -> None:
    """One deduped 'reconnect Notion' card (the /connect flow already
    exists — we only point at it)."""
    from . import notify
    notify.notify(
        bot, conn, "notion", "normal",
        "🔌 Notion needs reconnecting — the saved grant was rejected. "
        "Run `/connect notion` to refresh it; indexing resumes on the "
        "next tick.",
        dedupe_key="notion:reconnect")


def run(bot, payload) -> str:
    conn = dbmod.connect(bot.db_path)
    try:
        if not getattr(bot.s, "notion_index", False):
            return "notion-index off"
        row = _row(conn)
        if row is None:
            return "notion not connected"
        from . import connectflows
        cursor = dbmod.get_setting(conn, _CURSOR, "")
        try:
            out = _call(bot, conn, row, _SEARCH_SLUGS,
                        {"start_cursor": cursor or None,
                         "page_size": _PAGES_PER_TICK})
        except connectflows.ConnectError as e:
            _reconnect_card(bot, conn)
            return f"reconnect needed: {str(e)[:80]}"
        except Exception as e:
            log.info("notion search failed: %s", e)
            return f"search failed: {str(e)[:80]}"
        pages = _pages(out)[:_PAGES_PER_TICK]
        res = out.get("result", out)
        nxt = ""
        if isinstance(res, dict):
            nxt = str(res.get("next_cursor") or res.get("cursor")
                      or "")
        if not nxt and len(pages) >= _PAGES_PER_TICK:
            nxt = cursor or ""       # stay put; next tick continues
        changed, skipped = 0, 0
        bodies, kg_notes = {}, []
        from . import recall
        for p in pages:
            pid = str(p.get("id") or "")
            if not pid:
                continue
            title = _title(p)
            body = _body(bot, conn, row, pid)
            path = f"notion://{pid}"
            bodies[path] = body
            sha = recall._sha(title + "\n" + body)
            if _upsert(conn, pid, title, body,
                       now_iso()[:10]):
                changed += 1
            else:
                skipped += 1
            kg_notes.append((path, sha, title))
        try:
            from . import kg
            kg.build(conn, lambda p: bodies.get(p, ""), kg_notes)
        except Exception as e:
            log.info("kg notion build failed: %s", e)
        dbmod.set_setting(conn, _CURSOR, nxt)
        return (f"{changed} indexed, {skipped} unchanged, "
                f"cursor {'next' if nxt else 'end'}")
    finally:
        conn.close()

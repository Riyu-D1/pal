"""Composio REST API v3.1 — the extras catalog for /connect.

Used when the app isn't in the self-hosted MCP catalog and
COMPOSIO_API_KEY is set. The official API is plain REST, so a thin
client on pa.netclient keeps this dependency-free:

  POST /tool_router/session          {user_id} -> session_id
  POST /tool_router/session/{id}/link {toolkit} -> connect link
  GET  /connected_accounts/{id}      -> connection status
  GET  /toolkits?search=<q>          -> catalog for /connect autocomplete
  GET  /tools?toolkit_slug=<slug>    -> tools for a connected app
  POST /tools/execute/{slug}         -> run one tool (counts a call)

Every execute() bumps a local monthly counter (composio_usage table) —
Composio's free "Hobby" platform tier allows 100k tool calls/month but
only 20k on Composio-managed apps, which is what connect-link sessions
use; the owner is warned in Discord at 80% of that cap.
"""

import datetime
import urllib.parse

from .netclient import APIClient, HTTPError, TransportError
from .util import utcnow

USER_AGENT_NOTE = "pa connections"

_TOOLKIT_CACHE = {}
_TOOLKIT_CACHE_TTL = 300            # seconds; autocomplete fires per keypress


class ComposioError(Exception):
    pass


def _month() -> str:
    return utcnow().strftime("%Y-%m")


class ComposioClient:
    def __init__(self, settings, api_key: str, timeout: float = 20.0):
        self.api = APIClient(settings.composio_api_base, timeout=timeout)
        self._h = {"x-api-key": api_key}
        self.user_id = settings.composio_user_id

    def _req(self, method, path, body=None):
        try:
            h, data = self.api.request(method, path, body=body,
                                       headers=self._h)
            if data:
                try:
                    from .util import json_loads_bounded
                    return json_loads_bounded(data.decode("utf-8"))
                except ValueError:
                    return {}
            return {}
        except HTTPError as e:
            raise ComposioError(
                f"composio HTTP {e.status} on {method} {path}")
        except TransportError as e:
            raise ComposioError(f"composio unreachable: {e}")

    # -- connect link --------------------------------------------------------
    def connect_link(self, toolkit: str, callback_url: str = ""):
        """Create a tool-router session + auth link for `toolkit`.
        -> {session_id, redirect_url, connected_account_id}"""
        sess = self._req("POST", "/tool_router/session",
                         {"user_id": self.user_id}) or {}
        sid = sess.get("session_id") or sess.get("id")
        if not sid:
            raise ComposioError("tool_router/session returned no id")
        body = {"toolkit": toolkit}
        if callback_url:
            body["callback_url"] = callback_url
        link = self._req("POST", f"/tool_router/session/{sid}/link",
                         body) or {}
        url = (link.get("redirect_url") or link.get("redirectUrl")
               or link.get("link_url") or "")
        if not url:
            raise ComposioError(
                "link session returned no redirect_url")
        return {"session_id": sid, "redirect_url": url,
                "connected_account_id":
                    link.get("connected_account_id")
                    or link.get("connectedAccountId") or ""}

    def account(self, connected_account_id: str):
        """-> {id, status, toolkit} ; status ACTIVE means connected."""
        data = self._req("GET",
                         f"/connected_accounts/{connected_account_id}")
        return {"id": data.get("id") or connected_account_id,
                "status": (data.get("status") or "").upper(),
                "toolkit": (data.get("toolkit") or {}).get("slug")
                if isinstance(data.get("toolkit"), dict)
                else data.get("toolkit") or ""}

    # -- catalog -------------------------------------------------------------
    def toolkits(self, search: str = "", limit: int = 25):
        """Search the full toolkit catalog (live; powers /connect
        autocomplete). -> [{slug, name}] sorted by usage. Results are
        cached briefly per query — autocomplete fires on every
        keystroke."""
        key = ((search or "").strip().lower(), int(limit or 25))
        ent = _TOOLKIT_CACHE.get(key)
        if ent and ent[0] > utcnow():
            return ent[1]
        q = {"limit": max(1, min(int(limit or 25), 100)),
             "sort_by": "usage"}
        if (search or "").strip():
            q["search"] = search.strip()
        data = self._req("GET", "/toolkits?" + urllib.parse.urlencode(q))
        items = data.get("items") or data.get("toolkits") or []
        out = [{"slug": t.get("slug"), "name": t.get("name") or t["slug"]}
               for t in items
               if isinstance(t, dict) and t.get("slug")]
        if len(_TOOLKIT_CACHE) > 100:
            _TOOLKIT_CACHE.clear()
        _TOOLKIT_CACHE[key] = (
            utcnow() + datetime.timedelta(seconds=_TOOLKIT_CACHE_TTL),
            out)
        return out

    # -- tools ---------------------------------------------------------------
    def tools(self, toolkit: str):
        """-> [{slug, description, schema, read_only}] for a connected
        toolkit. The v3 filter is `toolkit_slug` — plain `toolkit` is
        silently ignored (it returned unrelated toolkits live)."""
        q = urllib.parse.urlencode(
            {"toolkit_slug": toolkit, "limit": 200})
        data = self._req("GET", f"/tools?{q}")
        items = data.get("items") or data.get("tools") or []
        out = []
        for t in items:
            if not isinstance(t, dict):
                continue
            slug = t.get("slug") or t.get("name") or ""
            if not slug:
                continue
            schema = (t.get("input_parameters") or t.get("input_schema")
                      or t.get("schema") or {})
            out.append({"slug": slug,
                        "description": (t.get("description") or "")[:300],
                        "schema": schema,
                        "read_only": tool_read_only(slug, t, toolkit)})
        return out

    def execute(self, slug: str, arguments: dict,
                connected_account_id: str = ""):
        body = {"user_id": self.user_id,
                "arguments": arguments or {}}
        if connected_account_id:
            body["connected_account_id"] = connected_account_id
        return self._req(
            "POST", f"/tools/execute/{urllib.parse.quote(slug)}", body)


# -- read-only classification ---------------------------------------------------
#
# Composio slugs are TOOLKIT_VERB_... (GMAIL_FETCH_EMAILS). Only the
# verbs below are pure reads; send/delete/create/modify and any unknown
# verb stays gated behind the owner's Approve/Reject.

READ_ONLY_VERBS = frozenset({
    "FETCH", "GET", "LIST", "SEARCH", "FIND", "READ", "RETRIEVE",
    "QUERY", "CHECK", "VIEW", "LOOKUP", "DOWNLOAD", "EXPORT",
    "COUNT", "COMPARE", "VALIDATE", "CALCULATE", "CONVERT",
})
_READ_ONLY_TAGS = frozenset({"read", "readonly", "readonlyhint"})


def tool_read_only(slug: str, item: dict, toolkit: str = "") -> bool:
    """True when the tool only reads data. Composio's own
    annotations/tags win; otherwise the slug's verb decides (unknown
    verbs fail closed — gated)."""
    ann = item.get("annotations") or {}
    if ann.get("readOnlyHint") or ann.get("read_only_hint"):
        return True
    tags = {str(t).strip().lower().replace("-", "").replace("_", "")
            for t in (item.get("tags") or [])}
    if tags & _READ_ONLY_TAGS:
        return True
    rest = slug.upper()
    tk = (toolkit or "").upper()
    if tk and rest.startswith(tk + "_"):
        rest = rest[len(tk) + 1:]
    elif "_" in rest:
        rest = rest.split("_", 1)[1]
    else:
        return False
    return rest.split("_", 1)[0] in READ_ONLY_VERBS


# -- monthly usage counter ----------------------------------------------------

def bump_usage(conn, scope: str = "owner-chat") -> int:
    """Increment this month's call counter; returns the new count.
    also charges the budget ledger under the caller's scope."""
    m = _month()
    conn.execute(
        "INSERT INTO composio_usage(month, calls) VALUES(?,1)"
        " ON CONFLICT(month) DO UPDATE SET calls=calls+1", (m,))
    try:
        from . import budget as _budget
        _budget.charge(conn, scope, "composio_calls", 1)
    except Exception:
        pass
    return usage_count(conn)


def usage_count(conn) -> int:
    row = conn.execute(
        "SELECT calls FROM composio_usage WHERE month=?",
        (_month(),)).fetchone()
    return row["calls"] if row else 0


def over_warn_threshold(settings, count: int) -> bool:
    limit = max(1, settings.composio_monthly_limit)
    return count >= int(limit * 0.8)

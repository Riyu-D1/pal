"""GitHub CI/PR events -> Discord cards.

On a webhook endpoint named 'github' or 'ci', CI-ish GitHub events
(workflow_run, check_suite, pull_request, issue_comment) are rendered
as a short card and posted to the owner DM — NO agent turn by
default (PA_GITHUB_HOOK_AGENT stays off; when on, the generic
untrusted-payload path runs instead). Nothing here writes back to
GitHub; the payload is display-only.
"""

import json
import logging

log = logging.getLogger("pa.ghcards")

GH_HOOK_NAMES = {"github", "ci"}
_EVENTS = ("workflow_run", "check_suite", "pull_request",
           "issue_comment")


def detect(event_header: str, body: bytes) -> str:
    """CI-ish GitHub event name or ''. The X-GitHub-Event header is
    authoritative; the JSON shape is the fallback so hooks fronted by
    a relay still resolve."""
    ev = (event_header or "").strip().lower()
    if ev in _EVENTS:
        return ev
    try:
        data = json.loads(body[:32000].decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(data, dict) or "repository" not in data:
        return ""
    for k in _EVENTS:
        if k in data:
            return k
    return ""


def _sha(v) -> str:
    return (str(v or "")[:7])


def render(event: str, payload: dict) -> str:
    """One Discord-card line set; '' when the event isn't one we
    render. Never raises — a weird payload just gets no card."""
    if not isinstance(payload, dict):
        return ""
    try:
        repo = ((payload.get("repository") or {}).get("full_name")
                or "?")
        if event == "workflow_run":
            w = payload.get("workflow_run")
            if not isinstance(w, dict):
                return ""
            concl = w.get("conclusion") or w.get("status") or "?"
            icon = {"success": "✅", "failure": "❌",
                    "cancelled": "🚫"}.get(concl, "⏳")
            return (f"{icon} **CI {concl}** — `{repo}`\n"
                    f"  {w.get('name') or '?'} · "
                    f"{w.get('head_branch') or '?'} @ "
                    f"{_sha(w.get('head_sha'))}\n"
                    f"  by {w.get('actor', {}).get('login') or '?'} · "
                    f"{w.get('html_url') or ''}").strip()
        if event == "check_suite":
            c = payload.get("check_suite")
            if not isinstance(c, dict):
                return ""
            concl = c.get("conclusion") or c.get("status") or "?"
            icon = {"success": "✅", "failure": "❌",
                    "cancelled": "🚫"}.get(concl, "⏳")
            app = (c.get("app") or {}).get("slug") or "checks"
            url = (payload.get("repository") or {}).get(
                "html_url") or ""
            return (f"{icon} **checks {concl}** — `{repo}`\n"
                    f"  {app} · {c.get('head_branch') or '?'} @ "
                    f"{_sha(c.get('head_sha'))}\n"
                    f"  {url}").strip()
        if event == "pull_request":
            p = payload.get("pull_request")
            if not isinstance(p, dict):
                return ""
            action = payload.get("action") or "?"
            head = _sha((p.get("head") or {}).get("sha"))
            return (f"🔀 **PR {action}** — `{repo}` "
                    f"#{p.get('number', '?')}\n"
                    f"  {p.get('title') or '(untitled)'}\n"
                    f"  by {(p.get('user') or {}).get('login') or '?'}"
                    f" · {head}\n  {p.get('html_url') or ''}").strip()
        if event == "issue_comment":
            issue = payload.get("issue") or {}
            c = payload.get("comment") or {}
            if not isinstance(issue, dict) or not isinstance(c, dict):
                return ""
            if "pull_request" not in issue:
                return ""            # only PR conversations are CI-ish
            return (f"💬 **PR comment** — `{repo}` "
                    f"#{issue.get('number', '?')}\n"
                    f"  {issue.get('title') or '(untitled)'}\n"
                    f"  by {(c.get('user') or {}).get('login') or '?'}"
                    f" · {c.get('html_url') or ''}").strip()
    except Exception as e:
        log.info("ghcard render failed: %s", e)
    return ""


def enqueue_card(conn, ep_row, card: str) -> None:
    """Queue the rendered card for the bot's 'post it, no agent turn'
    drain (source 'ghcard')."""
    from .util import now_iso
    conn.execute(
        "INSERT INTO chat_inbox(channel_id, prompt, source, status,"
        " created_at) VALUES('',?,'ghcard','queued',?)",
        (card[:8000], now_iso()))
    conn.execute(
        "UPDATE webhook_endpoints SET last_seen_at=? WHERE name=?",
        (now_iso(), ep_row["name"]))

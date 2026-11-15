"""A3: coursework layer — Google Classroom / Docs reads, a deadline
monitor, and PDF -> vault notes (final-5, PA_COURSEWORK, default off).

Toolkits are discovered AT RUNTIME: we look at the connections table
for a connected Composio account whose provider/meta toolkit smells
like Classroom or Docs, then pick tools by verb from whatever that
account actually offers — nothing is hard-coded as required, so a
free Composio tier without Classroom degrades to a clear
"not connected" answer while the rest of pal keeps working.

Everything unattended is READ-ONLY on the owner's Google account.
Any write (submit, comment, edit) goes through policy.decide and the
approval machinery — `write()` returns approval_required without ever
executing when it isn't allowed.

'coursework-watch' sysjob (migration 45, seeded disabled, hourly):
due dates land in pa_settings and notify() pings the owner at 24h and
2h leads — deduped, quiet-hours aware, one activity row each.
"""

import datetime
import logging
import re

from . import db as dbmod
from .util import now_iso

log = logging.getLogger("pa.coursework")

# candidate toolkits + tool verbs, matched loosely at runtime
_TOOLKIT_HINTS = ("classroom", "googleclassroom", "googledocs",
                  "docs", "drive")
_CLASS_VERBS = ("LIST_COURSES", "LIST_CLASSES", "GET_COURSES")
_WORK_VERBS = ("LIST_COURSEWORK", "LIST_ASSIGNMENTS", "GET_COURSEWORK",
               "LIST_COURSE_WORK")
_DOC_VERBS = ("GET_DOCUMENT", "READ_DOCUMENT", "EXPORT_DOCUMENT",
              "GET_DOC")
_DUE_LEADS = (2 * 60, 24 * 60)      # minutes, tightest first
_SEEN = "cw_seen:"
_DUE = "cw_due:"


def _meta_toolkit(row) -> str:
    import json
    try:
        meta = json.loads(row.get("meta_json") or "{}")
    except (ValueError, TypeError):
        meta = {}
    return str(meta.get("toolkit") or row.get("provider") or "").lower()


def find_toolkit(conn):
    """-> (connections row, toolkit slug) for a live Composio
    Classroom/Docs-ish connection, or (None, '')."""
    try:
        rows = conn.execute(
            "SELECT * FROM connections WHERE kind='composio' AND"
            " status='active'").fetchall()
    except Exception:
        return None, ""
    for r in rows:
        row = dict(r)
        tk = _meta_toolkit(row)
        if any(h in tk for h in _TOOLKIT_HINTS):
            return row, tk or row["provider"]
    return None, ""


def _pick_tool(conn, s, env, toolkit: str, verbs) -> str:
    """First tool slug the connected account offers whose verb
    matches — runtime detection, never a required slug."""
    try:
        from . import composio as composiomod
        cli = composiomod.ComposioClient(s, s.composio_key(env))
        tools = cli.tools(toolkit)
    except Exception as e:
        log.info("coursework tools probe failed: %s", e)
        return ""
    for t in tools:
        slug = (t.get("slug") or "").upper()
        if any(v in slug for v in verbs):
            return t["slug"]
    return ""


def _items(out: dict) -> list:
    """Normalize whatever the toolkit returned into a list of dicts."""
    res = out.get("result", out)
    for key in ("courses", "courseWork", "items", "data", "results"):
        v = res.get(key) if isinstance(res, dict) else None
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return [res] if isinstance(res, dict) else []


def list_classes(bot, conn) -> list:
    """-> [{id, name}] or {"error": ...}."""
    row, tk = find_toolkit(conn)
    if not row:
        return {"error": "not_connected",
                "message": "no Classroom/Docs toolkit on this account"
                           " — /connect one first"}
    slug = _pick_tool(conn, bot.s, bot.env, tk, _CLASS_VERBS)
    if not slug:
        return {"error": "toolkit_missing",
                "message": f"'{tk}' has no class-listing tool on this "
                           "free account"}
    from . import connectflows
    out = connectflows.call_tool(conn, bot.s, bot.env, row, slug, {})
    return [{"id": str(c.get("id") or c.get("courseId") or ""),
             "name": str(c.get("name") or c.get("title") or "?")}
            for c in _items(out)]


def list_assignments(bot, conn) -> list:
    """-> [{id, title, due, course}] with due normalized to ISO."""
    row, tk = find_toolkit(conn)
    if not row:
        return {"error": "not_connected",
                "message": "no Classroom/Docs toolkit on this account"
                           " — /connect one first"}
    slug = _pick_tool(conn, bot.s, bot.env, tk, _WORK_VERBS)
    if not slug:
        return {"error": "toolkit_missing",
                "message": f"'{tk}' has no assignment tool on this "
                           "free account"}
    from . import connectflows
    out = connectflows.call_tool(conn, bot.s, bot.env, row, slug, {})
    return [_norm_assignment(a) for a in _items(out)]


def _norm_assignment(a: dict) -> dict:
    due = (a.get("dueDate") or a.get("due_date") or a.get("due") or {})
    if isinstance(due, dict):
        try:
            t = due.get("dueTime") or {}
            due = datetime.datetime(
                int(due.get("year")), int(due.get("month")),
                int(due.get("day")), int(t.get("hours", 23)),
                int(t.get("minutes", 59)),
                tzinfo=datetime.timezone.utc).isoformat()
        except (TypeError, ValueError):
            due = ""
    return {"id": str(a.get("id") or a.get("courseWorkId") or ""),
            "title": str(a.get("title") or "?")[:160],
            "due": str(due or "")[:40],
            "course": str(a.get("courseId") or a.get("course") or "")}


def write(conn, s, slug: str, arguments: dict,
          unattended: bool = False) -> dict:
    """Every Classroom/Docs write funnels through policy.decide —
    submit/comment/edit all land here. Not allowed -> approval
    required, nothing is ever executed."""
    from . import policy
    d = policy.decide(conn, f"coursework.{slug}", arguments or {},
                      unattended=unattended,
                      account_owner="owner", action_class="write")
    if d.verdict != "allow":
        return {"error": "approval_required", "reason": d.reason,
                "verdict": d.verdict}
    row, _tk = find_toolkit(conn)
    if not row:
        return {"error": "not_connected"}
    from . import connectflows
    return connectflows.call_tool(conn, s, {}, row, slug,
                                  arguments or {})


# -- deadline monitor ----------------------------------------------------

def _parse_due(v):
    try:
        return datetime.datetime.fromisoformat(
            str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _fired(conn, aid: str, lead: int) -> bool:
    return bool(dbmod.get_setting(conn, f"{_DUE}{aid}:{lead}", ""))


def run(bot, payload) -> str:
    """'coursework-watch' sysjob: deadline pings at 24h/2h, deduped."""
    conn = dbmod.connect(bot.db_path)
    try:
        if not getattr(bot.s, "coursework", False):
            return "coursework off"
        from . import activity, notify
        items = list_assignments(bot, conn)
        if isinstance(items, dict):            # error envelope
            return items["error"]
        now = datetime.datetime.now(datetime.timezone.utc)
        fired = 0
        for a in items:
            aid = a.get("id") or a.get("title")
            if not aid or _parse_due(a.get("due")) is None:
                continue
            due = _parse_due(a["due"])
            away = (due - now).total_seconds() / 60.0
            if away < -60:
                continue
            for lead in _DUE_LEADS:
                if away <= lead and not _fired(conn, aid, lead):
                    text = (f"📚 **due {'today ' if lead <= 120 else ''}"
                            f"in ~{int(away / 60)}h{int(away % 60)}m**: "
                            f"{a['title']}"
                            + (f" ({a['course']})" if a['course'] else "")
                            + f"\n  due {a['due'][:16]}")
                    out = notify.notify(
                        bot, conn, "coursework", "high", text,
                        dedupe_key=f"cw:{aid}:{lead}")
                    if out in ("sent", "held"):
                        dbmod.set_setting(
                            conn, f"{_DUE}{aid}:{lead}", now_iso())
                        activity.record(
                            conn, "noticed",
                            f"deadline: {a['title'][:70]}",
                            body=text, source="coursework-watch",
                            ref={"id": aid, "lead": lead})
                        fired += 1
                        break        # tightest applicable lead only
        return f"{fired} deadline ping(s), {len(items)} assignment(s)"
    finally:
        conn.close()


# -- PDF -> vault notes --------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "pdf").lower())[:40] \
        .strip("-") or "pdf"


def extract_pdf_text(path: str) -> str:
    """pdftotext on the host if present — no new heavy deps. '' when
    the extractor is missing or the file can't be read."""
    import shutil, subprocess
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    try:
        out = subprocess.run(
            [exe, "-q", "-l", "30", path, "-"],
            capture_output=True, timeout=30)
        if out.returncode == 0:
            return out.stdout.decode("utf-8", "replace")[:50000]
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def pdf_to_note(conn, s, path: str, title: str = "",
                cls: str = "inbox") -> dict:
    """Extract PDF text (existing tools only) and write
    coursework/<cls>/<date>-<slug>.md through vault.write_note —
    git-versioned. No extractor -> a stub with the source pointer,
    never a crash."""
    from . import vault
    title = (title or "").strip() or "untitled"
    today = datetime.date.today().isoformat()
    rel = f"coursework/{_slug(cls) or 'inbox'}/{today}-{_slug(title)}.md"
    text = extract_pdf_text(path)
    if text:
        body = (f"# {title}\n\nsource: `{path}`\n\n---\n\n" + text)
    else:
        body = (f"# {title}\n\nsource: `{path}`\n\n"
                "*(no PDF extractor on this host — install `poppler` "
                "for `pdftotext`; the source file is linked above.)*\n")
    try:
        out = vault.write_note(conn, s, rel, body)
        out["extracted"] = bool(text)
        return out
    except Exception as e:
        return {"error": "vault_unavailable", "message": str(e)[:200]}

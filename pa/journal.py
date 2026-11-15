"""Profile note + daily journal (docs/PARITY.md #7).

- Profile.md: a human-readable vault note rendered from owner_facts —
  refreshed after every owner-fact write so the owner's shared profile
  lives in their notes too (not just the DB).
- daily-journal: a DISABLED-seeded system job ('/jobs daily-journal
  on' turns it on). Each evening it writes Journal/YYYY-MM-DD.md into
  the vault — today's chat/jobs/approvals/memory activity as Markdown
  bullets, optionally topped with a one-paragraph model summary.
  Without a vault the note goes to <data>/journal/.
"""

import logging
import os

from .util import now_iso

log = logging.getLogger("pa.journal")


def sync_profile(conn, settings) -> str:
    """Rewrite Profile.md from owner_facts. '' when no vault."""
    from . import vault
    if not vault.configured(settings):
        return ""
    facts = conn.execute(
        "SELECT key, value FROM owner_facts ORDER BY key").fetchall()
    lines = ["# Owner profile", "",
             "_Synced from pal's owner_facts — edit via chat "
             "('/memory forget fact <key>' or ask pal)._", ""]
    if facts:
        lines += [f"- **{f['key']}**: {f['value']}" for f in facts]
    else:
        lines.append("_(no facts recorded yet)_")
    out = vault.write_note(conn, settings, "Profile.md",
                           "\n".join(lines) + "\n")
    return out["path"]


def journal_text(conn, day: str) -> str:
    """Mechanical Markdown for one UTC day: chat turns, job runs,
    decisions and memory writes. Facts only — the model pass in run()
    adds prose on top."""
    like = day + "%"
    n_msgs = conn.execute(
        "SELECT COUNT(*) c, COUNT(DISTINCT channel_id) ch"
        " FROM chat_messages WHERE created_at LIKE ?", (like,)
    ).fetchone()
    jobs = conn.execute(
        "SELECT source, status, COUNT(*) n FROM chat_inbox"
        " WHERE created_at LIKE ? GROUP BY source, status",
        (like,)).fetchall()
    decisions = conn.execute(
        "SELECT status, COUNT(*) n FROM approvals"
        " WHERE decided_at LIKE ? GROUP BY status", (like,)).fetchall()
    mems = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE created_at LIKE ?",
        (like,)).fetchone()
    audits = conn.execute(
        "SELECT action, COUNT(*) n FROM audit_log WHERE at LIKE ?"
        " GROUP BY action ORDER BY n DESC LIMIT 10", (like,)
    ).fetchall()

    lines = [f"# Journal — {day}", ""]
    if n_msgs["c"]:
        lines.append(
            f"- {n_msgs['c']} chat messages across "
            f"{n_msgs['ch']} channel(s)")
    for j in jobs:
        lines.append(f"- {j['source']} inbox ×{j['n']} "
                     f"({j['status']})")
    for d in decisions:
        lines.append(f"- {d['n']} approval(s) → {d['status']}")
    if mems["c"]:
        lines.append(f"- {mems['c']} new memories saved")
    for a in audits:
        lines.append(f"- {a['action']} ×{a['n']}")
    if len(lines) == 2:
        lines.append("- quiet day — nothing recorded")
    return "\n".join(lines) + "\n"


def _dest_path(settings, day: str) -> str:
    """No-vault fallback: <data>/journal/<day>.md inside DATA_DIR."""
    base = os.path.join(os.path.dirname(
        os.path.abspath(settings.db_path)), "journal")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{day}.md")


def run(bot, payload: dict) -> str:
    """System-job handler (signature: run(bot, payload) -> str)."""
    from . import sysjobs, vault
    conn = bot._db()
    try:
        day = now_iso()[:10]
        body = journal_text(conn, day)
        summary = sysjobs.model_text(
            bot, conn,
            "Write ONE short paragraph (<=60 words) summing up the "
            "owner's day from this activity log — plain Markdown, no "
            "headers, no invented facts:\n\n" + body)
        if summary:
            body = body.rstrip() + f"\n\n> {summary}\n"
        if vault.configured(bot.s):
            out = vault.write_note(conn, bot.s,
                                   f"Journal/{day}.md", body)
            dest = out["path"]
        else:
            dest = _dest_path(bot.s, day)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(body)
        return f"journal written → {dest}"
    finally:
        conn.close()

"""evolve-digest sysjob (docs/EVOLVE.md #5): a weekly DM telling the
owner what pal improved, rolled back, or still needs them for.

Seeded disabled; quiet ("" → no DM) when the week had no changelog
events and nothing is open.
"""

import logging

from .. import sysjobs
from . import changelog, items

log = logging.getLogger("pa.evolve.digest")


def run(bot, payload) -> str:
    conn = bot._db()
    try:
        applied = [r for r in changelog.recent(
            conn, limit=20, verbs=("applied",), days=7)]
        rolled = [r for r in changelog.recent(
            conn, limit=20, verbs=("rolled_back",), days=7)]
        rejected = [r for r in changelog.recent(
            conn, limit=20, verbs=("rejected",), days=7)]
        open_items = items.list_items(conn)
        if not applied and not rolled and not rejected \
                and not open_items:
            return ""                      # quiet week — no DM
        lines = ["🧬 evolve digest — the week"]
        if applied:
            lines.append(f"✅ applied {len(applied)}:")
            for r in applied[:6]:
                lines.append(f"  • {r['text'][:100]}")
        if rolled:
            lines.append(f"↩️ rolled back {len(rolled)}:")
            for r in rolled[:4]:
                lines.append(f"  • {r['text'][:100]}")
        if rejected:
            lines.append(f"🚩 anti-gaming rejected {len(rejected)}:")
            for r in rejected[:6]:
                lines.append(f"  • {r['text'][:100]}")
        if open_items:
            need = [i for i in open_items
                    if i["status"] in ("awaiting_approval", "open",
                                       "needs_plan")]
            lines.append(f"⏳ {len(open_items)} open item(s)"
                         + (f", {len(need)} need a look: "
                            if need else ":"))
            for i in need[:5]:
                lines.append(
                    f"  • #{i['id']} [{i['shape']}/{i['risk']}] "
                    f"{i['title'][:80]}")
            lines.append("`/evolve` for the list, `/evolve <id>` "
                         "for a plan.")
        sysjobs.dm_and_voice(bot, "\n".join(lines), voice=False)
        return "digest sent"
    finally:
        conn.close()

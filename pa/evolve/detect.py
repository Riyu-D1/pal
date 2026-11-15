"""evolve-detect sysjob (docs/EVOLVE.md #4): cluster new incidents by
fingerprint, open/update an evolve_item when a threshold is crossed.

Seeded disabled; even when enabled it needs PA_EVOLVE=true to act.
Quiet by design — no owner DM ever; the run status lands on the
inbox row like every other system job.
"""

import datetime
import logging

from .. import db as dbmod
from ..util import now_iso, utcnow
from . import items

log = logging.getLogger("pa.evolve.detect")

SEEN_KEY = "evolve_detect_seen"
_WINDOW = datetime.timedelta(hours=24)

# incident kind -> (shape, risk) for items it opens
_KIND_MAP = {
    "not_configured": ("connector", "low"),
    "rate_limited": ("connector", "low"),
    "owner_correction": ("lesson", "low"),
    "feedback_bad": ("lesson", "low"),
    "low_confidence": ("lesson", "low"),
}
_DEFAULT = ("fix", "high")


def _shape_risk(kind):
    if kind == "eval_regress":
        return "fix", "high"
    return _KIND_MAP.get(kind, _DEFAULT)


def _crossed(conn, fp, kind, tool, count, s) -> bool:
    if kind == "health_fail" and count >= 1:
        return True
    if kind == "not_configured" and tool and count >= 2:
        return True
    return count >= s.evolve_detect_threshold


def run(bot, payload) -> str:
    if not bot.s.evolve:
        return "evolve off"
    conn = bot._db()
    try:
        seen = int(dbmod.get_setting(conn, SEEN_KEY, "0") or 0)
        rows = conn.execute(
            "SELECT * FROM incidents WHERE id>? ORDER BY id LIMIT 1000",
            (seen,)).fetchall()
        if not rows:
            return "quiet"
        dbmod.set_setting(conn, SEEN_KEY, str(rows[-1]["id"]))
        conn.commit()
        cutoff = (utcnow() - _WINDOW).isoformat()

        newest = {}          # fingerprint -> newest incident row
        for r in rows:
            newest[r["fingerprint"]] = r

        opened = 0
        for fp, r in newest.items():
            c = conn.execute(
                "SELECT COUNT(*) AS n FROM incidents"
                " WHERE fingerprint=? AND created_at>=?",
                (fp, cutoff)).fetchone()["n"]
            if not _crossed(conn, fp, r["kind"], r["tool"], c, bot.s):
                continue
            if opened >= bot.s.evolve_detect_max_items:
                break
            shape, risk = _shape_risk(r["kind"])
            iid = items.open_item(
                conn, shape, risk,
                title=f"{r['kind']}: {r['summary'][:200]}",
                incident_fingerprint=fp, dedupe_key=f"fp:{fp}",
                artifacts={"kinds": [r["kind"]],
                           "tool": r["tool"] or "",
                           "hits_24h": c})
            if iid:
                opened += 1
        return f"opened {opened} item(s)" if opened else "quiet"
    finally:
        conn.close()

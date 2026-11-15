"""final-2 M2: the sleeptime curator — a deterministic nightly pass
run from evolve-reflect (PA_SLEEPTIME, needs PA_MEMORY_BLOCKS).

- repeated owner-fact content seen across memories promotes into the
  'human' pinned block via an approval card (never silently);
- near-duplicate memories become a memory_write delete approval —
  nothing is ever deleted without the owner's click;
- stale '- YYYY-MM-DD' lines in the 'world' block are demoted by a
  direct versioned write (revertible via memory_block_history);
- a tool path that succeeded >=2 times today becomes a 'skill' evolve
  item proposal.
"""

import datetime
import logging
import re

from . import activity as actmod, approvals, memblocks

log = logging.getLogger("pa.sleeptime")

_DATE = re.compile(r"(20\d\d-\d\d-\d\d)")
STALE_DAYS = 30


def _repeated_facts(conn) -> list:
    """owner_fact values that also appear inside >=2 memory rows —
    evidence it's worth pinning on the human block."""
    facts = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM owner_facts").fetchall()}
    if not facts:
        return []
    mems = [r["content"].lower() for r in conn.execute(
        "SELECT content FROM memories").fetchall()]
    out = []
    for k, v in facts.items():
        hits = sum(1 for m in mems if str(v).lower()[:40] in m)
        if hits >= 2:
            out.append((k, v))
    return out[:3]


def _dupe_memories(conn) -> list:
    rows = conn.execute(
        "SELECT id, assistant_id, content FROM memories"
        " ORDER BY id").fetchall()
    seen, out = {}, []
    for r in rows:
        key = r["content"].strip().lower()[:120]
        if key and key in seen:
            out.append((r["id"], r["assistant_id"],
                        r["content"][:120]))
        else:
            seen[key] = r["id"]
    return out[:5]


def _stale_world_lines(conn, now=None) -> list:
    world = memblocks.get(conn, "world")["content"]
    now = now or datetime.datetime.now(datetime.timezone.utc).date()
    stale, keep = [], []
    for line in world.splitlines():
        m = _DATE.search(line)
        if m:
            try:
                when = datetime.date.fromisoformat(m.group(1))
            except ValueError:
                keep.append(line)
                continue
            if (now - when).days > STALE_DAYS:
                stale.append(line)
                continue
        keep.append(line)
    return stale, keep


def _success_paths(conn) -> list:
    try:
        return conn.execute(
            "SELECT detail, COUNT(*) n FROM turn_outcomes WHERE"
            " outcome='succeeded' AND created_at LIKE ?"
            " GROUP BY detail HAVING n>=2 ORDER BY n DESC LIMIT 3",
            (datetime.datetime.now(datetime.timezone.utc)
             .strftime("%Y-%m-%d") + "%",)).fetchall()
    except Exception:
        return []


def run(conn, s=None) -> dict:
    """-> {"promoted": n, "merge_proposals": n, "demoted": n,
           "skills": n}. Every destructive/touching-human change is a
    proposal card; block writes are versioned."""
    out = {"promoted": 0, "merge_proposals": 0, "demoted": 0,
           "skills": 0}
    try:
        # promote repeated facts into 'human' — always a card
        for key, value in _repeated_facts(conn):
            cur = memblocks.get(conn, "human")["content"]
            if key.lower() in cur.lower():
                continue
            new = (cur + f"\n- {key}: {value}").strip()
            aid = approvals.create_approval(
                conn, "job_action",
                {"tool": "memory_block_set",
                 "args": {"name": "human", "content": new},
                 "channel_id": "", "sleeptime": True})
            actmod.record(conn, "needs_approval",
                          f"sleeptime: pin '{key}' to the human "
                          "block", source="sleeptime",
                          ref={"approval_id": aid})
            out["promoted"] += 1
        # merge near-duplicates — delete proposal, never silent
        for mid, aid_, content in _dupe_memories(conn):
            aid = approvals.create_approval(
                conn, "memory_write",
                {"op": "delete", "memory_id": mid,
                 "assistant_id": aid_,
                 "reason": "sleeptime: duplicate memory",
                 "content": content})
            actmod.record(conn, "needs_approval",
                          f"sleeptime: merge duplicate memory "
                          f"#{mid} ({content[:60]})",
                          source="sleeptime",
                          ref={"approval_id": aid})
            out["merge_proposals"] += 1
        # demote stale world lines — versioned direct write
        stale, keep = _stale_world_lines(conn)
        if stale:
            memblocks.set_block(conn, "world", "\n".join(keep),
                                edited_by="sleeptime")
            actmod.record(conn, "did",
                          f"sleeptime: demoted {len(stale)} stale "
                          "world line(s)",
                          source="sleeptime")
            out["demoted"] += len(stale)
        # a path that worked twice -> skill proposal
        try:
            from .evolve import items
            for r in _success_paths(conn):
                iid = items.open_item(
                    conn, "skill", "low",
                    title=f"sleeptime: repeated success — "
                          f"{r['detail'][:100]}",
                    plan="this path succeeded twice today; a skill "
                         "could make it one call",
                    dedupe_key="sleeptime:"
                               + r["detail"][:80].lower())
                if iid:
                    out["skills"] += 1
        except Exception:
            pass
    except Exception:
        log.exception("sleeptime pass failed")
    return out

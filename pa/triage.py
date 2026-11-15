"""final-2 T1: inbox triage (PA_TRIAGE).

Every inbox-watch run labels each new mail into a pal-DB-only bucket
— reply-needed | fyi | newsletter | receipt | spam-ish — in the
inbox_triage table (migration 31). For reply-needed pal drafts a
reply locally: an activity 'draft' row + a send_email approval card.
Nothing touches Gmail until the owner taps Approve — there is no
unattended write path in this module at all.

bucket() is deterministic (cheap, testable); the draft body uses the
fast tier when it answers, else a template — the owner edits at the
Approve card either way.
"""

import logging
import re

from . import activity, approvals, sysjobs
from .util import now_iso

log = logging.getLogger("pa.triage")

BUCKETS = ("reply-needed", "fyi", "newsletter", "receipt",
           "spam-ish")

_BULK = re.compile(r"(?i)(no-?reply|donotreply|newsletter|digest|"
                   r"substack|mailchimp|mailer|bounce|notifications?@"
                   r"|alerts?@|update[s]?@|marketing)")
_RECEIPT = re.compile(r"(?i)(receipt|invoice|your order|order "
                      r"confirm|payment|e-?ticket|itinerary|refund|"
                      r"subscription renew)")
_NEWS = re.compile(r"(?i)(unsubscribe|weekly|daily digest|"
                   r"newsletter|roundup|edition|bulletin)")
_SPAM = re.compile(r"(?i)(congratulations|winner|prize|casino|"
                   r"lottery|click here|act now|limited time|"
                   r"free money|risk-?free)")
_ASK = re.compile(r"(?i)(\?|please|can you|could you|let me know|"
                  r"are you|do you|when you get a chance)")


def bucket(msg: dict) -> str:
    """Deterministic label from from/subject/snippet — no model."""
    frm = str(msg.get("from") or "")
    subj = str(msg.get("subject") or "")
    snip = str(msg.get("snippet") or "")
    hay = f"{subj} {snip}"
    if _SPAM.search(hay):
        return "spam-ish"
    if _RECEIPT.search(hay) or _RECEIPT.search(frm):
        return "receipt"
    if _NEWS.search(hay) or "newsletter" in frm.lower() \
            or "digest" in frm.lower():
        return "newsletter"
    if _BULK.search(frm):
        return "fyi"
    if _ASK.search(hay):
        return "reply-needed"
    return "fyi"


def _draft_body(bot, conn, msg) -> str:
    """Fast-tier draft, template fallback — capped short."""
    subj = (msg.get("subject") or "(no subject)")[:140]
    try:
        text = sysjobs.model_text(
            bot, conn,
            "Draft a short reply (3 sentences max, plain text, no "
            "sign-off beyond a first name placeholder) to this "
            "email.\nFrom: " + (msg.get("from") or "")[:120]
            + "\nSubject: " + subj + "\nSnippet: "
            + (msg.get("snippet") or "")[:300])
        if text:
            return text[:1000]
    except Exception:
        pass
    return (f"Hi — thanks for your note about \"{subj}\". "
            "I'll pick this up and come back to you shortly.")


def on_new_mail(bot, conn, msg: dict, mid: str) -> str:
    """Called from inboxwatch._run for each new message (flag already
    checked by the caller). Writes inbox_triage +, for reply-needed,
    a local draft + approval. Returns the bucket."""
    b = bucket(msg)
    aid_str = ""
    act_id = None
    if b == "reply-needed":
        body = _draft_body(bot, conn, msg)
        act_id = activity.record(
            conn, "draft",
            f"reply draft: {(msg.get('subject') or '(no subject)')[:80]}",
            body=body, source="triage",
            ref={"message_id": mid})
        apid = approvals.create_approval(
            conn, "job_action",
            {"tool": "send_email",
             "args": {"to": re.sub(r".*<([^>]+)>.*", r"\1",
                                   msg.get("from") or "")[:200],
                      "subject": "Re: "
                                 + (msg.get("subject") or "")[:200],
                      "body": body},
             "channel_id": "",
             "message_id": mid, "triage": True})
        activity.record(conn, "needs_approval",
                        f"send reply to "
                        f"{(msg.get('from') or '?')[:60]}",
                        source="triage",
                        ref={"approval_id": apid,
                             "message_id": mid})
        aid_str = apid
    conn.execute(
        "INSERT OR REPLACE INTO inbox_triage(message_id, bucket,"
        " subject, frm, activity_id, draft_approval_id, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (mid, b, (msg.get("subject") or "")[:300],
         (msg.get("from") or "")[:300], act_id, aid_str, now_iso()))
    return b


def today(conn) -> dict:
    """'/triage' — today's buckets with the reply-needed subjects."""
    day = now_iso()[:10] + "%"
    rows = conn.execute(
        "SELECT bucket, COUNT(*) n FROM inbox_triage"
        " WHERE created_at LIKE ? GROUP BY bucket",
        (day,)).fetchall()
    needs = conn.execute(
        "SELECT subject, frm FROM inbox_triage WHERE bucket="
        "'reply-needed' AND created_at LIKE ? ORDER BY rowid DESC"
        " LIMIT 10", (day,)).fetchall()
    return {"buckets": {r["bucket"]: r["n"] for r in rows},
            "reply_needed": [
                {"subject": r["subject"], "from": r["frm"]}
                for r in needs]}

"""Owner-approved provider setup (docs/EVOLVE.md #16).

A connector-shaped gap or "this needs a stronger official API"
produces a DM card: what, why, the official URL, and the standing
promise that pal will not type passwords, OTPs, or cards. Approve
opens the official page on the computer VM (browse_goto) — or just
DMs the URL when the computer isn't enabled. Deny does nothing.

Pal NEVER automates ChatGPT / Claude.ai / consumer-Gemini chat and
NEVER self-registers accounts. There is deliberately no helper in
this module (or anywhere in pa/) that POSTs to a consumer chat
endpoint — the unit test asserts it stays that way.
"""

import logging
import re

from .. import approvals

log = logging.getLogger("pa.evolve.provision")

_URL_OK = re.compile(r"^https://[a-z0-9.-]+", re.I)

COPY = ("🧩 provider setup requested\n"
        "{what}\nwhy: {why}\n\n"
        "Official page: {url}\n"
        "Approve opens it on your computer — pal never types "
        "passwords, OTPs, or cards, and never signs you up itself.")


def propose(conn, s, bot, what: str, why: str, url: str) -> dict:
    """Create the approval + post the card. Returns {approval_id} or
    {error}."""
    what, why, url = (what or "")[:200], (why or "")[:400], \
        (url or "").strip()
    if not _URL_OK.match(url):
        return {"error": "bad_url",
                "message": "needs a https official URL"}
    aid = approvals.create_approval(
        conn, "evolve_provision",
        {"tool": "provider_setup",
         "args": {"what": what, "why": why, "url": url}})
    if bot is not None:
        try:
            dm = bot._owner_dm_id()
            if dm:
                bot.api.post_message(
                    dm, COPY.format(what=what, why=why, url=url),
                    components=[[
                        {"type": 2, "style": 3, "label": "Open page",
                         "custom_id": f"evprov:{aid}:y"},
                        {"type": 2, "style": 4, "label": "Reject",
                         "custom_id": f"evprov:{aid}:n"}]])
        except Exception:
            log.info("provision card failed", exc_info=True)
    return {"approval_id": aid}


def run_approved(conn, s, bot, args: dict, browse=None) -> str:
    """The owner's Approve: browse_goto the official URL on the
    computer when it's enabled, else DM the URL. `browse` is
    injectable for tests. Never raises."""
    url = str(args.get("url") or "")
    opened = False
    if s.computer_enabled:
        try:
            svc = None
            if browse is None and bot is not None:
                svc = bot._computer_svc()
                browse = lambda u: svc.call("goto", {"url": u})
            if browse is not None:
                browse(url)
                opened = True
        except Exception as e:
            log.info("provision browse failed: %s", e)
    text = (f"Opened {url} on your computer — over to you."
            if opened else
            f"Provider setup: open {url} yourself — pal doesn't "
            "drive sign-ups.")
    if bot is not None:
        try:
            dm = bot._owner_dm_id()
            if dm:
                bot.api.post_message(dm, text)
        except Exception:
            pass
    return "opened" if opened else "dm_only"

"""Telegram mirror (docs/PARITY.md #26) — OFF by default.

`PA_TELEGRAM=true` + `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
(the owner's own chat with the bot). Inbound messages on that
chat run chat turns answered on Telegram; Discord-DM replies
mirror out. Only the configured chat is ever read/answered.

Bot API over stdlib urllib (no extra dep), ~30 s long-poll.
"""

import json
import urllib.request

API = "https://api.telegram.org"
MAX_LEN = 4000                        # telegram hard cap is 4096


def token(env) -> str:
    return (env.get("TELEGRAM_BOT_TOKEN") or "").strip()


def enabled(s, env) -> bool:
    return bool(s.telegram_enabled and token(env)
                and s.telegram_chat_id)


def _call(env, method: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        f"{API}/bot{token(env)}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def send(s, env, text: str, chat_id: str = None) -> bool:
    """sendMessage to the owner's chat (or an explicit one)."""
    if not enabled(s, env):
        return False
    cid = chat_id or s.telegram_chat_id
    try:
        for i in range(0, max(1, len(text)), MAX_LEN):
            _call(env, "sendMessage",
                  {"chat_id": cid,
                   "text": text[i:i + MAX_LEN]}, timeout=15)
        return True
    except Exception:
        return False


def poll(env, offset: int, timeout: int = 25) -> list:
    """getUpdates long-poll; returns [{id, chat_id, text}] for text
    messages only. Non-text updates are consumed but ignored."""
    try:
        out = _call(env, "getUpdates",
                    {"offset": offset, "timeout": timeout},
                    timeout=timeout + 10)
    except Exception:
        return []
    msgs = []
    for u in out.get("result") or []:
        m = u.get("message") or {}
        text = m.get("text")
        chat = str((m.get("chat") or {}).get("id") or "")
        msgs.append({"id": u.get("update_id"),
                     "chat_id": chat, "text": text or ""})
    return msgs

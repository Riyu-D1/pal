"""Discord owner interface: gateway bot + result notifier.

- Disabled cleanly when DISCORD_BOT_TOKEN is absent or DISCORD_ENABLED
  is false. Nothing here ever posts on its own unless the bot is
  enabled AND a route exists.
- Fail closed: only DISCORD_OWNER_ID can drive commands. Everyone else
  gets an ephemeral refusal (interactions) or silence (messages). When
  no owner id is configured, everyone is refused.
- Dedup: every dispatched event id lands once in `discord_events`;
  gateway redelivery after reconnect/resume can never double-run a
  command or double-reply. Result/approval notifications are deduped the
  same way (`task-notify:<id>` / `approval-notify:<id>`).
- Reconnect/resume: seq + session_id are kept; a drop re-dials and sends
  RESUME first, re-IDENTIFY on invalid session. Backoff is bounded and
  the loop is cancellable via stop().
- Replies go over the REST API (interaction callback / channel message).
  There is no auto-send of email or any external write — the only write
  path remains the approval queue, which these commands merely expose.
"""

import json
import logging
import re
import threading
import time
import urllib.parse

from . import approvals, attachments as attachmod, \
    busy as busymod, call as callmod, chattools, db as dbmod, \
    memory as memmod, progress as progressmod, telegram as tgmod, \
    voice as voicemod, worker as workmod
from .evolve import incidents as incidentsmod
from .netclient import APIClient, HTTPError, TransportError
from .util import json_dumps, json_loads_bounded, now_iso, \
    parse_iso, utcnow
from .wsclient import WSClient, WSClosed, WSError

log = logging.getLogger("pa.discord")

INTENTS = (1 << 9) | (1 << 12) | (1 << 15)  # guild msgs, DMs, msg content
MAX_MSG = 1900

_COMMANDS = [
    {"name": "help", "description": "List commands"},
    {"name": "assistants", "description": "List assistants"},
    {"name": "as", "description": "Set this channel's assistant",
     "options": [{"name": "name", "type": 3,
                  "description": "assistant name ('default' clears the "
                                 "override; empty = show current)",
                  "required": False}]},
    {"name": "tasks", "description": "List recent tasks"},
    {"name": "todo", "description": "Show / add / check off todos",
     "options": [{"name": "action", "type": 3,
                  "description": "'add <text>', 'done <n>', 'clear' "
                                 "(empty = list)",
                  "required": False}]},
    {"name": "update", "description": "Check for + apply a code "
     "update (approval card)"},
    {"name": "usage", "description": "Today's provider usage + "
     "counts"},
    {"name": "think", "description": "Next message uses the "
     "escalate tier (think harder)"},
    {"name": "good", "description": "Thumbs-up pal's last answer"},
    {"name": "bad", "description": "Thumbs-down pal's last answer",
     "options": [{"name": "note", "type": 3,
                  "description": "what was wrong (optional)",
                  "required": False}]},
    {"name": "evolve", "description": "Self-improvement items — "
     "open list, or one plan",
     "options": [{"name": "id", "type": 4,
                  "description": "item id (empty = list open)",
                  "required": False}]},
    {"name": "pages", "description": "List / unpublish shared pages",
     "options": [{"name": "slug", "type": 3,
                  "description": "page slug to unpublish "
                                 "(empty = list)",
                  "required": False}]},
    {"name": "stop", "description": "Stop the running turn"},
    {"name": "steer", "description": "Nudge the running turn",
     "options": [{"name": "note", "type": 3,
                  "description": "guidance for the in-flight turn",
                  "required": True}]},
    {"name": "status", "description": "System status (or one task)",
     "options": [{"name": "id", "type": 4,
                  "description": "task id (empty = system status)",
                  "required": False}]},
    {"name": "cancel", "description": "Cancel a task",
     "options": [{"name": "id", "type": 4, "description": "task id",
                  "required": True}]},
    {"name": "retry", "description": "Retry a finished task",
     "options": [{"name": "id", "type": 4, "description": "task id",
                  "required": True}]},
    {"name": "approvals", "description": "List pending approvals"},
    {"name": "policy", "description":
        "Explain the policy decision for a tool call"},
    {"name": "memory", "description": "List memories / forget one",
     "options": [{"name": "action", "type": 3,
                  "description": "'forget <id>' or 'forget fact <key>' "
                                 "(empty = list)",
                  "required": False},
                 {"name": "ref", "type": 3,
                  "description": "memory id or fact key",
                  "required": False}]},
    {"name": "approve", "description": "Approve an approval id",
     "options": [{"name": "id", "type": 3, "description": "approval id",
                  "required": True}]},
    {"name": "reject", "description": "Reject an approval id",
     "options": [{"name": "id", "type": 3, "description": "approval id",
                  "required": True}]},
    {"name": "schedules", "description": "List schedules"},
    {"name": "task", "description": "Create a task for an assistant",
     "options": [
         {"name": "assistant", "type": 3,
          "description": "assistant name", "required": True},
         {"name": "text", "type": 3,
          "description": "what to do", "required": True}]},
    {"name": "connect", "description": "Connect an app — sign-in link",
     "options": [{"name": "app", "type": 3,
                  "description": "app (notion, gmail, trello...) "
                                 "or url:https://...",
                  "required": True, "autocomplete": True},
                 {"name": "account", "type": 3,
                  "description": "account label for a second "
                                 "connection (e.g. pal)",
                  "required": False}]},
    {"name": "connections", "description": "List connected apps"},
    {"name": "disconnect", "description": "Disconnect an app",
     "options": [{"name": "app", "type": 3,
                  "description": "connected app",
                  "required": True, "autocomplete": True}]},
    {"name": "jobs", "description": "List agentic jobs / toggle one",
     "options": [{"name": "name", "type": 3,
                  "description": "job name (empty = list)",
                  "required": False, "autocomplete": True},
                 {"name": "state", "type": 3,
                  "description": "on | off",
                  "required": False}]},
    {"name": "voice", "description": "Voice-note replies "
                                     "on/off/auto",
     "options": [{"name": "state", "type": 3,
                  "description": "on | off | auto (empty = show "
                                 "state)",
                  "required": False}]},
    {"name": "call", "description": "Test a pal voice call",
     "options": [{"name": "text", "type": 3,
                  "description": "'test' or what pal should say",
                  "required": True}]},
    {"name": "watch", "description": "Inbox-watch vip/mute rules",
     "options": [{"name": "action", "type": 3,
                  "description": "vip | mute | remove (empty = list)",
                  "required": False},
                 {"name": "pattern", "type": 3,
                  "description": "email address or domain",
                  "required": False}]},
    {"name": "monitor", "description": "Standing monitors "
     "(page/mail/calendar) — list, toggle or remove",
     "options": [{"name": "action", "type": 3,
                  "description": "'<id> on|off|del' (empty = list)",
                  "required": False},
                 {"name": "arg", "type": 3,
                  "description": "monitor id / value",
                  "required": False}]},
    {"name": "credentials", "description": "Saved logins: add (private modal), otp, form "
     "(one-time link), del <domain>. Never shown back",
     "options": [{"name": "action", "type": 3,
                  "description": "add | otp | form | del <domain> "
                                 "(empty = list)",
                  "required": False},
                 {"name": "arg", "type": 3,
                  "description": "domain only — never a secret",
                  "required": False}]},
    {"name": "audit", "description": "Show the recent audit log",
     "options": [{"name": "filter", "type": 3,
                  "description": "optional action substring "
                                 "(e.g. 'approval')",
                  "required": False}]},
    {"name": "allow", "description": "Standing 'always allow' rules "
     "for low-risk tools",
     "options": [{"name": "action", "type": 3,
                  "description": "add <tool> [match] | del <id> "
                                 "(empty = list)",
                  "required": False},
                 {"name": "arg", "type": 3,
                  "description": "tool / id / match text",
                  "required": False}]},
    {"name": "hooks", "description": "Signed webhook endpoints "
     "— list, add, remove",
     "options": [{"name": "action", "type": 3,
                  "description": "add <name> [prompt] | del <name> "
                                 "| <name> on|off (empty = list)",
                  "required": False},
                 {"name": "arg", "type": 3,
                  "description": "name and/or prompt text",
                  "required": False}]},
    {"name": "briefing", "description": "Run the morning briefing now",
     "options": [{"name": "action", "type": 3,
                  "description": "'now' (empty = same)",
                  "required": False}]},
    {"name": "reminders", "description": "List or cancel reminders",
     "options": [{"name": "action", "type": 3,
                  "description": "'cancel <name>' (empty = list)",
                  "required": False},
                 {"name": "name", "type": 3,
                  "description": "reminder name (remind-xxxx)",
                  "required": False, "autocomplete": True}]},
    {"name": "desktop", "description": "pal's remote browser session "
                                     "(tailnet only; link goes to DMs)",
     "options": [{"name": "action", "type": 3,
                  "description": "'off' or minutes (empty = start)",
                  "required": False}]},
    {"name": "reset", "description": "Clear this channel's chat memory"
                                     " (backs it up first)"},
    {"name": "goals", "description": "Standing goals pal works on "
     "(read-only) — list, inspect, pause/done",
     "options": [{"name": "action", "type": 3,
                  "description": "add <title> | done|pause|active <id> "
                                 "| <id> (empty = list)",
                  "required": False},
                 {"name": "arg", "type": 3,
                  "description": "goal id or title text",
                  "required": False}]},
    {"name": "activity", "description": "What pal noticed / drafted / "
     "did — the local feed",
     "options": [{"name": "span", "type": 3,
                  "description": "today | week | all (default today)",
                  "required": False}]},
    {"name": "triage", "description": "Today's inbox buckets — "
     "reply-needed / fyi / newsletter / receipt / spam-ish"},
]


def _clip(text: str) -> str:
    text = str(text or "")
    return text if len(text) <= MAX_MSG else text[:MAX_MSG - 3] + "..."


def _reminder_payload(payload_json):
    """schedule-payload dict if it's a chat reminder, else None."""
    try:
        p = json_loads_bounded(payload_json or "{}")
    except ValueError:
        return None
    return p if isinstance(p, dict) and p.get("reminder") else None


_COMMAND_NAMES = {c["name"] for c in _COMMANDS} | {"deny"}
# replies containing account labels / sign-in links stay private
_EPHEMERAL_COMMANDS = {"connect", "connections", "disconnect",
                       "jobs", "watch", "reminders", "briefing",
                       "status", "desktop", "memory", "monitor",
                       "hooks", "allow", "audit", "credentials",
                       "evolve", "goals", "activity", "triage"}
_MENTION_RE = re.compile(r"<@!?\d+>")

# thumbs-up/down on every attended reply (docs/EVOLVE.md)
# clicks are owner-gated upstream like every other component.
_FEEDBACK_BUTTONS = [[
    {"type": 2, "style": 2, "emoji": {"name": "👍"},
     "custom_id": "fbup"},
    {"type": 2, "style": 2, "emoji": {"name": "👎"},
     "custom_id": "fbdn"}]]

# process-local liveness stamps for deep /healthz
# (PA_HEALTH_DISCORD) — updated on every gateway frame and on READY.
_GATEWAY = {"connected_at": None, "last_frame": None}


def gateway_age():
    """Seconds since the last gateway frame in THIS process, or None
    when the bot has never connected here."""
    t = _GATEWAY["last_frame"]
    return None if t is None else time.time() - t


def split_message(text: str, limit: int = MAX_MSG):
    """Break a long reply into <=limit chunks on line boundaries."""
    text = str(text or "")
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [text[:limit]]


class DiscordAPI:
    """REST client pinned to the Discord API base (test-overridable via
    DISCORD_API_BASE). Only the routes below exist; there is no way to
    send email or hit any non-Discord host from here."""

    def __init__(self, settings, token: str):
        self.client = APIClient(settings.discord_api_base, timeout=15)
        self._auth = {"Authorization": f"Bot {token}"}

    def interaction_reply(self, interaction_id: str, itoken: str,
                          content: str, ephemeral: bool = False,
                          components=None):
        data = {"content": _clip(content)}
        if ephemeral:
            data["flags"] = 64
        if components:
            data["components"] = [
                {"type": 1, "components": row} for row in components]
        try:
            self.client.post_json(
                f"/interactions/{interaction_id}/{itoken}/callback",
                {"type": 4, "data": data}, headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("interaction reply failed: %s", e)
            return False
        return True

    def interaction_update(self, interaction_id: str, itoken: str,
                           content: str):
        """Type-7 callback: edit the message a button lives on and drop
        its components."""
        try:
            self.client.post_json(
                f"/interactions/{interaction_id}/{itoken}/callback",
                {"type": 7, "data": {"content": _clip(content),
                                     "components": []}},
                headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("interaction update failed: %s", e)
            return False
        return True

    def interaction_modal(self, interaction_id: str, itoken: str,
                          modal: dict):
        """Type-9 callback: open a modal. The modal's fields travel
        back as an interaction submit — never as a channel message —
        so it is the safe way to collect a secret in Discord."""
        try:
            self.client.post_json(
                f"/interactions/{interaction_id}/{itoken}/callback",
                {"type": 9, "data": modal}, headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("interaction modal failed: %s", e)
            return False
        return True

    def create_message(self, channel_id: str, content: str,
                       components=None, message_reference=None):
        """POST a channel message; returns the message dict (its id is
        what live progress edits/deletes later) or None on failure."""
        data = {"content": _clip(content)}
        if components:
            data["components"] = [
                {"type": 1, "components": row} for row in components]
        if message_reference:
            data["message_reference"] = {
                "message_id": str(message_reference),
                "fail_if_not_exists": False}
        try:
            _h, msg = self.client.post_json(
                f"/channels/{channel_id}/messages", data,
                headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("post to channel %s failed: %s", channel_id, e)
            return None
        return msg if isinstance(msg, dict) else {}

    def post_message(self, channel_id: str, content: str,
                     components=None, message_reference=None) -> bool:
        return self.create_message(
            channel_id, content, components, message_reference) \
            is not None

    def edit_message(self, channel_id: str, message_id: str,
                     content: str) -> bool:
        """PATCH one message's content (the live status line)."""
        try:
            self.client.request(
                "PATCH",
                f"/channels/{channel_id}/messages/{message_id}",
                body={"content": _clip(content)}, headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("edit %s/%s failed: %s",
                        channel_id, message_id, e)
            return False
        return True

    def delete_message(self, channel_id: str, message_id: str) -> bool:
        try:
            self.client.request(
                "DELETE",
                f"/channels/{channel_id}/messages/{message_id}",
                headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("delete %s/%s failed: %s",
                        channel_id, message_id, e)
            return False
        return True

    def _reaction(self, method, channel_id, message_id, emoji) -> bool:
        path = (f"/channels/{channel_id}/messages/{message_id}"
                f"/reactions/{urllib.parse.quote(emoji, safe='')}/@me")
        try:
            self.client.request(method, path, headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("reaction %s on %s failed: %s",
                        method, message_id, e)
            return False
        return True

    def add_reaction(self, channel_id: str, message_id: str,
                     emoji: str) -> bool:
        return self._reaction(
            "PUT", channel_id, message_id, emoji)

    def delete_reaction(self, channel_id: str, message_id: str,
                        emoji: str) -> bool:
        return self._reaction(
            "DELETE", channel_id, message_id, emoji)

    def post_file(self, channel_id: str, filename: str, ctype: str,
                  data: bytes, content: str = "") -> bool:
        """Attach a file to a channel message (browse_screenshot's
        JPEGs ride this)."""
        payload = {"content": _clip(content),
                   "attachments": [{"id": "0", "filename": filename}]}
        body, mtype = voicemod.encode_multipart(
            [("payload_json", json_dumps(payload))],
            [("files[0]", filename, ctype, data)])
        try:
            self.client.request(
                "POST", f"/channels/{channel_id}/messages", body=body,
                headers={**self._auth, "Content-Type": mtype})
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("file post to %s failed: %s", channel_id, e)
            return False
        return True

    def reply(self, channel_id: str, text: str, message_id=None,
              components=None):
        """Send a possibly-long answer as a reply, split under MAX_MSG;
        components (e.g. feedback buttons) ride the last chunk."""
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            ref = message_id if i == 0 else None
            comp = components if i == len(chunks) - 1 else None
            if not self.post_message(channel_id, chunk, comp, ref):
                return False
        return True

    def interaction_autocomplete(self, interaction_id: str, itoken: str,
                                 choices):
        """Type-8 callback: answer an autocomplete interaction. Each
        choice is a string (name == value) or a (name, value) pair."""
        out = []
        for c in choices[:25]:
            if isinstance(c, (list, tuple)):
                name, value = str(c[0]), str(c[1])
            else:
                name = value = str(c)
            out.append({"name": name[:100], "value": value[:100]})
        try:
            self.client.post_json(
                f"/interactions/{interaction_id}/{itoken}/callback",
                {"type": 8, "data": {"choices": out}},
                headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("autocomplete reply failed: %s", e)
            return False
        return True

    def typing(self, channel_id: str) -> bool:
        try:
            self.client.post_json(f"/channels/{channel_id}/typing", {},
                                  headers=self._auth)
            return True
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("typing to %s failed: %s", channel_id, e)
            return False

    def post_voice_message(self, channel_id: str, ogg: bytes,
                           duration_secs: float, waveform_b64: str
                           ) -> bool:
        """Native Discord voice message: IS_VOICE_MESSAGE flag (1<<13)
        + one audio/ogg attachment carrying duration_secs and the
        base64 waveform preview. No content/embeds may ride along."""
        payload = {"flags": voicemod.FLAG_IS_VOICE_MESSAGE,
                   "attachments": [{
                       "id": "0", "filename": "voice-message.ogg",
                       "duration_secs": round(float(duration_secs), 2),
                       "waveform": waveform_b64}]}
        body, ctype = voicemod.encode_multipart(
            [("payload_json", json_dumps(payload))],
            [("files[0]", "voice-message.ogg", "audio/ogg", ogg)])
        try:
            self.client.request(
                "POST", f"/channels/{channel_id}/messages", body=body,
                headers={**self._auth, "Content-Type": ctype})
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("voice message to %s failed: %s", channel_id, e)
            return False
        return True

    def guild_channels(self, guild_id: str):
        """GET /guilds/{id}/channels -> list (used to find 'pal-call')."""
        try:
            _h, data = self.client.get_json(
                f"/guilds/{guild_id}/channels", headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("guild_channels %s failed: %s", guild_id, e)
            return None
        return data if isinstance(data, list) else None

    def create_voice_channel(self, guild_id: str, name: str,
                             permission_overwrites):
        """Create a GUILD_VOICE channel (type 2); overwrites is the
        Discord permission_overwrites array."""
        try:
            _h, data = self.client.post_json(
                f"/guilds/{guild_id}/channels",
                {"name": name, "type": 2,
                 "permission_overwrites": permission_overwrites},
                headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("create voice channel in %s failed: %s",
                        guild_id, e)
            return None
        return data

    def open_dm(self, user_id: str):
        try:
            _h, data = self.client.post_json(
                "/users/@me/channels", {"recipient_id": user_id},
                headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("open_dm failed: %s", e)
            return None
        return data.get("id")

    def register_commands(self, app_id: str) -> bool:
        try:
            self.client.request(
                "PUT", f"/applications/{app_id}/commands",
                body=_COMMANDS, headers=self._auth)
        except (HTTPError, TransportError, ValueError) as e:
            log.warning("command registration failed: %s", e)
            return False
        return True


# ---------------------------------------------------------------------------

def _dedup_insert(conn, event_id: str, kind: str) -> bool:
    """True if this event id is NEW (first delivery)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO discord_events(event_id, kind, created_at)"
        " VALUES(?,?,?)", (event_id, kind, now_iso()))
    return cur.rowcount == 1


def _event_user(payload: dict) -> str:
    m = payload.get("member") or {}
    user = m.get("user") or payload.get("user") or \
        (payload.get("author") or {})
    return str(user.get("id") or "")


def _argv_from_interaction(data: dict):
    """Slash-command payload -> argv like ['status', '3']."""
    name = (data.get("name") or "").lower()
    argv = [name] if name else []
    opts = sorted(data.get("options") or [],
                  key=lambda o: 0)
    for o in opts:
        argv.append(str(o.get("value") or ""))
    return argv


def _cred_modal(kind: str) -> dict:
    """the credential-capture modal. Values come back inside a
    type-5 interaction submit — they never appear as a channel
    message, so Discord never stores the secret in history."""
    def _field(cid, label, placeholder, required=True):
        return {"type": 1, "components": [
            {"type": 4, "custom_id": cid, "label": label,
             "style": 1, "required": required,
             "placeholder": placeholder}]}
    if kind == "otp":
        fields = [
            _field("domain", "Domain", "example.com"),
            _field("secret", "One-time code", "123456")]
        return {"custom_id": "credmodal:otp",
                "title": "Save a one-time code",
                "components": fields}
    fields = [
        _field("domain", "Domain", "example.com"),
        _field("username", "Username / email", "me@example.com"),
        _field("secret", "Password", "never shown back"),
        _field("label", "Label (optional)", "work",
               required=False)]
    return {"custom_id": "credmodal:pw",
            "title": "Save a login",
            "components": fields}


def _cred_components():
    """Buttons under /credentials replies — each opens the matching
    capture modal."""
    return [[{"type": 2, "style": 1, "label": "Save a password",
              "custom_id": "credmodal:pw"},
             {"type": 2, "style": 1, "label": "Save a one-time code",
              "custom_id": "credmodal:otp"}]]


def _looks_domain(tok: str) -> bool:
    """Heuristic for 'is this token a domain?' — used only to decide
    whether a pasted DM is credential-shaped while a capture is
    pending."""
    t = (tok or "").strip().lower()
    return ("." in t and "@" not in t and " " not in t
            and not t.startswith(".") and len(t) <= 120)


class _ChatProgress(threading.Thread):
    """Typing indicator + one 'working on it' note while a turn runs.

    The PA_CHAT_PROGRESS=false fallback; shares LiveProgress's
    start()/note_tool()/finish() surface so the workers don't care
    which is driving."""

    def __init__(self, api, channel_id, working_after=8.0):
        super().__init__(daemon=True, name="pa-chat-typing")
        self.api = api
        self.channel_id = channel_id
        self.working_after = working_after
        self._done = threading.Event()

    def run(self):
        self.api.typing(self.channel_id)
        noted = False
        while not self._done.wait(self.working_after):
            self.api.typing(self.channel_id)
            if not noted:
                self.api.post_message(self.channel_id, "working on it…")
                noted = True

    def note_tool(self, name):
        pass                            # no per-tool status on this path

    def finish(self, ok: bool = True):
        self.stop()

    def stop(self):
        self._done.set()


def _auth_failed(exc) -> bool:
    """The token is wrong, not the network: http 401 at the ws
    handshake or gateway close 4004 (authentication failed)."""
    return getattr(exc, "code", None) in (401, 4004)


class DiscordBot:
    def __init__(self, settings, db_path=None, env=None, notifier=True,
                 engine=None):
        import os
        self.s = settings
        self.env = os.environ if env is None else env
        self.db_path = db_path or settings.db_path
        self.token = settings.discord_token(self.env)
        self.api = DiscordAPI(settings, self.token) if self.token else None
        self.stop_event = threading.Event()
        self.session_id = None
        self.resume_url = None
        self.seq = None
        self.bot_user_id = None
        self._threads = []
        self._notifier = notifier
        self._engine_obj = engine
        self._computer = None
        self._chat_lock = threading.Lock()
        self._stop_flags = set()      # channels asked to /stop
        self._steer = {}              # channel -> [mid-turn notes]
        self._owner_dm = None         # lazily resolved for tg mirror
        self._calls = callmod.CallManager(
            self.s, self.env, self.api, db_path=self.db_path,
            engine_factory=self._engine, owner_id=self.owner_id)
        self._voice = voicemod.VoiceServices(
            self.s, self.env, self.api, call_manager=self._calls,
            voice_pref=self._voice_on)

    @property
    def enabled(self) -> bool:
        return bool(self.s.discord_enabled and self.token)

    @property
    def owner_id(self) -> str:
        return (self.s.discord_owner_id or "").strip()

    # -- top-level loops -----------------------------------------------------
    def start(self):
        """Launch gateway + notifier threads. No-op when disabled."""
        if not self.enabled:
            log.warning("discord disabled (%s)",
                        "DISCORD_ENABLED=false"
                        if not self.s.discord_enabled
                        else "DISCORD_BOT_TOKEN not set — put it in "
                             ".env to enable the bot")
            return False
        t = threading.Thread(target=self.run_forever, daemon=True,
                             name="pa-discord-gateway")
        t.start()
        self._threads.append(t)
        if self._notifier:
            n = threading.Thread(target=self.notifier_loop, daemon=True,
                                 name="pa-discord-notifier")
            n.start()
            self._threads.append(n)
        if tgmod.enabled(self.s, self.env):
            g = threading.Thread(target=self._tg_poll_loop, daemon=True,
                                 name="pa-telegram")
            g.start()
            self._threads.append(g)
        log.info("discord bot starting (owner %s)",
                 self.owner_id or "UNSET - refusing everyone")
        return True

    def stop(self):
        self.stop_event.set()
        for t in self._threads:
            t.join(timeout=5)
        self._calls.stop()

    def run_forever(self):
        """Connect -> session -> reconnect with bounded backoff."""
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self._session()
            except (WSClosed, WSError, OSError) as e:
                if _auth_failed(e):
                    log.error("Discord token rejected (401) — check "
                              "DISCORD_BOT_TOKEN")
                else:
                    log.warning("gateway session dropped: %s", e)
            except Exception:
                log.exception("gateway loop error")
            if self.stop_event.is_set():
                return
            self.stop_event.wait(min(backoff, 60.0))
            backoff = min(backoff * 2, 60.0)

    def _session(self):
        url = self.resume_url or self.s.discord_gateway_url
        ws = WSClient.connect(url, timeout=20)
        hb_stop = threading.Event()
        try:
            hello = json_loads_bounded(ws.recv())
            if hello.get("op") != 10:
                raise WSError("expected HELLO")
            interval = float(
                (hello.get("d") or {}).get("heartbeat_interval") or 45000)
            # The connect timeout (20s) is shorter than the heartbeat-ACK
            # cadence — without this the socket dies mid-session every
            # ~30s and events (incl. slash commands) are silently lost.
            ws.settimeout(interval / 1000.0 + 30)
            hb = threading.Thread(
                target=self._heartbeat, args=(ws, interval, hb_stop),
                daemon=True)
            hb.start()
            if self.session_id and self.seq is not None:
                ws.send_text(json.dumps({"op": 6, "d": {
                    "token": self.token, "session_id": self.session_id,
                    "seq": self.seq}}))
            else:
                ws.send_text(json.dumps({"op": 2, "d": {
                    "token": self.token, "intents": INTENTS,
                    "properties": {"os": "linux", "browser": "pa",
                                   "device": "pa"}}}))
            while not self.stop_event.is_set():
                payload = json_loads_bounded(ws.recv())
                self._dispatch(ws, payload)
        finally:
            hb_stop.set()
            ws.close()

    def _heartbeat(self, ws, interval_ms, stop):
        period = max(1.0, interval_ms / 1000.0) * 0.9
        while not stop.wait(period):
            try:
                ws.send_text(json.dumps({"op": 1, "d": self.seq}))
            except OSError:
                return

    def _dispatch(self, ws, payload):
        op = payload.get("op")
        d = payload.get("d") or {}
        _GATEWAY["last_frame"] = time.time()
        if payload.get("s") is not None:
            self.seq = payload["s"]
        if op == 0:
            t = payload.get("t")
            if t == "READY":
                _GATEWAY["connected_at"] = time.time()
                self.session_id = d.get("session_id")
                self.resume_url = d.get("resume_gateway_url") or \
                    self.s.discord_gateway_url
                self.bot_user_id = str(
                    (d.get("user") or {}).get("id") or "") or None
                self._calls.bot_user_id = self.bot_user_id or ""
                app = d.get("application") or {}
                if app.get("id"):
                    self.api.register_commands(app["id"])
                log.info("gateway READY (session %s)", self.session_id)
            elif t in ("MESSAGE_CREATE", "INTERACTION_CREATE"):
                self._handle_event(t, d)
        elif op == 7:                    # server requests reconnect
            raise WSClosed("gateway requested reconnect")
        elif op == 9:                    # invalid session
            if not d:
                self.session_id = None
                self.seq = None
            raise WSClosed("invalid session")
        elif op == 11:
            pass                          # heartbeat ack

    # -- events --------------------------------------------------------------
    def _handle_event(self, kind: str, d: dict):
        event_id = str(d.get("id") or "")
        if not event_id:
            return
        conn = dbmod.connect(self.db_path)
        try:
            user = _event_user(d)
            if kind == "INTERACTION_CREATE":
                if user != self.owner_id:
                    if d.get("type") == 4:
                        self.api.interaction_autocomplete(
                            event_id, str(d.get("token") or ""), [])
                    else:
                        self.api.interaction_reply(
                            event_id, str(d.get("token") or ""),
                            "Not authorized.", ephemeral=True)
                    return
            elif user != self.owner_id:
                return                      # fail closed, silently
            if kind == "INTERACTION_CREATE":
                if not _dedup_insert(conn, event_id, kind):
                    return                  # redelivery -> skip
                if d.get("type") == 3:      # message component (buttons)
                    self._handle_component(conn, d)
                    return
                if d.get("type") == 5:      # modal submit
                    self._handle_modal(conn, d)
                    return
                if d.get("type") == 4:      # autocomplete
                    self._handle_autocomplete(conn, d)
                    return
                argv = _argv_from_interaction(d.get("data") or {})
                mk = self._cred_modal_kind(argv)
                if mk:
                    # /credentials add|otp opens a private modal
                    # no secret ever travels as a channel message.
                    from . import secretform
                    secretform.mark_pending(conn, mk)
                    self.api.interaction_modal(
                        event_id, str(d.get("token") or ""),
                        _cred_modal(mk))
                    return
                reply = self.execute(
                    conn, argv, str(d.get("channel_id") or ""))
                comps = _cred_components() if argv and argv[
                    0].lower() == "credentials" and getattr(
                    self.s, "secret_capture", False) else None
                self.api.interaction_reply(
                    event_id, str(d.get("token") or ""), reply,
                    ephemeral=bool(argv) and
                    argv[0].lower() in _EPHEMERAL_COMMANDS,
                    components=comps)
                return
            route = self._message_route(d)
            if route is None:
                return
            if not _dedup_insert(conn, event_id, kind):
                return                      # redelivery -> skip
            if route == "chat":
                if self._maybe_secret_paste(conn, d):
                    return              # sealed or eaten — never a chat turn
                # "i'm busy"/"i'm free" feed the voice-notes
                # auto mode (pa.busy); the text still runs a normal turn
                busymod.note_text(conn, self._chat_text(d))
                self._start_chat(d)
                return
            if route == "voice":
                # a voice message marks the owner busy — auto
                # mode replies in kind for the next ~10 minutes
                busymod.note_voice(conn)
                self._start_voice(d)
                return
            if route == "import":
                self._start_import(d)
                return
            if route in ("image", "pdf"):
                self._start_file(d, route)
                return
            argv = self._argv_from_message(d)
            if self._maybe_text_secret(conn, d, argv):
                return                  # message deleted, not re-posted
            reply = self.execute(conn, argv,
                                 str(d.get("channel_id") or ""))
            comps = _cred_components() if argv and argv[
                0].lower() == "credentials" and getattr(
                self.s, "secret_capture", False) else None
            self.api.post_message(str(d.get("channel_id") or ""),
                                  reply, components=comps)
        except Exception:
            log.exception("event %s handling failed", event_id)
        finally:
            conn.close()

    # -- conversational routing ----------------------------------------------
    def _message_route(self, d: dict):
        """'command' | 'chat' | None for an owner MESSAGE_CREATE."""
        author = d.get("author") or {}
        if author.get("bot"):
            return None
        if self.bot_user_id and \
                str(author.get("id") or "") == self.bot_user_id:
            return None
        content = (d.get("content") or "").strip()
        if d.get("guild_id") is None or d.get("channel_type") == 1:
            # DM voice note / audio attachment: transcribe -> chat turn
            for att in d.get("attachments") or []:
                if voicemod.attachment_is_audio(att):
                    return "voice"
                if attachmod.is_image(att):
                    return "image"
                if attachmod.is_pdf(att):
                    return "pdf"
                if str(att.get("filename") or "").lower().endswith(
                        (".zip", ".json", ".md", ".txt")):
                    return "import"
        if not content:
            return None
        if d.get("guild_id") is None or d.get("channel_type") == 1:
            # DM: known commands still work; everything else is chat
            text = content[3:].strip() \
                if content.lower().startswith("pa ") else content
            argv = text.split()
            if argv and argv[0].lower() in _COMMAND_NAMES:
                return "command"
            return "chat"
        if self.s.discord_chat_channel_id and \
                str(d.get("channel_id")) == \
                self.s.discord_chat_channel_id:
            return "chat"
        if self.bot_user_id and re.search(
                rf"<@!?{re.escape(self.bot_user_id)}>", content):
            return "chat"
        return None

    def _chat_text(self, d: dict) -> str:
        text = _MENTION_RE.sub("", d.get("content") or "")
        if text.lower().startswith("pa "):
            text = text[3:]
        return text.strip()

    def _db(self):
        """Fresh DB connection for system-job handlers (callers
        close it — monitors.run, journal.run, backupjob.run)."""
        return dbmod.connect(self.db_path)

    def _engine(self):
        if self._engine_obj is None:
            from . import chatagent
            self._engine_obj = chatagent.ChatEngine(
                self.s, self.db_path, env=self.env,
                request_approval=self._request_chat_approval,
                tool_ctx_kwargs={
                    "voice": self._voice,
                    "computer": self._computer_svc(),
                    "post_file": self._post_file,
                    "request_ask": self._request_chat_ask,
                    "bot": self})
        return self._engine_obj

    def _computer_svc(self):
        """pal's browser service (local driver or remote computer-agent
        client, per PA_COMPUTER_*). Lazily built; launching Chromium
        itself stays lazy inside the service."""
        if self._computer is None:
            from . import computer
            self._computer = computer.service_for(self.s, self.env)
        return self._computer

    def _post_file(self, channel_id, filename, ctype, data) -> bool:
        if self.api is None:
            return False
        return self.api.post_file(channel_id, filename, ctype, data)

    def _desktop_cmd(self, conn, args, channel_id):
        """`/desktop [off|minutes]` — the session itself lives on the
        computer host (in-process or behind the computer-agent wire);
        the link only ever goes to the owner's DMs."""
        svc = self._computer_svc()
        if not svc.enabled():
            return ("pal's computer is off — set "
                    "PA_COMPUTER_ENABLED=true (docs/COMPUTER.md).")
        if args and str(args[0]).lower() == "off":
            out = svc.call("desktop_stop")
            return "Desktop session stopped — the browser profile " \
                "stays." if out.get("stopped") \
                else "No desktop session was running."
        minutes = None
        if args:
            try:
                minutes = int(args[0])
            except (ValueError, TypeError):
                return "usage: desktop [off | minutes]"
        out = svc.call("desktop_start",
                       {"minutes": minutes} if minutes else {})
        if out.get("error"):
            return f"Can't start the desktop: {out.get('message')}"
        url = out.get("url") or ""
        mins = max(1, int(out.get("expires_in") or 0) // 60)
        dm = self.api.open_dm(self.owner_id) \
            if self.api is not None and self.owner_id else None
        if not dm:
            svc.call("desktop_stop")
            return ("Couldn't open a DM to send the desktop link — "
                    "session stopped. Check DISCORD_OWNER_ID.")
        self.api.post_message(
            dm,
            "pal's desktop is up for ~"
            f"{mins} min (tailnet only):\n{url}\n"
            "It tears itself down at expiry — `desktop off` ends it "
            "early. Log into whatever you need (2FA is yours); I keep "
            "the session in the shared profile when it closes.")
        if str(channel_id) == str(dm):
            return f"Desktop link is above — expires in ~{mins} min."
        return ("Desktop is starting — the link is in your DMs. "
                "Never post it to a channel.")

    def _chat_progress_on(self) -> bool:
        """Live-progress preference: pa_settings 'chat_progress'
        (/settings) falling back to PA_CHAT_PROGRESS."""
        try:
            conn = dbmod.connect(self.db_path)
            try:
                v = dbmod.get_setting(conn, "chat_progress", "")
            finally:
                conn.close()
        except Exception:
            v = ""
        return v == "on" or (not v and self.s.chat_progress)

    def _voice_mode(self) -> str:
        """Owner's voice-note mode: pa_settings 'voice_notes'
        (/voice on|off|auto) -> 'on' | 'off' | 'auto' (default;
        PA_VOICE_NOTES=true still means 'on')."""
        return busymod.mode(self)

    def _voice_on(self) -> bool:
        """Should this reply carry a voice note? 'on' always, 'off'
        never, 'auto' only while the owner is busy (pa.busy)."""
        return busymod.voice_should_send(self)

    def _start_chat(self, d: dict):
        text = self._chat_text(d)
        channel_id = str(d.get("channel_id") or "")
        if not text:
            self.api.post_message(channel_id, "yes?")
            return
        th = threading.Thread(
            target=self._chat_worker,
            args=(channel_id, text),
            kwargs={"reply_to": str(d.get("id") or "")},
            daemon=True, name="pa-chat")
        th.start()

    # -- voice notes in ----------------------------------------------------
    def _start_voice(self, d: dict):
        """Owner sent an audio attachment in DM: download, transcribe,
        run a normal chat turn on the transcript, reply text + voice."""
        channel_id = str(d.get("channel_id") or "")
        att = next((a for a in d.get("attachments") or []
                    if voicemod.attachment_is_audio(a)), None)
        if att is None:
            return
        th = threading.Thread(
            target=self._voice_worker,
            args=(channel_id, att),
            kwargs={"reply_to": str(d.get("id") or "")},
            daemon=True, name="pa-voice-in")
        th.start()

    # -- chat-history imports ----------------------------------------------
    def _start_import(self, d: dict):
        """Owner DM'd a .zip/.json/.md/.txt — a chat export to file
        into the vault under Imports/."""
        channel_id = str(d.get("channel_id") or "")
        att = next((a for a in d.get("attachments") or []
                    if str(a.get("filename") or "").lower().endswith(
                        (".zip", ".json", ".md", ".txt"))), None)
        if att is None:
            return
        th = threading.Thread(
            target=self._import_worker,
            args=(channel_id, att),
            kwargs={"reply_to": str(d.get("id") or "")},
            daemon=True, name="pa-import")
        th.start()

    # -- inbound images / PDFs -------------------------------------------------
    def _start_file(self, d: dict, route: str):
        """Owner DM'd an image or PDF: download to the chat sandbox,
        then run a normal chat turn on a fenced untrusted block (plus
        whatever the owner typed as a caption)."""
        channel_id = str(d.get("channel_id") or "")
        pick = attachmod.is_image if route == "image" \
            else attachmod.is_pdf
        att = next((a for a in d.get("attachments") or []
                    if pick(a)), None)
        if att is None:
            return
        caption = self._chat_text(d)
        th = threading.Thread(
            target=self._file_worker,
            args=(channel_id, att, route, caption),
            kwargs={"reply_to": str(d.get("id") or "")},
            daemon=True, name="pa-file-in")
        th.start()

    def _image_caption(self, path):
        """Item-4 vision caption; None until a backend is configured —
        the turn then gets the plain 'image attached' stub."""
        try:
            from . import vision as visionmod
            return visionmod.caption_image(self.s, self.env, path)
        except Exception:
            return None

    def _file_worker(self, channel_id, att, route, caption,
                     reply_to=None):
        with self._chat_lock:
            prog = self._progress_for(channel_id, reply_to)
            reply = "(something broke)"
            ok = False
            try:
                prog.start("on it — " + ("reading the PDF…"
                                         if route == "pdf"
                                         else "getting the image…"))
                blob = voicemod.download_attachment(
                    self.s, str(att.get("url") or ""),
                    cap=self.s.attach_max_bytes)
                fname = attachmod.safe_filename(
                    str(att.get("filename") or route))
                path = attachmod.save_to_sandbox(
                    self.s, channel_id, fname, blob)
                if route == "pdf":
                    block = attachmod.pdf_turn_text(
                        fname, path, attachmod.extract_pdf_text(
                            blob, self.s.pdf_max_pages,
                            self.s.pdf_max_chars))
                else:
                    block = attachmod.image_turn_text(
                        fname, blob, path,
                        caption=self._image_caption(path))
                user_text = f"{caption}\n\n{block}" if caption else block
                out = self._engine().run_turn(
                    channel_id, user_text, progress=prog.note_tool)
                reply = out["text"]
                ok = True
            except Exception as e:
                log.exception("%s attachment handling failed", route)
                reply = f"(couldn't read that {route}: {str(e)[:120]})"
            finally:
                prog.finish(ok)
            self.api.reply(channel_id, reply, message_id=reply_to)
            if self._voice_on():
                self._voice.send_voice_note(channel_id, reply)

    def _import_worker(self, channel_id, att, reply_to=None):
        fname = str(att.get("filename") or "upload")
        try:
            blob = voicemod.download_attachment(
                self.s, str(att.get("url") or ""),
                cap=self.s.import_max_bytes)
            conn = dbmod.connect(self.db_path)
            try:
                from . import importers
                out = importers.run(conn, self.s, self.env, blob, fname)
            finally:
                conn.close()
            reply = (f"imported {out['imported']} "
                     f"({out['skipped']} already had, "
                     f"{out['digests']} digests) → `Imports/` — "
                     f"git: {out['git']}")
        except Exception as e:
            log.exception("import failed")
            reply = f"import failed: {str(e)[:150]}"
        self.api.reply(channel_id, reply, message_id=reply_to)

    def _progress_for(self, channel_id, message_id=None):
        """The per-turn progress driver: LiveProgress (status line +
        reactions) when PA_CHAT_PROGRESS is on, else the typing-only
        fallback."""
        if self._chat_progress_on() and self.api is not None:
            return progressmod.LiveProgress(
                self.api, channel_id, message_id=message_id)
        return _ChatProgress(
            self.api, channel_id, self.s.chat_working_seconds)

    def _voice_worker(self, channel_id, att, reply_to=None):
        with self._chat_lock:
            prog = self._progress_for(channel_id, reply_to)
            reply = "(something broke)"
            ok = False
            transcript = ""
            try:
                prog.start("on it — transcribing…")
                audio = voicemod.download_attachment(
                    self.s, str(att.get("url") or ""))
                transcript, _prov = voicemod.transcribe(
                    self.s, self.env, audio,
                    str(att.get("filename") or "voice.ogg"),
                    str(att.get("content_type") or ""))
                if not transcript:
                    reply = "(I couldn't make that out — try again?)"
                else:
                    out = self._engine().run_turn(
                        channel_id, transcript,
                        progress=prog.note_tool)
                    reply = out["text"]
                    ok = True
            except Exception as e:
                log.exception("voice note handling failed")
                reply = f"(couldn't transcribe that: {str(e)[:120]})"
            finally:
                prog.finish(ok)
            if transcript:
                self.api.reply(
                    channel_id, f"🎤 {transcript}\n\n{reply}",
                    message_id=reply_to)
                if self._voice_on():
                    self._voice.send_voice_note(channel_id, reply)
            else:
                self.api.reply(channel_id, reply, message_id=reply_to)

    def _chat_worker(self, channel_id, text, reply_to=None,
                     inbox_id=None):
        """One chat turn end-to-end; serialized so turns stay ordered
        and the single-session freebuff provider is never oversubscribed."""
        with self._chat_lock:
            prog = self._progress_for(channel_id, reply_to)
            reply = "(something broke)"
            ok = False
            ch = str(channel_id)
            try:
                prog.start()
                out = self._engine().run_turn(
                    channel_id, text, progress=prog.note_tool,
                    stop_check=lambda: ch in self._stop_flags,
                    steer_pull=lambda: self._steer.pop(ch, []))
                reply = out["text"]
                ok = True
            except Exception as e:
                log.exception("chat turn failed")
                reply = f"(something broke: {str(e)[:200]})"
            finally:
                prog.finish(ok)
                self._stop_flags.discard(ch)
            greet = self._away_greeting(channel_id)
            if greet:
                reply = f"{greet}\n\n{reply}"
            self.api.reply(channel_id, reply, message_id=reply_to,
                           components=_FEEDBACK_BUTTONS)
            self._tg_mirror(channel_id, reply)
            if self._voice_on():
                self._voice.send_voice_note(channel_id, reply)
            if inbox_id is not None:
                conn = dbmod.connect(self.db_path)
                try:
                    conn.execute(
                        "UPDATE chat_inbox SET status='done', reply=?,"
                        " finished_at=? WHERE id=?",
                        (reply[:4000], now_iso(), inbox_id))
                finally:
                    conn.close()

    # -- approval buttons ------------------------------------------------------
    def _request_chat_approval(self, name, args, approval_id):
        """chattools request_approval: DM buttons + wait for decision."""
        from . import review
        rl = review.label(name, args or {})
        body = (f"Approval needed: `{name}` {rl['text']}\n```json\n"
                f"{json_dumps(args)[:800]}\n```")
        if self.owner_id:
            dm = self.api.open_dm(self.owner_id)
            if dm:
                buttons = [
                    {"type": 2, "style": 3, "label": "Approve",
                     "custom_id": f"chatappr:{approval_id}:y"},
                    {"type": 2, "style": 4, "label": "Reject",
                     "custom_id": f"chatappr:{approval_id}:n"}]
                try:
                    from . import allowrules
                    if name in allowrules.ALWAYS_CAPABLE:
                        buttons.append(
                            {"type": 2, "style": 1,
                             "label": "Always allow",
                             "custom_id":
                             f"chatappr:{approval_id}:a"})
                except Exception:
                    pass
                self.api.post_message(dm, body, components=[buttons])
        deadline = time.monotonic() + self.s.chat_approval_wait
        return chattools.wait_decision(self.db_path, approval_id,
                                       deadline)

    def _request_chat_ask(self, question, choices, approval_id):
        """ask_owner's card: one button per choice (Yes/No when the
        tool gave none); _handle_component writes the clicked label
        back into the approval payload as 'answer'."""
        labels = [str(c)[:80] for c in (choices or ["Yes", "No"])][:5]
        row = [{"type": 2, "style": 1, "label": l,
                "custom_id": f"chatask:{approval_id}:{i}"}
               for i, l in enumerate(labels)]
        if self.owner_id:
            dm = self.api.open_dm(self.owner_id)
            if dm:
                self.api.post_message(dm, f"❓ {_clip(question)}",
                                      components=[row])
        deadline = time.monotonic() + self.s.chat_approval_wait
        status = chattools.wait_decision(self.db_path, approval_id,
                                         deadline)
        if status != "approved":
            return {"error": status,
                    "message": f"question {status} — don't keep "
                               "waiting on the owner"}
        conn = dbmod.connect(self.db_path)
        try:
            ap = approvals.get(conn, approval_id)
            answer = (json_loads_bounded(ap["payload_json"]) or {}).get(
                "answer") if ap else None
        finally:
            conn.close()
        return {"answer": answer or "(answer not recorded)"}

    def _handle_ask_click(self, conn, d, iid, token, parts):
        """chatask:<aid>:<i> — the clicked label is the answer."""
        aid, idx = parts[1], parts[2]
        row = approvals.get(conn, aid)
        if row is None or row["kind"] != "ask" or \
                row["status"] != "pending":
            self.api.interaction_update(
                iid, token, "(that question was already answered)")
            return
        payload = json_loads_bounded(row["payload_json"]) or {}
        choices = payload.get("choices") or ["Yes", "No"]
        try:
            label = str(choices[int(idx)])
        except (ValueError, IndexError):
            label = "?"
        payload["answer"] = label
        status = approvals.decide(conn, aid, True,
                                  edited_payload=payload)
        if status == "expired":
            self.api.interaction_update(
                iid, token, "That question expired (24h limit).")
            return
        self.api.interaction_update(
            iid, token,
            f"❓ {str(payload.get('question') or '')[:200]}\n"
            f"→ **{label}**")

    def _handle_goal_click(self, conn, d, iid, token, parts):
        """goalbtn:<id>:<status> — owner click flips a goal's
        status."""
        from . import goals as goalsmod
        try:
            gid, status = int(parts[1]), parts[2]
        except ValueError:
            self.api.interaction_reply(iid, token, "Unknown action.",
                                       ephemeral=True)
            return
        g = goalsmod.set_status(conn, gid, status, actor="owner")
        if g is None:
            self.api.interaction_update(iid, token, "(no such goal)")
            return
        dbmod.audit(conn, "owner", f"goal_{status}",
                    f"#{g['id']} {g['title'][:80]}")
        mark = {"active": "🎯 resumed", "paused": "⏸️ paused",
                "done": "✅ done"}.get(status, status)
        self.api.interaction_update(
            iid, token,
            f"{mark} — **#{g['id']}** {g['title'][:140]}")

    # -- secret capture -------------------------------------------------------
    def _cred_modal_kind(self, argv):
        """'credentials add'/'credentials otp' with NO further args on
        a slash command -> open the capture modal instead of replying."""
        if not getattr(self.s, "secret_capture", False):
            return None
        if len(argv) == 2 and argv[0].lower() == "credentials" \
                and argv[1].lower() == "add":
            return "password"
        if len(argv) == 2 and argv[0].lower() == "credentials" \
                and argv[1].lower() == "otp":
            return "otp"
        return None

    def _handle_modal(self, conn, d: dict):
        """Interaction type 5 — modal submit. The only path where a
        secret arrives inside the interaction payload (never a
        message). Seals to the vault and confirms domain-only."""
        iid, token = str(d.get("id")), str(d.get("token") or "")
        data = d.get("data") or {}
        cid = str(data.get("custom_id") or "")
        if cid not in ("credmodal:pw", "credmodal:otp"):
            self.api.interaction_reply(iid, token, "Unknown form.",
                                       ephemeral=True)
            return
        fields = {}
        for row in data.get("components") or []:
            for comp in row.get("components") or []:
                fields[str(comp.get("custom_id") or "")] = \
                    str(comp.get("value") or "")
        from . import credvault, secretform
        domain = fields.get("domain", "")
        secret = fields.get("secret", "")
        if cid == "credmodal:otp":
            out = credvault.add_otp(conn, self.env, domain, secret)
        else:
            out = credvault.add(conn, self.env, domain,
                                fields.get("username", "").strip(),
                                secret,
                                fields.get("label", "").strip())
        if "error" in out:
            self.api.interaction_reply(iid, token, out["message"],
                                       ephemeral=True)
            return
        secretform.clear_pending(conn)
        self.api.interaction_reply(
            iid, token,
            (f"one-time code for `{out['domain']}` saved — it's read "
             "once by browse_fill_secret then gone")
            if cid == "credmodal:otp" else
            f"login for `{out['domain']}` saved (sealed — I can't "
            "show it back)",
            ephemeral=True)

    @staticmethod
    def _parse_secret_text(kind: str, text: str):
        """Strict 'domain user secret' (password) or 'domain code'
        (otp) parse of a pasted DM. Returns (toks) or None."""
        toks = text.split()
        if kind == "otp":
            return toks if len(toks) == 2 and _looks_domain(
                toks[0]) else None
        return toks if len(toks) >= 3 and _looks_domain(toks[0]) \
            else None

    def _seal_parsed(self, conn, kind: str, toks: list):
        """(ok, domain|message) — seal parsed tokens into the vault."""
        from . import credvault
        if kind == "otp":
            out = credvault.add_otp(conn, self.env, toks[0], toks[1])
        else:
            out = credvault.add(conn, self.env, toks[0], toks[1],
                                toks[2],
                                " ".join(toks[3:]) if len(toks) > 3
                                else "")
        return (("error" not in out),
                out.get("domain") or out.get("message") or "")

    def _maybe_secret_paste(self, conn, d: dict) -> bool:
        """while a capture is pending, a credential-shaped DM is
        sealed + the message deleted instead of starting a chat turn
        (nothing is journaled). Returns True when the event was
        consumed."""
        if not getattr(self.s, "secret_capture", False):
            return False
        if d.get("guild_id") is not None and \
                d.get("channel_type") != 1:
            return False                # pastes only make sense in DMs
        from . import secretform
        kind = secretform.pending(conn)
        if not kind:
            return False
        text = (d.get("content") or "").strip()
        toks = self._parse_secret_text(kind, text)
        cid = str(d.get("channel_id") or "")
        mid = str(d.get("id") or "")
        if toks is None:
            # a bare blob or domain-ish message during a capture is
            # still probably a paste attempt — eat it (best-effort
            # delete) rather than journal it, but seal nothing.
            parts = text.split()
            if not parts or not any(
                    _looks_domain(t) or len(t) >= 8 and i == 0
                    for i, t in enumerate(parts)) \
                    or len(parts) > 3:
                return False            # ordinary chat — let it through
            self.api.delete_message(cid, mid)
            fmt = ("<domain> <username> <password>" if kind ==
                   "password" else "<domain> <code>")
            dbmod.audit(conn, "owner", "credential_paste_rejected", "")
            self.api.post_message(
                cid,
                f"I couldn't read that as `{fmt}` — deleted the "
                "message (best-effort; I can't always delete in DMs) "
                "and saved nothing. Try the `/credentials add` modal "
                "or `/credentials form`.")
            return True
        ok, res = self._seal_parsed(conn, kind, toks)
        self.api.delete_message(cid, mid)
        if ok:
            secretform.clear_pending(conn)
            self.api.post_message(
                cid,
                f"saved for `{res}` (sealed — never shown back). I "
                "deleted your message, but Discord doesn't always let "
                "me in DMs — the modal or one-time link is the safer "
                "path.")
        else:
            dbmod.audit(conn, "owner", "credential_paste_rejected",
                        toks[0][:120])
            self.api.post_message(
                cid, f"couldn't save that ({res}) — message deleted; "
                     "nothing was stored.")
        return True

    def _maybe_text_secret(self, conn, d: dict, argv) -> bool:
        """'credentials add <d> <u> <p>' typed as a plain message.
        The secret is sitting in a channel message — delete it
        best-effort; seal only when a capture is pending, otherwise
        refuse (text passwords are no longer accepted)."""
        if not getattr(self.s, "secret_capture", False):
            return False
        if not argv or argv[0].lower() != "credentials" \
                or len(argv) < 3 or argv[1].lower() not in ("add", "otp"):
            return False
        kind = "otp" if argv[1].lower() == "otp" else "password"
        toks = list(argv[2:])
        cid = str(d.get("channel_id") or "")
        self.api.delete_message(cid, str(d.get("id") or ""))
        from . import secretform
        if secretform.pending(conn) == kind:
            ok, res = self._seal_parsed(conn, kind, toks)
            if ok:
                secretform.clear_pending(conn)
                self.api.post_message(
                    cid,
                    f"saved for `{res}` and removed your message. "
                    "Please use `/credentials add` (no args) next time "
                    "— it opens a private modal.")
                return True
        dbmod.audit(conn, "owner", "credential_text_refused",
                    toks[0][:120] if toks else "")
        self.api.post_message(
            cid,
            "⚠️ that message had a secret in it — I deleted it "
            "(best-effort; I can't always delete in DMs) and saved "
            "nothing. Use `/credentials add` alone for the private "
            "modal, or `/credentials form` for a one-time link.")
        return True

    def _handle_component(self, conn, d: dict):
        iid, token = str(d.get("id")), str(d.get("token") or "")
        cid = str((d.get("data") or {}).get("custom_id") or "")
        parts = cid.split(":")
        if parts[0] in ("fbup", "fbdn"):
            good = parts[0] == "fbup"
            from .evolve import outcomes as outcomesmod
            outcomesmod.feedback(
                conn, str(d.get("channel_id") or ""), good)
            self.api.interaction_update(
                iid, token,
                "👍 noted — thanks" if good else
                "👎 noted — thanks for flagging it; `bad <what was "
                "wrong>` adds detail.")
            return
        if len(parts) == 3 and parts[0] == "ownkind":
            # the /connect 'whose account?' button
            from . import connectflows
            provider, kind = parts[1], parts[2]
            try:
                connectflows.set_owner_kind(conn, provider, kind)
                whose = "**your** account" if kind == "owner" \
                    else "**pal's** account"
                self.api.interaction_update(
                    iid, token,
                    f"{provider} marked as {whose}.")
            except Exception as e:
                self.api.interaction_update(
                    iid, token, f"couldn't mark account: {e}")
            return
        if len(parts) == 2 and parts[0] == "credmodal":
            # "Save password" / "Save code" buttons open the modal
            if not getattr(self.s, "secret_capture", False):
                self.api.interaction_reply(
                    iid, token,
                    "Secure capture is off (PA_SECRET_CAPTURE).",
                    ephemeral=True)
                return
            from . import secretform
            kind = "otp" if parts[1] == "otp" else "password"
            secretform.mark_pending(conn, kind)
            self.api.interaction_modal(iid, token, _cred_modal(kind))
            return
        if len(parts) == 3 and parts[0] == "chatask":
            self._handle_ask_click(conn, d, iid, token, parts)
            return
        if len(parts) == 3 and parts[0] == "evappr":
            self._handle_evolve_click(conn, d, iid, token, parts)
            return
        if len(parts) == 3 and parts[0] == "goalbtn":
            self._handle_goal_click(conn, d, iid, token, parts)
            return
        if len(parts) == 3 and parts[0] == "evprov":
            self._handle_provision_click(conn, d, iid, token, parts)
            return
        if len(parts) != 3 or parts[0] != "chatappr":
            self.api.interaction_reply(iid, token, "Unknown action.",
                                       ephemeral=True)
            return
        aid = parts[1]
        always = parts[2] == "a"
        verdict = parts[2] in ("y", "a")
        row = approvals.get(conn, aid)
        if row is None or row["status"] != "pending":
            self.api.interaction_update(
                iid, token,
                f"(approval {aid[:8]} is "
                f"{row['status'] if row else 'gone'})")
            return
        try:
            tool = json_loads_bounded(row["payload_json"]).get("tool") \
                or "action"
        except Exception:
            tool = "action"
        status = approvals.decide(conn, aid, verdict)
        if status == "expired":
            self.api.interaction_update(
                iid, token, f"Approval {aid[:8]} expired (24h limit).")
            return
        if always and verdict:
            from . import allowrules
            if tool in allowrules.ALWAYS_CAPABLE:
                allowrules.add(conn, tool, "")
                self.api.interaction_update(
                    iid, token,
                    f"Approved: `{tool}` ({aid[:8]}) — **always allow** "
                    f"rule saved for `{tool}`")
                return
            # 'Always' can't apply to this tool — the click still
            # approved this one call; say so rather than pretending.
            self.api.interaction_update(
                iid, token,
                f"Approved: `{tool}` ({aid[:8]}) — no standing rules "
                "for this tool")
            return
        if row["kind"] == "job_action":
            if verdict:
                self.api.interaction_update(
                    iid, token,
                    f"Approved: `{tool}` ({aid[:8]}) — running it now.")
                threading.Thread(
                    target=self._apply_job_action, args=(aid,),
                    daemon=True, name="pa-job-apply").start()
            else:
                self.api.interaction_update(
                    iid, token,
                    f"Rejected: `{tool}` ({aid[:8]}) — skipped.")
            return
        self.api.interaction_update(
            iid, token,
            f"{'Approved' if verdict else 'Rejected'}: `{tool}` "
            f"({aid[:8]}) — the chat turn continues.")

    def _handle_evolve_click(self, conn, d, iid, token, parts):
        """evappr:<aid>:y|n — the owner's click on an evolve apply
        card. Approve runs the Track A pipeline in a thread; Reject
        marks the item rejected."""
        from .evolve import apply as applymod, items as itemsmod
        aid, verdict = parts[1], parts[2] == "y"
        row = approvals.get(conn, aid)
        if row is None or row["status"] != "pending":
            self.api.interaction_update(
                iid, token,
                f"(approval {aid[:8]} is "
                f"{row['status'] if row else 'gone'})")
            return
        try:
            item_id = json_loads_bounded(
                row["payload_json"])["args"]["item_id"]
        except Exception:
            item_id = None
        approvals.decide(conn, aid, verdict)
        if not verdict:
            if item_id:
                itemsmod.set_status(conn, item_id, "rejected")
            self.api.interaction_update(
                iid, token, f"Rejected: evolve #{item_id} ({aid[:8]})")
            return
        self.api.interaction_update(
            iid, token,
            f"Approved: evolve #{item_id} ({aid[:8]}) — applying.")
        threading.Thread(
            target=self._apply_evolve_item, args=(aid,),
            daemon=True, name="pa-evolve-apply").start()

    def _handle_provision_click(self, conn, d, iid, token, parts):
        """evprov:<aid>:y|n — provider-setup card. Approve opens the
        official URL on the computer (or DMs it); reject is quiet."""
        aid, verdict = parts[1], parts[2] == "y"
        row = approvals.get(conn, aid)
        if row is None or row["status"] != "pending":
            self.api.interaction_update(
                iid, token,
                f"(approval {aid[:8]} is "
                f"{row['status'] if row else 'gone'})")
            return
        approvals.decide(conn, aid, verdict)
        if not verdict:
            self.api.interaction_update(
                iid, token, f"Rejected provider setup ({aid[:8]}).")
            return
        self.api.interaction_update(
            iid, token, f"Approved ({aid[:8]}) — opening the page.")
        try:
            args = json_loads_bounded(
                row["payload_json"]).get("args") or {}
        except Exception:
            args = {}
        threading.Thread(
            target=self._run_provision, args=(args,),
            daemon=True, name="pa-evolve-prov").start()

    def _run_provision(self, args):
        conn = self._db()
        try:
            from .evolve import provision
            provision.run_approved(conn, self.s, self, args)
        except Exception:
            pass
        finally:
            conn.close()

    def _apply_evolve_item(self, aid):
        conn = self._db()
        try:
            from .evolve import apply as applymod
            row = approvals.get(conn, aid)
            item_id = None
            if row:
                try:
                    item_id = json_loads_bounded(
                        row["payload_json"])["args"]["item_id"]
                except Exception:
                    pass
            if item_id:
                applymod.apply_approved(conn, self.s, item_id,
                                        bot=self)
        except Exception:
            pass
        finally:
            conn.close()

    def _apply_job_action(self, aid):
        """Execute one owner-approved job_action (from a queued
        unattended run). Runs outside the interaction deadline; the
        result is reported to the owner DM."""
        tool, channel_id = "action", ""
        conn = dbmod.connect(self.db_path)
        try:
            row = approvals.get(conn, aid)
            payload = json_loads_bounded(row["payload_json"]) if row \
                else {}
            tool = payload.get("tool") or "action"
            channel_id = payload.get("channel_id") or ""
            ctx = chattools.ChatToolContext(
                conn, self.s, None, str(channel_id or self.owner_id
                                        or "job"), env=self.env)
            try:
                out = chattools.execute_approved(ctx, tool,
                                                 payload.get("args")
                                                 or {})
                conn.execute(
                    "UPDATE approvals SET status='applied', applied_at=?"
                    " WHERE id=?", (now_iso(), aid))
            except Exception as e:
                out = {"error": "apply_failed", "message": str(e)[:300]}
        except Exception as e:
            log.exception("job_action apply failed")
            out = {"error": "apply_failed", "message": str(e)[:300]}
        finally:
            conn.close()
        target = str(channel_id) or self._owner_dm_id()
        if target:
            self.api.post_message(
                target, f"Applied `{tool}` ({aid[:8]}):\n"
                        f"```json\n{_clip(json_dumps(out))[:800]}\n```")

    def _feedback(self, conn, channel_id, good, args=None):
        """`/good`, `/bad [note]` — owner rates the last answer;
        lands as a feedback_* outcome row + incident."""
        from .evolve import outcomes as outcomesmod
        note = " ".join(args or [])[:400]
        outcomesmod.feedback(conn, channel_id, good, note=note)
        if good:
            return "👍 noted — thanks"
        return ("👎 noted — thanks for flagging it. `bad <what was "
                "wrong>` also records it as a correction to learn "
                "from.")

    def _capabilities_card(self, conn) -> str:
        """`/capabilities` — one card: what pal can do by area, what
        needs owner approval, which flags are off, online nodes."""
        s = self.s
        lines = ["🧠 **pal capabilities**"]
        areas = [
            ("chat & memory", True),
            ("schedules & reminders", True),
            ("goals", getattr(s, "goals", False)),
            ("proactive / monitors", getattr(s, "monitors", False)),
            ("phone (nodes)", bool((getattr(s, "nodes", None) or {}).get("phone"))),
            ("computer / browse", getattr(s, "computer", False)),
            ("made tools", getattr(s, "madetools", False)),
            ("self-improvement archive", getattr(s, "evolve_archive", False)),
            ("self-practice", getattr(s, "evolve_practice", False)),
        ]
        for name, on in areas:
            lines.append(f"  {'✅' if on else '—'} {name}"
                         + ("" if on else " (off)"))
        from . import chattools
        gated = [n for n, t in chattools.CHAT_TOOLS.items()
                 if t.get("approval")]
        lines.append("🔐 always needs your approval: "
                     + ", ".join(sorted(gated)[:14]))
        try:
            from . import nodes as nodesmod
            on_nodes = nodesmod.status_lines(conn, s, refresh=False)
            if on_nodes:
                lines.append("🛰 nodes: " + " | ".join(on_nodes[:4]))
            else:
                lines.append("🛰 nodes: none online")
        except Exception:
            pass
        return "\n".join(lines)

    def _memory_audit_cmd(self, conn, args):
        """`/memory audit` — untrusted-source memories/skills/blocks/
        facts added this week, with one-tap remove via
        `/memory forget <id>`."""
        from . import provenance
        lines = ["🧾 memory audit — untrusted sources this week"]
        found = 0
        for table, label, key in (("memories", "memory", "content"),
                                  ("owner_facts", "fact", "key"),
                                  ("memory_blocks", "block", "name"),
                                  ("skills", "skill", "name")):
            for r in provenance.untrusted_this_week(conn, table):
                found += 1
                rid = r["id"] if "id" in r.keys() else "?"
                lines.append(f"  [{r['source']}] {label} #{rid}: "
                             f"{str(r[key])[:70]}")
        if not found:
            lines.append("  nothing from email/web/file/import "
                         "this week ✅")
        else:
            lines.append("remove: `/memory forget <id>`")
        return "\n".join(lines)

    def _evolve_cmd(self, conn, args):
        """`/evolve` — open self-improvement items + recent
        applies/rollbacks; `/evolve <id>` shows one item's plan.
        `/evolve archive [target]` — variant lineage + scores;
        `/evolve revert <variant id>` — make a variant live."""
        from .evolve import changelog, items
        if args and args[0] == "archive":
            return self._evolve_archive_cmd(conn, args[1:])
        if args and args[0] == "revert":
            return self._evolve_revert_cmd(conn, args[1:])
        if args and args[0] == "practice":
            from .evolve import practice
            return practice.cmd(conn, self.s, args[1:])
        if args:
            try:
                item = items.get(conn, int(args[0]))
            except (TypeError, ValueError):
                return "usage: evolve [<item id>]"
            if item is None:
                return f"no evolve item {args[0]}"
            lines = [
                f"#{item['id']} [{item['shape']}/{item['risk']}] "
                f"{item['status']} — {item['title']}",
                f"hits {item['hits']} · updated {item['updated_at']}"]
            if item["eval_score"] is not None:
                lines.append(f"eval score: {item['eval_score']:.2f}")
            if item["error"]:
                lines.append(f"error: {item['error'][:200]}")
            if item["plan"]:
                lines.append("plan:\n" + item["plan"][:1200])
            return "\n".join(lines)
        open_items = items.list_items(conn, limit=10)
        events = changelog.recent(
            conn, limit=5,
            verbs=("applied", "rolled_back", "rejected"))
        head = (f"evolve: {'on' if self.s.evolve else 'off'} "
                f"(auto-apply "
                f"{'on' if self.s.evolve_auto_apply else 'off'})")
        lines = [head]
        if open_items:
            lines.append("Open items:")
            for i in open_items:
                lines.append(
                    f"  #{i['id']} [{i['shape']}/{i['risk']}, "
                    f"{i['status']}] {i['title'][:80]}"
                    + (f" ({i['hits']} hits)" if i["hits"] > 1 else ""))
        else:
            lines.append("No open items.")
        if events:
            lines.append("Recent:")
            for e in events:
                tag = f"#{e['item_id']} " if e["item_id"] else ""
                lines.append(f"  {e['verb']} {tag}{e['text'][:80]}")
        return "\n".join(lines)

    def _evolve_archive_cmd(self, conn, args):
        """`/evolve archive` — every archived target; `/evolve archive
        <kind:target|target>` — one target's variant lineage."""
        from .evolve import archive
        if not getattr(self.s, "evolve_archive", False):
            return ("the improvement archive is off "
                    "(PA_EVOLVE_ARCHIVE)")
        if not args:
            rows = archive.targets(conn)
            if not rows:
                return ("no archived variants yet — skill/snippet/"
                        "config/tool/memory-block changes land here "
                        "once the archive is on")
            lines = ["improvement archive:"]
            for r in rows[:20]:
                live_tag = f"live=#{r['live_id']}" \
                    if r["live_id"] else "no live"
                lines.append(f"  {r['kind']}:{r['target']} — "
                             f"{r['n']} variant(s), {live_tag}")
            return "\n".join(lines)
        target = args[0]
        if ":" in target:
            kind, _, target = target.partition(":")
        else:
            kind = ""
        if kind:
            lines = archive.lineage_lines(conn, kind, target)
        else:
            lines = []
            for k in archive.KINDS:
                lines += archive.lineage_lines(conn, k, target)
        return "\n".join(lines) if lines \
            else f"no variants for {args[0]!r}"

    def _evolve_revert_cmd(self, conn, args):
        """`/evolve revert <variant id>` — owner flips live back to a
        stored variant (its content returns to the store)."""
        from .evolve import archive
        if not getattr(self.s, "evolve_archive", False):
            return ("the improvement archive is off "
                    "(PA_EVOLVE_ARCHIVE)")
        try:
            vid = int(args[0])
        except (TypeError, ValueError, IndexError):
            return "usage: evolve revert <variant id>"
        out = archive.revert(conn, self.s, vid)
        if not out.get("ok"):
            return f"revert failed: {out.get('error')}"
        dbmod.audit(conn, "owner", "evolve_revert",
                    f"{out['kind']}:{out['target']} -> variant {vid}")
        return (f"↩️ {out['kind']}:{out['target']} is now variant "
                f"#{vid} (was #{out.get('previous_live') or '-'})")

    def _voice_cmd(self, conn, args):
        """`/voice [on|off|auto]` — owner toggle for voice-note
        replies. auto = only while you're busy (pa.busy triggers)."""
        state = (args[0] if args else "").strip().lower()
        if state == "on":
            dbmod.set_setting(conn, "voice_notes", "on")
            return ("voice-note replies ON — every reply includes a "
                    "voice note.")
        if state == "off":
            dbmod.set_setting(conn, "voice_notes", "off")
            return "voice-note replies off — text only."
        if state == "auto":
            dbmod.set_setting(conn, "voice_notes", "auto")
            return ("voice-note replies AUTO — I'll speak replies only "
                    "while you're busy (voice messages, \"i'm busy\", "
                    "or a calendar event running now).")
        cur = self._voice_mode()
        return (f"voice-note replies are {cur}"
                + (" (busy-only)" if cur == "auto" else "")
                + ". Use `voice on|off|auto`. Voice notes you send "
                  "are always transcribed either way.")

    def _call_cmd(self, conn, args):
        """`/call test|stats|<what to say>` — try the call flow, or
        show call_metrics timing stats."""
        text = " ".join(args).strip()
        if not text:
            return "usage: call <test|stats|what pal should say>"
        if text.lower() == "stats":
            return callmod.stats_text(conn)
        if text.lower() == "test":
            text = ("This is a test call from pal — your voice setup "
                    "works end to end.")
        res = self._calls.call(text, urgent=True, source="command")
        if res.get("calling"):
            return ("📞 ringing you now — join the voice channel "
                    "(check your DMs for the link).")
        return f"call failed: {res.get('error')} — {res.get('message', '')}"

    def _argv_from_message(self, d: dict):
        # DMs only: accept 'pa status 3' or bare 'status 3'
        if (d.get("guild_id") or d.get("channel_type") not in (None, 1)):
            return []                       # guild messages: slash only
        text = (d.get("content") or "").strip()
        if text.lower().startswith("pa "):
            text = text[3:]
        return text.split()

    # -- commands --------------------------------------------------------------
    def execute(self, conn, argv, channel_id: str = "") -> str:
        """Run one owner command; returns the reply text."""
        if not argv:
            return ("Commands: help, assistants, task <name> <text>, "
                    "tasks, status <id>, cancel <id>, retry <id>, "
                    "approvals, approve <id>, reject <id>, schedules, "
                    "connect <app> [account:<label>], connections, "
                    "disconnect <app>, jobs [<name> on|off], "
                    "voice [on|off|auto], call <test|text>, "
                    "desktop [off|minutes], nodes, files [project], "
                    "capabilities")
        cmd = argv[0].lower()
        try:
            if cmd == "help":
                return self.execute(conn, [])
            if cmd == "as":
                return self._as_cmd(conn, argv[1:], channel_id)
            if cmd == "assistants":
                return self._assistants(conn)
            if cmd == "todo":
                return self._todo_cmd(conn, argv[1:], channel_id)
            if cmd == "usage":
                return self._usage_cmd(conn)
            if cmd == "update":
                return self._update_cmd(conn, channel_id)
            if cmd == "pages":
                return self._pages_cmd(conn, argv[1:])
            if cmd == "stop":
                if channel_id in self._stop_flags:
                    return "Already stopping — the turn ends at the " \
                        "next step."
                self._stop_flags.add(str(channel_id))
                return "⏹ stopping — the turn ends at the next step."
            if cmd == "steer":
                note = " ".join(argv[1:]).strip()
                if not note:
                    return "usage: steer <note>"
                self._steer.setdefault(str(channel_id), []).append(
                    note[:500])
                return "steering note queued — the turn picks it up " \
                    "at the next step."
            if cmd == "tasks":
                return self._tasks(conn)
            if cmd == "nodes":
                return self._nodes(conn, argv[1:])
            if cmd == "capabilities":
                return self._capabilities_card(conn)
            if cmd == "files":
                return self._files(conn, argv[1:])
            if cmd == "status":
                return self._status(conn, argv[1:])
            if cmd == "cancel":
                return self._cancel(conn, argv[1:])
            if cmd == "retry":
                return self._retry(conn, argv[1:])
            if cmd == "approvals":
                return self._approvals(conn)
            if cmd == "policy":
                return self._policy_cmd(conn, argv[1:])
            if cmd == "memory":
                return self._memory_cmd(conn, argv[1:])
            if cmd == "approve":
                return self._decide(conn, argv[1:], True)
            if cmd in ("reject", "deny"):
                return self._decide(conn, argv[1:], False)
            if cmd in ("good", "bad"):
                return self._feedback(conn, channel_id, cmd == "good",
                                      argv[1:])
            if cmd == "evolve":
                return self._evolve_cmd(conn, argv[1:])
            if cmd == "think":
                if not self.s.router_escalate:
                    return ("Escalate tier is off "
                            "(PA_ROUTER_ESCALATE).")
                dbmod.set_setting(
                    conn, f"escalate_next:{channel_id}", "1")
                return ("🧠 armed — your next message gets the "
                        "escalate tier.")
            if cmd == "schedules":
                return self._schedules(conn)
            if cmd == "task":
                return self._create_task(conn, argv[1:])
            if cmd == "connect":
                return self._connect(conn, argv[1:], channel_id)
            if cmd in ("connections", "accounts"):
                return self._connections(conn)
            if cmd == "disconnect":
                return self._disconnect(conn, argv[1:])
            if cmd == "jobs":
                return self._jobs(conn, argv[1:])
            if cmd == "voice":
                return self._voice_cmd(conn, argv[1:])
            if cmd == "call":
                return self._call_cmd(conn, argv[1:])
            if cmd == "watch":
                return self._watch(conn, argv[1:])
            if cmd in ("monitor", "monitors"):
                return self._monitor(conn, argv[1:])
            if cmd == "hooks":
                return self._hooks(conn, argv[1:])
            if cmd == "allow":
                return self._allow(conn, argv[1:])
            if cmd == "audit":
                return self._audit(conn, argv[1:])
            if cmd == "credentials":
                return self._credentials(conn, argv[1:])
            if cmd == "briefing":
                return self._briefing(conn, argv[1:])
            if cmd == "reminders":
                return self._reminders(conn, argv[1:])
            if cmd == "desktop":
                return self._desktop_cmd(conn, argv[1:], channel_id)
            if cmd == "goals":
                return self._goals_cmd(conn, argv[1:], channel_id)
            if cmd == "activity":
                return self._activity_cmd(conn, argv[1:], channel_id)
            if cmd == "triage":
                return self._triage_cmd(conn)
            if cmd == "reset":
                return self._reset(conn, channel_id)
            return f"Unknown command {cmd!r}. Try: help"
        except Exception as e:
            return f"Error: {str(e)[:300]}"

    def _assistants(self, conn):
        rows = conn.execute(
            "SELECT id, name, provider_model FROM assistants"
            " ORDER BY id").fetchall()
        if not rows:
            return "No assistants yet."
        return "Assistants: " + "; ".join(
            f"#{r['id']} {r['name']} ({r['provider_model']})"
            for r in rows)

    def _usage_cmd(self, conn):
        import datetime
        day = datetime.datetime.now(datetime.timezone.utc) \
            .strftime("%Y-%m-%d")
        u = dbmod.usage_today(conn, day)
        pend = len(approvals.list_pending(conn))
        mons = conn.execute(
            "SELECT COUNT(*) n FROM monitors WHERE enabled=1") \
            .fetchone()["n"] if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='monitors'"
            ).fetchone() else 0
        sched = conn.execute(
            "SELECT COUNT(*) n FROM schedules WHERE enabled=1") \
            .fetchone()["n"]
        tok = u["tokens_in"] + u["tokens_out"]
        return (f"📊 today ({day} UTC): **{u['requests']}**/"
                f"{self.s.daily_request_limit} requests, "
                f"**{tok}**/{self.s.daily_token_limit} tokens "
                f"(in {u['tokens_in']}, out {u['tokens_out']})\n"
                f"pending approvals: {pend} · monitors on: {mons} · "
                f"schedules on: {sched}"
                + self._budget_usage(conn)
                + self._evolve_usage(conn, day))

    def _budget_usage(self, conn) -> str:
        """today's budget lines — remaining per resource for the
        tracked scopes, top-3 busiest, escalations left."""
        try:
            from . import budget as _budget
            left_e = _budget.remaining(conn, "owner-chat",
                                       "escalations")
            left_bg = _budget.remaining(conn, "background",
                                        "escalations")
            tops = _budget.top_scopes(conn, 3)
            lines = [f"\nbudgets — escalations left today: "
                     f"**{left_e}** (background {left_bg})"]
            if tops:
                lines.append("top: " + " · ".join(
                    f"{t['scope']}:{t['resource'].split(':')[0]} "
                    f"{t['used'] + t['reserved']}"
                    for t in tops))
            return "\n".join(lines)
        except Exception:
            return ""

    def _evolve_usage(self, conn, day) -> str:
        """evolve on/off + open items + last apply/rollback
        + escalate usage — omitted entirely when the tables aren't
        there yet."""
        try:
            if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='evolve_items'").fetchone() is None:
                return ""
            from .evolve import items
            open_items = items.list_items(conn)
        except Exception:
            return ""
        lines = ["\nevolve: " + ("on" if self.s.evolve else "off")]
        if open_items:
            lines.append(f"  open items: {len(open_items)}")
        try:
            last = conn.execute(
                "SELECT verb, text, created_at FROM evolve_changelog"
                " WHERE verb IN ('applied','rolled_back')"
                " ORDER BY id DESC LIMIT 1").fetchone()
        except Exception:
            last = None
        if last:
            lines.append(f"  last {last['verb']}: "
                         f"{last['text'][:60]} "
                         f"({last['created_at'][:10]})")
        esc = dbmod.get_setting(conn, f"router_escalate:{day}", "0")
        if self.s.router_escalate or esc not in ("", "0"):
            lines.append(f"  escalate today: {esc or '0'}/"
                         f"{self.s.router_escalate_rpd}")
        return "\n".join(lines)

    def _update_cmd(self, conn, channel_id):
        """`/update`: dry-run the remote, then post an approval card
        for the real pull (job_action: approving executes once)."""
        from . import selfupdate
        plan = selfupdate.plan()
        if plan.get("update_available") is False:
            return f"Already up to date at {plan['current_sha']}."
        aid = approvals.create_approval(
            conn, "job_action",
            {"tool": "self_update", "args": {},
             "channel_id": str(channel_id)})
        dbmod.audit(conn, "owner", "update_requested",
                    selfupdate.describe(plan)[:200])
        dm = self.api.open_dm(self.owner_id) if self.api else None
        if dm:
            self._post_job_pending(
                dm, [{"id": aid, "tool": "self_update", "args": {}}])
            card = " — approve the card in your DMs to pull."
        else:
            card = f" — approve {aid[:8]} to pull."
        return f"⬆️ {selfupdate.describe(plan)}{card}"

    def _pages_cmd(self, conn, args):
        from . import pages
        if args:
            slug = args[0].strip()
            return (f"Unpublished `{slug}`." if pages.unpublish(
                conn, slug) else f"No live page `{slug}`.")
        rows = pages.list_pages(conn)
        if not rows:
            return "No shared pages — ask me to `page_publish` one."
        base = (self.s.public_url or "").rstrip("/")
        return "\n".join(
            f"- **{r['title']}** — {base}/pages/{r['slug']}"
            for r in rows)

    def _as_cmd(self, conn, args, channel_id):
        """`/as [name|default]` — per-channel assistant pin; empty
        shows the effective assistant."""
        ch = str(channel_id)
        row = conn.execute(
            "SELECT assistant_name FROM chat_threads WHERE channel_id=?",
            (ch,)).fetchone()
        override = row["assistant_name"] if row else ""
        if not args:
            eff = override or self.s.chat_assistant
            return (f"This channel talks to **{eff}**"
                    + (f" (override; global is {self.s.chat_assistant})"
                       if override else " (global pin).")
                    + " `/as <name>` switches, `/as default` clears.")
        name = " ".join(args).strip()
        conn.execute(
            "INSERT OR IGNORE INTO chat_threads(channel_id, summary,"
            " created_at, updated_at) VALUES(?,'',?,?)",
            (ch, now_iso(), now_iso()))
        if name.lower() in ("default", "clear", "reset"):
            conn.execute(
                "UPDATE chat_threads SET assistant_name='',"
                " updated_at=? WHERE channel_id=?", (now_iso(), ch))
            return (f"Back to the global assistant "
                    f"({self.s.chat_assistant}).")
        a = conn.execute(
            "SELECT name FROM assistants WHERE name=?",
            (name,)).fetchone()
        if a is None:
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM assistants ORDER BY name").fetchall()]
            return (f"No assistant named {name!r} — have: "
                    + ", ".join(names))
        conn.execute(
            "UPDATE chat_threads SET assistant_name=?, updated_at=?"
            " WHERE channel_id=?", (a["name"], now_iso(), ch))
        dbmod.audit(conn, "owner", "as",
                    f"channel {ch} -> {a['name']}")
        return f"This channel now talks to **{a['name']}**."

    def _todo_cmd(self, conn, args, channel_id):
        ch = str(channel_id)
        sub = " ".join(args).strip()
        low = sub.lower()
        if low.startswith("done "):
            try:
                num = int(low.split(None, 1)[1])
            except (ValueError, IndexError):
                return "usage: todo done <n>"
            row = conn.execute(
                "SELECT id FROM chat_todos WHERE channel_id=?"
                " AND done=0 ORDER BY id LIMIT 1 OFFSET ?",
                (ch, num - 1)).fetchone()
            if row is None:
                return f"No open todo #{num}."
            conn.execute("UPDATE chat_todos SET done=1 WHERE id=?",
                         (row["id"],))
        elif low == "clear":
            conn.execute(
                "DELETE FROM chat_todos WHERE channel_id=? AND done=1",
                (ch,))
            return "Cleared the finished todos."
        elif low.startswith("add "):
            text = sub.split(None, 1)[1].strip()[:200]
            conn.execute(
                "INSERT INTO chat_todos(channel_id, text, created_at)"
                " VALUES(?,?,?)", (ch, text, now_iso()))
        elif low:
            return "usage: todo [add <text> | done <n> | clear]"
        rows = conn.execute(
            "SELECT text, done FROM chat_todos WHERE channel_id=?"
            " ORDER BY id LIMIT 30", (ch,)).fetchall()
        if not rows:
            return "No todos yet — `todo add <text>` or ask me to."
        return "\n".join(
            f"{'~~' if r['done'] else ''}{i + 1}. {r['text']}"
            f"{'~~' if r['done'] else ''}"
            for i, r in enumerate(rows))

    def _tasks(self, conn):
        rows = conn.execute(
            "SELECT t.id, t.title, t.state, a.name an,"
            " sa.role srole FROM tasks t"
            " JOIN assistants a ON a.id=t.assistant_id"
            " LEFT JOIN subagents sa ON sa.task_id=t.id"
            " ORDER BY t.id DESC LIMIT 8").fetchall()
        if not rows:
            return "No tasks yet."
        out = []
        for r in rows:
            line = f"#{r['id']} [{r['state']}] {r['title']} ({r['an']})"
            if r["srole"]:
                calls = conn.execute(
                    "SELECT COUNT(*) c FROM steps WHERE task_id=? AND"
                    " kind='tool_call'", (r["id"],)).fetchone()["c"]
                toks = conn.execute(
                    "SELECT COALESCE(SUM(tokens_in+tokens_out),0) t"
                    " FROM steps WHERE task_id=?", (r["id"],)
                ).fetchone()["t"]
                line += (f" · {r['srole']} · calls {calls} · "
                         f"tok {toks}")
            out.append(line)
        return "\n".join(out)

    def _nodes(self, conn, args):
        from . import nodes as nodesmod
        if args and args[0] in ("jobs", "job"):
            rows = conn.execute(
                "SELECT id, node, cap, status, error, created_at"
                " FROM node_jobs ORDER BY id DESC LIMIT 10").fetchall()
            if not rows:
                return "No node jobs yet."
            return "\n".join(
                f"#{r['id']} [{r['status']}] {r['cap']}"
                f"{' @' + r['node'] if r['node'] else ''}"
                + (f" — {r['error'][:60]}" if r['error'] else "")
                for r in rows)
        lines = nodesmod.status_lines(conn, self.s)
        if not lines:
            return ("No nodes configured — set PA_NODES="
                    "name=url,... and PA_NODE_<NAME>_TOKEN. "
                    "See docs/NODES.md.")
        return "**Nodes**\n" + "\n".join(lines)

    def _files(self, conn, args):
        if not getattr(self.s, "workspace", False):
            return ("The workspace is off — set PA_WORKSPACE=1 "
                    "(docs/WORKSPACE.md).")
        from . import workspace as wsmod
        project = " ".join(args).strip() or None
        return wsmod.files_line(conn, self.s, project)

    def _status(self, conn, args):
        if not args:
            from . import status as statusmod
            return statusmod.build(conn, self.s, self.env)
        t = self._task(conn, args)
        if isinstance(t, str):
            return t
        line = (f"#{t['id']} [{t['state']}] {t['title']}"
                f" (assistant {t['an']})")
        if t["error_kind"]:
            line += f"\nerror: {t['error_kind']} {t['error_detail'] or ''}"
            if t["reset_at"]:
                line += f" resets {t['reset_at']}"
        if t["result_json"]:
            try:
                res = json_loads_bounded(t["result_json"])
                summ = res.get("summary") or res.get("text") or ""
            except Exception:
                summ = ""
            if summ:
                line += f"\n{summ[:1200]}"
        return line

    def _task(self, conn, args):
        if not args:
            return "usage: <command> <task id>"
        try:
            tid = int(args[0])
        except ValueError:
            return "task id must be a number"
        t = conn.execute(
            "SELECT t.*, a.name an FROM tasks t JOIN assistants a"
            " ON a.id=t.assistant_id WHERE t.id=?", (tid,)).fetchone()
        return t if t else f"no task #{tid}"

    def _cancel(self, conn, args):
        t = self._task(conn, args)
        if isinstance(t, str):
            return t
        return (f"task #{t['id']} cancelled" if
                workmod.request_cancel(conn, t["id"])
                else f"task #{t['id']} already {t['state']}")

    def _retry(self, conn, args):
        t = self._task(conn, args)
        if isinstance(t, str):
            return t
        return (f"task #{t['id']} requeued" if
                workmod.request_retry(
                    conn, t["id"], self.s.task_deadline_seconds)
                else f"task #{t['id']} is not finished")

    def _memory_cmd(self, conn, args):
        """`/memory` — list this assistant's memories + owner facts;
        `/memory forget <id>` / `forget fact <key>` deletes (the
        command IS the owner, so no approval card is needed — still
        audited)."""
        ast = conn.execute(
            "SELECT id, name FROM assistants WHERE name=?",
            (self.s.chat_assistant,)).fetchone()
        sub = str(args[0]).lower() if args else ""
        if sub == "audit":
            return self._memory_audit_cmd(conn, args[1:])
        if sub in ("blocks", "block", "undo"):
            return self._memory_blocks_cmd(conn, sub, args[1:])
        if sub == "forget":
            ref = " ".join(str(a) for a in args[1:]).strip()
            if not ref:
                return ("usage: memory forget <memory id> | "
                        "memory forget fact <key>")
            if ref.lower().startswith("fact "):
                key = ref[5:].strip()
                if memmod.delete_owner_fact(conn, key):
                    dbmod.audit(conn, "owner", "memory_forget",
                                f"fact {key}")
                    try:
                        from . import journal
                        journal.sync_profile(conn, self.s)
                    except Exception:
                        pass
                    return f"Fact {key!r} forgotten."
                return f"No owner fact {key!r}."
            if ref.isdigit() and ast and memmod.delete_memory(
                    conn, int(ref), ast["id"]):
                dbmod.audit(conn, "owner", "memory_forget",
                            f"memory {ref}")
                return f"Memory {ref} forgotten."
            return (f"Nothing matches {ref!r} — use a memory id from "
                    "the list or 'fact <key>'.")
        facts = memmod.list_owner_facts(conn)
        mems = memmod.list_memories(conn, ast["id"]) if ast else []
        lines = []
        if facts:
            lines.append("**Owner facts:**")
            lines += [f"  {r['key']} = {_clip(r['value'])[:120]}"
                      for r in facts[:15]]
        if mems:
            lines.append("**Memories:** (forget with `memory forget "
                         "<id>`)")
            lines += [f"  #{r['id']} {_clip(r['content'])[:140]}"
                      for r in mems[:15]]
        return "\n".join(lines) or ("Nothing remembered yet — ask me "
                                    "to remember something.")

    def _memory_blocks_cmd(self, conn, sub, rest):
        """`/memory blocks` — the pinned persona/human/goals/world
        blocks; `/memory block <name> <text>` sets one (owner-direct,
        audited); `/memory undo [id]` walks a history row back."""
        if not getattr(self.s, "memory_blocks", False):
            return ("pinned memory blocks are off — set "
                    "PA_MEMORY_BLOCKS=true (docs/FEATURES.md).")
        from . import memblocks
        if sub == "block":
            name = str(rest[0]).lower() if rest else ""
            text = " ".join(str(x) for x in rest[1:])
            if name not in memblocks.NAMES or not text.strip():
                return ("usage: memory block "
                        "<persona|human|goals|world> <text>")
            b = memblocks.set_block(conn, name, text,
                                    edited_by="owner")
            dbmod.audit(conn, "owner", "memory_block",
                        f"{name} v{b['version']}")
            return (f"{name} v{b['version']} saved "
                    f"({len(b['content'])}/{memblocks.cap(name)} "
                    "chars).")
        if sub == "undo":
            hid = int(rest[0]) if rest and str(rest[0]).isdigit() \
                else 0
            out = memblocks.undo(conn, hid, edited_by="owner")
            if "error" in out:
                return out["message"]
            dbmod.audit(conn, "owner", "memory_block_undo",
                        f"{out['name']} -> v{out['version']}")
            return (f"undone — {out['name']} is back to v"
                    f"{out['version']}.")
        lines = memblocks.card_lines(conn)
        hist = memblocks.history(conn, limit=5)
        if hist:
            lines.append("_recent edits (undo with `memory undo "
                         "<id>`):_")
            lines += [f"  `#{h['id']}` {h['name']} by "
                      f"{h['edited_by']} {h['created_at'][5:16]}"
                      for h in hist]
        return "\n\n".join(lines)

    def _approvals(self, conn):
        rows = approvals.list_pending(conn)
        if not rows:
            return "No pending approvals."
        out = []
        for r in rows[:10]:
            out.append(f"{r['id'][:8]} [{r['kind']}] "
                       f"{_clip(r['payload_json'])[:160]}")
        return "Pending approvals:\n" + "\n".join(out)

    def _decide(self, conn, args, approve: bool):
        if not args:
            return "usage: approve|reject <approval id>"
        prefix = args[0].strip()
        rows = conn.execute(
            "SELECT id FROM approvals WHERE id LIKE ? AND"
            " status='pending'", (prefix + "%",)).fetchall()
        if len(rows) != 1:
            return ("no pending approval matches" if not rows else
                    "id prefix is ambiguous")
        aid = rows[0]["id"]
        status = approvals.decide(conn, aid, approve)
        if status == "approved":
            row = approvals.get(conn, aid)
            if row["kind"] == "chat_action":
                # the paused chat turn sees the decision itself
                return f"approval {aid[:8]} approved (chat resumes)"
            if row["kind"] == "job_action":
                # held job action: run it now, report in the DM
                threading.Thread(
                    target=self._apply_job_action, args=(aid,),
                    daemon=True, name="pa-job-apply").start()
                return (f"approval {aid[:8]} approved — running the "
                        "action in the background (result posts to the "
                        "owner DM)")
            approvals.apply_if_new(conn, aid, actor="owner-discord",
                                   settings=self.s)
            workmod.resume_waiting(conn, row)
            return f"approval {aid[:8]} approved and applied"
        return f"approval {aid[:8]} -> {status}"

    def _schedules(self, conn):
        from . import scheduler as sch
        rows = sch.list_schedules(conn)
        if not rows:
            return "No schedules."
        out = []
        for r in rows:
            state = "on" if r["enabled"] else "off"
            out.append(f"{r['name']} [{state}] {r['freq']} "
                       f"{r['time_of_day']} {r['timezone']}")
        return "Schedules:\n" + "\n".join(out)

    def _jobs(self, conn, args):
        """`/jobs` — list agentic jobs, or `jobs <name> on|off` to
        toggle one."""
        from . import scheduler as sch
        if args:
            name = args[0]
            state = (args[1] if len(args) > 1 else "").lower()
            if state not in ("on", "off", "enable", "disable",
                             "enabled", "disabled", "true", "false"):
                return (f"usage: jobs {name} on|off")
            row = sch.get_schedule(conn, name)
            if row is None or row["kind"] != "agent_job":
                return f"no agentic job named {name!r}"
            on = state in ("on", "enable", "enabled", "true")
            sch.set_enabled(conn, name, on)
            return f"job **{name}** {'enabled' if on else 'disabled'}."
        rows = [r for r in sch.list_schedules(conn)
                if r["kind"] == "agent_job"]
        if not rows:
            return ("No agentic jobs. Create one in chat, e.g. "
                    "\"every Sunday 6pm go through my emails and update "
                    "my brain\".")
        out = []
        for r in rows:
            state = "on" if r["enabled"] else "off"
            when = f"{r['freq']} {r['time_of_day']} {r['timezone']}"
            if r["freq"] == "weekly" and r["day_of_week"] is not None:
                when += f" (dow {r['day_of_week']})"
            if r["freq"] == "monthly" and r["day_of_month"]:
                when += f" (day {r['day_of_month']})"
            try:
                prompt = json_loads_bounded(r["payload_json"]).get(
                    "prompt") or ""
            except Exception:
                prompt = ""
            out.append(f"**{r['name']}** [{state}] {when}\n"
                       f"  {_clip(prompt)[:140]}")
        return "Agentic jobs:\n" + "\n".join(out)

    def _watch(self, conn, args):
        """/watch — teach the inbox watcher: vip <addr|domain> always
        alerts, mute <addr|domain> never does, remove deletes a rule."""
        if not args:
            rows = conn.execute(
                "SELECT pattern, action FROM watch_rules"
                " ORDER BY action, pattern").fetchall()
            if not rows:
                return ("No watch rules. `watch vip <address-or-domain>`"
                        " always alerts, `watch mute "
                        "<address-or-domain>` never does.")
            return "Watch rules:\n" + "\n".join(
                f"- {r['action']}: {r['pattern']}" for r in rows)
        action = args[0].lower()
        if action in ("vip", "mute") and len(args) > 1:
            pattern = " ".join(args[1:]).strip().lower()
            if not pattern or " " in pattern:
                return "usage: watch vip|mute <address-or-domain>"
            conn.execute(
                "INSERT INTO watch_rules(pattern, action, created_at)"
                " VALUES(?,?,?) ON CONFLICT(pattern) DO UPDATE SET"
                " action=excluded.action",
                (pattern, action, now_iso()))
            return f"watch rule saved: **{action}** `{pattern}`"
        if action in ("remove", "rm", "del", "off", "none") \
                and len(args) > 1:
            pattern = " ".join(args[1:]).strip().lower()
            cur = conn.execute(
                "DELETE FROM watch_rules WHERE pattern=?", (pattern,))
            return (f"watch rule removed: `{pattern}`" if cur.rowcount
                    else f"no rule for `{pattern}`")
        return "usage: watch vip|mute|remove <address-or-domain>"

    def _monitor(self, conn, args):
        """`/monitor` — list standing monitors; `/monitor <id>
        on|off|del` toggles or removes one. The owner IS the command,
        so no approval card (audited anyway)."""
        from . import monitors as monmod
        if not args:
            rows = monmod.list_monitors(conn)
            if not rows:
                return ("No monitors. Ask me to watch a URL, a Gmail "
                        "search, or your calendar ('calendar soon'). "
                        "Enable the monitor-tick job so they run.")
            lines = []
            for m in rows:
                state = "on" if m["enabled"] else "paused"
                q = (m["query"][:60] + "…") if len(
                    m["query"]) > 60 else m["query"]
                lines.append(f"- #{m['id']} **{m['kind']}** `{q}` "
                             f"every {m['every_minutes']}m ({state})")
            return "Monitors:\n" + "\n".join(lines)
        action = args[0].lower()
        if action.isdigit() or (len(args) > 1 and args[1].isdigit()):
            mid = int(action) if action.isdigit() else int(args[1])
            verb = "on" if action.isdigit() and len(args) > 1 else action
            if action.isdigit() and len(args) > 1:
                verb = args[1].lower()
            if verb in ("on", "enable"):
                out = monmod.set_monitor(conn, mid, True)
                return (f"monitor #{mid} enabled" if out["updated"]
                        else f"no monitor #{mid}")
            if verb in ("off", "pause", "disable"):
                out = monmod.set_monitor(conn, mid, False)
                return (f"monitor #{mid} paused" if out["updated"]
                        else f"no monitor #{mid}")
            if verb in ("del", "rm", "remove"):
                out = monmod.remove_monitor(conn, mid)
                return (f"monitor #{mid} removed" if out["removed"]
                        else f"no monitor #{mid}")
        return "usage: monitor [<id> on|off|del]"

    def _hooks(self, conn, args):
        """`/hooks` — manage signed webhook endpoints. The signing
        secret is shown ONCE at add time; the list never shows it."""
        from . import webhooks
        if not args:
            rows = webhooks.list_endpoints(conn)
            if not rows:
                return ("No webhook endpoints. `hooks add <name>` "
                        "creates one and shows its signing secret "
                        "once. POST https://<app>/hooks/<name> with "
                        "X-PA-Signature: sha256=<hmac of body>.")
            return "Webhook endpoints:\n" + "\n".join(
                f"- `{r['name']}` "
                f"({'on' if r['enabled'] else 'off'}"
                + (f", last seen {r['last_seen_at'][:16]}"
                   if r["last_seen_at"] else "") + ")"
                for r in rows)
        action = args[0].lower()
        if action == "add" and len(args) > 1:
            name = args[1]
            prompt = " ".join(args[2:])
            out = webhooks.create(conn, name, prompt)
            if "error" in out:
                return out["message"]
            text = ("Webhook created — **save this secret, it is "
                    "shown once**:\n"
                    f"URL path: `{out['url_path']}`\n"
                    f"secret: `{out['secret']}`\n"
                    "Header: `X-PA-Signature: sha256=<hmac of raw "
                    "body>`")
            if out["name"] in ("github", "ci"):
                text += ("\n\n**GitHub setup** (repo → Settings → "
                         "Webhooks → Add webhook):\n"
                         f"• Payload URL: `<PA_PUBLIC_URL>"
                         f"{out['url_path']}`\n"
                         "• Content type: `application/json`\n"
                         "• Secret: the value above\n"
                         "• Events: workflow runs, check suites, "
                         "pull requests, issue comments\n"
                         "GitHub signs with `X-Hub-Signature-256` — "
                         "already accepted. CI/PR events arrive as "
                         "cards, no agent turn "
                         "(PA_GITHUB_HOOK_AGENT to change).")
            return text
        if action in ("del", "rm", "remove") and len(args) > 1:
            return ("endpoint removed" if webhooks.remove(
                conn, args[1]) else f"no endpoint {args[1]!r}")
        if len(args) > 1 and args[1].lower() in ("on", "off"):
            ok = webhooks.set_enabled(conn, action,
                                      args[1].lower() == "on")
            return (f"`{action}` {args[1]}" if ok
                    else f"no endpoint {action!r}")
        return ("usage: hooks | hooks add <name> [prompt] | "
                "hooks del <name> | hooks <name> on|off")

    def _allow(self, conn, args):
        """`/allow` — standing auto-approve rules for LOW-RISK tools
        only. Anything that sends/pays/deletes/runs code can never
        have a rule (cards simply never offer 'Always')."""
        from . import allowrules
        if not args:
            rows = allowrules.list_rules(conn)
            if not rows:
                return ("No standing allow rules — a rule is created "
                        "when you press **Always allow** on an "
                        "approval card, or `allow add <tool>`. Only "
                        "low-risk tools qualify: "
                        + ", ".join(sorted(allowrules.ALWAYS_CAPABLE)))
            return "Allow rules:\n" + "\n".join(
                f"- #{r['id']} `{r['tool']}` matching "
                f"`{r['pattern'] or '*'}`" for r in rows)
        if args[0].lower() == "add" and len(args) > 1:
            out = allowrules.add(conn, args[1],
                                 " ".join(args[2:]))
            return (out["message"] if "error" in out
                    else f"rule saved: `{out['tool']}` "
                         f"matching `{out['pattern']}`")
        if args[0].lower() in ("del", "rm", "remove") \
                and len(args) > 1 and args[1].isdigit():
            return ("rule removed" if allowrules.remove(
                conn, int(args[1])) else f"no rule #{args[1]}")
        return ("usage: allow | allow add <tool> [match] | "
                "allow del <id>")

    def _audit(self, conn, args):
        """`/audit [filter]` — the last 20 audit rows, newest first.
        Who did what, when: approvals, memory writes, rule changes,
        webhook hits."""
        q = (" ".join(args) if args else "").lower()
        rows = conn.execute(
            "SELECT at, actor, action, detail FROM audit_log"
            " ORDER BY id DESC LIMIT 60").fetchall()
        if q:
            rows = [r for r in rows if q in r["action"].lower()
                    or q in (r["detail"] or "").lower()]
        rows = rows[:20]
        if not rows:
            return "Nothing audited yet."
        return "Audit log:\n" + "\n".join(
            f"- `{r['at'][5:16]}` {r['actor']}: **{r['action']}**"
            + (f" — {_clip(r['detail'])[:90]}" if r["detail"] else "")
            for r in rows)

    def _credentials(self, conn, args):
        """`/credentials` — the login vault. Values are stored sealed
        and are NEVER displayed; the list shows domains only.

        With PA_SECRET_CAPTURE (default on) there is no 'add <d> <u>
        <p>' fast path: the slash command opens a private modal (see
        _cred_modal_kind), `/credentials form` issues a one-time
        HTTPS link, and a pasted secret is only sealed while a capture
        is pending."""
        from . import credvault, secretform
        cap = getattr(self.s, "secret_capture", False)
        if not args:
            rows = credvault.list_credentials(conn)
            tip = (" `/credentials add` opens a private modal; "
                   "`/credentials form` gives a one-time link."
                   if cap else
                   " `credentials add <domain> <user> <pass>` stores "
                   "one sealed.")
            if not rows:
                return ("No saved logins." + tip +
                        " Values are never shown again.")
            return "Saved logins:\n" + "\n".join(
                f"- `{r['domain']}`"
                + (f" ({r['label']})" if r["label"] else "")
                for r in rows) + "\n" + tip
        sub = args[0].lower()
        if sub == "form":
            if not cap:
                return ("Secure capture is off "
                        "(PA_SECRET_CAPTURE) — `credentials add` is "
                        "the only path.")
            url = secretform.url(
                self.s, secretform.issue(conn, "password"))
            if not url:
                return ("The one-time link needs an https "
                        "PA_PUBLIC_URL — see docs/CONNECTIONS.md. "
                        "Meanwhile `/credentials add` opens the "
                        "private modal.")
            secretform.mark_pending(conn, "password")
            return ("🔐 one-time link (works once, expires ~10 min, "
                    "https only):\n" + url +
                    "\nNothing you type there is ever shown back.")
        if sub in ("add", "otp"):
            kind = "otp" if sub == "otp" else "password"
            toks = " ".join(str(a) for a in args[1:]).split()
            if not toks:
                if not cap:
                    return ("usage: credentials add <domain> "
                            "<username> <password>")
                secretform.mark_pending(conn, kind)
                fmt = ("`<domain> <username> <password>`"
                       if kind == "password"
                       else "`<domain> <code>`")
                return ("🔐 capture armed for ~10 min — paste " + fmt
                        + " here and I'll seal it + delete your "
                        "message, use `/credentials add` for the "
                        "modal, or `/credentials form` for a "
                        "one-time link.")
            if not cap:
                # flag off = legacy behaviour (base branch parity)
                if sub == "add" and len(toks) >= 3:
                    out = credvault.add(
                        conn, self.env, toks[0], toks[1], toks[2],
                        " ".join(toks[3:]) if len(toks) > 3 else "")
                    return (out["message"] if "error" in out
                            else f"login for `{out['domain']}` saved "
                                 "(sealed — I can't show it back)")
                return ("usage: credentials add <domain> "
                        "<username> <password>")
            if secretform.pending(conn) == kind:
                ok, res = self._seal_parsed(conn, kind, toks)
                if ok:
                    secretform.clear_pending(conn)
                    return (f"saved for `{res}` — but please don't "
                            "put secrets in command options; "
                            "`/credentials add` alone opens the "
                            "private modal.")
            return ("⚠️ don't put secrets in command arguments — "
                    "nothing was saved. `/credentials add` alone "
                    "opens the private modal, `/credentials form` "
                    "gives a one-time link.")
        if sub in ("del", "rm", "remove") and len(args) > 1:
            return ("login removed" if credvault.remove(
                conn, args[1]) else f"no login for {args[1]!r}")
        return ("usage: credentials | credentials add|otp "
                "[nothing else] | credentials form | "
                "credentials del <domain>")

    def _briefing(self, conn, args):
        """`/briefing now` — fire the morning-briefing job once; its
        summary + voice note post to the owner's DM."""
        from . import scheduler as sch
        if args and args[0].lower() != "now":
            return "usage: briefing now"
        res = sch.fire_now(conn, "morning-briefing")
        if res.get("fired"):
            return "briefing queued — it'll land in your DM shortly."
        return f"couldn't queue the briefing: {res.get('detail')}"

    def _reminders(self, conn, args):
        """`/reminders` lists chat reminders; `reminders cancel
        <name>` drops one (and any queued copy)."""
        from . import scheduler as sch
        if args and args[0].lower() in (
                "cancel", "delete", "del", "rm", "off", "remove"):
            name = (args[1] if len(args) > 1 else "").strip()
            if not name:
                return "usage: reminders cancel <name>"
            row = sch.get_schedule(conn, name)
            if row is None or row["kind"] != "chat_prompt" or \
                    not name.startswith("remind-"):
                return f"no reminder named {name!r}"
            sch.delete_schedule(conn, name)
            conn.execute(
                "DELETE FROM chat_inbox WHERE source=? AND"
                " status='queued'", (f"schedule:{name}",))
            return f"reminder **{name}** cancelled."
        rows = [r for r in sch.list_schedules(conn)
                if r["kind"] == "chat_prompt"
                and r["name"].startswith("remind-")]
        if not rows:
            return ("No reminders. Ask in chat, e.g. \"remind me in 20 "
                    "minutes to call mum\" or \"/reminders cancel "
                    "<name>\".")
        out = []
        for r in rows:
            try:
                p = json_loads_bounded(r["payload_json"])
                text = p.get("text") or (p.get("prompt") or "")
            except Exception:
                text = ""
            state = "on" if r["enabled"] else "off"
            when = f"{r['freq']} {r['time_of_day']} {r['timezone']}"
            if r["freq"] == "weekly" and r["day_of_week"] is not None:
                when += f" (dow {r['day_of_week']})"
            if r["freq"] in ("monthly", "interval") and r["day_of_month"]:
                unit = "min" if r["freq"] == "interval" else "day"
                when += f" ({unit} {r['day_of_month']})"
            out.append(f"**{r['name']}** [{state}] {when}\n"
                       f"  {_clip(str(text))[:100]}")
        return "Reminders:\n" + "\n".join(out)

    def _reset(self, conn, channel_id):
        """`/reset` — wipe this channel's chat memory so pal stops
        trusting stale context (old refusals included). Messages are
        exported to chat_history_backups first."""
        if not channel_id:
            return "reset needs a channel context"
        n = dbmod.reset_channel_history(conn, channel_id)
        return (f"🧹 cleared {n} message{'s' if n != 1 else ''} from "
                f"this channel's memory (backup kept in the DB).")

    def _create_task(self, conn, args):
        if len(args) < 2:
            return "usage: task <assistant name> <what to do>"
        name, text = args[0], " ".join(args[1:])
        a = conn.execute(
            "SELECT id, name FROM assistants WHERE name=? OR id=?",
            (name, name if name.isdigit() else -1)).fetchone()
        if not a:
            return f"no assistant named {name!r}"
        tid = workmod.create_task(
            conn, a["id"], text[:80] or "discord task", text,
            private=False,
            deadline_seconds=self.s.task_deadline_seconds)
        return f"task #{tid} queued for {a['name']}"

    def _policy_cmd(self, conn, args):
        """/policy <tool> [args-json] — show the Decision a call
        would get (class, account, verdict, reason)."""
        if not args:
            return ("usage: policy <tool> [args-json] — explains what "
                    "the policy engine would do with that call")
        from . import policy
        tool = args[0]
        try:
            import json as _j
            targs = _j.loads(" ".join(args[1:])) if len(args) > 1 \
                else {}
            if not isinstance(targs, dict):
                targs = {}
        except Exception:
            targs = {}
        d = policy.decide(conn, tool, targs)
        whose = {"owner": "**your** account",
                 "pal": "pal's account"}.get(d.account_owner,
                                           d.account_owner)
        mark = {"allow": "✅", "approve": "✋", "pending": "🕓",
                "deny": "⛔"}.get(d.verdict, "❓")
        tail = " (queued for approval — unattended)" if d.queued else ""
        return (f"{mark} `{tool}` → **{d.verdict}**{tail}\n"
                f"class `{d.action_class}` on {whose} — {d.reason}")

    # -- standing goals ------------------------------------------
    def _goals_cmd(self, conn, args, channel_id):
        """`/goals` — the standing-goals card. The list posts with
        pause/done buttons (goalbtn:<id>:<status>); `goals add|done|
        pause|active|<id>` all act directly — the command IS the
        owner, same reasoning as `/memory forget`."""
        if not getattr(self.s, "goals", False):
            return ("standing goals are off — set PA_GOALS=true "
                    "(docs/FEATURES.md).")
        from . import goals as goalsmod
        sub = " ".join(str(a) for a in args).strip()
        low = sub.lower()
        if low.startswith("add "):
            title = sub[4:].strip()
            if not title:
                return "usage: goals add <title>"
            gid = goalsmod.add_goal(conn, title)
            dbmod.audit(conn, "owner", "goal_add",
                        f"#{gid} {title[:80]}")
            return (f"🎯 **#{gid}** {title[:140]}\nstatus: active · "
                    "tell me the why/criteria/cadence and I'll "
                    "flesh it out (`/goals` lists, `goal_update` "
                    "edits).")
        m = re.match(r"^(done|pause|paused|active)\s+(\d+)", low)
        if m:
            status = {"pause": "paused", "paused": "paused",
                      "done": "done", "active": "active"}[m.group(1)]
            g = goalsmod.set_status(conn, int(m.group(2)), status,
                                    actor="owner")
            if g is None:
                return f"no goal #{m.group(2)}"
            dbmod.audit(conn, "owner", f"goal_{status}",
                        f"#{g['id']} {g['title'][:80]}")
            mark = {"done": "✅", "paused": "⏸️",
                    "active": "🎯"}[status]
            return f"{mark} **#{g['id']}** {g['title'][:140]} → {status}."
        if low.isdigit():
            g = goalsmod.get(conn, int(low))
            if g is None:
                return f"no goal #{low}"
            lines = [
                f"🎯 **#{g['id']}** {g['title']} [{g['status']}]",
                f"cadence: {g['cadence']} · sources: {g['sources']}"]
            if g["why"]:
                lines.append(f"why: {g['why'][:200]}")
            if g["criteria"]:
                lines.append(f"success: {g['criteria'][:200]}")
            if g["next_action"]:
                lines.append(f"next: {g['next_action'][:200]}")
            if g["progress"]:
                lines.append(f"progress: {g['progress'][:300]}")
            for e in goalsmod.events(conn, g["id"], 5):
                lines.append(f"  {e['created_at'][:16]} {e['verb']} — "
                             f"{e['note'][:80]}")
            return "\n".join(lines)
        if low and low not in ("all", "list"):
            return ("usage: goals [add <title> | done|pause|active "
                    "<id> | <id>]")
        rows = goalsmod.list_goals(conn)
        if not rows:
            return ("No standing goals yet — `/goals add <title>` or "
                    "ask me to propose one.")
        if self.api and channel_id:
            try:
                self._post_goal_buttons(channel_id, rows)
            except Exception:
                pass
        return "\n".join(goalsmod.card_lines(conn))

    def _post_goal_buttons(self, channel_id, rows):
        """Pause/Resume/Done buttons under the /goals card —
        one row per open goal (cap 5)."""
        comps = []
        for g in [r for r in rows if r["status"] != "done"][:5]:
            toggle = ({"type": 2, "style": 2, "label": "▶ resume",
                       "custom_id": f"goalbtn:{g['id']}:active"}
                      if g["status"] == "paused" else
                      {"type": 2, "style": 2, "label": "⏸ pause",
                       "custom_id": f"goalbtn:{g['id']}:paused"})
            comps.append([
                {"type": 2, "style": 2, "disabled": True,
                 "label": f"#{g['id']} {g['title'][:60]}",
                 "custom_id": f"goalinfo:{g['id']}"},
                toggle,
                {"type": 2, "style": 3, "label": "✅ done",
                 "custom_id": f"goalbtn:{g['id']}:done"}])
        if comps:
            self.api.post_message(str(channel_id),
                                  "goal actions:", components=comps)

    def _activity_cmd(self, conn, args, channel_id):
        """`/activity [today|week|all]` — the local feed of what pal
        noticed, drafted, queued for approval and did.
        Pending cards re-post with Approve/Reject buttons."""
        if not getattr(self.s, "activity_feed", False):
            return ("activity feed is off — set PA_ACTIVITY=true "
                    "(docs/FEATURES.md).")
        from . import activity as actmod
        span = (str(args[0]).lower() if args else "today")
        hours = {"today": 24, "week": 24 * 7}.get(span)
        lines = actmod.card_lines(conn, since_hours=hours)
        if not lines:
            return "Nothing on the feed yet."
        if self.api and channel_id:
            try:
                self._post_job_pending(
                    channel_id, actmod.pending_approvals(conn))
            except Exception:
                pass
        return "\n".join(lines)

    def _triage_cmd(self, conn):
        """`/triage` — today's pal-DB inbox buckets."""
        if not getattr(self.s, "triage", False):
            return ("inbox triage is off — set PA_TRIAGE=true "
                    "(docs/FEATURES.md).")
        from . import triage as triagemod
        t = triagemod.today(conn)
        if not t["buckets"]:
            return "Nothing triaged today — is inbox-watch on?"
        lines = ["**today's inbox** — " + ", ".join(
            f"{n} {b}" for b, n in sorted(t["buckets"].items()))]
        for r in t["reply_needed"][:5]:
            lines.append(
                f"• **{r['subject'][:80] or '(no subject)'}** — "
                f"{r['from'][:60]}")
        if t["reply_needed"]:
            lines.append("_reply drafts are waiting in /activity._")
        return "\n".join(lines)

    # -- connections library ---------------------------------------------------
    def _connect(self, conn, args, channel_id=""):
        if not args:
            return ("usage: connect <app> [account:<label>]\napps: "
                    + self._connect_names(conn))
        from . import connectflows
        app = args[0]
        account = ""
        for extra in args[1:]:
            account = extra.split(":", 1)[1] \
                if extra.lower().startswith("account:") else extra
        try:
            res = connectflows.start_connect(
                conn, self.s, self.env, app, account=account)
        except connectflows.ConnectError as e:
            return str(e)
        if not res["url"]:
            return f"✅ {res['label']} connected ({res['note']})."
        if res["kind"] == "composio":
            self._watch_composio(res["provider"], res.get("meta") or {})
        # ask whose account it is unless the label already says
        # the answer writes connections.owner_kind (fail closed:
        # 'owner' until the owner picks 'pal's')
        if not account and channel_id:
            provider = res["provider"]
            self.api.post_message(
                str(channel_id),
                f"Whose account is **{res['label']}**? (writes to or "
                "sends from **your** accounts always need your "
                "approval; pal's own accounts don't)",
                components=[[
                    {"type": 2, "style": 1, "label": "my account",
                     "custom_id": f"ownkind:{provider}:owner"},
                    {"type": 2, "style": 2, "label": "pal's account",
                     "custom_id": f"ownkind:{provider}:pal"}]])
        return (f"Sign in to connect **{res['label']}** (link valid "
                f"~10 min):\n{res['url']}\n_{res['note']}_")

    def _connect_names(self, conn):
        from . import connectflows
        names = sorted(connectflows.MCP_CATALOG) + ["google",
                                                  "url:<https://...>"]
        if self.s.composio_key(self.env):
            names.append("<any app via Composio — autocomplete "
                         "searches the full catalog>")
        return ", ".join(names)

    def _connections(self, conn):
        from . import connectflows
        rows = connectflows.list_all(conn)
        if not rows:
            return ("Nothing connected yet. `connect <app>` to add one — "
                    "apps: " + self._connect_names(conn))
        out = []
        for r in rows:
            line = f"**{r['provider']}** ({r['kind']}) — {r['status']}"
            if (r.get("account") or "owner") != "owner":
                line += f" — account '{r['account']}'"
            line += " — pal's" if r.get("owner_kind") == "pal" \
                else " — yours"
            if r["account_label"]:
                line += f" — {r['account_label']}"
            if r["status"] == "expired":
                line += " (run `connect` again)"
            out.append(line)
        return "Connected apps:\n" + "\n".join(out)

    def _disconnect(self, conn, args):
        if not args:
            return "usage: disconnect <app>"
        from . import connectflows
        row = connectflows.disconnect(conn, self.env, args[0].strip())
        if row is None:
            return f"{args[0]!r} is not connected"
        return f"Disconnected **{row['provider']}** — its tokens were " \
               "deleted."

    def _handle_autocomplete(self, conn, d):
        from . import connectflows
        data = d.get("data") or {}
        name = (data.get("name") or "").lower()
        focused = ""
        for o in data.get("options") or []:
            if o.get("focused"):
                focused = str(o.get("value") or "").lower()
        choices = []
        if name == "connect":
            choices = self._connect_choices(focused)
        elif name == "disconnect":
            choices = [r["provider"] for r in
                       connectflows.list_all(conn)
                       if r["provider"].startswith(focused)]
        elif name == "jobs":
            from . import scheduler as sched
            choices = [r["name"] for r in sched.list_schedules(conn)
                       if r["kind"] == "agent_job"
                       and r["name"].startswith(focused)]
        self.api.interaction_autocomplete(
            str(d.get("id") or ""), str(d.get("token") or ""), choices)

    def _connect_choices(self, current: str):
        """`/connect app` suggestions: built-in MCP servers plus the
        full Composio toolkit catalog (live search, cached ~5 min —
        autocomplete fires per keystroke).
        -> list of (display_name, value)."""
        seen, out = set(), []

        def add(name: str, label: str = "", value: str = ""):
            value = value or name
            if value in seen:
                return
            seen.add(value)
            out.append((f"{name} — {label}" if label else name, value))

        from . import connectflows
        for slug, info in sorted(connectflows.MCP_CATALOG.items()):
            if not current or current in slug or \
                    current in info["label"].lower():
                add(slug, info["label"])
        if "url:".startswith(current):
            add("url:", "any remote MCP server", "url:https://")
        if not current or "google".startswith(current):
            add("google", "built-in Gmail + Calendar")
        if self.s.composio_key(self.env):
            toolkits = self._search_toolkits(current)
            for t in toolkits:
                add(t["slug"], t["name"])
            if not toolkits and not current:
                for n in connectflows.COMMON_COMPOSIO:
                    add(n, "Composio")
        return out[:25]

    def _search_toolkits(self, query: str):
        """Live Composio catalog search through ComposioClient's
        module cache; API trouble just yields the built-ins."""
        from . import composio
        try:
            cli = composio.ComposioClient(
                self.s, self.s.composio_key(self.env),
                timeout=self.s.mcp_timeout)
            return cli.toolkits(query, limit=20)
        except Exception:
            return []

    def _watch_composio(self, provider, meta):
        """Poll the pending Composio account until it activates, then
        write the connection and confirm in the owner DM (the browser
        callback is a nice-to-have; this poll is what makes the
        'connected' state robust)."""
        acc_id = meta.get("connected_account_id")
        if not acc_id or not self.s.composio_key(self.env):
            return

        def poll():
            from . import composio as compmod, connectflows
            cli = compmod.ComposioClient(
                self.s, self.s.composio_key(self.env),
                timeout=self.s.mcp_timeout)
            for _ in range(150):             # ~5 minutes at 2s
                if self.stop_event.wait(2.0):
                    return
                try:
                    acc = cli.account(acc_id)
                except compmod.ComposioError:
                    continue
                status = acc["status"]
                if status == "ACTIVE":
                    conn = dbmod.connect(self.db_path)
                    try:
                        if connectflows.get(conn, provider) is None:
                            connectflows.activate_composio(
                                conn, provider, meta)
                        # NB: the oauth_states row stays — the browser
                        # redirect still needs it to show "Connected";
                        # it expires on its own in ~10 min.
                    finally:
                        conn.close()
                    dm = self.api.open_dm(self.owner_id) \
                        if self.owner_id else None
                    if dm:
                        self.api.post_message(
                            dm, f"✅ **{provider}** connected via "
                                "Composio — its tools are available in "
                                "chat.")
                    return
                if status in ("FAILED", "EXPIRED", "INACTIVE",
                              "REVOKED"):
                    return
        threading.Thread(target=poll, daemon=True,
                         name="pa-composio-watch").start()

    # -- result + approval notifications --------------------------------------
    def notifier_loop(self):
        ticks = 0
        while not self.stop_event.is_set():
            try:
                self.tick_notifications()
            except Exception:
                log.exception("notifier tick failed")
            try:
                self.tick_chat_inbox()
            except Exception:
                log.exception("chat inbox tick failed")
            ticks += 1
            if ticks % 6 == 1:          # ~once a minute
                try:
                    self.tick_health()
                except Exception:
                    log.exception("health tick failed")
            self.stop_event.wait(10.0)

    def tick_health(self):
        """Deduplicated failure alerts: job failed 2x, a connection
        expired, or every TTS provider is down -> one DM each."""
        if self.api is None or not self.owner_id:
            return
        conn = dbmod.connect(self.db_path)
        try:
            from . import status as statusmod
            alerts, clears = statusmod.health_alerts(
                conn, self.s, self.env)
            for key in clears:
                conn.execute(
                    "DELETE FROM discord_events WHERE event_id=?",
                    (f"alert:{key}",))
            dm = self._owner_dm_id()
            for key, text in alerts:
                if not dm:
                    break
                if not _dedup_insert(conn, f"alert:{key}", "alert"):
                    continue
                self.api.post_message(dm, text)
        finally:
            conn.close()

    def tick_chat_inbox(self):
        """Drain scheduled prompts: 'schedule:*' rows whose payload is a
        reminder deliver directly (DM + voice note, maybe a call);
        other schedule prompts run a normal turn; agentic jobs
        (source job:*) run an UNATTENDED turn and post a summary +
        queued approvals to the owner."""
        conn = dbmod.connect(self.db_path)
        rows = []
        payloads = {}
        try:
            rows = conn.execute(
                "SELECT id, channel_id, prompt, source FROM chat_inbox"
                " WHERE status='queued' ORDER BY id LIMIT 5").fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE chat_inbox SET status='running', started_at=?"
                    " WHERE id=?", (now_iso(), r["id"]))
                src = str(r["source"] or "")
                if src.startswith("schedule:"):
                    row = conn.execute(
                        "SELECT payload_json FROM schedules WHERE name=?",
                        (src[9:],)).fetchone()
                    if row:
                        payloads[r["id"]] = row["payload_json"]
        finally:
            conn.close()
        for r in rows:
            source = str(r["source"] or "")
            if source.startswith("call:"):
                self._drain_call(r)
                continue
            if source.startswith("job:"):
                th = threading.Thread(
                    target=self._job_worker,
                    args=(str(r["channel_id"]), str(r["prompt"]),
                          source[4:], r["id"]),
                    daemon=True, name="pa-job-inbox")
            elif source == "ghcard":
                th = threading.Thread(
                    target=self._card_worker,
                    args=(str(r["prompt"]), r["id"]),
                    daemon=True, name="pa-ghcard")
            elif source == "telegram":
                th = threading.Thread(
                    target=self._tg_worker,
                    args=(str(r["channel_id"]), str(r["prompt"]),
                          r["id"]),
                    daemon=True, name="pa-telegram-inbox")
            elif source.startswith("schedule:") and \
                    _reminder_payload(payloads.get(r["id"])) is not None:
                th = threading.Thread(
                    target=self._reminder_worker,
                    args=(str(r["channel_id"]),
                          _reminder_payload(payloads.get(r["id"])),
                          str(r["prompt"]), r["id"]),
                    daemon=True, name="pa-reminder")
            else:
                th = threading.Thread(
                    target=self._chat_worker,
                    args=(str(r["channel_id"]), str(r["prompt"])),
                    kwargs={"inbox_id": r["id"]},
                    daemon=True, name="pa-chat-inbox")
            th.start()

    # -- telegram mirror (PA_TELEGRAM; docs/PARITY.md #26) --------------------
    def _tg_poll_loop(self):
        """Long-poll getUpdates; owner-chat texts become chat_inbox
        'telegram' rows answered by _tg_worker on channel 'tg:<id>'."""
        offset = 0
        while not self.stop_event.is_set():
            for u in tgmod.poll(self.env, offset):
                offset = max(offset, int(u["id"] or 0) + 1)
                text = (u["text"] or "").strip()
                if not text or \
                        str(u["chat_id"]) != self.s.telegram_chat_id:
                    continue
                conn = dbmod.connect(self.db_path)
                try:
                    conn.execute(
                        "INSERT INTO chat_inbox(channel_id, prompt,"
                        " source, status, created_at)"
                        " VALUES(?,?,'telegram','queued',?)",
                        (f"tg:{u['chat_id']}", text[:8000], now_iso()))
                finally:
                    conn.close()
            self.stop_event.wait(1.0)

    def _card_worker(self, text, inbox_id):
        """'ghcard' rows: a pre-rendered card (GitHub CI/PR events)
        posted straight to the owner DM — no agent turn, so a bad
        payload can never reach the model."""
        try:
            dm = self._owner_dm_id()
            if dm:
                self.api.post_message(dm, text[:1900])
            status, reply = "done", "posted"
        except Exception as e:
            log.info("ghcard post failed: %s", e)
            status, reply = "failed", str(e)[:200]
        conn = dbmod.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE chat_inbox SET status=?, reply=?,"
                " finished_at=? WHERE id=?",
                (status, reply, now_iso(), inbox_id))
        finally:
            conn.close()

    def _tg_worker(self, channel_id, text, inbox_id):
        """Run a chat turn for a Telegram-originated message and answer
        on Telegram (channel 'tg:<chat_id>' keeps its own history)."""
        with self._chat_lock:
            try:
                out = self._engine().run_turn(channel_id, text)
                reply = out["text"]
            except Exception as e:
                log.exception("telegram turn failed")
                reply = f"(something broke: {str(e)[:200]})"
        tgmod.send(self.s, self.env, reply,
                   chat_id=channel_id.split(":", 1)[-1])
        conn = dbmod.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE chat_inbox SET status='done', reply=?,"
                " finished_at=? WHERE id=?",
                (reply[:4000], now_iso(), inbox_id))
        finally:
            conn.close()

    def _away_greeting(self, channel_id) -> str:
        """'While you were away' preamble: a 3-line
        activity recap prepended to the owner's first DM reply after
        >= 4h of silence — PA_ACTIVITY + PA_ACTIVITY_GREETING."""
        if not (getattr(self.s, "activity_feed", False) and
                getattr(self.s, "activity_greeting", False)):
            return ""
        try:
            conn = dbmod.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT created_at FROM chat_messages WHERE"
                    " channel_id=? ORDER BY id DESC LIMIT 1",
                    (str(channel_id),)).fetchone()
                if row is None:
                    return ""
                gap = (utcnow()
                       - parse_iso(row["created_at"])).total_seconds()
                if gap < 4 * 3600:
                    return ""
                if self._owner_dm is None:
                    self._owner_dm = str(
                        self.api.open_dm(self.owner_id) or "")
                if str(channel_id) != self._owner_dm:
                    return ""
                from . import activity as actmod
                return actmod.while_away(conn, hours=gap / 3600)
            finally:
                conn.close()
        except Exception:
            return ""

    def _tg_mirror(self, channel_id, reply):
        """Owner-DM replies also go to the linked Telegram chat."""
        if not tgmod.enabled(self.s, self.env) or self.api is None:
            return
        try:
            if self._owner_dm is None:
                self._owner_dm = str(
                    self.api.open_dm(self.owner_id) or "")
            if str(channel_id) == self._owner_dm:
                tgmod.send(self.s, self.env, reply)
        except Exception:
            log.exception("telegram mirror failed")

    def _drain_call(self, row):
        """chat_inbox rows with source 'call:*' — the bridge that lets
        task-agent runs (pa.tools call_owner) ring the owner. Prompt is
        JSON {"message","urgent"}; anything else is treated as the
        message text."""
        try:
            payload = json_loads_bounded(row["prompt"])
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            payload = {"message": str(row["prompt"] or "")[:500]}
        res = self._calls.call(
            str(payload.get("message") or "")[:500],
            urgent=bool(payload.get("urgent")),
            source=str(row["source"] or "call"))
        conn = dbmod.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE chat_inbox SET status='done', reply=?,"
                " finished_at=? WHERE id=?",
                (json_dumps(res)[:4000], now_iso(), row["id"]))
        finally:
            conn.close()

    def _reminder_worker(self, channel_id, payload, prompt, inbox_id):
        """A fired reminder: post it, read it out, and when urgent also
        ring the owner."""
        text = str(payload.get("text") or
                   prompt.replace("[scheduled reminder] ", ""))[:500]
        urgent = bool(payload.get("urgent"))
        target = channel_id or self._owner_dm_id()
        status, reply = "done", "delivered"
        try:
            if target:
                self.api.post_message(target, f"⏰ {text}")
                if self._voice is not None and self._voice_on():
                    self._voice.send_voice_note(
                        target, f"Reminder: {text}")
            if urgent:
                self._calls.call(f"Reminder. {text}", urgent=True,
                                 source="reminder")
        except Exception as e:
            log.exception("reminder delivery failed")
            status, reply = "failed", f"delivery failed: {str(e)[:200]}"
        conn = dbmod.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE chat_inbox SET status=?, reply=?, finished_at=?"
                " WHERE id=?", (status, reply, now_iso(), inbox_id))
        finally:
            conn.close()

    def _job_worker(self, channel_id, prompt, job_name, inbox_id):
        """One scheduled agentic-job run: a full unattended turn, then a
        short summary + Approve/Reject buttons for queued actions to the
        owner's Discord channel (or DM when no channel is routed).

        Names registered in pa/sysjobs.py (inbox-watch,
        morning-briefing) run their code handler instead of a ReAct
        turn."""
        from . import sysjobs
        handler = sysjobs.handler_for(job_name)
        if handler is not None:
            self._sysjob_worker(job_name, handler, inbox_id)
            return
        target = str(channel_id or "") or self._owner_dm_id()
        with self._chat_lock:
            reply, pending, status = "(job broke — see logs)", [], "done"
            try:
                out = self._engine().run_turn(
                    channel_id or target or "job", prompt,
                    unattended=True)
                reply = out["text"]
                pending = out.get("pending") or []
            except Exception as e:
                log.exception("job %s failed", job_name)
                status = "failed"
                reply = f"(job {job_name} broke: {str(e)[:200]})"
            if target:
                try:
                    self.api.post_message(
                        target,
                        f"⚙️ job **{job_name}**:\n{_clip(reply)}")
                    self._post_job_pending(target, pending)
                    if self._voice_on():
                        self._voice.send_voice_note(
                            target, f"job {job_name}: {reply[:600]}")
                except Exception:
                    log.exception("job %s result post failed", job_name)
            else:
                log.warning("job %s done but nowhere to post", job_name)
            conn = dbmod.connect(self.db_path)
            try:
                conn.execute(
                    "UPDATE chat_inbox SET status=?, reply=?,"
                    " finished_at=? WHERE id=?",
                    (status, reply[:4000], now_iso(), inbox_id))
            finally:
                conn.close()

    def _sysjob_worker(self, job_name, handler, inbox_id):
        """Code-run system job: read the schedule payload, run the
        handler, record done/failed on the inbox row. The handler posts
        its own Discord output."""
        from . import scheduler as sch
        conn = dbmod.connect(self.db_path)
        payload = {}
        try:
            row = sch.get_schedule(conn, job_name)
            if row:
                payload = json_loads_bounded(row["payload_json"]) or {}
        except Exception:
            pass
        status, reply = "done", ""
        try:
            reply = handler(self, payload) or ""
        except Exception as e:
            log.exception("system job %s failed", job_name)
            status, reply = "failed", str(e)[:300]
            try:
                incidentsmod.record(
                    conn, "sysjob", "exception",
                    summary=f"{job_name}: {str(e)[:200]}",
                    detail=f"{type(e).__name__}: {e}", tool=job_name)
            except Exception:
                pass
            try:
                dm = self._owner_dm_id()
                if dm:
                    self.api.post_message(
                        dm,
                        f"⚙️ job **{job_name}** failed: {str(e)[:300]}")
            except Exception:
                pass
        conn.execute(
            "UPDATE chat_inbox SET status=?, reply=?, finished_at=?"
            " WHERE id=?",
            (status, reply[:4000], now_iso(), inbox_id))
        conn.close()

    def _owner_dm_id(self):
        if not self.owner_id or self.api is None:
            return ""
        try:
            return str(self.api.open_dm(self.owner_id) or "")
        except Exception:
            return ""

    def _post_job_pending(self, channel_id, pending):
        """Unattended-run writes that were held: one DM-style message
        per action with Approve/Reject buttons (job_action rows never
        resume a turn — approving EXECUTES the tool once)."""
        for p in pending:
            from . import review
            rl = review.label(p["tool"], p.get("args") or {})
            body = (f"Job held `{p['tool']}` {rl['text']}:\n"
                    f"```json\n{_clip(json_dumps(p['args']))[:800]}\n```")
            self.api.post_message(channel_id, body, components=[[
                {"type": 2, "style": 3, "label": "Approve",
                 "custom_id": f"chatappr:{p['id']}:y"},
                {"type": 2, "style": 4, "label": "Reject",
                 "custom_id": f"chatappr:{p['id']}:n"}]])

    def tick_notifications(self):
        """Deliver: finished-task results to routed channels; pending
        approvals to the owner DM. Each is sent at most once."""
        conn = dbmod.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT t.*, a.name aname, a.id aid FROM tasks t"
                " JOIN assistants a ON a.id=t.assistant_id WHERE"
                " t.state IN ('succeeded','failed')"
                " AND t.discord_notified_at IS NULL"
                " AND t.discord_notify_attempts < 5"
                " ORDER BY t.id LIMIT 10").fetchall()
            for t in rows:
                conn.execute(
                    "UPDATE tasks SET discord_notify_attempts="
                    " discord_notify_attempts+1 WHERE id=?", (t["id"],))
                channel = self.s.discord_channel_for(
                    {"id": t["aid"], "name": t["aname"]})
                if not channel and self.owner_id:
                    # no routed channel: fall back to the owner DM rather
                    # than silently dropping the result
                    channel = self.api.open_dm(self.owner_id)
                if not channel:
                    continue  # unroutable this tick; attempts cap applies
                if not _dedup_insert(conn, f"task-notify:{t['id']}",
                                     "notify"):
                    continue
                mark = "succeeded" if t["state"] == "succeeded" else "FAILED"
                text = f"[{mark}] task #{t['id']} {t['title']}"
                summ = ""
                if t["result_json"]:
                    try:
                        res = json_loads_bounded(t["result_json"])
                        summ = res.get("summary") or res.get("text") or ""
                    except Exception:
                        summ = ""
                    if summ:
                        text += "\n" + summ[:1400]
                if t["state"] == "failed":
                    text += (f"\nerror: {t['error_kind']}"
                             f" {t['error_detail'] or ''}")
                if getattr(self.s, "research", False):
                    try:
                        from . import research as resmod
                        text += resmod.finish(self, conn, t)
                    except Exception:
                        log.exception("research finish failed "
                                      "task %s", t["id"])
                if self.api.post_message(channel, text):
                    conn.execute(
                        "UPDATE tasks SET discord_notified_at=?"
                        " WHERE id=?", (now_iso(), t["id"]))
                    if summ and t["state"] == "succeeded" \
                            and self._voice_on():
                        self._voice.send_voice_note(
                            channel, f"task {t['title']}: {summ[:600]}")
            # pending approvals -> owner DM (once each)
            if self.owner_id:
                for a in approvals.list_pending(conn):
                    if a["kind"] in ("chat_action", "job_action"):
                        continue  # buttons already posted (turn / job)
                    key = f"approval-notify:{a['id']}"
                    if not _dedup_insert(conn, key, "notify"):
                        continue
                    dm = self.api.open_dm(self.owner_id)
                    if not dm:
                        continue
                    self.api.post_message(
                        dm, f"Approval needed: [{a['kind']}] "
                            f"{_clip(a['payload_json'])[:400]}\n"
                            f"approve with: approve {a['id'][:8]}")
            # Composio free-tier warning — once per month at >=80%
            if self.owner_id and self.s.composio_key(self.env):
                from . import composio as compmod
                count = compmod.usage_count(conn)
                if compmod.over_warn_threshold(self.s, count):
                    key = f"composio-warn:{compmod._month()}"
                    if _dedup_insert(conn, key, "notify"):
                        dm = self.api.open_dm(self.owner_id)
                        if dm:
                            self.api.post_message(
                                dm, f"⚠️ Composio usage is {count} of "
                                f"~{self.s.composio_monthly_limit} free "
                                "managed tool calls this month (>=80%). "
                                "Connected apps keep working until the "
                                "cap; see docs/CONNECTIONS.md.")
        finally:
            conn.close()

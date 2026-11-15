"""Live turn progress: one editable status line + message reactions.

Replaces the passive typing indicator when PA_CHAT_PROGRESS is on
(default): the owner's message gets an eyes reaction, ONE status message
is posted ("on it — …") and edited as tools run, then everything is
torn down when the reply lands (reaction flips to ✅/⚠️, the status
line is deleted so the channel isn't littered).

Only tool NAMES ever reach the status line — never arguments, which can
hold email bodies or URLs with tokens. Edits are throttled so a burst
of quick tools can't spam the REST API. Typing keeps firing in the
background as a fallback signal.
"""

import threading
import time

_EYES = "👀"
_DONE = "✅"
_WARN = "⚠️"

# tool name -> status phrase; unknown tools show their bare name
_VERBS = {
    "web_search": "searching the web…",
    "fetch_url": "fetching the page…",
    "read_url": "reading the page…",
    "email_list": "checking mail…",
    "email_read": "reading an email…",
    "calendar_list": "checking the calendar…",
    "calendar_create": "making an event…",
    "vault_search": "searching notes…",
    "vault_read": "reading a note…",
    "vault_write": "writing a note…",
    "vault_append": "updating a note…",
    "memory_search": "checking memory…",
    "memory_save": "saving to memory…",
    "app_tools": "listing app tools…",
    "shell": "running a command…",
    "mac": "running on the Mac…",
    "remind_me": "setting a reminder…",
    "job_add": "setting up the job…",
    "send_voice_note": "recording a voice note…",
    "call_owner": "ringing you…",
    "browse_goto": "opening the page…",
    "browse_tabs": "checking tabs…",
    "browse_snapshot": "looking at the page…",
    "browse_click": "clicking…",
    "browse_type": "typing…",
    "browse_press": "pressing a key…",
    "browse_screenshot": "taking a screenshot…",
    "browse_extract": "reading the page…",
}

EDIT_MIN_SECONDS = 1.2          # bound status-line PATCH traffic
TYPING_EVERY = 9.0              # Discord typing lasts ~10s


class LiveProgress:
    """Drives one turn's status line + reactions on a DiscordAPI."""

    def __init__(self, api, channel_id, message_id=None):
        self.api = api
        self.channel_id = channel_id
        self.message_id = str(message_id) if message_id else None
        self.status_id = None
        self._last_edit = None      # first tool note always edits
        self._stop = threading.Event()
        self._thread = None

    def start(self, ack: str = "on it — working…"):
        if self.api is None:
            return
        try:
            if self.message_id:
                self.api.add_reaction(self.channel_id, self.message_id,
                                      _EYES)
            msg = self.api.create_message(self.channel_id, ack)
            if isinstance(msg, dict) and msg.get("id"):
                self.status_id = str(msg["id"])
        except Exception:
            self.status_id = None
        try:
            self.api.typing(self.channel_id)
        except Exception:
            pass
        self._thread = threading.Thread(
            target=self._typing_loop, daemon=True, name="pa-typing")
        self._thread.start()

    def _typing_loop(self):
        while not self._stop.wait(TYPING_EVERY):
            try:
                self.api.typing(self.channel_id)
            except Exception:
                return

    def note_tool(self, name):
        """chattools.run calls this as each tool starts."""
        if self.api is None or self.status_id is None:
            return
        now = time.monotonic()
        if self._last_edit is not None and \
                now - self._last_edit < EDIT_MIN_SECONDS:
            return
        self._last_edit = now
        # app-tool names carry the account label (gmail@pal.X); never
        # show it — and args never reach this line at all
        disp = str(name).split("@", 1)[0].rsplit(".", 1)[-1][:40]
        verb = _VERBS.get(name) or f"{disp}…"
        self.api.edit_message(self.channel_id, self.status_id,
                              f"on it — {verb}")

    def finish(self, ok: bool = True):
        """Turn done: flip the reaction, remove the status line."""
        self._stop.set()
        if self.api is None:
            return
        if self.message_id:
            self.api.add_reaction(self.channel_id, self.message_id,
                                  _DONE if ok else _WARN)
            self.api.delete_reaction(self.channel_id, self.message_id,
                                     _EYES)
        if self.status_id and not self.api.delete_message(
                self.channel_id, self.status_id):
            self.api.edit_message(self.channel_id, self.status_id,
                                  "(done)")
        self.status_id = None

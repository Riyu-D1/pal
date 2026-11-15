"""V5 streaming call brain (pa/callbrain.py).

The pipeline fallback's think path: instead of running the full chat
agent (64 tool schemas, ~17k prompt chars, non-streaming ReAct), a
call turn goes to the router's `call` tier — a tiny prompt that
streams. CallBrain turns the delta stream into a `reply_iter` the
Conversation speaker drains clause-by-clause: TTS for the first
clause starts while the model is still generating.

Escape hatch: the model may emit ``[[ASK_PAL: <question>]]`` when it
needs tools or account data. Everything before the marker is spoken,
a one-line holding phrase plays immediately, then the full engine
answers in the background (ask_seconds budget). On timeout pal says
the DM line and the answer is DMed when it lands.
"""

import logging
import re
import threading
from collections import deque

from .util import estimate_tokens

log = logging.getLogger("pa.callbrain")

_MARK = re.compile(r"\[\[\s*ASK_PAL\s*:\s*", re.I)
_MARK_FULL = "[[ASK_PAL:"

PERSONA = (
    "You are pal, the owner's personal assistant, on a live phone "
    "call. Reply in short spoken sentences — British English, warm, "
    "no lists, no markdown, never more than two sentences. If you "
    "need account data or a tool, output [[ASK_PAL: <question>]] on "
    "its own and nothing else. If the owner says goodbye or thanks, "
    "answer briefly and warmly.")

MAX_PROMPT_TOKENS = 600
HISTORY_TURNS = 6


def _suffix_prefix(s, marker):
    """Length of the longest suffix of `s` that is a strict prefix of
    `marker` — the part to hold back because it might still grow into
    a marker."""
    for n in range(min(len(s), len(marker) - 1), 0, -1):
        if marker.startswith(s[-n:]):
            return n
    return 0


def _extract_ask(buf):
    """(safe_text, rest, question). safe_text may be spoken now; rest
    is the unprocessed tail. question is not None when a complete
    [[ASK_PAL: ...]] marker was consumed."""
    m = _MARK.search(buf)
    if m is None:
        tail = _suffix_prefix(buf, _MARK_FULL)
        if tail:
            return buf[:-tail], buf[-tail:], None
        return buf, "", None
    safe = buf[:m.start()]
    end = buf.find("]]", m.end())
    if end == -1:
        return safe, buf[m.start():], None
    return safe, buf[end + 2:], buf[m.end():end].strip()


def _cut_clause(buf, limit):
    """(clause, rest): the first speakable clause in buf — a sentence
    end (>= 8 chars) or a comma/semicolon/dash after >= 30 chars, all
    within `limit`. (None, buf) when nothing is ripe yet."""
    window = buf[:limit + 1]
    for i, ch in enumerate(window):
        if ch in ".!?" and i >= 8:
            return buf[:i + 1].strip(), buf[i + 1:]
    for i, ch in enumerate(window):
        if ch in ",;—:" and i >= 30:
            return buf[:i + 1].strip(), buf[i + 1:]
    if len(buf) > limit + 40:
        i = buf.rfind(" ", 30, limit)
        if i > 0:
            return buf[:i].strip(), buf[i + 1:]
    return None, buf


class CallBrain:
    """think(heard) -> {"reply_iter": iter(str), "done": bool} for
    callconv.Conversation — streamed via the router `call` tier.

    stream:     messages -> iterator[str] (or raises ProviderError)
    ask_pal:    question -> reply text for [[ASK_PAL:]] escapes
    on_hold:    called with the question when an ASK_PAL fires
    on_dm:      called (question, answer|None) when a slow ask_pal
                answer must go to the owner's DM instead
    facts:      pinned owner-facts block for the system prompt
    """

    def __init__(self, stream, ask_pal=None, persona=PERSONA,
                 facts="", hold="One sec, let me check that.",
                 dm_line="I'll DM you that — it's taking a moment.",
                 sorry="Sorry, I lost my train of thought.",
                 ask_seconds=8.0, on_hold=None, on_dm=None,
                 is_wrapup=None,
                 farewell="Right — leaving you to it. Ping me if "
                          "you need me.",
                 history=()):
        self._stream = stream
        self._ask_pal = ask_pal
        self._wrapup = is_wrapup
        self.farewell = farewell
        self.persona = persona
        self.facts = facts
        self.hold = hold
        self.dm_line = dm_line
        self.sorry = sorry
        self.ask_seconds = ask_seconds
        self.on_hold = on_hold
        self.on_dm = on_dm
        self.history = deque(history, maxlen=HISTORY_TURNS)

    # -- the Conversation think seam -----------------------------------
    def think(self, heard):
        if self._wrapup is not None and self._wrapup(heard):
            return {"reply": self.farewell, "done": True,
                    "heard": heard}
        return {"reply_iter": self._gen(heard), "heard": heard,
                "done": False}

    # -- internals ------------------------------------------------------
    def _msgs(self, heard):
        sys = self.persona + (("\n" + self.facts) if self.facts else "")
        msgs = [{"role": "system", "content": sys}]
        pairs = list(self.history)
        while pairs and estimate_tokens(
                str(msgs) + str(pairs) + heard) > MAX_PROMPT_TOKENS:
            pairs.pop(0)
        for h, s in pairs:
            msgs.append({"role": "user", "content": h})
            msgs.append({"role": "assistant", "content": s})
        msgs.append({"role": "user", "content": heard})
        return msgs

    def _gen(self, heard):
        msgs = self._msgs(heard)
        said = ""
        buf = ""
        try:
            for d in self._stream(msgs):
                buf += d
                safe, buf, q = _extract_ask(buf)
                if safe:
                    said += safe
                    yield safe
                if q is not None:
                    for piece in self._ask(q):
                        said += piece
                        yield piece
                    break
            tail = buf.strip()
            if tail:
                said += tail
                yield tail
        except Exception as e:
            log.info("call brain stream failed (%s) — full-engine "
                     "fallback", type(e).__name__)
            fb = self._fallback(heard)
            if fb:
                said += fb
                yield fb
        self.history.append((heard, said.strip()[:300]))

    def _ask(self, question):
        """Holding line, then the full-engine answer or the DM line."""
        if self.on_hold:
            try:
                self.on_hold(question)
            except Exception:
                pass
        if self.hold:
            yield self.hold
        ans = self._answer(question)
        if ans:
            yield ans
        else:
            yield self.dm_line

    def _answer(self, question):
        """Run ask_pal in a daemon thread under the ask_seconds budget;
        a slow answer is handed to on_dm instead of blocking speech."""
        if self._ask_pal is None:
            return None
        box = {}

        def run():
            try:
                box["r"] = self._ask_pal(question)
            except Exception:
                box["r"] = None
        t = threading.Thread(target=run, daemon=True,
                             name="pa-call-askpal")
        t.start()
        t.join(self.ask_seconds)
        if "r" in box:
            return box["r"]
        if self.on_dm is not None:
            def wait():
                t.join(120)
                try:
                    self.on_dm(question, box.get("r"))
                except Exception:
                    pass
            threading.Thread(target=wait, daemon=True,
                             name="pa-call-askpal-dm").start()
        return None

    def _fallback(self, heard):
        if self._ask_pal is not None:
            try:
                return self._ask_pal(heard)
            except Exception:
                pass
        return self.sorry

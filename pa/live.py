"""Gemini Live call engine (PA_CALL_ENGINE=auto|live|pipeline).

One BidiGenerateContent WebSocket session replaces the whole
STT→LLM→TTS pipeline: Discord audio goes in as 16 kHz mono PCM,
model audio comes out as 24 kHz PCM and lands straight in the
CallAudioSource — the model speaks itself, endpointing and
interruption are server-side, and ``serverContent.interrupted``
maps to the same queue-clear barge-in the pipeline uses.

Everything is injected for tests — ``ws_factory`` returns a fake
socket, ``ask_pal`` / ``fast_tools`` are plain callables — so no
network ever runs in CI.

Fallback contract (PA_CALL_ENGINE=auto): the caller retries with the
pipeline when run() returns {"reason": "live_unavailable"} — raised
for setup errors, close code 1011 and HTTP 429.
"""

import asyncio
import base64
import json
import logging
import time

log = logging.getLogger("pa.live")

WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
          "google.ai.generativelanguage.v1beta.GenerativeService."
          "BidiGenerateContent")
DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_VOICE = "Kore"
_GOAWAY_CODES = {1011}                  # server restart / quota reset

SYSTEM = (
    "You are pal, the owner's personal assistant, on a live phone "
    "call. Speak in short natural sentences — one or two at a time, "
    "British English, no lists, no markdown, no emoji. If you need "
    "data (mail, calendar, files, the web, or anything slow), call "
    "ask_pal and say a short holding phrase first. Never read out "
    "secrets. Keep answers under ~15 seconds of speech unless asked "
    "for more.")

ASK_PAL_TOOL = {
    "name": "ask_pal",
    "description": "Ask pal's main agent — use for anything that "
                   "needs data, tools, files, mail, calendar, the "
                   "web or a long answer.",
    "parameters": {"type": "object",
                   "properties": {"question": {"type": "string"}},
                   "required": ["question"]}}
FAST_TOOL_DECLS = [
    {"name": "now", "description": "Current date, time and day of "
                                   "week (owner's timezone).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "calendar_today", "description": "The owner's remaining "
                                             "calendar events today.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "unread_summary", "description": "Count + senders of the "
                                             "owner's unread inbox.",
     "parameters": {"type": "object", "properties": {}}}]


# ---------------------------------------------------------------------------
# audio conversion (stdlib audioop — same helpers as callconv)

def _audioop():
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import audioop
        return audioop


def in_48k_stereo_to_16k_mono(frame: bytes) -> bytes:
    """One Discord frame (48 kHz stereo s16le) -> 16 kHz mono s16le
    for realtimeInput."""
    a = _audioop()
    mono = a.tomono(frame, 2, 0.5, 0.5)
    out, _ = a.ratecv(mono, 2, 1, 48000, 16000, None)
    return out


def out_24k_mono_to_48k_stereo(data: bytes) -> bytes:
    """Model audio (24 kHz mono s16le) -> 48 kHz stereo s16le frames
    for CallAudioSource."""
    a = _audioop()
    up, _ = a.ratecv(data, 2, 1, 24000, 48000, None)
    return a.tostereo(up, 2, 1.0, 1.0)


# ---------------------------------------------------------------------------
# session

class LiveUnavailable(Exception):
    """Setup/connect failure the caller should fall back from
    (auto mode): connect error, close code 1011, HTTP 429."""


def engine_order(settings, env=None) -> list:
    """PA_CALL_ENGINE: auto -> [live, pipeline]; live -> [live];
    pipeline -> [pipeline]. Live is dropped from the order entirely
    when GEMINI_API_KEY is absent."""
    want = (getattr(settings, "call_engine", "auto") or "auto").lower()
    order = {"auto": ["live", "pipeline"], "live": ["live"],
             "pipeline": ["pipeline"]}.get(want, ["live", "pipeline"])
    if "live" in order and not (
            (env or {}).get("GEMINI_API_KEY") or "").strip():
        order.remove("live")
    return order


def _declarations(fast_tools):
    """Setup's functionDeclarations: ask_pal plus whichever fast
    read-only tools the caller supplied."""
    names = set(fast_tools or ())
    return [ASK_PAL_TOOL] + \
        [d for d in FAST_TOOL_DECLS if d["name"] in names]


class LiveSession:
    """One BidiGenerateContent WebSocket: connect, send setup, send/
    receive raw JSON messages. `ws_factory` (tests) replaces aiohttp."""

    def __init__(self, env, *, model, voice, system, tools,
                 ws_factory=None):
        self.env = env or {}
        self.model = model
        self.voice = voice
        self.system = system
        self.tools = tools
        self._factory = ws_factory
        self.ws = None
        self.sent = []                  # outbound JSON (for tests)

    async def connect(self, resume_handle=None, timeout=15):
        if self._factory is not None:
            self.ws = await self._factory()
        else:
            import aiohttp
            key = (self.env.get("GEMINI_API_KEY") or "").strip()
            if not key:
                raise LiveUnavailable("no GEMINI_API_KEY")
            self._http = aiohttp.ClientSession()
            try:
                # the key rides in the header — NEVER log the URL
                self.ws = await self._http.ws_connect(
                    WS_URL, headers={"x-goog-api-key": key},
                    timeout=timeout)
            except Exception as e:
                status = getattr(e, "status", None)
                await self._http.close()
                raise LiveUnavailable(
                    f"live ws_connect failed (status {status})") from e
        msg = {"setup": {
            "model": f"models/{self.model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": self.voice}}}},
            "systemInstruction": {"parts": [{"text": self.system}]},
            "sessionResumption": (
                {"handle": resume_handle} if resume_handle else {}),
            "realtimeInputConfig": {
                "automaticActivityDetection": {
                    "disabled": False}},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "tools": [{"functionDeclarations":
                       _declarations(self.tools)}]}}
        await self.send(msg)
        return self.ws

    async def send(self, obj):
        self.sent.append(obj)
        if self.ws is not None:
            await self.ws.send_str(json.dumps(obj))

    async def receive(self):
        """-> parsed dict or {'_closed': code}."""
        msg = await self.ws.receive()
        data = getattr(msg, "data", msg)
        mtype = getattr(msg, "type", None)
        name = getattr(mtype, "name", "") if mtype is not None else ""
        if name in ("CLOSED", "CLOSE", "ERROR") or \
                isinstance(data, int) or data is None:
            return {"_closed": data if isinstance(data, int) else -1}
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", "replace")
        try:
            return json.loads(data)
        except Exception:
            return {"_raw": data}

    async def close(self):
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:
            pass
        http = getattr(self, "_http", None)
        if http is not None:
            try:
                await http.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# engine

class LiveEngine:
    """Drop-in replacement for callconv.Conversation: the pump calls
    feed_frame(), run() drives the session until stop/deadline.

    Seams: source (CallAudioSource — play + clear), ask_pal(question),
    fast_tools {name: callable}, sleep/now injected for fake clocks.
    """

    def __init__(self, settings, env, *, source, ask_pal=None,
                 fast_tools=None, session_factory=None,
                 ask_timeout=6.0, sleep=None, now=None):
        self.ask_timeout = ask_timeout
        self.s = settings
        self.env = env or {}
        self.source = source
        self.ask_pal = ask_pal
        self.fast_tools = fast_tools or {}
        self._sleep = sleep or asyncio.sleep
        self._now = now or time.monotonic
        self._factory = session_factory
        self._q = None                  # outbound msg queue
        self._inbuf = bytearray()       # pending mic audio (48k frames)
        self._inlock = asyncio.Lock()
        self._stop = False
        self._reason = "ended"
        self._resume = None
        self._heard, self._said = [], []
        self._tasks = []
        self._ask_running = 0
        self._close_code = None
        self._opening_sent = False
        self.turns = 0
        self.interrupted = 0
        self.live_seconds = 0.0

    # -- pump seam ------------------------------------------------------
    def feed_frame(self, frame):
        if not self._stop and frame:
            self._inbuf += frame

    def request_stop(self, reason="ended"):
        self._reason = reason
        self._stop = True

    def note_playback_started(self):
        pass                      # server-side VAD: nothing to suppress

    # -- outbound -------------------------------------------------------
    async def _mic_loop(self, sess):
        """Batch ~100 ms of mic frames -> realtimeInput.audio b64."""
        while not self._stop:
            await self._sleep(0.1)
            if not self._inbuf:
                continue
            async with self._inlock:
                raw = bytes(self._inbuf)
                self._inbuf.clear()
            pcm16 = in_48k_stereo_to_16k_mono(raw)
            await sess.send({"realtimeInput": {"audio": {
                "data": base64.b64encode(pcm16).decode(),
                "mimeType": "audio/pcm;rate=16000"}}})

    async def _tool_call(self, sess, call):
        name = call.get("name") or ""
        args = call.get("args") or {}
        result, still = "", False
        if name == "ask_pal" and self.ask_pal is not None:
            self._ask_running += 1
            q = args.get("question") or ""
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self.ask_pal, q),
                    timeout=self.ask_timeout)
            except asyncio.TimeoutError:
                still = True
                # keep working; the late answer is injected as a text
                # turn (or the run ends first)
                async def _later():
                    try:
                        out = await asyncio.to_thread(self.ask_pal, q)
                        if out and not self._stop:
                            await sess.send({"clientContent": {
                                "turns": [{"role": "user", "parts": [{
                                    "text": f"[ask_pal late answer] {out}"}]}],
                                "turnComplete": True}})
                    except Exception:
                        pass
                    finally:
                        self._ask_running -= 1
                self._tasks.append(asyncio.ensure_future(_later()))
                result = "still working — tell the owner you're on it"
            except Exception as e:
                self._ask_running -= 1
                result = f"tool failed: {e}"
        elif name in self.fast_tools:
            try:
                result = str(await asyncio.to_thread(
                    self.fast_tools[name]) or "")
            except Exception as e:
                result = f"tool failed: {e}"
        else:
            result = f"unknown tool {name!r}"
        await sess.send({"toolResponse": {"functionResponses": [{
            "id": call.get("id") or "", "name": name,
            "response": {"result": result}}]}})

    async def _handle(self, sess, msg):
        """One inbound message. Returns 'reconnect'/'close'/None."""
        if "_closed" in msg:
            self._close_code = msg["_closed"]
            return "closed"
        if "goAway" in msg:
            log.info("live: goAway received — will reconnect soon")
            return "goaway"
        sr = msg.get("sessionResumption") or {}
        if sr.get("newHandle"):
            self._resume = sr["newHandle"]
        if sr.get("handle"):
            self._resume = sr["handle"]
        sc = msg.get("serverContent") or {}
        if sc.get("interrupted"):
            self.interrupted += 1
            try:
                self.source.clear()
            except Exception:
                pass
            log.info("live: interrupted — playback queue cleared")
        turn = sc.get("modelTurn") or {}
        for part in turn.get("parts") or []:
            data = (part.get("inlineData") or {}).get("data")
            if data:
                pcm = out_24k_mono_to_48k_stereo(
                    base64.b64decode(data))
                try:
                    self.source.feed(pcm)
                except Exception:
                    pass
        it = sc.get("inputTranscription") or {}
        if it.get("text"):
            self._heard.append(it["text"])
        ot = sc.get("outputTranscription") or {}
        if ot.get("text"):
            self._said.append(ot["text"])
        if sc.get("turnComplete"):
            self.turns += 1
        tc = msg.get("toolCall") or {}
        for call in tc.get("functionCalls") or []:
            await self._tool_call(sess, call)
        return None

    async def _session(self, deadline):
        """Run one socket until it closes, the deadline hits or a
        goAway says reconnect."""
        model = getattr(self.s, "live_model", DEFAULT_MODEL) or \
            DEFAULT_MODEL
        voice = getattr(self.s, "live_voice", DEFAULT_VOICE) or \
            DEFAULT_VOICE
        sess = (self._factory or LiveSession)(
            self.env, model=model, voice=voice,
            system=getattr(self.s, "live_system", SYSTEM) or SYSTEM,
            tools=self.fast_tools or None)
        try:
            await sess.connect(resume_handle=self._resume)
        except LiveUnavailable:
            raise
        except Exception as e:
            raise LiveUnavailable(f"live setup failed: {e}") from e
        log.info("live: session up (model %s, voice %s)", model, voice)
        if self._opening and not self._opening_sent:
            self._opening_sent = True
            await sess.send({"clientContent": {"turns": [{
                "role": "user", "parts": [{"text":
                    self._opening + " — greet the owner now, "
                    "one short line."}]}],
                "turnComplete": True}})
        mic = asyncio.ensure_future(self._mic_loop(sess))
        try:
            while not self._stop:
                left = deadline - self._now()
                if left <= 0:
                    self._reason = "timeout"
                    break
                try:
                    msg = await asyncio.wait_for(
                        sess.receive(), timeout=min(30, max(1, left)))
                except asyncio.TimeoutError:
                    continue                       # idle: keep pumping
                act = await self._handle(sess, msg)
                if act == "goaway":
                    return "reconnect"
                if act == "closed":
                    return "closed"
            return "stopped"
        finally:
            mic.cancel()
            await sess.close()

    async def run(self, deadline, opening=None):
        """-> {"reason","turns","heard","said","interrupted",
        "engine":"live"} or {"reason":"live_unavailable"} for the
        auto fallback. `opening` is sent once as a clientContent
        prompt so the model greets first."""
        self._opening = opening or ""
        self._q = asyncio.Queue()
        t0 = self._now()
        last_error = None
        reconnects = 0
        while not self._stop:
            try:
                outcome = await self._session(deadline)
            except LiveUnavailable as e:
                last_error = str(e)
                break
            except Exception as e:
                log.warning("live: session error: %r", e)
                last_error = repr(e)
                break
            if outcome in ("reconnect", "closed") and \
                    self._now() < deadline and reconnects < 3 and \
                    (outcome == "reconnect" or self._resume):
                reconnects += 1
                log.info("live: resuming (handle %s, reason %s)",
                         (self._resume or "")[:12], outcome)
                continue
            if outcome == "closed" and \
                    self._close_code in _GOAWAY_CODES:
                last_error = f"live closed early ({self._close_code})"
            break
        self.live_seconds = self._now() - t0
        if last_error is not None and not self._heard and \
                not self._said and self.turns == 0:
            return {"reason": "live_unavailable", "detail": last_error,
                    "turns": 0, "heard": [], "said": []}
        return {"reason": self._reason, "turns": self.turns,
                "heard": self._heard, "said": self._said,
                "interrupted": self.interrupted,
                "live_seconds": self.live_seconds,
                "engine": "live",
                "error": last_error}


# ---------------------------------------------------------------------------
# live_min budget gate (PA_LIVE_BUDGET)

class LiveBudget:
    """Reserve `live_min` under the 'call' scope before a Live socket
    opens, then settle the real ceil(live_seconds/60) on teardown.

    - reserve() False -> the hold would exceed the call-scope cap;
      the caller maps that to live_unavailable (auto) or a short
      spoken "minutes used up" (live-only) and never opens a socket.
    - settle() charges ceil(seconds/60), minimum 1 once a session
      actually ran; a run that never connected (live_unavailable
      before setup finished) charges 0.
    - storage failures fail OPEN: a broken ledger must not mute a
      call — the hold is dropped and the session proceeds.

    The Gemini key never passes through here — nothing in this class
    touches or logs it.
    """

    def __init__(self, db_path, settings, env):
        self.db_path = db_path
        self.s = settings
        self.env = env or {}
        self.conn = None
        self.hold = 0

    def reserve(self, remaining_seconds: float) -> bool:
        from . import budget, db as dbmod
        try:
            if not self.db_path:
                return True
            self.conn = dbmod.connect(self.db_path)
            dbmod.migrate(self.conn)
            hold = int(-(-max(1.0, float(remaining_seconds)) // 60))
            self.hold = max(1, min(120, hold))
            if budget.would_exceed(self.conn, "call", "live_min",
                                   self.hold, s=self.s, env=self.env):
                self._close()
                return False
            budget.reserve(self.conn, "call", "live_min", self.hold)
            return True
        except Exception:
            log.exception("live budget reserve failed")
            self._close()
            return True

    def settle(self, live_seconds: float, session_started: bool):
        if self.conn is None:
            return
        from . import budget
        try:
            used = 0
            if session_started and live_seconds > 0:
                used = int(max(1, -(-float(live_seconds) // 60)))
            if used:
                budget.settle(self.conn, "call", "live_min", used)
            left = self.hold - used
            if left > 0:
                budget.release(self.conn, "call", "live_min", left)
        except Exception:
            log.exception("live budget settle failed")
        finally:
            self._close()

    def _close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        self.hold = 0

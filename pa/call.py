"""Discord 'call': pal rings the owner in a voice channel and talks.

Bots can't ring a DM, so a call is: ping the owner (voice-channel
mention + DM), join a private voice channel, speak the alert with TTS,
then optionally listen and converse until the owner leaves, says bye,
or the call times out. A short summary lands in the owner DM.

The voice itself is deliberately NOT implemented here. Since March
2026 Discord requires DAVE end-to-end encryption on every voice
connection — the voice gateway, UDP socket, Opus codec and MLS key
schedule are all handled by discord.py >= 2.7 (voice extra) + the
`davey` package. Receiving audio additionally needs
discord-ext-voice-recv plus a small shim that routes inbound packets
through discord.py's DAVE session for decryption (community approach;
upstream support is still pending). When receive is unavailable the
call still speaks — the owner replies with a voice note instead.

Guard rails live here, not in the voice lib: quiet hours (only urgent
calls pass), a per-hour call cap, and one call at a time.
"""

import collections
import datetime
import io
import logging
import struct
import threading
import time

from . import callconv
from . import live as livemod
from . import voice as voicemod

log = logging.getLogger("pa.call")

_DEAF_DM = ("📞 pal is in the call but can't hear you — inbound voice "
            "decryption (DAVE) is failing. Reply in DM or send a voice "
            "note instead. (Host-side check: `python -m pa call-diag`)")

# voice-recv's sink base; plain object when the extra isn't installed.
try:
    from discord.ext.voice_recv import AudioSink as _AudioSink
except Exception:
    class _AudioSink:
        def __init__(self, *a, **kw):
            pass


def in_quiet_hours(quiet: str, tz_name: str,
                   now_utc: datetime.datetime = None) -> bool:
    """'HH-HH' local window, e.g. '23-07' covers 23:00-06:59."""
    try:
        a, b = (int(x) for x in (quiet or "").split("-", 1))
        if not (0 <= a <= 23 and 0 <= b <= 23) or a == b:
            return False
    except (ValueError, TypeError):
        return False
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name or "Europe/London")
    except Exception:
        return False
    hour = (now_utc or datetime.datetime.now(
        datetime.timezone.utc)).astimezone(tz).hour
    if a > b:
        return hour >= a or hour < b      # wraps midnight
    return a <= hour < b


class CallMetrics:
    """call_metrics row writer (V1) — one bound recorder per call.

    `turn(dict)` is the Conversation `metrics` seam (one row per
    logged turn); `call(dict)` writes the per-call summary row when
    the call ends. Every write is best-effort: metrics must never be
    able to break a call.
    """

    def __init__(self, db_path, call_id, engine="pipeline"):
        self.db_path = db_path
        self.call_id = call_id
        self.engine = engine
        self._conn = None
        self._failed = False

    def _db(self):
        if self._conn is None:
            from . import db as dbmod
            self._conn = dbmod.connect(self.db_path)
            dbmod.migrate(self._conn)
        return self._conn

    _TURN_COLS = ("turn_seq", "heard", "eos_ms", "stt_ms", "llm_backend",
                  "llm_first_token_ms", "llm_total_ms", "tts_provider",
                  "tts_first_ms", "first_audio_ms", "gaps_ms", "fillers",
                  "barge_in_ms", "interrupted", "dropped")
    _CALL_COLS = ("rx_ok", "rx_fail", "rx_silence", "rx_padded",
                  "peak_rss_mb", "ffmpeg_spawns", "call_seconds",
                  "turns", "reason")

    def _write(self, kind, cols, row):
        if self._failed or not self.db_path:
            return
        try:
            from .util import now_iso
            vals = {"call_id": self.call_id, "kind": kind,
                    "engine": row.get("engine") or self.engine,
                    "created_at": now_iso()}
            # absent/None fields stay out of the INSERT so column
            # defaults (fillers=0, interrupted=0) apply
            vals.update({k: v for k in cols
                         if (v := row.get(k)) is not None})
            names = ",".join(vals)
            self._db().execute(
                f"INSERT INTO call_metrics({names}) "
                f"VALUES({','.join('?' for _ in vals)})",
                tuple(vals.values()))
        except Exception as e:
            self._failed = True
            log.warning("call metrics write failed: %s", e)

    def turn(self, row):
        self._write("turn", self._TURN_COLS, row)

    def call(self, row):
        self._write("call", self._CALL_COLS, row)

    def close(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None


class CallLimiter:
    """Sliding-window per-hour call cap (in-process)."""

    def __init__(self):
        self._times = collections.deque()
        self._lock = threading.Lock()

    def allow(self, max_per_hour: int) -> bool:
        if max_per_hour <= 0:
            return False
        with self._lock:
            cutoff = time.monotonic() - 3600
            while self._times and self._times[0] < cutoff:
                self._times.popleft()
            return len(self._times) < max_per_hour

    def record(self):
        with self._lock:
            self._times.append(time.monotonic())


class CallManager:
    """Guard rails + orchestration around a VoiceRuntime.

    `runtime` does the actual join/speak/listen (DiscordPyRuntime
    below; a fake in tests) via run_call(); `api` is DiscordAPI for
    mentions, DMs and channel find/create; `engine` is the chat engine
    used for the two-way part."""

    def __init__(self, settings, env, api, db_path=None,
                 engine_factory=None, owner_id="", bot_user_id="",
                 runtime_factory=None, limiter=None):
        self.s = settings
        self.env = env
        self.api = api
        self.db_path = db_path
        self._engine_factory = engine_factory
        self.owner_id = str(owner_id or "")
        self.bot_user_id = str(bot_user_id or "")
        self._runtime_factory = runtime_factory
        self._rt = None
        self._limiter = limiter or CallLimiter()
        self._lock = threading.Lock()
        self._call_lock = threading.Lock()    # one call at a time

    # -- public API -----------------------------------------------------
    def call(self, message: str, urgent: bool = False,
             source: str = "chat") -> dict:
        """Kick off a call (returns immediately, the flow runs on a
        daemon thread). Always returns a dict — never raises."""
        message = (message or "").strip()
        if not message:
            return {"error": "empty_message"}
        if not self.s.call_guild_id:
            return {"error": "not_configured",
                    "message": "set PA_CALL_GUILD_ID (see docs/VOICE.md)"}
        if self._call_lock.locked():
            return {"error": "busy",
                    "message": "a call is already in progress"}
        if in_quiet_hours(self.s.quiet_hours, self.s.timezone) \
                and not urgent:
            return {"error": "quiet_hours",
                    "message": "quiet hours — only urgent calls "
                               "ring now (flag urgent=True)"}
        if not self._limiter.allow(self.s.call_max_per_hour):
            return {"error": "rate_limited",
                    "message": "call cap reached for this hour"}
        # held by the call thread for the whole ring->hang-up
        if not self._call_lock.acquire(blocking=False):
            return {"error": "busy",
                    "message": "a call is already in progress"}
        try:
            self._limiter.record()
            threading.Thread(
                target=self._run_locked,
                args=(message, urgent, source),
                daemon=True, name="pa-call").start()
            return {"calling": True,
                    "message": "ringing the owner now"}
        except Exception:
            self._call_lock.release()
            raise

    def _run_locked(self, *args):
        try:
            self._run(*args)
        finally:
            self._call_lock.release()

    # -- flow -------------------------------------------------------------
    def _engine(self):
        return self._engine_factory() if self._engine_factory else None

    def _call_voice(self):
        """One CallTTS per call: a single provider locked for every
        clip (say), plus a disk-cached variant for static lines
        (say_cached) so fillers never burn TTS quota twice. Format
        follows the playback path: PCM for in-proc, OGG for ffmpeg."""
        fmt = "pcm" if getattr(self.s, "call_audio", "inproc") \
            != "ffmpeg" else "ogg"
        return voicemod.CallTTS(self.s, self.env, fmt=fmt)

    def _transcribe_fn(self):
        """WAV bytes -> {heard, noise}: Whisper with hallucination meta,
        then the noise/too-short filters. Failures keep the call
        listening (heard="")."""
        def transcribe(wav: bytes, expecting_short=False):
            try:
                text, _prov, meta = voicemod.transcribe_meta(
                    self.s, self.env, wav, "call.wav", "audio/wav")
            except Exception as e:
                log.warning("call transcribe failed: %s", e)
                return {"heard": "", "noise": False}
            if not callconv.transcript_ok(
                    text, meta, expecting_short=expecting_short):
                log.info("call: dropped noise transcript %r",
                         (text or "")[:60])
                return {"heard": "", "noise": True}
            return {"heard": text.strip(), "noise": False}
        return transcribe

    def _think_fn(self, channel_id):
        """heard text -> {reply, done}: one call-mode engine turn;
        `done` on wrap-up words ('thanks', 'bye', 'that's it')."""
        def think(heard: str):
            done = callconv.is_wrapup(heard)
            reply = ("Right — leaving you to it. Ping me if you "
                     "need me.")
            if not done:
                eng = self._engine()
                if eng is not None:
                    try:
                        out = eng.run_turn(
                            str(channel_id), f"[call] {heard}",
                            call=True)
                        reply = out.get("text") or reply
                    except Exception:
                        log.exception("call engine turn failed")
                        reply = "Sorry, I lost my train of thought."
            return {"reply": reply, "done": done}
        return think

    def _live_fast_tools(self):
        """V4: the three instant read-only tools Live can call — each
        returns a short string, never raises, uses the existing gcal /
        gmail read helpers."""
        def _now():
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(getattr(self.s, "timezone", "")
                              or "UTC")
            except Exception:
                import datetime as _dt
                tz = _dt.timezone.utc
            import datetime as _dt
            return _dt.datetime.now(tz).strftime(
                "%A %d %B %Y, %H:%M %Z")

        def _cal():
            try:
                from . import calsource, db as dbmod
                conn = dbmod.connect(self.db_path) \
                    if self.db_path else None
                try:
                    evs = calsource.list_events(
                        self.s, self.env, conn, days_ahead=1,
                        max_results=5)
                finally:
                    if conn is not None:
                        conn.close()
                if evs is None:
                    return "calendar unavailable"
                if not evs:
                    return "nothing left on today's calendar"
                return "; ".join(
                    f"{e.get('summary') or '?'}"
                    for e in evs[:5])[:400]
            except Exception as e:
                return f"calendar unavailable: {type(e).__name__}"

        def _unread():
            try:
                from . import gmail, db as dbmod
                conn = dbmod.connect(self.db_path) \
                    if self.db_path else None
                try:
                    gm = gmail.GmailClient(self.s, self.env, conn=conn)
                    msgs = gm.list_messages(
                        query="is:unread", max_results=8)
                finally:
                    if conn is not None:
                        conn.close()
                if not msgs:
                    return "inbox is clear"
                return f"{len(msgs)} unread; latest: " + "; ".join(
                    str(m.get("subject") or m.get("snippet") or "?")[:60]
                    for m in msgs[:3])[:400]
            except Exception as e:
                return f"mail unavailable: {type(e).__name__}"

        return {"now": _now, "calendar_today": _cal,
                "unread_summary": _unread}

    def _resolve_channel(self) -> str:
        """PA_CALL_CHANNEL_ID, else find-or-create the private
        'pal-call' voice channel in PA_CALL_GUILD_ID (owner + bot
        only)."""
        if self.s.call_channel_id:
            return self.s.call_channel_id
        guild = self.s.call_guild_id
        try:
            for ch in self.api.guild_channels(guild) or []:
                if ch.get("type") == 2 and ch.get("name") == "pal-call":
                    return str(ch["id"])
            # VIEW_CHANNEL(1<<10) | CONNECT(1<<20) | SPEAK(1<<21)
            overwrites = [{"id": guild, "type": 0, "deny": "3148800"}]
            for uid in (self.owner_id, self.bot_user_id):
                if uid:
                    overwrites.append(
                        {"id": uid, "type": 1, "allow": "3148800"})
            ch = self.api.create_voice_channel(
                guild, "pal-call", overwrites)
            return str((ch or {}).get("id") or "")
        except Exception:
            log.exception("call channel resolve failed")
            return ""

    def _metrics(self, call_id, engine="pipeline"):
        """The per-call call_metrics recorder (V1); None when disabled
        or unconfigured — the call then runs exactly as before."""
        if not getattr(self.s, "call_metrics", True):
            return None
        db_path = self.db_path or getattr(self.s, "db_path", "")
        if not db_path:
            return None
        return CallMetrics(db_path, call_id, engine=engine)

    def _run(self, message: str, urgent: bool, source: str):
        summary = {"reason": "error", "turns": 0, "heard": []}
        channel_id = ""
        from .util import new_id
        metrics = self._metrics(new_id(6))
        t_start = time.monotonic()
        try:
            channel_id = self._resolve_channel()
            if not channel_id:
                summary["reason"] = "no_channel"
                log.warning("call: no usable voice channel")
                return
            self._notify(channel_id, message, urgent)
            call_tts = self._call_voice()
            spawns0 = _FFMPEG_SPAWNS["n"]
            result = self._runtime().run_call(
                channel_id=channel_id, owner_id=self.owner_id,
                message=message,
                join_seconds=self.s.call_join_seconds,
                listen_seconds=self.s.call_listen_seconds,
                max_seconds=self.s.call_max_seconds,
                say=call_tts.say,
                say_cached=call_tts.say_cached,
                transcribe=self._transcribe_fn(),
                think=self._think_fn(channel_id),
                notify=self._dm_owner,
                metrics=metrics,
                say_info=lambda: {"tts_provider": call_tts.provider})
            summary.update(result or {})
            if metrics is not None:
                rx = dict(summary.get("rx") or _dave_rx_stats())
                metrics.call({
                    "engine": summary.get("engine") or "pipeline",
                    "rx_ok": rx.get("ok"), "rx_fail": rx.get("fail"),
                    "rx_silence": rx.get("silence"),
                    "rx_padded": rx.get("padded"),
                    "peak_rss_mb": _rss_mb(),
                    "ffmpeg_spawns": _FFMPEG_SPAWNS["n"] - spawns0,
                    "call_seconds": round(
                        time.monotonic() - t_start, 2),
                    "turns": summary.get("turns"),
                    "reason": summary.get("reason")})
        except ImportError as e:
            summary["reason"] = "voice_extras_missing"
            log.warning("call: voice extras not installed: %s "
                        "(pip install -r requirements-voice.txt)", e)
        except Exception:
            log.exception("call flow failed")
        finally:
            if metrics is not None:
                metrics.close()
            self._summarize(channel_id, message, summary)

    def _dm_owner(self, text):
        """One-off owner DM used by the runtime when it can't hear."""
        try:
            dm = self.api.open_dm(self.owner_id) if self.owner_id else ""
            if dm:
                return bool(self.api.post_message(dm, text))
        except Exception:
            log.exception("call owner-dm failed")
        return False

    def _notify(self, channel_id: str, message: str, urgent: bool):
        ping = f"<@{self.owner_id}>" if self.owner_id else "owner"
        flag = " 🚨" if urgent else ""
        try:
            self.api.post_message(
                channel_id, f"📞 pal is calling {ping}{flag}")
            dm = self.api.open_dm(self.owner_id) if self.owner_id else ""
            if dm:
                self.api.post_message(
                    dm, f"📞 pal is calling — join <#{channel_id}>{flag}\n"
                        + message[:300])
        except Exception:
            log.exception("call notify failed")

    def _summarize(self, channel_id, message, summary):
        reason = summary.get("reason", "ended")
        turns = summary.get("turns", 0)
        heard = summary.get("heard") or []
        lines = [f"📞 call over ({reason}, {turns} turn"
                 f"{'s' if turns != 1 else ''}) — {message[:200]}"]
        for h in heard[-5:]:
            lines.append(f"  heard: {h[:160]}")
        rt = self._rt
        if rt is not None and not getattr(rt, "can_listen", True):
            lines.append("(one-way call: owner audio wasn't received — "
                         "voice-recv/DAVE shim unavailable)")
        elif summary.get("deaf"):
            lines.append("(pal couldn't hear the owner — inbound "
                         "decryption never succeeded; run "
                         "`python -m pa call-diag` on the host)")
        target = ""
        try:
            target = self.api.open_dm(self.owner_id) \
                if self.owner_id else ""
        except Exception:
            pass
        target = target or channel_id
        if not target:
            return
        try:
            if not self.api.post_message(target, "\n".join(lines)):
                log.warning("call summary post failed")
        except Exception:
            log.exception("call summary post failed")

    # -- runtime ---------------------------------------------------------
    def _runtime(self):
        if self._rt is not None:
            return self._rt
        with self._lock:
            if self._rt is None:
                factory = self._runtime_factory or DiscordPyRuntime
                self._rt = factory(
                    self.s.discord_token(self.env), self.owner_id,
                    self.s, self.env)
        return self._rt

    def stop(self):
        rt = self._rt
        if rt is not None and hasattr(rt, "stop"):
            try:
                rt.stop()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# discord.py runtime — lazily imported so the base app needs no extras.

class DiscordPyRuntime:
    """One discord.py voice client on its own asyncio loop/thread.

    Intents: guilds + guild voice states only. A second gateway
    connection alongside pa's own wsclient is fine (Discord allows
    several sessions per bot token). Voice send needs PyNaCl + davey;
    receive needs discord-ext-voice-recv + the DAVE shim below.
    """

    can_listen = False

    def __init__(self, token, owner_id, settings=None, env=None):
        import asyncio
        import discord
        self._asyncio = asyncio
        self._discord = discord
        self._settings = settings
        self._env = env
        self.owner_id = str(owner_id or "")
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._joined = asyncio.Event()
        self._left = asyncio.Event()
        self._call_channel = None
        intents = discord.Intents(guilds=True, voice_states=True)
        self._client = discord.Client(intents=intents)
        self._recv_cls = discord.VoiceClient
        self.can_listen = self._setup_recv()
        client = self._client

        @client.event
        async def on_ready():
            self._ready.set()

        @client.event
        async def on_voice_state_update(member, before, after):
            if str(getattr(member, "id", "")) != self.owner_id:
                return
            bc = getattr(before, "channel", None)
            ac = getattr(after, "channel", None)
            ch = self._call_channel
            if ch is not None and ac is not None and ac.id == ch.id:
                self._joined.set()
            if ch is not None and bc is not None and bc.id == ch.id \
                    and (ac is None or ac.id != ch.id):
                self._left.set()

        def run():
            asyncio.set_event_loop(self._loop)
            self._loop.create_task(client.start(token))
            try:
                self._loop.run_forever()
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True,
                                        name="pa-voice-client")
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("discord.py voice client never got READY")

    # -- receive plumbing -------------------------------------------------
    def _setup_recv(self):
        """Enable discord-ext-voice-recv when installed, applying the
        DAVE decrypt shim (private APIs — guarded; failures degrade to
        one-way calls)."""
        try:
            from discord.ext import voice_recv
        except Exception:
            log.info("voice-recv not installed — calls are speak-only")
            return False
        try:
            self._recv_cls = voice_recv.VoiceRecvClient
            if _patch_voice_recv_dave():
                log.info("voice-recv enabled (DAVE shim applied)")
            else:
                log.warning("voice-recv present but the DAVE shim "
                            "didn't apply — receive may produce no "
                            "audio")
            return True
        except Exception:
            log.exception("voice-recv setup failed")
            return False

    # -- the call ----------------------------------------------------------
    def run_call(self, *, channel_id, owner_id, message, join_seconds,
                 listen_seconds, max_seconds, say, transcribe, think,
                 notify=None, say_cached=None, metrics=None,
                 say_info=None) -> dict:
        """Blocking entry: schedules the async flow on the voice loop.
        `notify` (optional) is called with a string when pal realises
        mid-call it can't hear the owner — CallManager routes it to the
        owner's DM. `say_cached` (optional) is say() for static lines —
        the filler bank warms through it so clips synthesise once ever.
        `metrics` (optional) is the CallMetrics recorder for this call.
        """
        fut = self._asyncio.run_coroutine_threadsafe(
            self._call(channel_id, owner_id, message, join_seconds,
                       listen_seconds, max_seconds, say,
                       say_cached or say, transcribe, think, notify,
                       metrics, say_info),
            self._loop)
        return fut.result(timeout=max_seconds + 120)

    async def _call(self, channel_id, owner_id, message, join_seconds,
                    listen_seconds, max_seconds, say, say_cached,
                    transcribe, think, notify, metrics, say_info):
        result = {"reason": "ended", "turns": 0, "heard": []}
        channel = self._client.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self._client.fetch_channel(
                    int(channel_id))
            except Exception as e:
                log.warning("call: channel fetch failed: %s", e)
                result["reason"] = "no_channel"
                return result
        self._call_channel = channel
        self._joined.clear()
        self._left.clear()
        try:
            vc = await channel.connect(cls=self._recv_cls, timeout=45)
        except Exception as e:
            log.warning("call: voice connect failed: %s", e)
            result["reason"] = "connect_failed"
            return result
        # the owner may already sit in the channel (a second ring, or a
        # faster join than the first state event) — no update will fire
        if any(str(getattr(m, "id", "")) == self.owner_id
               for m in getattr(channel, "members", []) or []):
            self._joined.set()
        deadline = self._loop.time() + max_seconds
        # V2: one AudioSource for the whole call when PA_CALL_AUDIO=
        # inproc — the speaker enqueues PCM and reads silence between
        # clips; `ffmpeg` keeps the per-clip FFmpegPCMAudio path.
        audio_src = None
        procs = []
        if getattr(self._settings, "call_audio", "inproc") != "ffmpeg":
            audio_src = CallAudioSource()
            vc.play(audio_src)
            play_clip = lambda data: self._play_pcm(audio_src, data)  # noqa: E731
            stop_playback = audio_src.clear
        else:
            play_clip = lambda ogg: self._play_ogg(vc, ogg, procs)  # noqa: E731
            stop_playback = lambda: vc.stop()  # noqa: E731
        try:
            if not await self._wait_joined(join_seconds):
                result["reason"] = "no_answer"
                return result
            if not self.can_listen:
                await self._speak_text(
                    play_clip, say, f"{message} I can't hear you — "
                                    "reply with a voice note or a DM.")
                result["reason"] = "one_way"
                await self._asyncio.sleep(3)
                return result
            sink = _UtteranceSink()
            _dave_rx_reset()
            vc.listen(sink)
            # V6: the filler bank warms after the greeting, in an
            # executor — off the critical path (say_cached makes the
            # warm free on every call after the first)
            bank = callconv.FillerBank()
            deaf = {"at": self._loop.time() + min(10, listen_seconds),
                    "warned": False}

            def health():
                if not _is_listening(vc):
                    return "dead"
                if not deaf["warned"] and \
                        self._loop.time() >= deaf["at"] and \
                        _dave_rx_broken():
                    deaf["warned"] = True
                    return "deaf"
                return None

            conv_result = None
            engines = livemod.engine_order(self._settings, self._env)
            for eng_kind in engines:
                if eng_kind == "live":
                    # V4: the model speaks itself — server-side VAD +
                    # interruption; out audio lands in a CallAudioSource
                    src = audio_src or CallAudioSource()
                    if audio_src is None:
                        vc.play(src)
                    # final-5 F3: live_min budget gate (PA_LIVE_BUDGET)
                    # — reserve the call's remaining minutes before the
                    # socket opens, settle ceil(live_seconds/60) after.
                    gate = None
                    if getattr(self._settings, "live_budget", False):
                        gate = livemod.LiveBudget(
                            getattr(self._settings, "db_path", ""),
                            self._settings, self._env)
                        if not gate.reserve(
                                max(0.0,
                                    deadline - self._loop.time())):
                            if len(engines) > 1:
                                conv_result = {
                                    "reason": "live_unavailable",
                                    "detail": "live_min budget spent"}
                                log.warning(
                                    "call: %s — pipeline fallback",
                                    conv_result["detail"])
                                continue
                            await self._speak_text(
                                play_clip, say,
                                "Today's live minutes are used up — "
                                "message me instead.")
                            result["reason"] = "live_budget"
                            return result
                    eng = livemod.LiveEngine(
                        self._settings, self._env, source=src,
                        ask_pal=lambda q: (
                            (think(q) or {}).get("reply") or ""),
                        fast_tools=self._live_fast_tools())
                    pump = self._loop.create_task(
                        self._pump(sink, str(owner_id), eng))
                    left = self._loop.create_task(self._watch_left(eng))
                    try:
                        conv_result = await eng.run(
                            deadline,
                            opening=f"{message} I'm listening — "
                                    "go ahead.")
                    finally:
                        pump.cancel()
                        left.cancel()
                        if gate is not None:
                            gate.settle(
                                getattr(eng, "live_seconds", 0.0),
                                bool((conv_result or {}).get("engine")))
                    if (conv_result or {}).get(
                            "reason") == "live_unavailable":
                        log.warning(
                            "call: live unavailable (%s) — "
                            "pipeline fallback",
                            (conv_result or {}).get("detail"))
                        conv_result = None
                        continue
                    break
                endp = callconv.Endpointer(
                    callconv.make_vad(self._settings),
                    barge_in=getattr(
                        self._settings, "call_barge_in", True),
                    tail_ms=getattr(
                        self._settings, "call_tail_ms", 500),
                    tail_after_short_ms=getattr(
                        self._settings, "call_tail_short_ms", 700))
                pipe_think = think
                if getattr(self._settings, "call_stream_brain", True):
                    # V5: streamed call-tier brain; the full engine
                    # only runs for [[ASK_PAL]] escapes + fallback.
                    try:
                        from . import callbrain, router as routermod
                        rp = routermod.RouterProvider(
                            self._settings, self._env)
                        brain = callbrain.CallBrain(
                            stream=lambda m: rp.chat_stream(
                                m, max_completion_tokens=220),
                            ask_pal=lambda q: (
                                (think(q) or {}).get("reply") or ""),
                            is_wrapup=callconv.is_wrapup,
                            on_dm=(
                                lambda q, a: notify(
                                    "from the call — you asked "
                                    f"about: {q}\n{a or 'still no answer'}"
                                )) if notify else None)
                        pipe_think = brain.think
                    except Exception:
                        log.exception("call brain setup failed")
                conv = callconv.Conversation(
                    transcribe=transcribe, think=pipe_think, say=say,
                    say_cached=say_cached,
                    play=play_clip,
                    stop_playback=stop_playback,
                    pack=lambda pcm: voicemod.pcm_to_wav(
                        pcm, channels=2),
                    bank=bank, endpointer=endp,
                    health=health, on_notify=notify, deaf_dm=_DEAF_DM,
                    warm=lambda: bank.warm(say_cached),
                    metrics=metrics.turn if metrics is not None
                    else None,
                    say_info=say_info, engine="pipeline")
                pump = self._loop.create_task(
                    self._pump(sink, str(owner_id), conv))
                left = self._loop.create_task(self._watch_left(conv))
                try:
                    conv_result = await conv.run(
                        deadline,
                        opening=f"{message} I'm listening — go ahead.")
                finally:
                    pump.cancel()
                    left.cancel()
                break
            result.update(conv_result or {})
            if conv_result is None:
                result["reason"] = "live_unavailable"
            if self._left.is_set() and result["reason"] == "ended":
                result["reason"] = "owner_left"
            result["deaf"] = bool(deaf["warned"] or _dave_rx_broken())
            result["rx"] = _dave_rx_stats()
            return result
        finally:
            try:
                vc.stop_listening()
            except Exception:
                pass
            try:
                if audio_src is not None:
                    audio_src.close()
                # kill any in-flight FFmpegPCMAudio before disconnect,
                # else discord.py drops it mid-stream ("ffmpeg process
                # has not terminated") and the socket waits on it
                if vc.is_playing():
                    log.info("call playback stop: call end (disconnect)")
                vc.stop()
                _reap(procs)
                await self._asyncio.sleep(0.15)
            except Exception:
                pass
            try:
                await vc.disconnect()
            except Exception:
                pass
            self._call_channel = None

    async def _pump(self, sink, owner_id, conv):
        """Drain the sink into the endpointer: one 20 ms frame per
        cycle, or a silence tick when Discord sends nothing."""
        try:
            while True:
                frame = sink.pop(owner_id)
                conv.feed_frame(frame)
                await self._asyncio.sleep(0.02 if frame is None else 0)
        except self._asyncio.CancelledError:
            pass

    async def _watch_left(self, conv):
        try:
            await self._left.wait()
            conv.request_stop("owner_left")
        except self._asyncio.CancelledError:
            pass

    async def _speak_text(self, play_clip, say, text):
        ogg = await self._asyncio.to_thread(say, text)
        if not ogg:
            return False
        await play_clip(ogg)
        return True

    async def _play_pcm(self, src, pcm):
        """V2 in-proc play seam: enqueue one clip of 48 kHz stereo PCM
        and return when it drains (or clear() fires — barge-in)."""
        log.info("call playback: clip start (%d bytes pcm)", len(pcm))
        src.feed(pcm)
        drained = await src.wait_drained(
            len(pcm), self._asyncio.sleep, self._loop.time)
        if not drained:
            log.info("call playback stop: drain deadline hit")

    async def _wait_joined(self, seconds):
        deadline = self._loop.time() + seconds
        while self._loop.time() < deadline:
            try:
                await self._asyncio.wait_for(
                    self._joined.wait(),
                    timeout=min(1.0, deadline - self._loop.time()))
                return True
            except self._asyncio.TimeoutError:
                pass
        return False

    async def _play_ogg(self, vc, ogg_bytes, procs=None):
        """One OGG clip to the voice channel; returns when it finishes
        or vc.stop() fires (barge-in) — the PA_CALL_AUDIO=ffmpeg play
        seam. Strictly one clip at a time: a lingering play is stopped
        (and logged) rather than letting vc.play raise 'Already
        playing'. The ffmpeg Popen is tracked in `procs` so _reap can
        kill+wait it at call end — no zombie survives hang-up."""
        done = self._asyncio.Event()
        _FFMPEG_SPAWNS["n"] += 1
        src = self._discord.FFmpegPCMAudio(io.BytesIO(ogg_bytes),
                                          pipe=True)
        if procs is not None:
            procs.append(getattr(src, "_process", None)
                         or getattr(src, "process", None))
        try:
            if vc.is_playing():
                log.warning("call playback stop: stale play still "
                            "active when next clip arrived — stopping")
                vc.stop()
            vc.play(src, after=lambda e: self._loop.call_soon_threadsafe(
                done.set))
        except Exception as e:
            log.warning("call playback failed to start: %s", e)
            return
        try:
            await self._asyncio.wait_for(done.wait(), timeout=120)
        except self._asyncio.TimeoutError:
            log.info("call playback stop: clip timed out after 120s")
            vc.stop()

    # -- diagnostics -------------------------------------------------------
    def run_diag(self, *, channel_id, seconds=30):
        """Join the call channel and log a DAVE/ssrc/decrypt snapshot
        every 2 s — the `python -m pa call-diag` check. Returns the
        last snapshot."""
        fut = self._asyncio.run_coroutine_threadsafe(
            self._diag(channel_id, seconds), self._loop)
        return fut.result(timeout=seconds + 90)

    async def _diag(self, channel_id, seconds):
        channel = self._client.get_channel(int(channel_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(channel_id))
        vc = await channel.connect(cls=self._recv_cls, timeout=45)
        _dave_rx_reset()
        snap = {}
        try:
            if hasattr(vc, "listen"):
                vc.listen(_UtteranceSink())
            else:
                log.warning("call-diag: no voice-recv — session "
                            "state only (install requirements-voice.txt)")
            end = self._loop.time() + seconds
            while self._loop.time() < end:
                snap = self._diag_snapshot(vc)
                log.info("call-diag: %s", snap)
                await self._asyncio.sleep(2)
            snap = self._diag_snapshot(vc)
            log.info("call-diag final: %s", snap)
            return snap
        finally:
            try:
                vc.stop_listening()
            except Exception:
                pass
            try:
                await vc.disconnect()
            except Exception:
                pass

    def _diag_snapshot(self, vc):
        """Everything needed to place a 'pal can't hear me' failure:
        the ssrc->user map against the users the DAVE session actually
        built decryptors for, plus the decrypt counters."""
        state = getattr(vc, "_connection", None)
        session = getattr(state, "dave_session", None)
        ssrc_map = dict(getattr(vc, "_ssrc_to_id", {}) or {})
        snap = {
            "listening": _is_listening(vc)
            if hasattr(vc, "listen") else "no voice-recv",
            "dave_protocol_version": getattr(
                state, "dave_protocol_version", None),
            "ssrc_map": {str(k): v for k, v in ssrc_map.items()},
            "rx": _dave_rx_stats(),
        }
        if session is not None:
            try:
                users = _safe_call(session.get_user_ids) or []
                snap["session"] = {
                    "ready": getattr(session, "ready", None),
                    "status": str(getattr(session, "status", None)),
                    "epoch": getattr(session, "epoch", None),
                    "can_encrypt": getattr(session, "can_encrypt", None),
                    "user_ids": users,
                    # mapped speakers the session can't decrypt for yet
                    "missing_decryptors": [
                        uid for uid in ssrc_map.values()
                        if uid not in set(users)
                        and uid != getattr(session, "user_id", None)],
                }
            except Exception as e:
                snap["session_error"] = repr(e)
        return snap

    def stop(self):
        try:
            self._asyncio.run_coroutine_threadsafe(
                self._client.close(), self._loop).result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


class CallAudioSource:
    """V2: one long-lived AudioSource per call — gapless, no ffmpeg.

    discord.py's player thread calls read() every 20 ms; we answer
    3,840-byte 48 kHz s16le stereo frames off a thread-safe queue of
    whole clips, zeros when empty so the source never ends mid-call.
    Barge-in is clear() — the next read is silence instantly. A short
    clip tail pads to the frame boundary (<=19 ms).
    """

    FRAME = callconv.FRAME_BYTES               # 3840

    def __init__(self):
        self._q = collections.deque()          # bytearrays (clips)
        self._buf = None                       # clip in progress
        self._lock = threading.Lock()
        self.frames_read = 0
        self.underruns = 0
        self.closed = False

    # -- discord.AudioSource interface ------------------------------------
    def is_opus(self):
        return False

    def read(self):
        with self._lock:
            if self._buf is None:
                if not self._q:
                    self.underruns += 1
                    return bytes(self.FRAME)
                self._buf = self._q.popleft()
            if len(self._buf) >= self.FRAME:
                out = bytes(self._buf[:self.FRAME])
                del self._buf[:self.FRAME]
                if not self._buf:
                    self._buf = None
            else:                       # clip tail pads to the frame
                out = bytes(self._buf).ljust(self.FRAME, b"\x00")
                self._buf = None
            self.frames_read += 1
            return out

    def cleanup(self):
        self.close()

    # -- producer / control side -------------------------------------------
    def feed(self, pcm):
        """One clip of s16le stereo 48 kHz; ignored once closed."""
        if pcm:
            with self._lock:
                if not self.closed:
                    self._q.append(bytearray(pcm))

    def clear(self):
        """Barge-in: drop everything buffered — next read is silence."""
        with self._lock:
            self._q.clear()
            self._buf = None

    def empty(self):
        with self._lock:
            return not self._q and not self._buf

    def queued_ms(self):
        with self._lock:
            n = sum(len(c) for c in self._q) + len(self._buf or b"")
        return n * 20 // self.FRAME

    async def wait_drained(self, pcm_len, sleep, now, pad_s=1.5):
        """Await playback of the last-fed clip: sleeps until the queue
        is empty or the clip's own duration (+pad) elapses. `sleep` and
        `now` are injected (asyncio.sleep / loop.time in production,
        fakes in tests); clear() makes empty() true instantly."""
        secs = pcm_len / (self.FRAME * 50)       # 20 ms frames
        deadline = now() + secs + pad_s
        while not self.empty():
            if now() >= deadline:
                return False
            await sleep(0.01)
        return True

    def close(self):
        with self._lock:
            self.closed = True
            self._q.clear()
            self._buf = None


def _reap(procs):
    """Kill+wait every spawned process (no ffmpeg may survive a call —
    the observed `[ffmpeg] <defunct>` zombie came from a lingering
    FFmpegPCMAudio after hang-up)."""
    for p in procs:
        if p is None:
            continue
        try:
            p.kill()
        except Exception:
            pass
    for p in procs:
        if p is None:
            continue
        try:
            p.wait(timeout=3)
        except Exception:
            pass


class _UtteranceSink(_AudioSink):
    """Queues 20 ms PCM frames per user for _pump to consume."""

    def __init__(self):
        try:
            super().__init__()
        except Exception:
            pass
        self._frames = collections.deque()
        self._lock = threading.Lock()

    def wants_opus(self):
        return False

    def write(self, user, data):
        pcm = getattr(data, "pcm", None)
        if not pcm:
            return
        uid = str(getattr(user, "id", "")) if user is not None else ""
        with self._lock:
            self._frames.append((uid, bytes(pcm)))

    def cleanup(self):
        pass

    def pop(self, owner_id=None):
        """Oldest queued frame, restricted to the owner when given."""
        with self._lock:
            while self._frames:
                uid, pcm = self._frames.popleft()
                if owner_id is None or uid == owner_id:
                    return pcm
            return None


def _safe_call(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# /call stats (V1): last call + 7-day stage percentiles from call_metrics.

_STAGES = ("first_audio_ms", "stt_ms", "llm_total_ms", "tts_first_ms",
           "gaps_ms", "eos_ms")


def _pctl(vals, q):
    """Nearest-rank percentile of a non-empty list of numbers."""
    vals = sorted(vals)
    rank = max(0, min(len(vals) - 1,
                    int(round(q / 100.0 * (len(vals) - 1)))))
    return vals[rank]


def call_stats(conn, days=7):
    """-> {last_call, last_turns, stage_p50_p90, n_calls, n_turns} or
    None when the call_metrics table is missing/empty."""
    try:
        row = conn.execute(
            "SELECT call_id, created_at, engine, call_seconds, turns,"
            " reason, rx_ok, rx_fail, rx_silence, rx_padded,"
            " peak_rss_mb, ffmpeg_spawns FROM call_metrics"
            " WHERE kind='call' ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:
        return None
    if row is None:
        return {"last_call": None, "last_turns": [], "stages": {},
                "n_calls": 0, "n_turns": 0, "days": days}
    last = dict(row)
    turns = [dict(r) for r in conn.execute(
        "SELECT * FROM call_metrics WHERE call_id=? AND kind='turn'"
        " ORDER BY turn_seq", (last["call_id"],)).fetchall()]
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days)
              ).strftime("%Y-%m-%dT%H:%M:%S")
    stage_rows = conn.execute(
        "SELECT * FROM call_metrics WHERE kind='turn'"
        " AND created_at>=?", (cutoff,)).fetchall()
    stages = {}
    for col in _STAGES:
        vals = [r[col] for r in stage_rows if r[col] is not None]
        if vals:
            stages[col] = {"p50": _pctl(vals, 50),
                           "p90": _pctl(vals, 90), "n": len(vals)}
    n_calls = conn.execute(
        "SELECT COUNT(DISTINCT call_id) n FROM call_metrics"
        " WHERE created_at>=?", (cutoff,)).fetchone()["n"]
    return {"last_call": last, "last_turns": turns, "stages": stages,
            "n_calls": n_calls, "n_turns": len(stage_rows),
            "days": days}


def _fmt_ms(v):
    return f"{int(v)}ms" if v is not None else "-"


def stats_text(conn, days=7) -> str:
    """The `/call stats` reply: last call summary + 7-day p50/p90 per
    pipeline stage."""
    st = call_stats(conn, days=days)
    if st is None or st["last_call"] is None:
        return ("📞 no call metrics yet — they record on the next "
                "call (PA_CALL_METRICS).")
    c = st["last_call"]
    lines = [
        f"📞 last call {c['call_id'][:8]} ({c['created_at'][:16]}, "
        f"{c['engine']}, {c['call_seconds'] or 0:.0f}s, "
        f"{c['turns'] or 0} turns, {c['reason']})"]
    rx = "rx ok=%s fail=%s silence=%s padded=%s" % (
        c.get("rx_ok") or 0, c.get("rx_fail") or 0,
        c.get("rx_silence") or 0, c.get("rx_padded") or 0)
    lines.append(f"  {rx} · peak {c['peak_rss_mb'] or '?'}MB · "
                 f"ffmpeg spawns {c['ffmpeg_spawns'] or 0}")
    for t in st["last_turns"][:8]:
        lines.append(
            f"  t{t['turn_seq']}: heard={t['heard'][:40]!r} "
            f"first_audio={_fmt_ms(t['first_audio_ms'])} "
            f"stt={_fmt_ms(t['stt_ms'])} "
            f"llm={_fmt_ms(t['llm_total_ms'])}({t['llm_backend'] or '-'}) "
            f"tts={_fmt_ms(t['tts_first_ms'])}({t['tts_provider'] or '-'}) "
            f"gap={_fmt_ms(t['gaps_ms'])} fillers={t['fillers']}")
    lines.append(f"last {st['days']}d: {st['n_calls']} calls, "
                 f"{st['n_turns']} turns")
    for col, label in (("first_audio_ms", "first-audio"),
                       ("stt_ms", "stt"), ("llm_total_ms", "llm"),
                       ("tts_first_ms", "tts-first"),
                       ("gaps_ms", "gap"), ("eos_ms", "eos")):
        s = st["stages"].get(col)
        if s:
            lines.append(f"  {label}: p50={s['p50']}ms "
                         f"p90={s['p90']}ms (n={s['n']})")
    return "\n".join(lines)


def _is_listening(vc):
    try:
        return bool(vc.is_listening())
    except Exception:
        return True                       # unknown — don't block audio


# ---------------------------------------------------------------------------
# DAVE receive shim. Two private-API patches, both wrapped so upstream
# changes degrade calls to one-way instead of crashing:
#
# 1. PacketDecoder._decode_packet — route each inbound RTP payload
#    through discord.py's dave_session.decrypt() before the Opus
#    decoder sees it (voice-recv predates mandatory E2EE).
# 2. DiscordVoiceWebSocket.received_message / received_binary_message —
#    discord.py 2.7.1 processes the MLS messages but never calls
#    dave_session.prepare_transition()/execute_transition() (see its
#    own comment: "the session should be signaled too"). In davey,
#    prepare_transition is what builds the per-user decryptors and sets
#    session.ready — without it every decrypt() raises
#    NoDecryptorForUser forever, and can_encrypt stays false so our own
#    outbound audio goes out DAVE-unencrypted too.

_DAVE_RX = {"frames": 0, "ok": 0, "fail": 0, "pending": 0,
            "not_ready": 0, "no_user": 0, "silence": 0, "padded": 0,
            "last_error": "", "ok_users": set()}
_DAVE_LOG_N = {"note": 0, "warn": 0}
_MLS_OPS = frozenset((25, 27, 29, 30))   # inbound MLS binary opcodes

# V1/V2: FFmpegPCMAudio spawns per call — the in-proc path (V2) must
# keep this at zero for the whole conversation.
_FFMPEG_SPAWNS = {"n": 0}


def _rss_mb():
    """Peak RSS in MB (ru_maxrss: KiB on Linux, bytes on macOS)."""
    try:
        import resource
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(v / (1024 * 1024)) if v > 10 * 1024 * 1024 \
            else int(v / 1024)
    except Exception:
        return None


def _dave_rx_reset():
    for k, v in _DAVE_RX.items():
        if isinstance(v, int):
            _DAVE_RX[k] = 0
        elif isinstance(v, set):
            v.clear()
        else:
            _DAVE_RX[k] = ""


def _dave_rx_stats():
    return {k: (sorted(v) if isinstance(v, set) else v)
            for k, v in _DAVE_RX.items()}


def _dave_rx_broken(min_frames=8):
    """Inbound frames are flowing but none ever decrypted — the
    signature of a busted DAVE receive path (not just a quiet owner)."""
    s = _DAVE_RX
    return s["ok"] == 0 and \
        s["fail"] + s["pending"] + s["not_ready"] + s["no_user"] \
        >= min_frames


def _dave_rx_note(fmt, *args):
    """Transient drops (join window): first at INFO, then every 100."""
    _DAVE_LOG_N["note"] += 1
    n = _DAVE_LOG_N["note"]
    if n == 1 or n % 100 == 0:
        log.info("dave rx pending #%s: " + fmt, n, *args)


def _dave_rx_warn(fmt, *args):
    """Real failures: first at WARNING, then every 50 — no log flood."""
    _DAVE_LOG_N["warn"] += 1
    n = _DAVE_LOG_N["warn"]
    if n == 1 or n % 50 == 0:
        log.warning("dave rx failure #%s: " + fmt, n, *args)


_OPUS_SILENCE = b"\xf8\xff\xfe"


def _rtp_payload(data):
    """RTP packet -> media payload (skips CSRCs + one extension block).
    Falls back to the whole buffer when it doesn't parse as a v2
    header — voice-recv's decrypted_data is often the payload alone."""
    if len(data) < 12 or (data[0] & 0xC0) != 0x80:
        return data
    n = 12 + (data[0] & 0x0F) * 4
    if data[0] & 0x10:                    # X bit: extension block
        if len(data) < n + 4:
            return data
        n += 4 + struct.unpack_from(">H", data, n + 2)[0] * 4
    return data[n:] if n < len(data) else b""


def _rtp_strip_padding(data):
    """V3: when the RTP P bit (0x20) is set the last byte is the pad
    count and padding must come OFF before DAVE sees the frame —
    davey rejects padded buffers (the UnencryptedWhenPassthrough /
    DecryptionFailed spam). -> (data, stripped?)"""
    if len(data) < 2 or not (data[0] & 0x20):
        return data, False
    pad = data[-1]
    if pad == 0 or pad >= len(data):
        return data, False                # malformed pad count
    if data[len(data) - pad:-1].strip(b"\x00"):
        return data, False                # pad bytes aren't zeros
    # pad bytes + count off, P bit cleared so downstream sees a clean
    # unpadded frame
    return bytes([data[0] & ~0x20]) + data[1:-pad], True


def _dave_decrypt(session, uid, data, *, ssrc, media_type, silence):
    """One inbound RTP payload through the DAVE session. Returns the
    plaintext for the Opus decoder, or `silence` when it can't be
    decrypted yet — substituting silence instead of feeding encrypted
    bytes to Opus keeps the decode thread alive, and every later packet
    retries the same lookup, so a user whose MLS commit lands a second
    late starts decrypting automatically on their next frame."""
    st = _DAVE_RX
    st["frames"] += 1
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        st["no_user"] += 1
        _dave_rx_note("no user mapped to ssrc %s yet", ssrc)
        return silence
    if not getattr(session, "ready", False):
        # no per-user decryptors exist until the session transition runs
        st["not_ready"] += 1
        _dave_rx_note("dave session not ready (uid=%s ssrc=%s)",
                      uid, ssrc)
        return silence
    try:
        out = session.decrypt(uid, media_type, data)
    except Exception as e:
        if "NoDecryptorForUser" in str(e):
            st["pending"] += 1
            _dave_rx_note("no decryptor for user %s (ssrc %s) yet — "
                          "mls commit still in flight", uid, ssrc)
        else:
            st["fail"] += 1
            st["last_error"] = repr(e)
            _dave_rx_warn("decrypt failed for user %s (ssrc %s): %r",
                          uid, ssrc, e)
        return silence
    st["ok"] += 1
    if uid not in st["ok_users"]:
        st["ok_users"].add(uid)
        log.info("DAVE receive active for user %s (ssrc %s)",
                 uid, ssrc)
    return out or silence


def _dave_signal_json(ws, msg):
    """Pass the DAVE transition ops discord.py handles for bookkeeping
    on to the session itself (op 21 prepare / op 22 execute)."""
    try:
        session = getattr(ws._connection, "dave_session", None)
        if session is None or not isinstance(msg, dict):
            return
        op = msg.get("op")
        data = msg.get("d") or {}
        if op == getattr(ws, "DAVE_PREPARE_TRANSITION", 21):
            tid = int(data.get("transition_id", 0))
            version = int(data.get("protocol_version", 0))
            session.prepare_transition(tid, version)
            # tid 0 transitions apply immediately (same as discord.py's
            # own bookkeeping); execute is a no-op if already applied
            if tid == 0:
                session.execute_transition(0)
            log.debug("dave rx: prepared transition %s -> v%s",
                      tid, version)
        elif op == getattr(ws, "DAVE_EXECUTE_TRANSITION", 22):
            session.execute_transition(int(data.get("transition_id", 0)))
            log.debug("dave rx: executed transition %s",
                      data.get("transition_id"))
    except Exception as e:
        _dave_rx_warn("dave transition signal failed: %r", e)


def _dave_signal_binary(ws, msg):
    """op 29 (announce commit) / op 30 (welcome): after discord.py
    merged them into the group, prepare the session transition so the
    new epoch's ratchets/decryptors get built."""
    try:
        state = ws._connection
        session = getattr(state, "dave_session", None)
        if session is None or len(msg) < 5:
            return
        if msg[2] not in (
                getattr(ws, "MLS_ANNOUNCE_COMMIT_TRANSITION", 29),
                getattr(ws, "MLS_WELCOME", 30)):
            return
        transition_id = struct.unpack_from(">H", msg, 3)[0]
        version = getattr(state, "dave_protocol_version", 0) or \
            getattr(session, "protocol_version", 0)
        if version:
            session.prepare_transition(transition_id, version)
            if transition_id == 0:
                session.execute_transition(0)
            log.debug("dave rx: prepared mls transition %s (v%s)",
                      transition_id, version)
    except Exception as e:
        _dave_rx_warn("dave mls transition signal failed: %r", e)


def _dave_mls_stash(state):
    stash = getattr(state, "_pa_mls_stash", None)
    if stash is None:
        stash = state._pa_mls_stash = collections.deque(maxlen=16)
    return stash


async def _dave_mls_flush(ws, orig_binary):
    """Replay MLS binary frames that arrived before the session existed
    (received_binary_message drops them when dave_session is None, e.g.
    right after a reconnect — the server never resends)."""
    stash = getattr(ws._connection, "_pa_mls_stash", None)
    if not stash:
        return
    log.info("dave rx: replaying %s early MLS frame(s)", len(stash))
    while stash:
        data = stash.popleft()
        try:
            await orig_binary(ws, data)
        except Exception as e:
            log.warning("dave rx: replayed MLS frame failed: %r", e)
        _dave_signal_binary(ws, data)


def _patch_dave_decode():
    try:
        import davey
        from discord.ext.voice_recv import opus as _vr_opus
        from discord.ext.voice_recv.rtp import OPUS_SILENCE
    except Exception:
        return False
    if getattr(_vr_opus.PacketDecoder._decode_packet,
               "_pa_dave", False):
        return True                          # already wrapped
    orig = _vr_opus.PacketDecoder._decode_packet

    def decode(self, packet):
        try:
            data = getattr(packet, "decrypted_data", None)
            if packet is not None and data:
                # V3: strip RTP padding BEFORE DAVE (padded frames are
                # what UnencryptedWhenPassthrough spammed about), then
                # pass the canonical Opus silence frame straight to the
                # decoder — Discord sends it DAVE-unencrypted.
                data, was_padded = _rtp_strip_padding(data)
                if was_padded:
                    packet.decrypted_data = data
                    _DAVE_RX["padded"] += 1
                if _rtp_payload(data) == _OPUS_SILENCE:
                    packet.decrypted_data = _OPUS_SILENCE
                    _DAVE_RX["silence"] += 1
                    return orig(self, packet)
            if packet is not None and data and len(data) > 3:
                vc = self.sink.voice_client
                state = vc._connection
                session = getattr(state, "dave_session", None)
                if session is not None and \
                        getattr(state, "dave_protocol_version", 0) > 0:
                    uid = getattr(self, "_cached_id", None) or \
                        vc._get_id_from_ssrc(self.ssrc)
                    packet.decrypted_data = _dave_decrypt(
                        session, uid, data, ssrc=self.ssrc,
                        media_type=davey.MediaType.audio,
                        silence=OPUS_SILENCE)
        except Exception:
            pass
        return orig(self, packet)

    decode._pa_dave = True
    _vr_opus.PacketDecoder._decode_packet = decode
    return True


def _patch_dave_signals():
    try:
        import discord.gateway as _gw
        ws_cls = _gw.DiscordVoiceWebSocket
    except Exception:
        return False
    if getattr(ws_cls.received_message, "_pa_dave", False):
        return True                          # already wrapped
    orig_msg = ws_cls.received_message
    orig_bin = ws_cls.received_binary_message

    async def received_message(self, msg):
        await orig_msg(self, msg)
        try:
            _dave_signal_json(self, msg)
            await _dave_mls_flush(self, orig_bin)
        except Exception:
            log.exception("dave transition hook failed")

    async def received_binary_message(self, msg):
        await orig_bin(self, msg)
        try:
            state = self._connection
            if getattr(state, "dave_session", None) is None:
                if len(msg) > 2 and msg[2] in _MLS_OPS:
                    _dave_mls_stash(state).append(bytes(msg))
                return
            await _dave_mls_flush(self, orig_bin)
            _dave_signal_binary(self, msg)
        except Exception:
            log.exception("dave binary hook failed")

    received_message._pa_dave = True
    received_binary_message._pa_dave = True
    ws_cls.received_message = received_message
    ws_cls.received_binary_message = received_binary_message
    return True


def _patch_voice_recv_dave():
    """Route inbound voice packets through discord.py's DAVE session
    before the Opus decoder sees them, and feed the session the
    transition signals discord.py 2.7.1 drops (community shim —
    Discord's mandatory E2EE encrypts received audio and voice-recv
    predates it).

    Uses private attributes in discord.py/voice-recv/davey; wrapped so
    any upstream change degrades calls to one-way instead of crashing.
    """
    decode_ok = _patch_dave_decode()
    if not _patch_dave_signals():
        log.warning("DAVE transition-signal patch didn't apply — "
                    "decryptors may never be built and receive could "
                    "stay deaf")
    return decode_ok


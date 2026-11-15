"""Call conversation machinery: VAD, endpointing, and the turn loop.

Everything here is Discord-free and synchronous-or-asyncio only, so the
whole thing is unit-testable with fakes — pa/call.py wires it to
discord.py's voice client, voice-recv's frame sink and the TTS/STT
providers.

Pieces:

- ``WebRtcVAD`` / ``EnergyVAD`` + ``make_vad`` — per-frame speech
  decision. WebRTC's GMM VAD (``webrtcvad-wheels``, a ~50 KB C
  extension) is the default when installed; it beats a raw energy
  threshold on steady background noise (fans, keyboard, TV) while
  staying real-time on a 1-4 core VM. silero-vad would need
  onnxruntime (~100 MB of deps) for a marginal gain — overkill here.
- ``Endpointer`` — turns a frame stream into utterance events: a
  pre-speech ring buffer keeps onsets, a short adaptive
  end-of-speech tail (~650-900 ms) decides when the owner finished,
  too-short bursts are dropped as noise, and a sustained-speech
  event drives barge-in. Barge-in is deliberately hard to trigger:
  it needs >=500 ms of VAD-voiced audio inside a 700 ms sliding
  window, every voiced frame louder than an adaptive noise floor,
  and never fires inside the first ~400 ms of a clip — mic noise,
  Discord audio artefacts and speaker echo of pal's own voice no
  longer chop playback into stutter.
- ``transcript_ok`` — drops Whisper noise/hallucination transcripts
  (empty, "Thank you.", "you", music, high no_speech_prob /
  low avg_logprob from verbose_json segments).
- ``FillerBank`` — a small set of ack/progress/nudge clips
  pre-synthesised once per call (TTS is rate-limited: fillers are
  never synthesised per-turn).
- ``Conversation`` — the supervisor: one "current turn" with a
  generation counter (new speech supersedes in-flight work — no FIFO
  queue; interrupted utterances merge into the next one), ack fillers
  ~1 s after end-of-speech, a progress line for slow turns, barge-in
  (stop playback on sustained owner speech), sentence-pipelined TTS
  playback, idle nudge -> wrap-up, and one log line per turn.
"""

import array
import asyncio
import collections
import logging
import math
import random
import re
import struct
import time

log = logging.getLogger("pa.callconv")

FRAME_MS = 20                    # voice-recv emits 20 ms frames
FRAME_BYTES = 3840               # 20 ms of 48 kHz stereo s16le

# wrap-up: a farewell CLOSING the utterance ('say bye to sam' does not
# hang the call up), or the whole utterance is just thanks.
_BYE_TAIL_RE = re.compile(
    r"\b(bye|bye bye|goodbye|see you( later)?|talk (to you )?"
    r"(soon|later)|catch you later|cheers|good ?night|that'?s all|"
    r"that'?s it|nothing else|that'?s everything|all good|hang up|"
    r"got to go|gotta go)[.! ]*$", re.I)
_THANKS_ONLY_RE = re.compile(
    r"^(thanks|thank you|thx|ty|ok thanks|okay thanks)[.! ]*$", re.I)

# Whole-utterance Whisper hallucinations on silence/noise — matched on
# the punctuation-stripped lower-case transcript.
_HALLUCINATE = {
    "", ".", "..", "...", "thank you", "thanks for watching",
    "thanks for watching!", "you", "bye bye", "music", "[music]",
    "(music)", "♪", "hmm", "huh", "mm", "mhm", "mm-hmm", "mmhmm",
    "uh", "um", "ah", "oh", "so", "subtitles",
}

# one-word answers are ALSO classic noise hallucinations, but a
# genuine "Okay." right after pal asked a question is a real answer.
# These pass transcript_ok only while a reply is expected (see
# Conversation._expecting_short: pal's last line ended with '?' or pal
# spoke within the last ~8 s).
_SHORT_REPLY = {
    "okay", "ok", "yes", "no", "yeah", "yep", "nope", "sure", "nah",
    "right", "correct", "fine", "maybe",
}
_SHORT_WINDOW_S = 8.0


# ---------------------------------------------------------------------------
# VAD

class EnergyVAD:
    """Fallback when webrtcvad is absent: mean |sample| over an
    adaptive noise floor (EMA of quiet frames) so a loud room doesn't
    trip a fixed threshold. Stereo frames: both channels averaged by
    the interleaved mean — fine for energy."""

    def __init__(self, floor_ratio=3.0, min_threshold=450):
        self._floor = 200.0
        self._ratio = floor_ratio
        self._min = min_threshold

    def is_speech(self, frame: bytes) -> bool:
        n = len(frame) // 2
        if not n:
            return False
        samples = struct.unpack(f"<{n}h", frame[:n * 2])
        mean = sum(abs(v) for v in samples) / n
        speech = mean > max(self._min, self._floor * self._ratio)
        if not speech:
            self._floor = 0.95 * self._floor + 0.05 * mean
        return speech


class WebRtcVAD:
    """webrtcvad on 20 ms 48 kHz frames (GMM — discriminates steady
    noise a plain energy gate accepts). Stereo -> mono: one channel is
    enough for the decision and halves the work."""

    def __init__(self, aggressiveness=2, fallback=None):
        import webrtcvad
        self._vad = webrtcvad.Vad(aggressiveness)
        self._fallback = fallback or EnergyVAD()

    def is_speech(self, frame: bytes) -> bool:
        if len(frame) != FRAME_BYTES:
            return self._fallback.is_speech(frame)
        samples = array.array("h", frame)
        try:
            return self._vad.is_speech(
                samples[0::2].tobytes(), 48000)
        except Exception:
            return self._fallback.is_speech(frame)


def make_vad(settings=None):
    """PA_CALL_VAD: auto (default) / webrtc / energy. Auto uses WebRTC's
    VAD when the extra is installed, else the adaptive energy gate."""
    want = (getattr(settings, "call_vad", "auto") or "auto").lower()
    if want in ("auto", "webrtc"):
        try:
            vad = WebRtcVAD()
            log.info("call vad: webrtcvad (mode 2)")
            return vad
        except Exception as e:
            if want == "webrtc":
                log.warning("PA_CALL_VAD=webrtc but webrtcvad is "
                            "unusable (%s) — energy fallback", e)
            else:
                log.info("call vad: webrtcvad absent (%s) — energy "
                         "fallback", e)
    return EnergyVAD()


# ---------------------------------------------------------------------------
# endpointing

def _rms(frame):
    """Root-mean-square amplitude of a s16le frame (interleaved
    stereo treated as one stream — fine for an energy estimate)."""
    n = len(frame) // 2
    if not n:
        return 0.0
    samples = struct.unpack(f"<{n}h", frame[:n * 2])
    return math.sqrt(sum(v * v for v in samples) / n)


class Endpointer:
    """20 ms frame stream -> speech/utterance events.

    ``feed(frame)`` takes one PCM frame (bytes) or ``None`` for a 20 ms
    silence tick (Discord stops sending when nobody talks, so the pump
    injects ticks on empty reads). Returns events:

    - ``("speech", diag)`` — once per segment, when barge-in triggers:
      >= ``barge_voiced_ms`` of voiced audio inside the trailing
      ``barge_window_ms`` window, each voiced frame louder than an
      adaptive noise floor (EMA of non-speech frame energy, times
      ``barge_floor_ratio``, floored at ``barge_min_energy``), and
      only after ``barge_grace_s`` has elapsed since
      ``note_playback_started()``. Backchannels, clicks and quiet
      noise never interrupt; a burst during the clip's opening grace
      is merely deferred — it still fires if the owner keeps talking.
      ``barge_in=False`` disables the trigger entirely.
    - ``("utterance", pcm)`` — the owner finished: ``tail`` silence
      followed speech, the segment had >= ``min_speech_ms`` of voiced
      audio, or ``max_ms`` of speech forced a cut.

    The tail is adaptive: a longer pause is allowed after a short burst
    (the owner is probably mid-thought) than after a long monologue
    (probably done). Trailing silence is trimmed from the emitted PCM.
    """

    def __init__(self, vad, *, min_speech_ms=300, barge_voiced_ms=500,
                 barge_window_ms=700, barge_floor_ratio=2.0,
                 barge_min_energy=500.0, barge_grace_s=0.4,
                 barge_in=True,
                 tail_ms=500, tail_after_short_ms=700, short_ms=2000,
                 max_ms=45000, prebuf_ms=400, keep_tail_ms=100,
                 frame_ms=FRAME_MS, frame_bytes=FRAME_BYTES):
        self._vad = vad
        self.frame_ms = frame_ms
        self.frame_bytes = frame_bytes
        self.min_speech_ms = min_speech_ms
        self.barge_voiced_ms = barge_voiced_ms
        self.barge_floor_ratio = barge_floor_ratio
        self.barge_min_energy = barge_min_energy
        self.barge_grace_s = barge_grace_s
        self.barge_in = barge_in
        self.tail_ms = tail_ms
        self.tail_after_short_ms = tail_after_short_ms
        self.short_ms = short_ms
        self.max_ms = max_ms
        self.keep_tail_ms = keep_tail_ms
        self._pre = collections.deque(maxlen=max(1, prebuf_ms // frame_ms))
        self._win = collections.deque(
            maxlen=max(1, barge_window_ms // frame_ms))
        self._bfloor = 200.0        # EMA of non-speech frame energy
        self._no_barge_until = 0.0  # clip-start grace deadline
        self._reset()

    def _reset(self):
        self._speech = False
        self._buf = bytearray()
        self._pre.clear()
        self._win.clear()
        self._voiced = 0            # total voiced ms in the segment
        self._tail = 0              # trailing silence ms
        self._tail_start = 0        # buf length when the tail began
        self._barged = False

    def note_playback_started(self):
        """A new clip began streaming — suppress barge-in for
        ``barge_grace_s`` so connect noise and the clip's own echo
        can't instantly kill it."""
        self._no_barge_until = time.monotonic() + self.barge_grace_s

    def feed(self, frame):
        if frame is not None and not frame:
            frame = None
        speech = frame is not None and self._vad.is_speech(frame)
        energy = _rms(frame) if frame is not None else 0.0
        if frame is not None and not speech:
            self._bfloor = 0.95 * self._bfloor + 0.05 * energy
        voiced = speech and energy >= max(
            self.barge_min_energy, self._bfloor * self.barge_floor_ratio)
        self._win.append(1 if voiced else 0)
        if not self._speech:
            if speech:
                self._speech = True
                self._buf = bytearray()
                for f in self._pre:
                    self._buf += f
                self._pre.clear()
                self._buf += frame
                self._voiced = self.frame_ms
                self._tail = 0
                self._tail_start = len(self._buf)
            elif frame is not None:
                self._pre.append(frame)
            return []

        if speech:
            self._buf += frame
            self._voiced += self.frame_ms
            if self._tail:
                self._tail = 0
                self._tail_start = len(self._buf)
            ev = []
            win_ms = sum(self._win) * self.frame_ms
            if not self._barged and self.barge_in \
                    and win_ms >= self.barge_voiced_ms \
                    and time.monotonic() >= self._no_barge_until:
                self._barged = True
                ev.append(("speech", {
                    "voiced_ms": win_ms,
                    "window_ms": len(self._win) * self.frame_ms,
                    "rms": energy,
                    "floor": self._bfloor}))
            if self._voiced >= self.max_ms:
                pcm, diag = self._emit()
                ev.append(("utterance", pcm, diag))
            return ev

        # silence — real quiet frames keep audio (Discord does send
        # some); None ticks advance the clock only.
        self._tail += self.frame_ms
        if frame is not None:
            self._buf += frame
        allowed = (self.tail_after_short_ms
                   if self._voiced < self.short_ms else self.tail_ms)
        if self._tail >= allowed:
            pcm, diag = self._emit()
            return [("utterance", pcm, diag)] if pcm is not None else []
        return []

    def _emit(self):
        """-> (pcm, diag): the trimmed utterance plus end-of-speech
        diagnostics (voiced/tail ms — the call_metrics eos_ms)."""
        tail_bytes = len(self._buf) - self._tail_start
        keep = min(tail_bytes,
                   self.keep_tail_ms // self.frame_ms * self.frame_bytes)
        pcm = bytes(self._buf[:self._tail_start + keep])
        voiced, tail = self._voiced, self._tail
        self._reset()
        if voiced < self.min_speech_ms:
            log.info("call turn: dropped=short voiced=%dms", voiced)
            return None, None            # too short: a click, a cough
        return pcm, {"voiced_ms": voiced, "tail_ms": tail}


def merge_pcm(a: bytes, b: bytes, gap_ms=300) -> bytes:
    """Concatenate two utterance PCMs with a silence gap — the merged
    audio transcribes as one continuous utterance, which is how a
    superseded turn's speech stays part of the next turn."""
    gap = bytes(FRAME_BYTES * (gap_ms // FRAME_MS))
    return (a or b"") + gap + (b or b"")


# ---------------------------------------------------------------------------
# transcript + wrap-up filters

def transcript_ok(text, meta=None, *, no_speech_max=0.6,
                  logprob_min=-1.0, expecting_short=False):
    """False for empty/noise/hallucinated Whisper output. `meta` is
    transcribe_meta()'s dict: segments' max no_speech_prob and min
    avg_logprob veto a transcript when present (absent = pass).
    `expecting_short` keeps one-word answers ("Okay.", "yeah")
    when pal's last line asked something or pal just spoke — outside
    that window they're still noise."""
    t = (text or "").strip()
    norm = t.lower().strip(".!?,…♪ ")
    if not norm or norm in _HALLUCINATE:
        return False
    if norm in _SHORT_REPLY and not expecting_short:
        return False
    meta = meta or {}
    nsp = meta.get("no_speech_prob")
    if nsp is not None and nsp >= no_speech_max:
        return False
    alp = meta.get("avg_logprob")
    if alp is not None and alp <= logprob_min:
        return False
    return True


def is_wrapup(text) -> bool:
    """The owner is done: the utterance ends in a farewell, or is only
    'thanks' ('thanks — one more thing' does NOT match)."""
    t = (text or "").strip()
    return bool(t) and (bool(_BYE_TAIL_RE.search(t))
                        or bool(_THANKS_ONLY_RE.match(t)))


_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_FENCE_RE = re.compile(r"```.*?```", re.S)
_MARKDOWN_RE = re.compile(r"[*_`#>|~]")
_BULLET_RE = re.compile(r"(?:^|\s)[-•–]\s+")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def split_for_tts(text, max_chars=240, max_chunks=6, min_chunk=35):
    """Reply text -> spoken chunks for pipelined synthesis: markdown /
    code / URLs stripped, split on sentence boundaries, small pieces
    merged so chunks stay between min_chunk and max_chars (chunking at
    commas when a single sentence overshoots)."""
    t = _FENCE_RE.sub(" ", text or "")
    t = _URL_RE.sub(" ", t)
    t = _BULLET_RE.sub(" ", t)
    t = _MARKDOWN_RE.sub("", t)
    parts = [p.strip() for p in _SENT_RE.split(t) if p.strip()]
    out = []
    cur = ""
    for part in parts:
        if len(part) > max_chars:                     # overlong sentence
            subs = [s.strip() for s in re.split(r"[,;:]\s+|\s+-\s+",
                                                part) if s.strip()]
        else:
            subs = [part]
        for s in subs:
            if cur and len(cur) + 1 + len(s) > max_chars:
                out.append(cur)
                cur = s
            else:
                cur = f"{cur} {s}".strip() if cur else s
            while len(cur) > max_chars:               # still too long
                out.append(cur[:max_chars])
                cur = cur[max_chars:].strip()
    if cur:
        out.append(cur)
    # merge trailing tiny chunks into the previous one
    merged = []
    for c in out:
        if merged and len(c) < min_chunk \
                and len(merged[-1]) + 1 + len(c) <= max_chars:
            merged[-1] = merged[-1] + " " + c
        else:
            merged.append(c)
    if len(merged) > max_chunks:                      # bound the queue
        merged = merged[:max_chunks - 1] + [
            " ".join(merged[max_chunks - 1:])[:max_chars * 2]]
    return [c for c in merged if c]


# ---------------------------------------------------------------------------
# fillers

ACK_FILLERS = ("Mm-hm.", "One sec.", "Let me check.", "On it.")
PROGRESS_FILLERS = ("Still pulling that up…", "Almost there — one sec.")
NUDGE_FILLERS = ("Anything else?",)
CHECKIN_FILLERS = ("Still there?",)


class FillerBank:
    """Pre-synthesised short clips, warmed once per call so an ack can
    play within ~1 s of end-of-speech for free (no per-turn TTS hit —
    Gemini's free tier is rate-limited). `get()` only serves what warm()
    already made; it never synthesises on demand."""

    KINDS = {"ack": ACK_FILLERS, "progress": PROGRESS_FILLERS,
             "nudge": NUDGE_FILLERS, "checkin": CHECKIN_FILLERS}

    def __init__(self, kinds=None, rng=None):
        self._texts = dict(kinds or self.KINDS)
        self._clips = {k: [] for k in self._texts}
        self._rng = rng or random.Random()
        self._last = {}

    def warm(self, say):
        """Blocking: synthesise every configured clip once. Call in an
        executor at call start; failures just leave fewer clips."""
        for kind, texts in self._texts.items():
            for t in texts:
                try:
                    ogg = say(t)
                except Exception:
                    ogg = None
                if ogg:
                    self._clips[kind].append(ogg)

    def add(self, kind, ogg):
        self._clips.setdefault(kind, []).append(ogg)

    def get(self, kind):
        clips = self._clips.get(kind) or []
        if not clips:
            return None
        if len(clips) == 1:
            return clips[0]
        last = self._last.get(kind, -1)
        choices = [i for i in range(len(clips)) if i != last]
        idx = self._rng.choice(choices)
        self._last[kind] = idx
        return clips[idx]

    def ready(self, kind):
        return bool(self._clips.get(kind))


# ---------------------------------------------------------------------------
# the conversation

class ConvConfig:
    """Tunables for Conversation — tests shrink these to milliseconds."""
    def __init__(self, **kw):
        # filler discipline: one ack at ~1.2 s, one "still on it"
        # at ~5 s, never more than two fillers per turn.
        self.ack_delay = kw.get("ack_delay", (1.2, 1.45))
        self.progress_first = kw.get("progress_first", 5.0)
        self.progress_every = kw.get("progress_every", 0.0)
        self.max_fillers = kw.get("max_fillers", 2)
        self.idle_nudge = kw.get("idle_nudge", 20.0)
        self.idle_end = kw.get("idle_end", 15.0)
        self.merge_gap_ms = kw.get("merge_gap_ms", 300)
        self.merge_cap_ms = kw.get("merge_cap_ms", 30000)
        self.reply_chunk_chars = kw.get("reply_chunk_chars", 240)
        self.reply_max_chunks = kw.get("reply_max_chunks", 6)
        self.tick = kw.get("tick", 1.0)
        self.idle_line = kw.get(
            "idle_line",
            "Alright — I'll leave it there. Ping me if you need anything.")
        self.deaf_line = kw.get(
            "deaf_line",
            "Heads up — I can't hear you. The encrypted audio isn't "
            "decrypting on my side. Check your DMs.")


class Conversation:
    """The two-way part of a call: owner speech in via feed_frame(),
    pal's voice out via play().

    Injected seams (all sync except `play`):

    - transcribe(wav_bytes) -> {"heard": str, "noise": bool}
    - think(heard_text)     -> {"reply": str, "done": bool}
    - say(text)             -> ogg bytes | None   (TTS for replies and
      the occasional dynamic line — fillers come from `bank`)
    - say_cached(text)      -> ogg bytes | None   (like say(), but for
      STATIC lines — disk-cached so they cost TTS quota once ever;
      defaults to say())
    - play(ogg) -> coroutine — plays one clip; MUST return promptly
      when stop_playback() is called (the runtime wires vc.stop).
    - stop_playback()       -> stops whatever play() is streaming now
    - pack(pcm)             -> bytes given to transcribe (wav wrap)
    - health()              -> None | "deaf" | "dead"  (polled each tick)
    - on_notify(text)       -> side-channel message (the deaf DM)
    - say_info()            -> optional dict of TTS facts merged into the
      turn metric (provider name; CallTTS knows its locked provider)
    metrics(dict) -> optional per-turn metric sink (
      call_metrics rows; called where `call turn:` logs)

    Turn model: a single `current` turn carries a generation counter.
    A new utterance while a turn is in-flight supersedes it — the stale
    result is dropped and the two utterances' audio is merged (a
    continuation). While pal is speaking, sustained owner speech is a
    barge-in: playback stops and nothing is queued.
    """

    def __init__(self, *, transcribe, think, say, play, stop_playback,
                 say_cached=None, pack=None, bank=None, endpointer=None,
                 config=None, health=None, on_notify=None, deaf_dm=None,
                 warm=None,
                 metrics=None, say_info=None, engine="pipeline",
                 rng=None):
        self.transcribe = transcribe
        self.think = think
        self.say = say
        self.say_cached = say_cached or say
        self.play = play
        self.stop_playback = stop_playback
        self.pack = pack or (lambda pcm: pcm)
        self.bank = bank or FillerBank(rng=rng)
        self.endp = endpointer
        self.cfg = config or ConvConfig()
        self.health = health or (lambda: None)
        self.on_notify = on_notify
        self._deaf_dm = deaf_dm
        self._warm = warm
        self._metrics = metrics
        self._say_info = say_info
        self.engine = engine
        self._rng = rng or random.Random()
        self._q = None               # event queue (created in run)
        self._spkq = None            # speaker queue
        self._drop = False
        self._playing = False
        self._playing_tag = None
        self._running = False
        self._stop_reason = None
        self._info = {}              # gen -> turn record (reset in run)
        # short-reply filter context — the last line pal spoke and
        # when playback ended. A '?' keeps the answer window open.
        self._spoke_text = ""
        self._spoke_at = None
        try:
            import inspect
            self._stt_ctx = "expecting_short" in inspect.signature(
                transcribe).parameters
        except Exception:
            self._stt_ctx = False

    # -- input side (called by the frame pump, on the loop) -----------
    def feed_frame(self, frame):
        if not self._running or self.endp is None:
            return
        for ev in self.endp.feed(frame):
            self._q.put_nowait(ev)

    def request_stop(self, reason="ended"):
        self._stop_reason = reason
        if self._q is not None:
            self._q.put_nowait(("stop",))

    # -- the loop -----------------------------------------------------
    async def run(self, deadline, opening=None):
        """Returns {"reason", "turns", "heard", "interrupted"}."""
        loop = asyncio.get_running_loop()
        now = loop.time
        self._q = asyncio.Queue()
        self._spkq = asyncio.Queue()
        self._running = True
        speaker = asyncio.create_task(self._speaker())
        if opening:
            self._enqueue_reply(opening, tag="opening")
        elif self._warm is not None:
            # no greeting to wait for — warm now, still in an executor
            warm, self._warm = self._warm, None
            asyncio.get_running_loop().run_in_executor(None, warm)
        gen = 0
        turn = None                    # in-flight turn dict
        task = None
        ack_at = prog_at = idle_at = None
        idle_at = now() + self.cfg.idle_nudge
        nudged = False
        ending = None
        deaf_warned = False
        info = self._info = {}         # gen -> per-turn log/metric record
        result = {"reason": "ended", "turns": 0, "heard": [],
                  "interrupted": 0}
        try:
            while True:
                t_now = now()
                if self._stop_reason:
                    result["reason"] = self._stop_reason
                    break
                if t_now >= deadline:
                    result["reason"] = "timeout"
                    break
                waits = [deadline, t_now + self.cfg.tick]
                for t in (ack_at, prog_at):
                    if t:
                        waits.append(t)
                if idle_at and turn is None and not self._playing:
                    waits.append(idle_at)
                wait = max(0.0, min(waits) - t_now)
                try:
                    ev = await asyncio.wait_for(
                        self._q.get(), timeout=wait)
                except asyncio.TimeoutError:
                    ev = ("tick",)
                kind = ev[0]
                t_now = now()

                if kind == "speech":
                    if self._playing or not self._spkq.empty():
                        diag = ev[1] if len(ev) > 1 \
                            and isinstance(ev[1], dict) else {}
                        self._interrupt(info, diag=diag)
                        result["interrupted"] += 1
                        tag = self._playing_tag
                        if isinstance(tag, int) and tag in info:
                            info[tag]["interrupted"] = True
                elif kind == "utterance":
                    if task is not None and not task.done():
                        task.cancel()
                    pcm = ev[1]
                    if not pcm:
                        continue
                    diag = ev[2] if len(ev) > 2 \
                        and isinstance(ev[2], dict) else {}
                    if turn is not None:
                        # the previous turn never answered — treat the
                        # new speech as its continuation and answer the
                        # merged audio once
                        pcm = self._merge(turn["pcm"], pcm)
                        self._log_turn(info.pop(turn["gen"], None),
                                       superseded=True)
                    gen += 1
                    turn = {"gen": gen, "pcm": pcm, "heard": None,
                            "t_end": t_now, "t_ack": None,
                            "t_audio": None, "interrupted": False,
                            "tail_ms": diag.get("tail_ms"),
                            "fillers": 0, "enq_fillers": 0}
                    info[gen] = turn
                    task = asyncio.create_task(
                        self._run_turn(gen, pcm))
                    ack_at = t_now + self._rng.uniform(*self.cfg.ack_delay)
                    prog_at = t_now + self.cfg.progress_first
                    idle_at = None
                    ending = None
                elif kind == "turn_done":
                    g, out = ev[1], ev[2]
                    if g != gen:
                        continue      # stale result — discarded
                    rec = info.get(g)
                    heard = (out or {}).get("heard") or ""
                    if rec is not None:
                        rec["heard"] = heard
                        for k, v in (out or {}).get("_times", {}).items():
                            rec[k] = v
                        for k, v in (out or {}).get("metrics", {}).items():
                            rec[k] = v
                    if not heard:
                        # noise / STT failure — not a turn, but the
                        # owner heard silence: log why
                        self._log_turn(
                            rec,
                            dropped="noise"
                            if (out or {}).get("noise") else "stt_empty")
                        turn = None
                        task = None
                        ack_at = prog_at = None
                        idle_at = t_now + self.cfg.idle_nudge
                        info.pop(g, None)
                        continue
                    result["turns"] += 1
                    result["heard"].append(heard)
                    reply = (out or {}).get("reply") or ""
                    if (out or {}).get("reply_iter") is not None:
                        # streaming brain: the speaker drains the
                        # delta stream into clauses — TTS starts while
                        # the model is still generating.
                        self._spkq.put_nowait(
                            ("stream", out["reply_iter"], g, "reply"))
                    elif reply:
                        self._enqueue_reply(reply, tag=g)
                    if (out or {}).get("done"):
                        ending = "bye"
                        if not self._playing and self._spkq.empty() \
                                and not reply:
                            result["reason"] = "bye"
                            break
                    turn = None
                    task = None
                    ack_at = prog_at = None
                    idle_at = t_now + self.cfg.idle_nudge
                elif kind == "audio":
                    _tag, akind = ev[1], ev[2]
                    rec = info.get(_tag) if isinstance(_tag, int) else None
                    if rec is not None:
                        if akind == "reply" and rec["t_audio"] is None:
                            rec["t_audio"] = t_now
                        elif akind in ("ack", "progress"):
                            rec["fillers"] = rec.get("fillers", 0) + 1
                            if rec["t_ack"] is None:
                                rec["t_ack"] = t_now
                elif kind == "item_done":
                    _tag, ikind, interrupted = ev[1], ev[2], ev[3]
                    if _tag == "opening" and self._warm is not None:
                        # filler-bank warm-up runs after the
                        # greeting, in an executor — never inside the
                        # turn that produced the first reply audio.
                        warm, self._warm = self._warm, None
                        asyncio.get_running_loop().run_in_executor(
                            None, warm)
                    if ikind == "reply" and isinstance(_tag, int):
                        self._log_turn(info.pop(_tag, None),
                                       interrupted=interrupted)
                elif kind == "idle":
                    idle_at = t_now + self.cfg.idle_nudge
                    if ending:
                        result["reason"] = ending
                        break
                elif kind in ("tick", "stop"):
                    if kind == "stop":
                        continue    # loop head picks up _stop_reason
                    if ack_at and t_now >= ack_at and turn is not None:
                        ack_at = None
                        clip = self.bank.get("ack")
                        if clip and turn["enq_fillers"] < \
                                self.cfg.max_fillers:
                            turn["enq_fillers"] += 1
                            self._enqueue_clip(clip, tag=turn["gen"],
                                               kind="ack")
                    if prog_at and t_now >= prog_at and turn is not None:
                        # progress_every=0 -> fire once, no re-arm
                        prog_at = t_now + self.cfg.progress_every \
                            if self.cfg.progress_every else None
                        clip = self.bank.get("progress")
                        if clip and turn["enq_fillers"] < \
                                self.cfg.max_fillers:
                            turn["enq_fillers"] += 1
                            self._enqueue_clip(clip, tag=turn["gen"],
                                               kind="progress")
                    if not deaf_warned:
                        h = self.health()
                        if h == "dead":
                            deaf_warned = True
                            await self._deaf_notice()
                            result["reason"] = "recv_died"
                            break
                        if h == "deaf":
                            deaf_warned = True
                            await self._deaf_notice()
                    if idle_at and t_now >= idle_at and turn is None \
                            and not self._playing:
                        if not nudged:
                            nudged = True
                            idle_at = t_now + self.cfg.idle_end
                            clip = self.bank.get(
                                "nudge" if result["turns"] else "checkin")
                            if clip:
                                self._enqueue_clip(clip, tag="nudge",
                                                   kind="nudge")
                            else:
                                # static line — say_cached so it costs
                                # quota once ever, not once per call
                                ogg = await asyncio.to_thread(
                                    self.say_cached,
                                    "Anything else?"
                                    if result["turns"]
                                    else "Still there?")
                                if ogg:
                                    self._enqueue_clip(
                                        ogg, tag="nudge", kind="nudge")
                        else:
                            ending = "idle"
                            self._enqueue_reply(self.cfg.idle_line,
                                                tag="end")
                # other events ignored
        finally:
            self._running = False
            if self._playing:
                # a clip may still be streaming (e.g. the owner hung
                # up mid-sentence) — end it so the ffmpeg source dies
                log.info("call playback stop: conversation end")
                self._stop_playing()
            self._spkq.put_nowait(None)
            if task is not None and not task.done():
                task.cancel()
            try:
                await asyncio.wait_for(speaker, timeout=5)
            except Exception:
                speaker.cancel()
        result.setdefault("reason", "ended")
        return result

    def _expecting_short(self):
        """A one-word owner reply is plausibly real: pal's last line
        ended with a question, or pal spoke within the last few
        seconds. Outside that window 'Okay.' on noise is noise."""
        if self._spoke_at is None:
            return False
        if (self._spoke_text or "").rstrip().endswith("?"):
            return True
        return time.monotonic() - self._spoke_at <= _SHORT_WINDOW_S

    async def _run_turn(self, gen, pcm):
        try:
            t0 = time.monotonic()
            if self._stt_ctx:
                t = await asyncio.to_thread(
                    self.transcribe, self.pack(pcm),
                    expecting_short=self._expecting_short())
            else:
                t = await asyncio.to_thread(
                    self.transcribe, self.pack(pcm))
            stt_ms = int((time.monotonic() - t0) * 1000)
            heard = (t or {}).get("heard") or ""
            if not heard:
                out = {"heard": "", "noise": bool((t or {}).get("noise")),
                       "_times": {"stt_ms": stt_ms}}
            else:
                t1 = time.monotonic()
                out = await asyncio.to_thread(self.think, heard)
                out = dict(out or {})
                out["heard"] = heard
                times = dict(out.get("_times") or {})
                times.setdefault("stt_ms", stt_ms)
                times.setdefault(
                    "llm_total_ms", int((time.monotonic() - t1) * 1000))
                out["_times"] = times
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("call turn pipeline failed")
            out = {"heard": "", "reply": "", "done": False}
        self._q.put_nowait(("turn_done", gen, out))

    # -- speaker: strictly serialised playback + sentence pipelining ---
    def _enqueue_clip(self, ogg, *, tag, kind):
        self._spkq.put_nowait(("clip", ogg, tag, kind))

    def _enqueue_reply(self, text, *, tag):
        chunks = split_for_tts(text, max_chars=self.cfg.reply_chunk_chars,
                               max_chunks=self.cfg.reply_max_chunks)
        if chunks:
            self._spkq.put_nowait(("reply", chunks, tag, "reply"))

    def _stop_playing(self):
        try:
            self.stop_playback()
        except Exception:
            pass

    def _interrupt(self, info=None, diag=None):
        """Barge-in: stop the current clip NOW and drop anything queued
        — the owner's new utterance becomes the next turn; nothing waits
        in line."""
        diag = diag or {}
        log.info("call barge-in: voiced=%sms/%sms rms=%.0f floor=%.0f",
                 diag.get("voiced_ms", "?"), diag.get("window_ms", "?"),
                 diag.get("rms", 0.0), diag.get("floor", 0.0))
        self._drop = True
        while True:
            try:
                item = self._spkq.get_nowait()
            except asyncio.QueueEmpty:
                break
            if info is not None and isinstance(item[2], int):
                self._log_turn(info.pop(item[2], None), interrupted=True)
        log.info("call playback stop: barge-in")
        t0 = time.monotonic()
        self._stop_playing()
        rec = self._info.get(self._playing_tag) \
            if isinstance(self._playing_tag, int) else None
        if rec is not None and rec.get("barge_in_ms") is None:
            rec["barge_in_ms"] = int((time.monotonic() - t0) * 1000)

    async def _speaker(self):
        while True:
            item = await self._spkq.get()
            if item is None:
                return
            kind, payload, tag, akind = item
            self._drop = False
            self._playing_tag = tag
            self._playing = True
            interrupted = False
            try:
                if kind == "clip":
                    self._q.put_nowait(("audio", tag, akind))
                    await self._play_ogg(payload)
                    interrupted = self._drop
                elif kind == "stream":
                    interrupted = await self._play_stream(payload, tag)
                else:
                    interrupted = await self._play_reply(payload, tag)
            finally:
                self._playing = False
                self._playing_tag = None
            self._q.put_nowait(("item_done", tag, akind, interrupted))
            if self._spkq.empty():
                self._q.put_nowait(("idle",))

    async def _play_ogg(self, ogg, text=""):
        try:
            log.info("call playback: clip start (%d bytes)", len(ogg))
            if self.endp is not None:
                try:
                    self.endp.note_playback_started()
                except Exception:
                    pass
            await self.play(ogg)
            self._spoke_at = time.monotonic()
            if text:
                self._spoke_text = text
        except Exception:
            log.exception("call playback failed")

    async def _play_reply(self, texts, tag):
        """Speak a reply in sentence chunks: each chunk's TTS runs while
        the previous one plays, so the first words land ~one synthesis
        in instead of after the whole reply. Returns True when a
        barge-in dropped the tail."""
        pending = None
        announced = False
        rec = self._info.get(tag) if isinstance(tag, int) else None
        prev_end = None
        loop = asyncio.get_running_loop()
        for i, t in enumerate(texts):
            if self._drop:
                break
            try:
                t0 = time.monotonic()
                ogg = await pending if pending is not None else \
                    await asyncio.to_thread(self.say, t)
            except Exception:
                ogg = None
            if rec is not None:
                if rec.get("tts_first_ms") is None and ogg:
                    rec["tts_first_ms"] = int(
                        (time.monotonic() - t0) * 1000)
                    if self._say_info is not None:
                        try:
                            for k, v in (self._say_info() or {}).items():
                                rec.setdefault(k, v)
                        except Exception:
                            pass
            pending = None
            if i + 1 < len(texts):
                pending = asyncio.create_task(
                    asyncio.to_thread(self.say, texts[i + 1]))
            if ogg is None:
                continue
            if not announced:
                announced = True
                self._q.put_nowait(("audio", tag, "reply"))
            if rec is not None and prev_end is not None:
                gap = int((loop.time() - prev_end) * 1000)
                rec["gaps_ms"] = max(rec.get("gaps_ms") or 0, gap)
            await self._play_ogg(ogg, text=t)
            prev_end = loop.time()
        if pending is not None:
            pending.cancel()
        return self._drop

    async def _play_stream(self, it, tag):
        """speak a streamed reply — deltas become clauses (first
        ≤80 chars, then ≤160); clause n+1 is pulled and synthesised
        while n plays, so TTS starts while the model still streams."""
        from .callbrain import _cut_clause
        loop = asyncio.get_running_loop()
        rec = self._info.get(tag) if isinstance(tag, int) else None
        state = {"buf": "", "eof": False, "first": True}

        def _pull():
            try:
                return next(it)
            except StopIteration:
                return None

        async def _clause():
            limit = 80 if state["first"] else 160
            while True:
                cut, rest = _cut_clause(state["buf"], limit)
                if cut:
                    state["buf"] = rest
                    state["first"] = False
                    return cut
                if state["eof"]:
                    tail = state["buf"].strip()
                    state["buf"] = ""
                    return tail or None
                try:
                    d = await loop.run_in_executor(None, _pull)
                except Exception:
                    d = None
                if d is None:
                    state["eof"] = True
                else:
                    state["buf"] += d

        announced = False
        prev_end = None
        pending = None
        cur = await _clause()
        while cur is not None:
            if self._drop:
                break
            t0 = time.monotonic()
            try:
                ogg = await pending if pending is not None else \
                    await asyncio.to_thread(self.say, cur)
            except Exception:
                ogg = None
            if rec is not None:
                if rec.get("tts_first_ms") is None and ogg:
                    rec["tts_first_ms"] = int(
                        (time.monotonic() - t0) * 1000)
                    if self._say_info is not None:
                        try:
                            for k, v in (
                                    self._say_info() or {}).items():
                                rec.setdefault(k, v)
                        except Exception:
                            pass
            pending = None
            nxt = await _clause()
            if nxt is not None:
                pending = asyncio.create_task(
                    asyncio.to_thread(self.say, nxt))
            if ogg is None:
                cur = nxt
                continue
            if not announced:
                announced = True
                self._q.put_nowait(("audio", tag, "reply"))
            if rec is not None and prev_end is not None:
                gap = int((loop.time() - prev_end) * 1000)
                rec["gaps_ms"] = max(rec.get("gaps_ms") or 0, gap)
            await self._play_ogg(ogg, text=cur)
            prev_end = loop.time()
            cur = nxt
        if pending is not None:
            pending.cancel()
        return self._drop

    # -- helpers ------------------------------------------------------
    def _merge(self, old_pcm, new_pcm):
        pcm = merge_pcm(old_pcm, new_pcm, self.cfg.merge_gap_ms)
        cap = self.cfg.merge_cap_ms // FRAME_MS * FRAME_BYTES
        return pcm[-cap:] if len(pcm) > cap else pcm

    async def _deaf_notice(self):
        try:
            ogg = await asyncio.to_thread(
                self.say_cached, self.cfg.deaf_line)
            if ogg:
                self._enqueue_clip(ogg, tag="deaf", kind="clip")
        except Exception:
            pass
        if self.on_notify and self._deaf_dm:
            try:
                await asyncio.to_thread(self.on_notify, self._deaf_dm)
            except Exception:
                log.exception("deaf notify failed")

    def _log_turn(self, rec, superseded=False, interrupted=False,
                  dropped=None):
        """One `call turn:` line per utterance — including dropped ones
        (noise / STT failure / too short), so silence in a live call is
        always explainable from the logs."""
        if rec is None:
            if dropped:
                log.info("call turn: dropped=%s", dropped)
            return
        heard = rec.get("heard")
        if not heard and not (dropped or superseded):
            return
        t_end = rec.get("t_end")

        def ms(key):
            v = rec.get(key)
            if v is None or t_end is None:
                return "-"
            return f"{(v - t_end) * 1000:.0f}ms"
        log.info("call turn: heard=%r ack=%s first_audio=%s "
                 "interrupted=%s%s%s stt=%sms llm=%sms(%s) tts=%sms(%s) "
                 "gap=%sms fillers=%s engine=%s",
                 (heard or "")[:80], ms("t_ack"), ms("t_audio"),
                 bool(rec.get("interrupted") or interrupted),
                 f" dropped={dropped}" if dropped else "",
                 " superseded" if superseded else "",
                 rec.get("stt_ms") or "-",
                 rec.get("llm_total_ms") or "-",
                 rec.get("llm_backend") or "-",
                 rec.get("tts_first_ms") or "-",
                 rec.get("tts_provider") or "-",
                 rec.get("gaps_ms") if rec.get("gaps_ms") is not None
                 else "-",
                 rec.get("fillers", 0), self.engine)
        if self._metrics is not None:
            try:
                self._metrics(self._metric_row(
                    rec, interrupted=interrupted, dropped=dropped,
                    superseded=superseded))
            except Exception:
                pass

    def _metric_row(self, rec, interrupted=False, dropped=None,
                    superseded=False):
        """One call_metrics row dict for this turn."""
        t_end = rec.get("t_end")

        def rel_ms(key):
            v = rec.get(key)
            return int((v - t_end) * 1000) \
                if v is not None and t_end is not None else None
        return {
            "turn_seq": rec.get("gen") or 0,
            "heard": (rec.get("heard") or "")[:200],
            "eos_ms": rec.get("tail_ms"),
            "stt_ms": rec.get("stt_ms"),
            "llm_backend": rec.get("llm_backend"),
            "llm_first_token_ms": rec.get("llm_first_token_ms"),
            "llm_total_ms": rec.get("llm_total_ms"),
            "tts_provider": rec.get("tts_provider"),
            "tts_first_ms": rec.get("tts_first_ms"),
            "first_audio_ms": rel_ms("t_audio"),
            "gaps_ms": rec.get("gaps_ms"),
            "fillers": rec.get("fillers") or 0,
            "barge_in_ms": rec.get("barge_in_ms"),
            "interrupted": int(bool(
                rec.get("interrupted") or interrupted)),
            "dropped": dropped or ("superseded" if superseded else None),
        }

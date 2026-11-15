"""Call conversation: endpointing/VAD/transcript filters (pure), and
the asyncio Conversation against fakes — barge-in (stop playback,
supersede, no queue), filler timing, sentence pipelining, idle nudge,
wrap-up.

Frames are markers: VOICED/QUIET bytes drive a scripted VAD, and the
endpointer counts frames — endpointing tests are deterministic, no
sleeps. Conversation tests use millisecond-scale ConvConfig values.
"""

import asyncio
import struct
import unittest
import unittest.mock

from pa import callconv

FRAME = callconv.FRAME_BYTES                    # 3840 (20 ms stereo)
VOICED = b"\x10\x40" * (FRAME // 2)             # loud
QUIET = b"\x00\x00" * (FRAME // 2)              # silence


class FakeVAD:
    """is_speech = 'is this the VOICED marker frame'."""
    def is_speech(self, frame):
        return frame == VOICED


def _endpointer(**kw):
    kw.setdefault("min_speech_ms", 60)
    kw.setdefault("barge_voiced_ms", 60)
    kw.setdefault("barge_window_ms", 200)
    kw.setdefault("barge_grace_s", 0)       # tests don't wait real time
    kw.setdefault("tail_ms", 80)
    kw.setdefault("tail_after_short_ms", 120)
    kw.setdefault("short_ms", 1000)
    return callconv.Endpointer(FakeVAD(), **kw)


def _feed(ep, frames):
    out = []
    for f in frames:
        out.extend(ep.feed(f))
    return out


class TestEndpointer(unittest.TestCase):
    def test_utterance_after_tail(self):
        ep = _endpointer()
        self.assertEqual(_feed(ep, [QUIET] * 5), [])
        ev = _feed(ep, [VOICED] * 5 + [None] * 6)  # 120 ms short tail
        utt = [e for e in ev if e[0] == "utterance"]
        self.assertEqual(len(utt), 1)
        self.assertIn(VOICED, utt[0][1])

    def test_speech_event_fires_once_at_threshold(self):
        ep = _endpointer(barge_voiced_ms=60)        # 3 voiced frames
        ev = _feed(ep, [VOICED] * 10)
        self.assertEqual(sum(1 for e in ev if e[0] == "speech"), 1)
        # the event fires on the 3rd voiced frame
        ev2 = _feed(_endpointer(barge_voiced_ms=60), [VOICED] * 3)
        self.assertTrue(any(e[0] == "speech" for e in ev2))

    def test_speech_event_needs_windowed_voiced_ms(self):
        # real thresholds: 500 ms voiced inside a 700 ms window — three
        # spaced 100 ms noise bursts never add up to it, sustained
        # speech does.
        ep = _endpointer(barge_voiced_ms=500, barge_window_ms=700)
        for _ in range(3):
            ev = _feed(ep, [VOICED] * 5 + [None] * 35)
            self.assertFalse(any(e[0] == "speech" for e in ev))
        ev = _feed(ep, [VOICED] * 30)             # 600 ms sustained
        self.assertTrue(any(e[0] == "speech" for e in ev))

    def test_barge_energy_floor_gates_quiet_speech(self):
        # VAD says "speech" on frames the noise floor says are too
        # quiet to interrupt for — echo/hiss must not stop a clip.
        class RmsVAD:
            def __init__(self, t):
                self.t = t
            def is_speech(self, f):
                return callconv._rms(f) > self.t
        def frame(amp):
            return struct.pack(f"<{FRAME // 2}h",
                               *([amp] * (FRAME // 2)))
        ep = callconv.Endpointer(
            RmsVAD(700), min_speech_ms=60, barge_voiced_ms=60,
            barge_window_ms=200, barge_grace_s=0, tail_ms=80,
            tail_after_short_ms=120, short_ms=1000)
        # a noisy room: steady ~600-amplitude background is NOT speech
        # to the VAD but raises the adaptive floor to ~570 -> gate ~1140
        _feed(ep, [frame(600)] * 60)
        self.assertGreater(ep._bfloor, 400)
        # VAD-voiced but below the raised floor: not barge material
        ev = _feed(ep, [frame(900)] * 10)
        self.assertFalse(any(e[0] == "speech" for e in ev))
        # genuinely loud speech clears it
        ev = _feed(ep, [frame(5000)] * 5)
        self.assertTrue(any(e[0] == "speech" for e in ev))

    def test_barge_grace_after_playback_start(self):
        ep = _endpointer(barge_voiced_ms=60, barge_grace_s=30)
        ep.note_playback_started()                  # clip just started
        ev = _feed(ep, [VOICED] * 10)               # in grace: silent
        self.assertFalse(any(e[0] == "speech" for e in ev))
        # grace over, owner still talking -> deferred trigger still fires
        ep._no_barge_until = 0
        ev = _feed(ep, [VOICED] * 2)
        self.assertTrue(any(e[0] == "speech" for e in ev))

    def test_barge_in_off_never_interrupts(self):
        ep = _endpointer(barge_in=False)
        ev = _feed(ep, [VOICED] * 30)
        self.assertFalse(any(e[0] == "speech" for e in ev))
        # utterances still work — only the barge trigger is off
        ev = _feed(ep, [None] * 8)
        self.assertTrue(any(e[0] == "utterance" for e in ev))

    def test_too_short_utterance_logs_dropped_turn(self):
        ep = _endpointer(min_speech_ms=100)
        with self.assertLogs("pa.callconv", level="INFO") as cm:
            _feed(ep, [VOICED] * 2 + [None] * 10)
        self.assertTrue(any("call turn:" in m and "dropped=short" in m
                            for m in cm.output), cm.output)

    def test_short_burst_dropped(self):
        ep = _endpointer(min_speech_ms=300)
        ev = _feed(ep, [VOICED] * 2 + [None] * 10)  # 40 ms < 300 ms
        self.assertFalse(any(e[0] == "utterance" for e in ev))
        # and the dropped segment doesn't poison the next one
        ev = _feed(ep, [VOICED] * 20 + [None] * 6)
        self.assertTrue(any(e[0] == "utterance" for e in ev))

    def test_prebuffer_keeps_onset(self):
        ep = _endpointer(prebuf_ms=60)              # 3 quiet frames
        _feed(ep, [QUIET] * 3)
        ev = _feed(ep, [VOICED] * 4 + [None] * 6)
        pcm = next(e[1] for e in ev if e[0] == "utterance")
        self.assertTrue(pcm.startswith(QUIET))      # onset kept
        self.assertIn(VOICED, pcm)

    def test_pause_inside_tail_continues_utterance(self):
        ep = _endpointer()
        ev = _feed(ep, [VOICED] * 4 + [None] * 2 + [VOICED] * 4
                   + [None] * 6)
        utts = [e for e in ev if e[0] == "utterance"]
        self.assertEqual(len(utts), 1)              # one segment

    def test_short_speech_gets_longer_tail(self):
        # short burst (< short_ms): tail_after_short_ms (120) applies —
        # 5 silence frames (100 ms) must NOT end it, 6 must.
        ep = _endpointer()
        ev = _feed(ep, [VOICED] * 4 + [None] * 5)
        self.assertFalse(any(e[0] == "utterance" for e in ev))
        ev = _feed(ep, [None])
        self.assertTrue(any(e[0] == "utterance" for e in ev))

    def test_long_speech_gets_short_tail(self):
        # long monologue (> short_ms): tail_ms (80) applies — 4 frames
        # of silence ends it.
        ep = _endpointer()
        ev = _feed(ep, [VOICED] * 60 + [None] * 4)  # 1.2 s > short_ms
        self.assertTrue(any(e[0] == "utterance" for e in ev))

    def test_max_ms_forces_emit(self):
        ep = _endpointer(max_ms=200)                # 10 frames
        ev = _feed(ep, [VOICED] * 15)
        self.assertTrue(any(e[0] == "utterance" for e in ev))

    def test_trailing_silence_trimmed(self):
        ep = _endpointer(keep_tail_ms=0)
        ev = _feed(ep, [VOICED] * 5 + [QUIET] * 6)
        pcm = next(e[1] for e in ev if e[0] == "utterance")
        # emitted pcm is voiced frames + prebuffer only — no 6 quiet
        # frames of tail (6*3840 bytes)
        self.assertLessEqual(len(pcm), len(VOICED) * 5 + FRAME * 3)


class TestVAD(unittest.TestCase):
    def _frame(self, amp):
        return struct.pack(f"<{FRAME // 2}h",
                           *([amp] * (FRAME // 2)))

    def test_energy_vad(self):
        vad = callconv.EnergyVAD()
        self.assertFalse(vad.is_speech(self._frame(0)))
        self.assertTrue(vad.is_speech(self._frame(20000)))

    def test_energy_floor_adapts(self):
        vad = callconv.EnergyVAD()
        for _ in range(60):
            vad.is_speech(self._frame(300))         # loud-ish room
        floor = vad._floor
        self.assertGreater(floor, 200)
        # a fixed threshold would still call 600 'speech'; the raised
        # floor rejects it as background
        self.assertFalse(vad.is_speech(self._frame(600)))

    def test_make_vad_forced_and_fallback(self):
        class S:
            call_vad = "energy"
        self.assertIsInstance(callconv.make_vad(S()),
                              callconv.EnergyVAD)

    def test_make_vad_missing_webrtc_falls_back(self):
        import sys
        class S:
            call_vad = "auto"
        with unittest.mock.patch.dict(sys.modules, {"webrtcvad": None}):
            self.assertIsInstance(callconv.make_vad(S()),
                                  callconv.EnergyVAD)


class TestTranscriptFilter(unittest.TestCase):
    def test_hallucinations_dropped(self):
        for t in ("", "Thank you.", "thank you", "you", "[Music]",
                  "(music)", "♪", "Hmm.", "uh", "ok", ".", ".."):
            self.assertFalse(callconv.transcript_ok(t), repr(t))

    def test_real_speech_passes(self):
        for t in ("do you have access to my email?", "what's the time",
                  "thanks — one more thing"):
            self.assertTrue(callconv.transcript_ok(t), repr(t))

    def test_meta_vetoes(self):
        self.assertFalse(callconv.transcript_ok(
            "something", {"no_speech_prob": 0.9}))
        self.assertFalse(callconv.transcript_ok(
            "something", {"avg_logprob": -1.4}))
        self.assertTrue(callconv.transcript_ok(
            "something", {"no_speech_prob": 0.2, "avg_logprob": -0.3}))
        self.assertTrue(callconv.transcript_ok("something", {}))

    def test_wrapup(self):
        for t in ("bye", "ok thanks bye", "that's it", "thanks",
                  "thank you", "nothing else", "goodnight",
                  "great, see you later", "that's all"):
            self.assertTrue(callconv.is_wrapup(t), repr(t))
        for t in ("thanks — one more thing", "what's the weather",
                  "say bye to sam", "goodnight stories are fun", ""):
            self.assertFalse(callconv.is_wrapup(t), repr(t))

    def test_merge_pcm(self):
        a, b = VOICED * 2, VOICED * 3
        merged = callconv.merge_pcm(a, b, gap_ms=60)
        self.assertEqual(len(merged),
                         len(a) + len(b) + FRAME * 3)
        self.assertTrue(merged.startswith(a) and merged.endswith(b))


class TestSplitForTTS(unittest.TestCase):
    def test_sentences_and_markdown(self):
        text = ("Sure — **checking** now. Here's a link "
                "https://example.com/x and `code`. Second sentence!")
        chunks = callconv.split_for_tts(text, max_chars=80)
        self.assertTrue(all("http" not in c and "`" not in c
                            and "*" not in c for c in chunks))
        self.assertTrue(any("Second sentence" in c for c in chunks))

    def test_long_text_bounded(self):
        text = ". ".join(f"sentence number {i}" for i in range(30))
        chunks = callconv.split_for_tts(text, max_chars=60,
                                        max_chunks=4)
        self.assertLessEqual(len(chunks), 4)
        self.assertTrue(all(len(c) <= 120 for c in chunks))

    def test_empty(self):
        self.assertEqual(callconv.split_for_tts(""), [])
        self.assertEqual(callconv.split_for_tts("```code```"), [])


class TestFillerBank(unittest.TestCase):
    def test_warm_and_get_varied(self):
        said = []
        bank = callconv.FillerBank(
            kinds={"ack": ("one", "two", "three")})
        bank.warm(lambda t: said.append(t) or b"ogg:" + t.encode())
        self.assertEqual(said, ["one", "two", "three"])
        got = {bank.get("ack") for _ in range(10)}
        self.assertEqual(got, {b"ogg:one", b"ogg:two", b"ogg:three"})

    def test_missing_kind_and_failed_warm(self):
        bank = callconv.FillerBank(kinds={"ack": ("x",)})
        self.assertIsNone(bank.get("ack"))
        bank.warm(lambda t: None)                   # TTS down
        self.assertIsNone(bank.get("ack"))          # never synths now
        bank.add("ack", b"clip")
        self.assertEqual(bank.get("ack"), b"clip")
        self.assertTrue(bank.ready("ack"))


# ---------------------------------------------------------------------------
# Conversation — asyncio, all seams faked

class FakeIO:
    """transcribe/think/say/play/stop seams + the utterance driver."""

    def __init__(self):
        self.transcripts = []        # queued {"heard":...} dicts
        self.trans_calls = []        # wavs seen
        self.thinks = []             # heard texts seen
        self.think_replies = []      # queued {"reply","done"}
        self.think_delay = 0.0
        self.say_calls = []
        self.say_cached_calls = []   # static lines via say_cached
        self.play_calls = []         # oggs attempted
        self.finished = []           # (ogg, stopped) completed plays
        self.stops = 0
        self.active = 0              # plays currently in-flight
        self.max_active = 0          # overlap detector (must stay 1)
        self.play_seconds = 0.02     # clip length unless interrupted
        self._stop = None
        self._rel = None

    def bind(self):
        # events are created lazily inside play() — asyncio.Event()
        # binds the running loop on old Pythons, and each test's
        # asyncio.run() is a fresh loop
        self._stop = None
        self._rel = None

    def release(self):
        """Finish the current play cleanly (clip ended naturally)."""
        self._rel.set()

    def transcribe(self, wav):
        self.trans_calls.append(wav)
        if self.transcripts:
            return self.transcripts.pop(0)
        return {"heard": "", "noise": False}

    def think(self, heard):
        self.thinks.append(heard)
        if self.think_delay:
            import time as _t
            _t.sleep(self.think_delay)
        if self.think_replies:
            return self.think_replies.pop(0)
        if callconv.is_wrapup(heard):
            return {"reply": "Right — leaving you to it. Ping me if "
                             "you need me.", "done": True}
        return {"reply": "okay then.", "done": False}

    def say(self, text):
        self.say_calls.append(text)
        return b"ogg:" + text.encode()

    def say_cached(self, text):
        self.say_cached_calls.append(text)
        return self.say(text)

    async def play(self, ogg):
        self.play_calls.append(ogg)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self._stop is None:
            self._stop = asyncio.Event()
            self._rel = asyncio.Event()
        try:
            self._stop.clear()
            self._rel.clear()
            t_stop = asyncio.create_task(self._stop.wait())
            t_rel = asyncio.create_task(self._rel.wait())
            done, pending = await asyncio.wait(
                {t_stop, t_rel}, timeout=self.play_seconds,
                return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            self.finished.append((ogg, t_stop in done))
        finally:
            self.active -= 1

    def stop_playback(self):
        self.stops += 1
        if self._stop is not None:
            self._stop.set()


def _cfg(**kw):
    base = dict(ack_delay=(0.04, 0.04), progress_first=0.15,
                progress_every=0.25, idle_nudge=0.3, idle_end=0.2,
                tick=0.02)
    base.update(kw)
    return callconv.ConvConfig(**base)


def _conv(io_, cfg=None, **kw):
    ep = kw.pop("endpointer", None) or _endpointer()
    io_.bind()
    c = callconv.Conversation(
        transcribe=io_.transcribe, think=io_.think, say=io_.say,
        say_cached=io_.say_cached,
        play=io_.play, stop_playback=io_.stop_playback,
        config=cfg or _cfg(), endpointer=ep, **kw)
    return c


async def _until(pred, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.005)
    return False


async def _feed_utt(conv, voiced=6, tail=8, gap=0.001):
    for _ in range(voiced):
        conv.feed_frame(VOICED)
        await asyncio.sleep(gap)
    for _ in range(tail):
        conv.feed_frame(None)
        await asyncio.sleep(gap)


def _run_conv(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


class TestConversation(unittest.TestCase):
    def _run(self, conv, script, seconds=8):
        async def main():
            task = asyncio.create_task(
                conv.run(asyncio.get_running_loop().time() + seconds))
            await script(conv)
            return await task
        return _run_conv(main())

    def test_turn_transcribe_think_speak(self):
        io_ = FakeIO()
        io_.transcripts = [{"heard": "do you have my email?"}]
        io_.think_replies = [{"reply": "Yes I do.", "done": False}]
        conv = _conv(io_)

        async def script(conv):
            await _feed_utt(conv)
            self.assertTrue(await _until(lambda: io_.play_calls))
            conv.request_stop("test_done")

        res = self._run(conv, script)
        self.assertEqual(io_.thinks, ["do you have my email?"])
        self.assertTrue(any(b"Yes I do." in o for o in io_.play_calls))
        self.assertEqual(res["turns"], 1)
        self.assertEqual(res["heard"], ["do you have my email?"])

    def test_noise_utterance_ignored(self):
        io_ = FakeIO()
        io_.transcripts = [{"heard": "", "noise": True}]
        conv = _conv(io_)

        async def script(conv):
            await _feed_utt(conv)
            self.assertTrue(await _until(
                lambda: len(io_.trans_calls) == 1))
            await asyncio.sleep(0.05)
            conv.request_stop("test_done")

        self._run(conv, script)
        self.assertEqual(io_.thinks, [])              # no turn ran
        self.assertFalse(io_.play_calls)              # nothing spoken

    def test_barge_in_stops_playback_no_queue(self):
        io_ = FakeIO()
        io_.transcripts = [{"heard": "question one"},
                           {"heard": "question two"}]
        io_.think_replies = [
            {"reply": "first chunk. second chunk. third chunk.",
             "done": False},
            {"reply": "second answer.", "done": True}]
        io_.play_seconds = 5.0                        # replies block
        conv = _conv(io_, cfg=_cfg(reply_chunk_chars=20))

        async def script(conv):
            await _feed_utt(conv)
            self.assertTrue(await _until(
                lambda: any(b"first chunk" in o
                            for o in io_.play_calls)))
            # owner talks over pal: >barge_voiced_ms in the window -> stop NOW
            for _ in range(4):
                conv.feed_frame(VOICED)
                await asyncio.sleep(0.001)
            self.assertTrue(await _until(lambda: io_.stops >= 1))
            await _feed_utt(conv, voiced=0, tail=8)
            self.assertTrue(await _until(
                lambda: any(b"second answer" in o
                            for o in io_.play_calls), timeout=4))
            io_.release()          # wrap-up reply finishes -> bye end

        res = self._run(conv, script)
        played = [o for o in io_.play_calls]
        # reply 1 was interrupted mid-play; its later chunks never
        # played (no FIFO queue of pending replies)
        self.assertFalse(any(b"second chunk" in o or b"third chunk" in o
                             for o in played))
        self.assertTrue(any(b"second answer" in o for o in played))
        self.assertGreaterEqual(io_.stops, 1)
        self.assertEqual(res["interrupted"], 1)
        self.assertEqual(res["reason"], "bye")

    def test_supersede_merges_no_stale_reply(self):
        io_ = FakeIO()
        io_.think_delay = 0.25            # turn 1 still running on U2
        io_.transcripts = [{"heard": "part one"},
                           {"heard": "part one and part two"}]
        io_.think_replies = [{"reply": "stale answer.", "done": False},
                             {"reply": "merged answer.", "done": False}]
        conv = _conv(io_)

        async def script(conv):
            await _feed_utt(conv)
            await asyncio.sleep(0.05)          # turn 1 in-flight
            await _feed_utt(conv)          # supersedes + merges
            self.assertTrue(await _until(
                lambda: len(io_.trans_calls) == 2))
            self.assertTrue(await _until(
                lambda: any(b"merged answer" in o
                            for o in io_.play_calls)))
            conv.request_stop("test_done")

        self._run(conv, script)
        # the second transcribe saw the MERGED audio (utterance 1 + gap
        # + utterance 2), and only the merged turn produced a reply
        self.assertGreater(len(io_.trans_calls[1]),
                           len(io_.trans_calls[0]))
        self.assertFalse(any(b"stale answer" in o
                             for o in io_.play_calls))
        self.assertTrue(any(b"merged answer" in o
                            for o in io_.play_calls))
        # both think calls may have run (one thread can't be killed);
        # the stale one's reply is discarded
        self.assertIn("part one and part two", io_.thinks[-1])

    def test_ack_filler_for_slow_turn(self):
        io_ = FakeIO()
        io_.think_delay = 0.2
        io_.transcripts = [{"heard": "check my email"}]
        bank = callconv.FillerBank(kinds={})
        bank.add("ack", b"ACK-CLIP")
        conv = _conv(io_, bank=bank)

        async def script(conv):
            await _feed_utt(conv)
            self.assertTrue(await _until(
                lambda: b"ACK-CLIP" in io_.play_calls))
            conv.request_stop("test_done")

        self._run(conv, script)
        self.assertLess(io_.play_calls.index(b"ACK-CLIP"),
                        len(io_.play_calls))
        # the ack played before the reply could be spoken
        self.assertEqual(io_.play_calls[0], b"ACK-CLIP")

    def test_quick_turn_no_filler(self):
        io_ = FakeIO()
        io_.transcripts = [{"heard": "quick one"}]
        bank = callconv.FillerBank(kinds={})
        bank.add("ack", b"ACK-CLIP")
        conv = _conv(io_, cfg=_cfg(ack_delay=(0.5, 0.5)), bank=bank)

        async def script(conv):
            await _feed_utt(conv)
            self.assertTrue(await _until(
                lambda: any(b"okay then" in o
                            for o in io_.play_calls)))
            conv.request_stop("test_done")

        self._run(conv, script)
        self.assertNotIn(b"ACK-CLIP", io_.play_calls)

    def test_progress_line_once_for_long_turn(self):
        io_ = FakeIO()
        io_.think_delay = 0.35
        io_.transcripts = [{"heard": "pull up the logs"}]
        bank = callconv.FillerBank(kinds={})
        bank.add("progress", b"PROG")
        conv = _conv(io_, bank=bank)

        async def script(conv):
            await _feed_utt(conv)
            self.assertTrue(await _until(
                lambda: any(b"okay then" in o
                            for o in io_.play_calls), timeout=4))
            conv.request_stop("test_done")

        self._run(conv, script)
        self.assertEqual(io_.play_calls.count(b"PROG"), 1)

    def test_sentence_pipelining(self):
        io_ = FakeIO()
        io_.transcripts = [{"heard": "tell me"}]
        io_.think_replies = [{
            "reply": "First part here now. Second part follows after.",
            "done": False}]
        io_.play_seconds = 0.2
        conv = _conv(io_, cfg=_cfg(reply_chunk_chars=30))

        async def script(conv):
            await _feed_utt(conv)
            # first chunk playing; second must already be synthesising
            self.assertTrue(await _until(
                lambda: any(b"First part" in o
                            for o in io_.play_calls)))
            self.assertTrue(await _until(
                lambda: len(io_.say_calls) >= 2))
            self.assertTrue(await _until(
                lambda: any(b"Second part" in o
                            for o in io_.play_calls)))
            conv.request_stop("test_done")

        self._run(conv, script)
        played = io_.play_calls
        order = [played.index(next(o for o in played
                                   if b"First part" in o)),
                 played.index(next(o for o in played
                                   if b"Second part" in o))]
        self.assertEqual(order, sorted(order))     # in order
        # pipelined: the second synthesis ran while the first played
        self.assertEqual(io_.say_calls[:2],
                         ["First part here now.",
                          "Second part follows after."])

    def test_idle_nudge_then_end(self):
        io_ = FakeIO()
        bank = callconv.FillerBank(kinds={})
        bank.add("checkin", b"STILL-THERE")
        conv = _conv(io_, cfg=_cfg(idle_nudge=0.1, idle_end=0.1),
                     bank=bank)

        async def script(conv):
            pass                                     # owner silent

        res = self._run(conv, script, seconds=5)
        self.assertIn(b"STILL-THERE", io_.play_calls)
        self.assertTrue(any(b"leave it there" in o
                            for o in io_.play_calls))
        self.assertEqual(res["reason"], "idle")

    def test_wrapup_bye(self):
        io_ = FakeIO()
        io_.transcripts = [{"heard": "thanks, that's it"}]
        conv = _conv(io_)      # think default: wrap-up -> done

        async def script(conv):
            await _feed_utt(conv)

        res = self._run(conv, script, seconds=5)
        self.assertEqual(res["reason"], "bye")
        self.assertTrue(any(b"leaving you to it" in o
                            for o in io_.play_calls))

    def test_request_stop_owner_left(self):
        io_ = FakeIO()
        conv = _conv(io_)

        async def script(conv):
            await asyncio.sleep(0.05)
            conv.request_stop("owner_left")

        res = self._run(conv, script)
        self.assertEqual(res["reason"], "owner_left")

    def test_opening_speaks_first(self):
        io_ = FakeIO()
        conv = _conv(io_)

        async def main():
            task = asyncio.create_task(conv.run(
                asyncio.get_running_loop().time() + 3,
                opening="hello there"))
            self.assertTrue(await _until(lambda: io_.play_calls))
            conv.request_stop("test_done")
            await task

        _run_conv(main())
        self.assertEqual(io_.say_calls[0], "hello there")
        self.assertTrue(any(b"hello there" in o
                            for o in io_.play_calls))

    def test_playback_strictly_sequential(self):
        # clips + a multi-chunk reply back to back: every play() runs
        # alone, in order, and nothing kills a clip because the next
        # one is ready (that was the live 'glitchy cut-off' symptom)
        io_ = FakeIO()
        io_.play_seconds = 0.03
        conv = _conv(io_, cfg=_cfg(reply_chunk_chars=20))

        async def script(conv):
            await asyncio.sleep(0.02)
            conv._enqueue_clip(b"c1", tag="x", kind="ack")
            conv._enqueue_clip(b"c2", tag="x", kind="ack")
            conv._enqueue_reply(
                "first part here. second part follows after it.",
                tag=9)
            self.assertTrue(await _until(
                lambda: len(io_.finished) >= 5))   # 2 clips + 3 chunks
            conv.request_stop("test_done")

        self._run(conv, script)
        self.assertEqual(io_.max_active, 1)
        self.assertEqual(io_.play_calls[:2], [b"c1", b"c2"])
        # natural ends — the queued clips were never stopped for being
        # behind schedule
        self.assertFalse(any(stopped for _o, stopped in io_.finished))

    def test_short_noise_bursts_dont_barge_but_speech_does(self):
        io_ = FakeIO()
        io_.play_seconds = 5.0                      # clip stays playing
        io_.transcripts = [{"heard": ""}] * 5       # bursts -> noise
        ep = callconv.Endpointer(
            FakeVAD(), min_speech_ms=60, barge_voiced_ms=120,
            barge_window_ms=180, barge_grace_s=0, tail_ms=80,
            tail_after_short_ms=120, short_ms=1000)
        conv = _conv(io_, endpointer=ep)

        async def script(conv):
            await asyncio.sleep(0.02)
            conv._enqueue_clip(b"clip", tag="x", kind="ack")
            self.assertTrue(await _until(lambda: io_.play_calls))
            # three spaced 60 ms bursts: max 60 ms voiced in any 180 ms
            # window — under the 120 ms barge bar, playback keeps going
            for _ in range(3):
                for _ in range(3):
                    conv.feed_frame(VOICED)
                    await asyncio.sleep(0.001)
                for _ in range(12):
                    conv.feed_frame(None)
                    await asyncio.sleep(0.001)
            await asyncio.sleep(0.05)
            self.assertEqual(io_.stops, 0)
            # 160 ms sustained owner speech IS a real barge-in
            for _ in range(8):
                conv.feed_frame(VOICED)
                await asyncio.sleep(0.001)
            self.assertTrue(await _until(lambda: io_.stops >= 1))
            conv.request_stop("test_done")

        self._run(conv, script)
        self.assertGreaterEqual(io_.stops, 1)
        # the clip that was barged shows up as a stopped play
        self.assertTrue(any(stopped for _o, stopped in io_.finished))

    def test_barge_in_disabled_never_stops(self):
        io_ = FakeIO()
        io_.play_seconds = 5.0
        ep = callconv.Endpointer(
            FakeVAD(), barge_in=False, min_speech_ms=60,
            barge_voiced_ms=60, barge_window_ms=200, barge_grace_s=0,
            tail_ms=80, tail_after_short_ms=120, short_ms=1000)
        conv = _conv(io_, endpointer=ep)

        async def script(conv):
            await asyncio.sleep(0.02)
            conv._enqueue_clip(b"clip", tag="x", kind="ack")
            self.assertTrue(await _until(lambda: io_.play_calls))
            # a full second of owner speech — playback untouched
            for _ in range(50):
                conv.feed_frame(VOICED)
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.05)
            self.assertEqual(io_.stops, 0)
            # but the utterance still endpointed into a turn
            await _feed_utt(conv, voiced=0, tail=8)
            self.assertTrue(await _until(
                lambda: len(io_.trans_calls) >= 1))
            conv.request_stop("test_done")

        self._run(conv, script)
        # the only stop is the run-end cleanup of the still-playing
        # clip — owner speech never triggered a barge-in
        self.assertLessEqual(io_.stops, 1)

    def test_dropped_turn_logs_call_turn_line(self):
        io_ = FakeIO()
        io_.transcripts = [{"heard": "", "noise": True}]
        conv = _conv(io_)

        async def script(conv):
            await _feed_utt(conv)
            self.assertTrue(await _until(
                lambda: len(io_.trans_calls) == 1))
            await asyncio.sleep(0.05)
            conv.request_stop("test_done")

        with self.assertLogs("pa.callconv", level="INFO") as cm:
            self._run(conv, script)
        self.assertTrue(any("call turn:" in m and "dropped=noise" in m
                            for m in cm.output), cm.output)

    # -- V6 filler discipline -----------------------------------------
    def _bank(self):
        bank = callconv.FillerBank()
        bank.add("ack", b"ACKCLIP")
        bank.add("progress", b"PROGCLIP")
        return bank

    def _filler_plays(self, io_):
        return [o for o in io_.play_calls
                if o in (b"ACKCLIP", b"PROGCLIP")]

    def test_fillers_capped_at_two(self):
        """12 s turn: ack + one progress, progress_every=0 stops the
        'still pulling that up' loop that used to fire every 8 s."""
        io_ = FakeIO()
        io_.transcripts = [{"heard": "slow question"}]
        io_.think_replies = [{"reply": "done.", "done": True}]
        io_.think_delay = 0.6            # scaled-down 12 s think
        io_.release_after = None
        cfg = _cfg(ack_delay=(0.05, 0.05), progress_first=0.15,
                   progress_every=0.0, tick=0.01)
        conv = _conv(io_, cfg=cfg, bank=self._bank())

        async def script(conv):
            await _feed_utt(conv)
            self.assertTrue(await _until(
                lambda: any(b"done." in o for o in io_.play_calls),
                timeout=4))
            conv.request_stop("test_done")

        self._run(conv, script)
        fillers = self._filler_plays(io_)
        self.assertEqual(fillers, [b"ACKCLIP", b"PROGCLIP"])

    def test_fast_turn_gets_no_fillers(self):
        """0.8 s turn (scaled): answer lands before the ack timer —
        no filler at all."""
        io_ = FakeIO()
        io_.transcripts = [{"heard": "quick question"}]
        io_.think_replies = [{"reply": "fast.", "done": True}]
        io_.think_delay = 0.02
        cfg = _cfg(ack_delay=(0.15, 0.15), progress_first=0.3,
                   progress_every=0.0, tick=0.01)
        conv = _conv(io_, cfg=cfg, bank=self._bank())

        async def script(conv):
            await _feed_utt(conv)
            self.assertTrue(await _until(
                lambda: any(b"fast." in o for o in io_.play_calls)))
            await asyncio.sleep(0.25)
            conv.request_stop("test_done")

        self._run(conv, script)
        self.assertEqual(self._filler_plays(io_), [])

    def test_hard_cap_with_rearming_progress(self):
        """max_fillers=2 holds even when progress re-arms."""
        io_ = FakeIO()
        io_.transcripts = [{"heard": "slow question"}]
        io_.think_replies = [{"reply": "done.", "done": True}]
        io_.think_delay = 0.7
        cfg = _cfg(ack_delay=(0.05, 0.05), progress_first=0.1,
                   progress_every=0.1, max_fillers=2, tick=0.01)
        conv = _conv(io_, cfg=cfg, bank=self._bank())

        async def script(conv):
            await _feed_utt(conv)
            self.assertTrue(await _until(
                lambda: any(b"done." in o for o in io_.play_calls),
                timeout=4))
            conv.request_stop("test_done")

        self._run(conv, script)
        self.assertLessEqual(len(self._filler_plays(io_)), 2)

    def test_warm_runs_after_opening(self):
        """The bank warms only once the greeting has played — never on
        the critical path."""
        io_ = FakeIO()
        warmed = []
        conv = _conv(io_, warm=lambda: warmed.append(True))

        async def script(conv):
            self.assertTrue(await _until(lambda: io_.play_calls))
            self.assertTrue(await _until(lambda: warmed, timeout=2))
            conv.request_stop("test_done")

        self._run(conv, script)
        self.assertEqual(warmed, [True])

    def test_nudge_falls_back_to_say_cached(self):
        io_ = FakeIO()
        conv = _conv(io_, cfg=_cfg(idle_nudge=0.1, idle_end=0.1),
                     bank=callconv.FillerBank(kinds={}))

        async def script(conv):
            pass

        self._run(conv, script, seconds=5)
        # no bank clips: the checkin line went through say_cached —
        # a static line, disk-cached instead of burning quota per call
        self.assertIn("Still there?", io_.say_cached_calls)


if __name__ == "__main__":
    unittest.main()

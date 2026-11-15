"""final-1 V3: receive-path fixes — RTP padding strip before DAVE,
Opus silence passthrough, the context-aware short-reply filter, and
the snappier end-of-speech tails."""

import struct
import unittest
import unittest.mock

from pa import call as callmod
from pa import callconv


def _rtp(payload, pad=0, ccrc=0, ext=b""):
    """Craft an RTP packet: v2 header (+P when pad), optional extension
    block, payload, zero-padding ending in the pad count."""
    first = 0x80 | ccrc | (0x20 if pad else 0) | (0x10 if ext else 0)
    # v2 header: flags+PT | seq(2) | timestamp(4) | ssrc(4) = 12 bytes
    hdr = bytes([first, 0x60]) + b"\x00\x01" + b"\x00\x00\x00\x02" + \
        b"\xde\xad\xbe\xef"
    hdr += b"\xca\x01\xca\x02" * ccrc      # CSRC list (4 bytes each)
    if ext:
        hdr += b"\xbe\xde" + struct.pack(">H", len(ext) // 4) + ext
    pkt = hdr + payload
    if pad:
        pkt += bytes(pad - 1) + bytes([pad])
    return pkt


class TestRtpPadding(unittest.TestCase):
    def setUp(self):
        callmod._dave_rx_reset()

    def test_unpadded_passthrough(self):
        data = _rtp(b"opus-data")
        out, stripped = callmod._rtp_strip_padding(data)
        self.assertIs(out, data)
        self.assertFalse(stripped)

    def test_padded_strips_zeros_and_count(self):
        payload = b"\xf8\xff\xfe" + b"xx"      # 5-byte fake payload
        data = _rtp(payload, pad=4)
        out, stripped = callmod._rtp_strip_padding(data)
        self.assertTrue(stripped)
        # pad bytes + count off, P bit cleared in the header
        self.assertEqual(out, bytes([data[0] & ~0x20]) + data[1:-4])
        self.assertEqual(callmod._rtp_payload(out), payload)

    def test_pad_zero_and_oversize_rejected(self):
        for bad in (_rtp(b"ab") + b"\x00",           # count 0
                    _rtp(b"ab") + b"\xff"):          # count > len
            out, stripped = callmod._rtp_strip_padding(bad)
            self.assertFalse(stripped)
            self.assertIs(out, bad)

    def test_nonzero_pad_bytes_rejected(self):
        data = b"\xb0\x60" + b"padme!!!ab\x03"  # 'b0..' P set, junk pad
        out, stripped = callmod._rtp_strip_padding(data)
        self.assertFalse(stripped)

    def test_first_byte_without_p_bit_untouched(self):
        out, stripped = callmod._rtp_strip_padding(b"\x80\x60....")
        self.assertFalse(stripped)


class TestRtpPayloadParse(unittest.TestCase):
    def test_plain_header(self):
        self.assertEqual(callmod._rtp_payload(_rtp(b"PAYLOAD")), b"PAYLOAD")

    def test_csrcs_skipped(self):
        self.assertEqual(
            callmod._rtp_payload(_rtp(b"PAYLOAD", ccrc=1)), b"PAYLOAD")

    def test_extension_skipped(self):
        self.assertEqual(
            callmod._rtp_payload(_rtp(b"PAYLOAD", ext=b"\x01\x02\x03\x04")),
            b"PAYLOAD")

    def test_non_rtp_returns_whole(self):
        self.assertEqual(callmod._rtp_payload(b"\xf8\xff\xfe"),
                         b"\xf8\xff\xfe")


class TestSilencePassthrough(unittest.TestCase):
    """The decode() shim: silence goes to orig untouched (no DAVE),
    padded frames are stripped first, real frames reach _dave_decrypt."""

    def _shim(self):
        """Rebuild the decode() closure against fakes, like
        _patch_dave_decode does."""
        import sys, types
        calls = {"orig": [], "dave": []}
        vr = types.ModuleType("discord.ext.voice_recv.opus")
        vr.OPUS_SILENCE = callmod._OPUS_SILENCE

        class PD:
            def _decode_packet(self, packet):
                calls["orig"].append(packet.decrypted_data)
                return "decoded"

        vr.PacketDecoder = PD
        sys.modules["discord.ext.voice_recv.opus"] = vr
        sys.modules["discord.ext.voice_recv.rtp"] = \
            types.SimpleNamespace(OPUS_SILENCE=callmod._OPUS_SILENCE)
        sys.modules["davey"] = types.SimpleNamespace(
            MediaType=types.SimpleNamespace(audio="audio"))
        davey = sys.modules["davey"]

        orig = PD._decode_packet

        def decode(self, packet):
            # mirror of the shim body in _patch_dave_decode
            try:
                data = getattr(packet, "decrypted_data", None)
                if packet is not None and data:
                    data, was_padded = callmod._rtp_strip_padding(data)
                    if was_padded:
                        packet.decrypted_data = data
                        callmod._DAVE_RX["padded"] += 1
                    if callmod._rtp_payload(data) == callmod._OPUS_SILENCE:
                        packet.decrypted_data = callmod._OPUS_SILENCE
                        callmod._DAVE_RX["silence"] += 1
                        return orig(self, packet)
                if packet is not None and data and len(data) > 3:
                    calls["dave"].append(data)
            except Exception:
                pass
            return orig(self, packet)
        return decode, calls, PD

    def setUp(self):
        callmod._dave_rx_reset()

    def _packet(self, data):
        return type("P", (), {"decrypted_data": data})()

    def test_silence_never_touches_dave(self):
        decode, calls, PD = self._shim()
        p = self._packet(callmod._OPUS_SILENCE)
        decode(PD(), p)
        self.assertEqual(calls["dave"], [])
        self.assertEqual(calls["orig"], [callmod._OPUS_SILENCE])
        self.assertEqual(callmod._dave_rx_stats()["silence"], 1)

    def test_silence_inside_rtp_header(self):
        decode, calls, PD = self._shim()
        p = self._packet(_rtp(callmod._OPUS_SILENCE))
        decode(PD(), p)
        self.assertEqual(calls["dave"], [])
        self.assertEqual(callmod._dave_rx_stats()["silence"], 1)

    def test_padded_frame_stripped_before_dave(self):
        decode, calls, PD = self._shim()
        body = b"\xde\xad\xbe\xef" * 4
        p = self._packet(_rtp(body, pad=5))
        decode(PD(), p)
        self.assertEqual(calls["dave"], [_rtp(body)])
        stats = callmod._dave_rx_stats()
        self.assertEqual(stats["padded"], 1)
        self.assertEqual(stats["silence"], 0)

    def test_plain_frame_reaches_dave(self):
        decode, calls, PD = self._shim()
        p = self._packet(_rtp(b"real-opus"))
        decode(PD(), p)
        self.assertEqual(calls["dave"], [_rtp(b"real-opus")])
        self.assertEqual(callmod._dave_rx_stats()["padded"], 0)


class TestShortReplyFilter(unittest.TestCase):
    def test_dropped_without_context(self):
        for t in ("Okay.", "yeah", "sure", "no", "OK"):
            self.assertFalse(callconv.transcript_ok(t), t)

    def test_kept_when_expected(self):
        for t in ("Okay.", "yeah", "no", "nope"):
            self.assertTrue(
                callconv.transcript_ok(t, expecting_short=True), t)

    def test_other_hallucinations_still_dropped_even_expected(self):
        for t in ("Thank you.", "you", ".", "music"):
            self.assertFalse(
                callconv.transcript_ok(t, expecting_short=True), t)

    def test_real_text_unaffected(self):
        self.assertTrue(callconv.transcript_ok("tell me a story"))
        self.assertTrue(callconv.transcript_ok(
            "okay tell me a story"))


class _FakeVAD:
    def __init__(self, pattern):
        self.pattern = list(pattern)

    def is_speech(self, frame):
        return self.pattern.pop(0) if self.pattern else False


class _LoudVAD:
    """Always-speech VAD with a loud frame for endpointer tests."""
    def __init__(self, speech=True):
        self.speech = speech

    def is_speech(self, frame):
        return self.speech


def _loud_frame():
    return b"\x7f\x00" * 1920


class TestEndpointerTails(unittest.TestCase):
    def test_new_defaults(self):
        e = callconv.Endpointer(_LoudVAD())
        self.assertEqual(e.tail_ms, 500)
        self.assertEqual(e.tail_after_short_ms, 700)

    def test_long_speech_tail_500(self):
        """>short_ms of speech then silence: utterance emits after
        tail_ms of quiet (25 frames at 20 ms)."""
        e = callconv.Endpointer(_LoudVAD(), tail_ms=500,
                                tail_after_short_ms=700,
                                barge_in=False)
        loud = _loud_frame()
        events = []
        for _ in range(150):                     # 3 s of speech
            events += e.feed(loud)
        emitted = None
        frames = 0
        while emitted is None and frames < 100:
            frames += 1
            evs = e.feed(None)
            for ev in evs:
                if ev[0] == "utterance":
                    emitted = ev
        self.assertIsNotNone(emitted)
        self.assertEqual(frames, 25)             # 500 ms tail
        self.assertEqual(emitted[2]["tail_ms"], 500)

    def test_short_burst_tail_700(self):
        e = callconv.Endpointer(_LoudVAD(), tail_ms=500,
                                tail_after_short_ms=700,
                                short_ms=2000, barge_in=False)
        loud = _loud_frame()
        for _ in range(50):                      # 1 s of speech (<short)
            e.feed(loud)
        frames = 0
        emitted = None
        while emitted is None and frames < 100:
            frames += 1
            for ev in e.feed(None):
                if ev[0] == "utterance":
                    emitted = ev
        self.assertIsNotNone(emitted)
        self.assertEqual(frames, 35)             # 700 ms tail


class TestExpectingShortWiring(unittest.TestCase):
    def test_expecting_short_context(self):
        """Conversation passes expecting_short based on pal's last
        spoken line + recency."""
        conv = callconv.Conversation(
            transcribe=lambda wav, expecting_short=False: {
                "heard": "", "noise": False},
            think=lambda h: {"reply": ""},
            say=lambda t: b"x", play=lambda o: None,
            stop_playback=lambda: None)
        self.assertFalse(conv._expecting_short())
        conv._spoke_at = callconv.time.monotonic()
        conv._spoke_text = "Tell me a fact."
        self.assertTrue(conv._expecting_short())      # recent speech
        conv._spoke_at -= 20                           # 20 s ago
        self.assertFalse(conv._expecting_short())
        conv._spoke_text = "Want me to check?"
        self.assertTrue(conv._expecting_short())       # question keeps it

    def test_run_turn_passes_flag(self):
        seen = {}

        def transcribe(wav, expecting_short=False):
            seen["es"] = expecting_short
            return {"heard": "x", "noise": False}

        conv = callconv.Conversation(
            transcribe=transcribe,
            think=lambda h: {"reply": "ok"},
            say=lambda t: b"x", play=lambda o: None,
            stop_playback=lambda: None)
        conv._spoke_at = callconv.time.monotonic()
        conv._spoke_text = "Ready?"
        import asyncio
        async def main():
            conv._q = asyncio.Queue()
            await conv._run_turn(1, b"pcm")
        asyncio.run(main())
        self.assertTrue(seen["es"])

    def test_stt_ctx_detection(self):
        conv = callconv.Conversation(
            transcribe=lambda wav, expecting_short=False: {},
            think=lambda h: {}, say=lambda t: None,
            play=lambda o: None, stop_playback=lambda: None)
        self.assertTrue(conv._stt_ctx)
        conv2 = callconv.Conversation(
            transcribe=lambda wav: {},
            think=lambda h: {}, say=lambda t: None,
            play=lambda o: None, stop_playback=lambda: None)
        self.assertFalse(conv2._stt_ctx)


class TestTailConfig(unittest.TestCase):
    def test_env_overrides(self):
        from pa.config import load_settings
        s = load_settings({"PA_CALL_TAIL_MS": "400",
                           "PA_CALL_TAIL_SHORT_MS": "900"})
        self.assertEqual(s.call_tail_ms, 400)
        self.assertEqual(s.call_tail_short_ms, 900)
        s = load_settings({})
        self.assertEqual(s.call_tail_ms, 500)
        self.assertEqual(s.call_tail_short_ms, 700)


if __name__ == "__main__":
    unittest.main()

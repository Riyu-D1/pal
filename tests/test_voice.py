"""Voice: TTS fallback chain, native voice-message payload,
transcription routing, call flow + rate limits (voice lib mocked).

Nothing here needs ffmpeg or real provider keys: conversion is stubbed
through the `convert` seam and provider functions are replaced.
"""

import base64
import json
import os
import re
import tempfile
import threading
import unittest
import unittest.mock

from pa import call as callmod
from pa import callconv, chattools, db as dbmod, discordbot
from pa import voice as voicemod
from tests.helpers import Fixture, TmpEnv, wait_for

OWNER = "owner-1"


def _settings(**env):
    t = TmpEnv(**env)
    return t, t.settings


# ---------------------------------------------------------------------------
# TTS chain

class TestTTSChain(unittest.TestCase):
    def setUp(self):
        self.env, self.s = _settings()
        self.calls = []
        self._orig = dict(voicemod._TTS)
        self._cool = dict(voicemod._COOLDOWNS)
        voicemod._COOLDOWNS.clear()

    def tearDown(self):
        voicemod._TTS.update(self._orig)
        for k in set(voicemod._TTS) - set(self._orig):
            del voicemod._TTS[k]
        voicemod._COOLDOWNS.clear()
        voicemod._COOLDOWNS.update(self._cool)

    def _plug(self, name, keys, fn):
        voicemod._TTS[name] = (keys, fn)

    def test_order_and_missing_config_skip(self):
        def gem(_s, e, text, style):
            self.calls.append("gemini")
            return b"audio", ()
        def fish(_s, e, text, style):
            self.calls.append("fish")
            return b"audio", ()
        self._plug("gemini", ("GEMINI_API_KEY",), gem)
        self._plug("fish", ("FISH_API_KEY",), fish)
        self.s.tts_order = "gemini,fish"
        env = {"FISH_API_KEY": "k"}          # gemini key missing -> skip
        ogg, dur, wave, provider = voicemod.synthesize(
            self.s, env, "hello", convert=lambda a, ia: (b"o", 1.0, "w"))
        self.assertEqual(provider, "fish")
        self.assertEqual(self.calls, ["fish"])
        self.assertEqual((ogg, dur, wave), (b"o", 1.0, "w"))

    def test_error_falls_through_and_cools(self):
        def bad(_s, e, text, style):
            self.calls.append("gemini")
            raise voicemod.VoiceError("nope", kind="rate_limited")
        def good(_s, e, text, style):
            self.calls.append("fish")
            return b"audio", ()
        self._plug("gemini", ("GEMINI_API_KEY",), bad)
        self._plug("fish", ("FISH_API_KEY",), good)
        self.s.tts_order = "gemini,fish"
        env = {"GEMINI_API_KEY": "k", "FISH_API_KEY": "k"}
        _o, _d, _w, provider = voicemod.synthesize(
            self.s, env, "hi", convert=lambda a, ia: (b"o", 1, "w"))
        self.assertEqual(provider, "fish")
        # gemini is now cooling: a second call skips it entirely
        voicemod.synthesize(self.s, env, "hi",
                            convert=lambda a, ia: (b"o", 1, "w"))
        self.assertEqual(self.calls, ["gemini", "fish", "fish"])

    def test_all_fail_raises_with_reasons(self):
        self._plug("gemini", ("GEMINI_API_KEY",),
                   lambda _s, e, t, st: (_ for _ in ()).throw(
                       voicemod.VoiceError("x", kind="bad_response")))
        self.s.tts_order = "gemini,fish"
        with self.assertRaises(voicemod.VoiceError) as cm:
            voicemod.synthesize(self.s, {"GEMINI_API_KEY": "k"}, "hi",
                                convert=lambda a, ia: (b"", 0, ""))
        self.assertIn("gemini:bad_response", str(cm.exception))
        self.assertIn("fish:needs config", str(cm.exception))


# ---------------------------------------------------------------------------
# CallTTS — one provider per call + the persistent clip cache

class TestCallTTS(unittest.TestCase):
    def setUp(self):
        _t, self.s = _settings()
        self.calls = []
        self._orig = dict(voicemod._TTS)
        voicemod._COOLDOWNS.clear()
        self.cache = tempfile.mkdtemp(prefix="pa-vcache-")

    def tearDown(self):
        voicemod._TTS.clear()
        voicemod._TTS.update(self._orig)
        voicemod._COOLDOWNS.clear()

    def _plug(self, name, keys, fn):
        voicemod._TTS[name] = (keys, fn)

    def _tts(self, order, env):
        return voicemod.CallTTS(
            self.s, env, cache_dir=self.cache, order=order,
            convert=lambda a, ia: (a, 1.0, "w"))

    def test_cached_clip_synthesises_once_ever(self):
        def cf(_s, e, text, style):
            self.calls.append(text)
            return b"AUD:" + text.encode(), ()
        self._plug("cloudflare", ("CF_KEY",), cf)
        env = {"CF_KEY": "k"}
        t1 = self._tts(["cloudflare"], env)
        self.assertEqual(t1.say_cached("Mm-hm."), b"AUD:Mm-hm.")
        self.assertEqual(self.calls, ["Mm-hm."])
        # second call, fresh CallTTS, same cache dir — NO provider call
        t2 = self._tts(["cloudflare"], env)
        self.assertEqual(t2.say_cached("Mm-hm."), b"AUD:Mm-hm.")
        self.assertEqual(self.calls, ["Mm-hm."])
        # a different static line synthesises once, then caches too
        self.assertEqual(t2.say_cached("Anything else?"),
                         b"AUD:Anything else?")
        self.assertEqual(self.calls, ["Mm-hm.", "Anything else?"])
        files = os.listdir(self.cache)
        self.assertTrue(any(
            f.startswith("cloudflare-luna-") and f.endswith(".ogg")
            for f in files), files)

    def test_filler_bank_warm_free_on_second_call(self):
        texts = ["Mm-hm.", "One sec.", "Anything else?"]
        def cf(_s, e, text, style):
            self.calls.append(text)
            return b"AUD:" + text.encode(), ()
        self._plug("cloudflare", ("CF_KEY",), cf)
        env = {"CF_KEY": "k"}
        callconv.FillerBank(kinds={"ack": tuple(texts)}).warm(
            self._tts(["cloudflare"], env).say_cached)
        self.assertEqual(sorted(self.calls), sorted(texts))
        bank2 = callconv.FillerBank(kinds={"ack": tuple(texts)})
        bank2.warm(self._tts(["cloudflare"], env).say_cached)
        self.assertEqual(len(self.calls), 3)      # zero new synths
        self.assertTrue(bank2.ready("ack"))

    def test_one_provider_per_call(self):
        def a(_s, e, t, st):
            self.calls.append("a")
            return b"A", ()
        def b(_s, e, t, st):
            self.calls.append("b")
            return b"B", ()
        self._plug("azure", ("A_KEY",), a)
        self._plug("fish", ("F_KEY",), b)
        env = {"A_KEY": "k", "F_KEY": "k"}
        tts = self._tts(["azure", "fish"], env)
        self.assertEqual(tts.say("one"), b"A")
        self.assertEqual(tts.say("two"), b"A")
        self.assertEqual(tts.say_cached("three"), b"A")
        self.assertEqual(self.calls, ["a", "a", "a"])
        self.assertEqual(tts.provider, "azure")

    def test_failover_switches_and_stays(self):
        def a(_s, e, t, st):
            self.calls.append("a")
            raise voicemod.VoiceError("down", kind="unavailable")
        def b(_s, e, t, st):
            self.calls.append("b")
            return b"B", ()
        self._plug("azure", ("A_KEY",), a)
        self._plug("fish", ("F_KEY",), b)
        env = {"A_KEY": "k", "F_KEY": "k"}
        tts = self._tts(["azure", "fish"], env)
        self.assertEqual(tts.say("x"), b"B")
        self.assertEqual(tts.provider, "fish")
        # azure never gets retried inside this call — voice can't
        # flip-flop between providers mid-call
        self.assertEqual(tts.say("y"), b"B")
        self.assertEqual(tts.say_cached("z"), b"B")
        self.assertEqual(self.calls, ["a", "b", "b", "b"])

    def test_first_usable_provider_picked(self):
        # first order entry has no key -> the call locks the next one
        def b(_s, e, t, st):
            self.calls.append("b")
            return b"B", ()
        self._plug("azure", ("A_KEY",),
                   lambda *_a: self.fail("unconfigured provider ran"))
        self._plug("fish", ("F_KEY",), b)
        tts = self._tts(["azure", "fish"], {"F_KEY": "k"})
        self.assertEqual(tts.say("hi"), b"B")
        self.assertEqual(tts.provider, "fish")

    def test_all_providers_down_returns_none(self):
        self._plug("azure", ("A_KEY",),
                   lambda *_a: (_ for _ in ()).throw(
                       voicemod.VoiceError("x", kind="bad_response")))
        tts = self._tts(["azure"], {"A_KEY": "k"})
        self.assertIsNone(tts.say("hi"))
        self.assertIsNone(tts.say_cached("hi"))

    # -- daily char budgets --------------------------------------------
    def test_spent_provider_skipped_before_lock(self):
        """Azure's daily chars are gone -> the call locks cloudflare
        without ever trying azure."""
        def a(_s, e, t, st):
            self.calls.append("a")
            return b"A", ()
        def cf(_s, e, t, st):
            self.calls.append("cf")
            return b"C", ()
        self._plug("azure", ("A_KEY",), a)
        self._plug("cloudflare", ("CF_KEY",), cf)
        env = {"A_KEY": "k", "CF_KEY": "k",
               "PA_TTS_CHARS_AZURE_DAY": "5"}
        # burn azure's tiny budget via a prior synth (records chars)
        self.assertEqual(
            voicemod.tts_char_budget(self.s, env, "azure"), 5)
        from pa import db as dbmod
        conn = dbmod.connect(self.s.db_path)
        dbmod.migrate(conn)
        from pa.util import utcnow
        conn.execute(
            "INSERT INTO voice_usage(name, day, n) VALUES(?,?,?)",
            ("tts_chars:azure", utcnow().strftime("%Y-%m-%d"), 10))
        conn.commit()
        conn.close()
        tts = self._tts(["azure", "cloudflare"], env)
        self.assertEqual(tts.say("hello"), b"C")
        self.assertEqual(tts.provider, "cloudflare")
        self.assertEqual(self.calls, ["cf"])      # azure never called

    def test_budget_records_chars_on_synth(self):
        def cf(_s, e, t, st):
            return b"C", ()
        self._plug("cloudflare", ("CF_KEY",), cf)
        env = {"CF_KEY": "k"}
        voicemod.synthesize(
            self.s, env, "twenty characters here",
            order=["cloudflare"],
            convert=lambda a, ia: (a, 1.0, "w"))
        self.assertEqual(
            voicemod.tts_chars_used(self.s, "cloudflare"), 22)

    def test_char_budget_overrides(self):
        env = {}
        self.assertEqual(
            voicemod.tts_char_budget(self.s, env, "cloudflare"), 3000)
        self.assertEqual(
            voicemod.tts_char_budget(
                self.s, {"PA_TTS_CHARS_CLOUDFLARE_DAY": "99"},
                "cloudflare"), 99)
        self.s.tts_chars_day = 7
        self.assertEqual(
            voicemod.tts_char_budget(self.s, env, "azure"), 7)

    def test_call_tts_order_default(self):
        self.assertEqual(voicemod.call_tts_order(self.s),
                         ["azure", "cloudflare", "gemini"])

    def test_status_shows_chars_left(self):
        from pa import status as statusmod
        self._plug("azure", ("A_KEY",),
                   lambda *_a: self.fail("unused"))
        self.s.tts_order = "azure"
        self.s.tts_chars_day = 0
        conn = self.s and __import__("pa.db", fromlist=["x"])
        import pa.db as dbmod
        c = dbmod.connect(self.s.db_path)
        dbmod.migrate(c)
        out = statusmod.build(c, self.s, {"A_KEY": "k"}, probe=None)
        self.assertIn("chars left", out)


# ---------------------------------------------------------------------------
# voice-message payload + helpers (pure-python, no ffmpeg)

class TestVoicePayload(unittest.TestCase):
    def test_flag_and_multipart_payload(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        t, s = _settings(DISCORD_BOT_TOKEN="tok",
                         DISCORD_API_BASE=fx.web_base)
        api = discordbot.DiscordAPI(s, "tok")
        ok = api.post_voice_message("c-1", b"OGGDATA", 2.5, "d2F2ZQ==")
        self.assertTrue(ok)
        post = next(c for c in fx.calls
                    if c["path"] == "/channels/c-1/messages")
        self.assertIn("multipart/form-data; boundary=",
                      post["headers"]["Content-Type"])
        body = post["body"]
        self.assertIsInstance(body, (bytes, str))
        raw = body if isinstance(body, bytes) else body.encode()
        self.assertIn(b"OGGDATA", raw)
        m = re.search(rb'name="payload_json"\r\n\r\n(.*?)\r\n--pa-',
                      raw, re.S)
        payload = json.loads(m.group(1))
        self.assertEqual(payload["flags"], 1 << 13)
        att = payload["attachments"][0]
        self.assertEqual(att["duration_secs"], 2.5)
        self.assertEqual(att["waveform"], "d2F2ZQ==")
        self.assertEqual(att["filename"], "voice-message.ogg")

    def test_attachment_is_audio(self):
        self.assertTrue(voicemod.attachment_is_audio(
            {"content_type": "audio/ogg", "filename": "voice-message.ogg"}))
        self.assertTrue(voicemod.attachment_is_audio(
            {"content_type": "", "filename": "clip.MP3"}))
        self.assertFalse(voicemod.attachment_is_audio(
            {"content_type": "image/png", "filename": "pic.png"}))

    def test_download_allowlist(self):
        _t, s = _settings()
        with self.assertRaises(voicemod.VoiceError):
            voicemod.download_attachment(s, "https://evil.example/a.ogg")
        with self.assertRaises(voicemod.VoiceError):
            voicemod.download_attachment(s, "http://cdn.discordapp.com/x")

    def test_duration_and_waveform(self):
        # 1 s of 48kHz mono s16le: half sine-ish, half silence
        pcm = b"\x00\x40" * 24000 + b"\x00\x00" * 24000
        self.assertAlmostEqual(voicemod.duration_secs(pcm), 1.0)
        pts = base64.b64decode(voicemod.waveform_b64(pcm))
        self.assertTrue(1 <= len(pts) <= 256)
        self.assertGreater(max(pts), 0)
        self.assertIn(0, pts)
        wav = voicemod.pcm_to_wav(pcm)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav[:16])


# ---------------------------------------------------------------------------
# STT routing

class TestTranscribe(unittest.TestCase):
    def setUp(self):
        self.env, self.s = _settings()
        self._orig = dict(voicemod._STT)

    def tearDown(self):
        voicemod._STT.clear()
        voicemod._STT.update(self._orig)

    def test_groq_first_then_cloudflare(self):
        calls = []
        def groq(_s, e, a, fn, ct):
            calls.append("groq")
            raise voicemod.VoiceError("down", kind="rate_limited")
        def cf(_s, e, a, fn, ct):
            calls.append("cf")
            return "hello there"
        voicemod._STT.clear()
        voicemod._STT.update({
            "groq": (("GROQ_API_KEY",), groq),
            "cloudflare": (("CLOUDFLARE_ACCOUNT_ID",
                            "CLOUDFLARE_API_TOKEN"), cf)})
        env = {"GROQ_API_KEY": "g", "CLOUDFLARE_ACCOUNT_ID": "a",
               "CLOUDFLARE_API_TOKEN": "t"}
        text, provider = voicemod.transcribe(self.s, env, b"aud")
        self.assertEqual((text, provider), ("hello there", "cloudflare"))
        self.assertEqual(calls, ["groq", "cf"])

    def test_missing_groq_key_skips(self):
        def cf(_s, e, a, fn, ct):
            return "hi"
        voicemod._STT.clear()
        voicemod._STT.update({
            "groq": (("GROQ_API_KEY",),
                     lambda *_a: "groq!"),
            "cloudflare": (("CLOUDFLARE_ACCOUNT_ID",
                            "CLOUDFLARE_API_TOKEN"), cf)})
        env = {"CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_API_TOKEN": "t"}
        text, provider = voicemod.transcribe(self.s, env, b"aud")
        self.assertEqual(provider, "cloudflare")

    def test_none_configured_raises(self):
        with self.assertRaises(voicemod.VoiceError) as cm:
            voicemod.transcribe(self.s, {}, b"aud")
        self.assertIn("needs config", str(cm.exception))

    def test_groq_parses_raw_json_body(self):
        # netclient.request returns raw bytes, not parsed JSON (live bug)
        class FakeClient:
            def request(self, *a, **k):
                return {}, b'{"text": " check my email "}'
        orig = voicemod._client
        voicemod._client = lambda *_a, **_k: FakeClient()
        try:
            text = voicemod._stt_groq(self.s, {"GROQ_API_KEY": "g"},
                                      b"aud", "call.wav", "audio/wav")
        finally:
            voicemod._client = orig
        self.assertEqual(text, "check my email")


# ---------------------------------------------------------------------------
# CallManager — voice lib replaced by a fake runtime

class FakeApi:
    def __init__(self):
        self.posts = []             # (channel_id, text)
        self.channels = []
        self.created = None
        self.dm_id = "dm-1"

    def post_message(self, channel_id, text, **kw):
        self.posts.append((str(channel_id), text))
        return True

    def open_dm(self, user_id):
        return self.dm_id

    def guild_channels(self, guild_id):
        return self.channels

    def create_voice_channel(self, guild_id, name, overwrites):
        self.created = (guild_id, name, overwrites)
        return {"id": "vc-9"}


class FakeRuntime:
    can_listen = True

    def __init__(self, result=None, block=None, script=None):
        self.calls = []
        self.result = result or {"reason": "bye", "turns": 1,
                                 "heard": ["all good"]}
        self.block = block
        self.script = script

    def run_call(self, **kw):
        self.calls.append(kw)
        if self.script:
            self.script(kw)
        if self.block is not None:
            self.block.wait(timeout=5)
        return self.result


def _manager(env_overrides=None, runtime=None, api=None, engine=None,
             limiter=None):
    t = TmpEnv(**(env_overrides or {}))
    s = t.settings
    s.call_guild_id = "g-1"
    api = api or FakeApi()
    cm = callmod.CallManager(
        s, {}, api, db_path=t.db_path,
        engine_factory=(lambda: engine) if engine else None,
        owner_id=OWNER, bot_user_id="bot-1",
        runtime_factory=(lambda *a: runtime) if runtime else None,
        limiter=limiter)
    return t, cm, api


class TestCallManager(unittest.TestCase):
    def test_not_configured_without_guild(self):
        t = TmpEnv()
        cm = callmod.CallManager(t.settings, {}, FakeApi())
        out = cm.call("hello")
        self.assertEqual(out["error"], "not_configured")

    def test_full_flow_notifies_runs_summarizes(self):
        rt = FakeRuntime()
        api = FakeApi()
        api.channels = [{"id": "vc-1", "type": 2, "name": "pal-call"}]
        _t, cm, api = _manager(runtime=rt, api=api)
        out = cm.call("server on fire", urgent=True)
        self.assertTrue(out["calling"])
        wait_for(lambda: any("call over" in p[1] for p in api.posts),
                 timeout=5)
        # mention in the voice channel + DM invite + summary DM
        posts = [p for p in api.posts]
        self.assertTrue(any(p[0] == "vc-1" and "calling" in p[1]
                            for p in posts))
        self.assertTrue(any(p[0] == "dm-1" and "join <#vc-1>" in p[1]
                            for p in posts))
        self.assertTrue(any(p[0] == "dm-1" and "call over" in p[1]
                            and "bye" in p[1] for p in posts))
        self.assertEqual(rt.calls[0]["channel_id"], "vc-1")
        self.assertEqual(rt.calls[0]["owner_id"], OWNER)

    def test_channel_created_when_missing(self):
        rt = FakeRuntime()
        api = FakeApi()            # guild_channels -> [] : create
        _t, cm, api = _manager(runtime=rt, api=api)
        out = cm.call("hi", urgent=True)
        self.assertTrue(out["calling"])
        wait_for(lambda: rt.calls, timeout=5)
        self.assertIsNotNone(api.created)
        gid, name, overwrites = api.created
        self.assertEqual((gid, name), ("g-1", "pal-call"))
        deny = next(o for o in overwrites if o["type"] == 0)
        self.assertEqual(deny["id"], "g-1")       # @everyone denied
        allows = {o["id"] for o in overwrites if o["type"] == 1}
        self.assertEqual(allows, {OWNER, "bot-1"})
        self.assertEqual(rt.calls[0]["channel_id"], "vc-9")

    def test_rate_limit(self):
        limiter = callmod.CallLimiter()
        block = threading.Event()
        rt = FakeRuntime(block=block)
        _t, cm, api = _manager(runtime=rt, limiter=limiter,
                               env_overrides={"PA_CALL_MAX_PER_HOUR": "1"})
        first = cm.call("one", urgent=True)
        self.assertTrue(first["calling"])
        wait_for(lambda: rt.calls, timeout=5)
        # in-flight call reports busy; after hang-up the hour-cap bites
        self.assertEqual(cm.call("mid", urgent=True)["error"], "busy")
        block.set()
        wait_for(lambda: any("call over" in p[1] for p in api.posts),
                 timeout=5)
        wait_for(lambda: not cm._call_lock.locked(), timeout=5)
        second = cm.call("two", urgent=True)
        self.assertEqual(second["error"], "rate_limited")

    def test_quiet_hours_refuse_nonurgent(self):
        rt = FakeRuntime()
        _t, cm, api = _manager(
            runtime=rt, env_overrides={"PA_QUIET_HOURS": "00-23"})
        # quiet window covers every hour except the boundary edge —
        # non-urgent is refused, urgent still rings
        with unittest.mock.patch.object(
                callmod, "in_quiet_hours", return_value=True):
            self.assertEqual(cm.call("ping")["error"], "quiet_hours")
            self.assertTrue(cm.call("ping", urgent=True)["calling"])
        wait_for(lambda: rt.calls, timeout=5)

    def test_busy_when_call_in_flight(self):
        block = threading.Event()
        rt = FakeRuntime(block=block)
        api = FakeApi()
        api.channels = [{"id": "vc-1", "type": 2, "name": "pal-call"}]
        _t, cm, api = _manager(runtime=rt, api=api)
        self.assertTrue(cm.call("one", urgent=True)["calling"])
        wait_for(lambda: rt.calls, timeout=5)
        self.assertEqual(cm.call("two", urgent=True)["error"], "busy")
        block.set()

    def test_transcribe_think_turn(self):
        seen = {}
        def script(kw):
            t = kw["transcribe"](b"wav")
            seen["heard"] = t["heard"]
            seen.update(kw["think"](t["heard"]))
        rt = FakeRuntime(script=script)
        engine = unittest.mock.Mock()
        engine.run_turn.return_value = {"text": "on it"}
        _t, cm, _a = _manager(runtime=rt, engine=engine)
        with unittest.mock.patch.object(
                voicemod, "transcribe_meta",
                return_value=("is the server ok", "groq",
                              {"no_speech_prob": 0.1})):
            self.assertTrue(cm.call("check", urgent=True)["calling"])
            wait_for(lambda: rt.calls and seen, timeout=5)
        self.assertEqual(seen["heard"], "is the server ok")
        self.assertEqual(seen["reply"], "on it")
        self.assertFalse(seen["done"])
        engine.run_turn.assert_called_once()
        chan, text = engine.run_turn.call_args[0]
        self.assertIn("is the server ok", text)
        self.assertTrue(engine.run_turn.call_args[1].get("call"))

    def test_transcribe_drops_noise(self):
        for text, meta in (("Thank you.", {}),
                           ("you", {"no_speech_prob": 0.95}),
                           ("some words", {"avg_logprob": -1.3})):
            seen = {}
            def script(kw):
                seen.update(kw["transcribe"](b"wav"))
            rt = FakeRuntime(script=script)
            _t, cm, _a = _manager(runtime=rt)
            with unittest.mock.patch.object(
                    voicemod, "transcribe_meta",
                    return_value=(text, "groq", meta)):
                self.assertTrue(
                    cm.call("check", urgent=True)["calling"])
                wait_for(lambda: seen, timeout=5)
            self.assertEqual(seen["heard"], "", (text, meta))
            self.assertTrue(seen["noise"], (text, meta))

    def test_think_bye_ends(self):
        seen = {}
        def script(kw):
            t = kw["transcribe"](b"wav")
            seen["heard"] = t["heard"]
            seen.update(kw["think"](t["heard"]))
        rt = FakeRuntime(script=script)
        _t, cm, _a = _manager(runtime=rt)
        with unittest.mock.patch.object(
                voicemod, "transcribe_meta",
                return_value=("ok thanks bye", "groq", {})):
            cm.call("check", urgent=True)
            wait_for(lambda: seen, timeout=5)
        self.assertTrue(seen["done"])


# ---------------------------------------------------------------------------
# DAVE receive shim — fakes for davey / voice-recv / discord.gateway

class FakeDaveSession:
    """davey.DaveSession stand-in: `users` are the uids the session has
    decryptors for (i.e. group members after update_ratchets ran)."""

    def __init__(self, users=(), ready=True, protocol_version=1):
        self.users = set(users)
        self.ready = ready
        self.protocol_version = protocol_version
        self.decrypted = []
        self.prepared = []
        self.executed = []
        self.decrypt_error = None

    def decrypt(self, user_id, media_type, data):
        self.decrypted.append((user_id, media_type, data))
        if self.decrypt_error:
            raise self.decrypt_error
        if user_id not in self.users:
            raise ValueError("Failed to decrypt: NoDecryptorForUser")
        return b"plain:" + data

    def prepare_transition(self, transition_id, protocol_version):
        self.prepared.append((transition_id, protocol_version))

    def execute_transition(self, transition_id):
        self.executed.append(transition_id)
        return True

    def get_user_ids(self):
        return sorted(self.users)


def _fake_ws(session=None, protocol_version=1):
    import types
    state = types.SimpleNamespace(
        dave_session=session,
        dave_protocol_version=protocol_version)
    return types.SimpleNamespace(
        _connection=state,
        DAVE_PREPARE_TRANSITION=21,
        DAVE_EXECUTE_TRANSITION=22,
        MLS_ANNOUNCE_COMMIT_TRANSITION=29,
        MLS_WELCOME=30)


def _mls_frame(seq, op, transition_id, payload=b"x"):
    import struct
    return struct.pack(">HBH", seq, op, transition_id) + payload


class TestDaveDecrypt(unittest.TestCase):
    def setUp(self):
        callmod._dave_rx_reset()

    def test_success_returns_plaintext_and_marks_user(self):
        s = FakeDaveSession(users={42})
        out = callmod._dave_decrypt(
            s, "42", b"enc", ssrc=7, media_type="audio", silence=b"S")
        self.assertEqual(out, b"plain:enc")
        self.assertEqual(s.decrypted, [(42, "audio", b"enc")])
        stats = callmod._dave_rx_stats()
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(stats["ok_users"], [42])
        self.assertFalse(callmod._dave_rx_broken())

    def test_not_ready_drops_to_silence_without_decrypt(self):
        s = FakeDaveSession(users={42}, ready=False)
        out = callmod._dave_decrypt(
            s, 42, b"enc", ssrc=7, media_type="audio", silence=b"S")
        self.assertEqual(out, b"S")
        self.assertEqual(s.decrypted, [])        # never even tried
        self.assertEqual(callmod._dave_rx_stats()["not_ready"], 1)

    def test_missing_decryptor_is_pending_not_failure(self):
        s = FakeDaveSession(users=set())
        out = callmod._dave_decrypt(
            s, 42, b"enc", ssrc=7, media_type="audio", silence=b"S")
        self.assertEqual(out, b"S")
        stats = callmod._dave_rx_stats()
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["fail"], 0)
        self.assertEqual(stats["last_error"], "")

    def test_real_failure_counts_and_keeps_last_error(self):
        s = FakeDaveSession(users={42})
        s.decrypt_error = ValueError(
            "Failed to decrypt: InvalidNonce")
        out = callmod._dave_decrypt(
            s, 42, b"enc", ssrc=7, media_type="audio", silence=b"S")
        self.assertEqual(out, b"S")
        stats = callmod._dave_rx_stats()
        self.assertEqual(stats["fail"], 1)
        self.assertIn("InvalidNonce", stats["last_error"])

    def test_unmapped_ssrc_drops(self):
        s = FakeDaveSession(users={42})
        out = callmod._dave_decrypt(
            s, None, b"enc", ssrc=7, media_type="audio", silence=b"S")
        self.assertEqual(out, b"S")
        self.assertEqual(s.decrypted, [])
        self.assertEqual(callmod._dave_rx_stats()["no_user"], 1)

    def test_broken_predicate(self):
        s = FakeDaveSession(users=set())
        for _ in range(callmod._dave_rx_broken.__defaults__[0]):
            callmod._dave_decrypt(
                s, 42, b"e", ssrc=1, media_type="a", silence=b"S")
        self.assertTrue(callmod._dave_rx_broken())
        # one success flips it back
        s.users.add(42)
        callmod._dave_decrypt(
            s, 42, b"e", ssrc=1, media_type="a", silence=b"S")
        self.assertFalse(callmod._dave_rx_broken())


class TestDaveTransitionSignals(unittest.TestCase):
    """The shim feeds discord.py's bookkeeping-only transition handling
    into the davey session (prepare/execute + mls commits/welcomes)."""

    def test_prepare_transition_op21(self):
        s = FakeDaveSession()
        callmod._dave_signal_json(
            _fake_ws(s),
            {"op": 21, "d": {"transition_id": 5,
                            "protocol_version": 1}})
        self.assertEqual(s.prepared, [(5, 1)])

    def test_execute_transition_op22(self):
        s = FakeDaveSession()
        callmod._dave_signal_json(
            _fake_ws(s), {"op": 22, "d": {"transition_id": 5}})
        self.assertEqual(s.executed, [5])

    def test_prepare_transition_zero_executes_immediately(self):
        s = FakeDaveSession()
        callmod._dave_signal_json(
            _fake_ws(s),
            {"op": 21, "d": {"transition_id": 0,
                            "protocol_version": 1}})
        self.assertEqual(s.prepared, [(0, 1)])
        self.assertEqual(s.executed, [0])

    def test_other_ops_ignored(self):
        s = FakeDaveSession()
        ws = _fake_ws(s)
        for op in (2, 4, 5, 11, 13):
            callmod._dave_signal_json(ws, {"op": op, "d": {}})
        self.assertEqual(s.prepared + s.executed, [])

    def test_mls_commit_prepares_with_current_version(self):
        s = FakeDaveSession()
        callmod._dave_signal_binary(
            _fake_ws(s, protocol_version=1),
            _mls_frame(9, 29, 7, b"commit"))
        self.assertEqual(s.prepared, [(7, 1)])

    def test_mls_welcome_prepares(self):
        s = FakeDaveSession()
        callmod._dave_signal_binary(
            _fake_ws(s), _mls_frame(9, 30, 0, b"welcome"))
        self.assertEqual(s.prepared, [(0, 1)])
        # welcome carries tid 0 -> transition applies immediately
        self.assertEqual(s.executed, [0])

    def test_non_mls_binary_ignored(self):
        s = FakeDaveSession()
        callmod._dave_signal_binary(_fake_ws(s), _mls_frame(9, 25, 0))
        self.assertEqual(s.prepared, [])

    def test_no_session_is_noop(self):
        ws = _fake_ws(None)
        callmod._dave_signal_json(
            ws, {"op": 21, "d": {"transition_id": 1,
                                 "protocol_version": 1}})
        callmod._dave_signal_binary(ws, _mls_frame(1, 29, 0))

    def test_session_error_is_swallowed(self):
        class Bad(FakeDaveSession):
            def prepare_transition(self, *_a):
                raise RuntimeError("boom")
        callmod._dave_signal_json(
            _fake_ws(Bad()),
            {"op": 21, "d": {"transition_id": 1,
                            "protocol_version": 1}})   # no raise


class TestDavePatch(unittest.TestCase):
    """_patch_voice_recv_dave against injected fake modules."""

    def _fake_modules(self):
        import sys, types
        opus = types.ModuleType("discord.ext.voice_recv.opus")

        class PacketDecoder:
            def _decode_packet(self, packet):
                return packet
        opus.PacketDecoder = PacketDecoder

        rtp = types.ModuleType("discord.ext.voice_recv.rtp")
        rtp.OPUS_SILENCE = b"SIL"

        davey = types.ModuleType("davey")
        davey.MediaType = types.SimpleNamespace(audio="audio")

        gw = types.ModuleType("discord.gateway")

        class WS:
            async def received_message(self, msg):
                self.got_msg = msg

            async def received_binary_message(self, msg):
                self.got_bin = msg
        gw.DiscordVoiceWebSocket = WS

        vr = types.ModuleType("discord.ext.voice_recv")
        vr.opus = opus
        vr.rtp = rtp
        mods = {
            "davey": davey,
            "discord": types.ModuleType("discord"),
            "discord.gateway": gw,
            "discord.ext": types.ModuleType("discord.ext"),
            "discord.ext.voice_recv": vr,
            "discord.ext.voice_recv.opus": opus,
            "discord.ext.voice_recv.rtp": rtp,
        }
        for name in ("discord", "discord.ext",
                     "discord.ext.voice_recv"):
            mods[name].__path__ = []
        return sys, types, mods, opus, gw

    def test_patch_wires_decode_and_ws(self):
        import asyncio
        sys, types, mods, opus, gw = self._fake_modules()
        with unittest.mock.patch.dict(sys.modules, mods):
            self.assertTrue(callmod._patch_voice_recv_dave())
            self.assertTrue(callmod._patch_voice_recv_dave())  # idempotent

            session = FakeDaveSession(users={5})
            vc = types.SimpleNamespace(
                _connection=types.SimpleNamespace(
                    dave_session=session, dave_protocol_version=1),
                _get_id_from_ssrc=lambda s: {3: 5}.get(s))
            dec = opus.PacketDecoder()
            dec.ssrc = 3
            dec._cached_id = None
            dec.sink = types.SimpleNamespace(voice_client=vc)
            pkt = types.SimpleNamespace(decrypted_data=b"enc1")
            callmod._dave_rx_reset()
            dec._decode_packet(pkt)
            self.assertEqual(pkt.decrypted_data, b"plain:enc1")

            # silence passthrough: <=3 byte payloads are left alone
            pkt2 = types.SimpleNamespace(decrypted_data=b"\xf8\xff\xfe")
            dec._decode_packet(pkt2)
            self.assertEqual(pkt2.decrypted_data, b"\xf8\xff\xfe")

            # ws op 29 -> session.prepare_transition(tid, version)
            ws = gw.DiscordVoiceWebSocket()
            ws._connection = vc._connection
            asyncio.run(ws.received_binary_message(
                _mls_frame(2, 29, 11, b"c")))
            self.assertEqual(session.prepared, [(11, 1)])

            # ws op 22 -> session.execute_transition(tid)
            asyncio.run(ws.received_message(
                {"op": 22, "d": {"transition_id": 11}}))
            self.assertEqual(session.executed, [11])

    def test_early_mls_frames_replayed_once_session_exists(self):
        import asyncio
        sys, types, mods, opus, gw = self._fake_modules()
        with unittest.mock.patch.dict(sys.modules, mods):
            self.assertTrue(callmod._patch_voice_recv_dave())
            state = types.SimpleNamespace(
                dave_session=None, dave_protocol_version=1)
            ws = gw.DiscordVoiceWebSocket()
            ws._connection = state
            # binary frame for op 29 while session is None -> stashed,
            # not signaled
            asyncio.run(ws.received_binary_message(
                _mls_frame(1, 29, 3, b"c")))
            self.assertTrue(state._pa_mls_stash)
            # session appears (a later message created it): next binary
            # frame flushes the stash through the original handler
            state.dave_session = FakeDaveSession()
            asyncio.run(ws.received_binary_message(
                _mls_frame(2, 29, 4, b"d")))
            self.assertFalse(state._pa_mls_stash)
            self.assertEqual(state.dave_session.prepared,
                             [(3, 1), (4, 1)])


class TestCallNotifyAndDiag(unittest.TestCase):
    def test_notify_dm_reaches_owner(self):
        def script(kw):
            kw["notify"]("pal can't hear you")
        rt = FakeRuntime(script=script)
        api = FakeApi()
        api.channels = [{"id": "vc-1", "type": 2, "name": "pal-call"}]
        _t, cm, api = _manager(runtime=rt, api=api)
        cm.call("hi", urgent=True)
        wait_for(lambda: any(
            p[0] == "dm-1" and "can't hear" in p[1]
            for p in api.posts), timeout=5)

    def test_run_call_gets_notify_callback(self):
        rt = FakeRuntime()
        api = FakeApi()
        api.channels = [{"id": "vc-1", "type": 2, "name": "pal-call"}]
        _t, cm, api = _manager(runtime=rt, api=api)
        cm.call("hi", urgent=True)
        wait_for(lambda: rt.calls, timeout=5)
        self.assertTrue(callable(rt.calls[0]["notify"]))

    def test_run_call_gets_call_voice_seams(self):
        rt = FakeRuntime()
        api = FakeApi()
        api.channels = [{"id": "vc-1", "type": 2, "name": "pal-call"}]
        _t, cm, api = _manager(runtime=rt, api=api)
        cm.call("hi", urgent=True)
        wait_for(lambda: rt.calls, timeout=5)
        # per-call CallTTS: dynamic + disk-cached static lines
        self.assertTrue(callable(rt.calls[0]["say"]))
        self.assertTrue(callable(rt.calls[0]["say_cached"]))

    def test_deaf_summary_note(self):
        rt = FakeRuntime(result={"reason": "timeout", "turns": 0,
                                 "heard": [], "deaf": True})
        api = FakeApi()
        api.channels = [{"id": "vc-1", "type": 2, "name": "pal-call"}]
        _t, cm, api = _manager(runtime=rt, api=api)
        cm.call("hi", urgent=True)
        wait_for(lambda: any("call over" in p[1] for p in api.posts),
                 timeout=5)
        self.assertTrue(any("couldn't hear" in p[1]
                            for p in api.posts))

    def test_call_diag_needs_token(self):
        import os
        from pa import __main__ as pamain
        with unittest.mock.patch.dict(
                os.environ,
                {"PA_ENV_FILE": "/nonexistent/.env"}, clear=True):
            import io, contextlib
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                # no DISCORD_BOT_TOKEN -> clean refusal, not a crash
                try:
                    rc = pamain.main(["call-diag"])
                except SystemExit as e:
                    rc = e.code
        self.assertEqual(rc, 2)


class TestQuietHours(unittest.TestCase):
    import datetime

    def _at(self, hour):
        import datetime
        return datetime.datetime(2026, 3, 10, hour, 0,
                                 tzinfo=datetime.timezone.utc)

    def test_wraparound_window(self):
        # Europe/London == UTC in March (pre-DST): 23-07 quiet
        self.assertTrue(callmod.in_quiet_hours(
            "23-07", "Europe/London", self._at(23)))
        self.assertTrue(callmod.in_quiet_hours(
            "23-07", "Europe/London", self._at(3)))
        self.assertFalse(callmod.in_quiet_hours(
            "23-07", "Europe/London", self._at(12)))
        self.assertFalse(callmod.in_quiet_hours(
            "23-07", "Europe/London", self._at(7)))

    def test_bad_window_is_off(self):
        self.assertFalse(callmod.in_quiet_hours("", "Europe/London"))
        self.assertFalse(callmod.in_quiet_hours(
            "99-99", "Europe/London", self._at(3)))
        self.assertFalse(callmod.in_quiet_hours(
            "05-05", "Europe/London", self._at(5)))


class TestLimiter(unittest.TestCase):
    def test_window(self):
        lim = callmod.CallLimiter()
        self.assertTrue(lim.allow(2))
        lim.record()
        self.assertTrue(lim.allow(2))
        lim.record()
        self.assertFalse(lim.allow(2))
        self.assertFalse(lim.allow(0))


# ---------------------------------------------------------------------------
# VoiceServices + chat tools

class TestVoiceTools(unittest.TestCase):
    def test_send_voice_note_posts(self):
        api = FakeApi()
        api.post_voice_message = unittest.mock.Mock(return_value=True)
        vs = voicemod.VoiceServices(_settings()[1], {}, api)
        with unittest.mock.patch.object(
                voicemod, "synthesize",
                return_value=(b"ogg", 1.5, "d2F2ZQ==", "gemini")):
            out = vs.send_voice_note("c-1", "hello")
        self.assertTrue(out["sent"])
        self.assertEqual(out["provider"], "gemini")
        api.post_voice_message.assert_called_once_with(
            "c-1", b"ogg", 1.5, "d2F2ZQ==")

    def test_send_voice_note_tts_failure(self):
        vs = voicemod.VoiceServices(_settings()[1], {}, FakeApi())
        with unittest.mock.patch.object(
                voicemod, "synthesize",
                side_effect=voicemod.VoiceError("all down")):
            out = vs.send_voice_note("c-1", "hello")
        self.assertEqual(out["error"], "tts_failed")

    def test_chattools_dispatch(self):
        t = TmpEnv()
        conn = t.conn()
        self.addCleanup(conn.close)
        vs = unittest.mock.Mock()
        vs.send_voice_note.return_value = {"sent": True}
        vs.call_owner.return_value = {"calling": True}
        ctx = chattools.ChatToolContext(
            conn, t.settings, None, "c-1", env={}, voice=vs)
        out = chattools.run(ctx, "send_voice_note", {"text": "hi"})
        self.assertTrue(out["sent"])
        out = chattools.run(ctx, "call_owner", {"message": "m",
                                                "urgent": True})
        self.assertTrue(out["calling"])
        vs.call_owner.assert_called_once_with("m", urgent=True,
                                              source="chat")
        # no voice wired -> clean error, not a crash
        ctx2 = chattools.ChatToolContext(
            conn, t.settings, None, "c-1", env={})
        out = chattools.run(ctx2, "send_voice_note", {"text": "hi"})
        self.assertEqual(out["error"], "not_configured")


# ---------------------------------------------------------------------------
# bot-side: /voice toggle, /call command, call inbox drain, voice-note route

class FakeCallMgr:
    def __init__(self):
        self.calls = []

    def call(self, message, urgent=False, source="chat"):
        self.calls.append((message, urgent, source))
        return {"calling": True, "message": "ringing"}

    def stop(self):
        pass


class TestBotSide(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.env = TmpEnv(
            DISCORD_BOT_TOKEN="fake-bot-token",
            DISCORD_OWNER_ID=OWNER,
            DISCORD_API_BASE=self.fx.web_base)
        self.env.conn().close()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)
        self.bot._calls = FakeCallMgr()

    def tearDown(self):
        self.fx.close()

    def test_voice_toggle(self):
        conn = self.env.conn()
        self.addCleanup(conn.close)
        # unset -> auto is the default mode
        self.assertIn("auto", self.bot.execute(conn, ["voice"]))
        self.assertIn("ON", self.bot.execute(conn, ["voice", "on"]))
        self.assertEqual(
            dbmod.get_setting(conn, "voice_notes"), "on")
        self.assertTrue(self.bot._voice_on())
        self.assertIn("AUTO", self.bot.execute(conn, ["voice", "auto"]))
        self.assertEqual(
            dbmod.get_setting(conn, "voice_notes"), "auto")
        self.bot.execute(conn, ["voice", "off"])
        self.assertFalse(self.bot._voice_on())

    def test_call_command(self):
        conn = self.env.conn()
        self.addCleanup(conn.close)
        reply = self.bot.execute(conn, ["call", "test"])
        self.assertIn("ringing", reply)
        self.assertEqual(self.bot._calls.calls[0][1], True)  # urgent

    def test_call_inbox_drain(self):
        conn = self.env.conn()
        conn.execute(
            "INSERT INTO chat_inbox(channel_id, prompt, source, status,"
            " created_at) VALUES(?,?,?,?,?)",
            ("call", json.dumps({"message": "urgent thing",
                                 "urgent": True}),
             "call:task:7", "queued", "2026-01-01T00:00:00Z"))
        conn.close()
        self.bot.tick_chat_inbox()
        self.assertEqual(self.bot._calls.calls,
                         [("urgent thing", True, "call:task:7")])

    def test_voice_attachment_route(self):
        """DM audio attachment -> voice worker -> transcript turn +
        text reply + voice note."""
        d = {"id": "m1", "channel_id": "dm1", "channel_type": 1,
             "author": {"id": OWNER},
             "content": "",
             "attachments": [{
                 "id": "a1", "filename": "voice-message.ogg",
                 "content_type": "audio/ogg",
                 "url": "https://cdn.discordapp.com/att/1/voice.ogg"}]}
        self.assertEqual(self.bot._message_route(d), "voice")

    def test_guild_audio_ignored(self):
        d = {"id": "m1", "channel_id": "g1", "guild_id": "g-1",
             "author": {"id": OWNER}, "content": "",
             "attachments": [{"filename": "a.ogg",
                            "content_type": "audio/ogg"}]}
        self.assertIsNone(self.bot._message_route(d))


if __name__ == "__main__":
    unittest.main()

"""final-1 V4: Gemini Live engine — fake-WebSocket tests for setup
shape, audio framing, interrupted flush, toolCall -> toolResponse,
resumption reconnect, and pipeline fallback on 1011/429."""

import asyncio
import base64
import json
import time
import unittest

from pa import live as livemod
from pa import call as callmod


class FakeWSMessage:
    def __init__(self, data=None, type=None):
        self.data = data
        self.type = type


class _T:
    """Enum-like message types matching aiohttp's WSMsgType."""
    TEXT = type("MT", (), {"name": "TEXT"})()
    CLOSED = type("MT", (), {"name": "CLOSED"})()


class FakeWS:
    """Scriptable BidiGenerateContent socket: `script` is a list of
    dicts (or {"_closed": code}) the engine receives in order; `sent`
    collects every JSON message the engine sends."""
    def __init__(self, script=()):
        self.script = list(script)
        self.sent = []
        self.closed = False

    async def send_str(self, data):
        self.sent.append(json.loads(data))

    async def receive(self):
        await asyncio.sleep(0)
        if self.script:
            m = self.script.pop(0)
            if "_closed" in m:
                return FakeWSMessage(data=m["_closed"], type=_T.CLOSED)
            return FakeWSMessage(data=json.dumps(m), type=_T.TEXT)
        return FakeWSMessage(data=1000, type=_T.CLOSED)

    async def close(self):
        self.closed = True


def _session(script):
    """A session_factory yielding a LiveSession bound to a FakeWS."""
    ws = FakeWS(script)
    def factory(env, **kw):
        kw.pop("ws_factory", None)
        return livemod.LiveSession(env, ws_factory=lambda: _mk(ws),
                                   **kw)
    async def _mk(ws=ws):
        return ws
    factory.ws = ws
    return factory


def _run(coro):
    return asyncio.run(coro)


class TestSetupShape(unittest.TestCase):
    def test_setup_message(self):
        f = _session([{"_closed": 1000}])
        src = callmod.CallAudioSource()
        eng = livemod.LiveEngine(object(), {}, source=src,
                                 session_factory=f)
        _run(eng.run(time.monotonic() + 50, opening="hi there"))
        setup = f.ws.sent[0]["setup"]
        self.assertEqual(setup["model"],
                         "models/gemini-3.1-flash-live-preview")
        self.assertEqual(setup["generationConfig"]
                         ["responseModalities"], ["AUDIO"])
        self.assertEqual(setup["generationConfig"]["speechConfig"]
                         ["voiceConfig"]["prebuiltVoiceConfig"]
                         ["voiceName"], "Kore")
        self.assertIn("systemInstruction", setup)
        self.assertEqual(setup["sessionResumption"], {})
        self.assertIn("inputAudioTranscription", setup)
        self.assertIn("outputAudioTranscription", setup)
        names = [d["name"] for d in
                 setup["tools"][0]["functionDeclarations"]]
        self.assertIn("ask_pal", names)

    def test_opening_prompt_sent_once(self):
        f = _session([{"_closed": 1000}])
        eng = livemod.LiveEngine(object(), {},
                                 source=callmod.CallAudioSource(),
                                 session_factory=f)
        _run(eng.run(time.monotonic() + 50, opening="greet msg"))
        opens = [m for m in f.ws.sent if "clientContent" in m]
        self.assertEqual(len(opens), 1)
        self.assertIn("greet msg", json.dumps(opens[0]))


class TestAudioFraming(unittest.TestCase):
    def test_mic_audio_realtime_input(self):
        f = _session([{"_closed": 1000}])
        eng = livemod.LiveEngine(object(), {},
                                 source=callmod.CallAudioSource(),
                                 session_factory=f)
        frame = b"\x01\x02" * 1920             # 20 ms 48k stereo
        eng.feed_frame(frame)

        async def main():
            sess = livemod.LiveSession({}, model="m", voice="v",
                                       system="s", tools=None,
                                       ws_factory=_mk)
            await sess.connect()
            task = asyncio.ensure_future(eng._mic_loop(sess))
            await asyncio.sleep(0.25)
            eng._stop = True
            await asyncio.sleep(0.15)
            task.cancel()
        async def _mk():
            return f.ws
        _run(main())
        msgs = [m for m in f.ws.sent if "realtimeInput" in m]
        self.assertTrue(msgs)
        a = msgs[0]["realtimeInput"]["audio"]
        self.assertEqual(a["mimeType"], "audio/pcm;rate=16000")
        pcm = base64.b64decode(a["data"])
        # 20 ms @ 48k stereo -> 20 ms @ 16k mono = 640 bytes
        self.assertAlmostEqual(len(pcm), 640, delta=64)

    def test_model_audio_feeds_source(self):
        clip = base64.b64encode(b"\x00\x01" * 2400).decode()
        f = _session([
            {"serverContent": {"modelTurn": {"parts": [
                {"inlineData": {"data": clip,
                                "mimeType": "audio/pcm;rate=24000"}}]},
                               "turnComplete": True}},
            {"_closed": 1000}])
        src = callmod.CallAudioSource()
        eng = livemod.LiveEngine(object(), {}, source=src,
                                 session_factory=f)
        out = _run(eng.run(time.monotonic() + 50))
        self.assertEqual(out["turns"], 1)
        self.assertFalse(src.empty())


class TestInterruptedFlush(unittest.TestCase):
    def test_interrupted_clears_queue(self):
        f = _session([
            {"serverContent": {"interrupted": True}},
            {"_closed": 1000}])
        src = callmod.CallAudioSource()
        src.feed(b"\x01" * 3840 * 10)
        eng = livemod.LiveEngine(object(), {}, source=src,
                                 session_factory=f)
        out = _run(eng.run(time.monotonic() + 50))
        self.assertEqual(out["interrupted"], 1)
        self.assertTrue(src.empty())


class TestToolCall(unittest.TestCase):
    def test_tool_call_round_trip(self):
        got = {}
        def ask_pal(q):
            got["q"] = q
            return "the answer is 4"
        f = _session([
            {"toolCall": {"functionCalls": [
                {"id": "c1", "name": "ask_pal",
                 "args": {"question": "how many?"}}]}},
            {"_closed": 1000}])
        eng = livemod.LiveEngine(object(), {},
                                 source=callmod.CallAudioSource(),
                                 ask_pal=ask_pal, session_factory=f)
        _run(eng.run(time.monotonic() + 50))
        self.assertEqual(got["q"], "how many?")
        resp = [m for m in f.ws.sent if "toolResponse" in m]
        self.assertTrue(resp)
        fr = resp[0]["toolResponse"]["functionResponses"][0]
        self.assertEqual(fr["id"], "c1")
        self.assertEqual(fr["response"]["result"], "the answer is 4")

    def test_fast_tool(self):
        f = _session([
            {"toolCall": {"functionCalls": [
                {"id": "c2", "name": "now", "args": {}}]}},
            {"_closed": 1000}])
        eng = livemod.LiveEngine(
            object(), {}, source=callmod.CallAudioSource(),
            fast_tools={"now": lambda: "Thursday 12 November 2026"},
            session_factory=f)
        _run(eng.run(time.monotonic() + 50))
        resp = [m for m in f.ws.sent if "toolResponse" in m]
        self.assertIn("Thursday", json.dumps(resp))

    def test_slow_ask_pal_still_working(self):
        import time
        def slow(q):
            time.sleep(0.3)
            return "late"
        f = _session([
            {"toolCall": {"functionCalls": [
                {"id": "c3", "name": "ask_pal", "args": {"q": "x"}}]}},
            {"_closed": 1000}])
        eng = livemod.LiveEngine(object(), {},
                                 source=callmod.CallAudioSource(),
                                 ask_pal=slow, ask_timeout=0.05,
                                 session_factory=f)
        async def _mk():
            return f.ws
        sess = livemod.LiveSession({}, model="m", voice="v",
                                   system="s", tools=None,
                                   ws_factory=_mk)
        _run(sess.connect())
        _run(eng._tool_call(sess, {
            "id": "c3", "name": "ask_pal", "args": {"q": "x"}}))
        resp = [m for m in sess.sent if "toolResponse" in m]
        self.assertIn("still working", json.dumps(resp))


class TestResumption(unittest.TestCase):
    def test_goaway_reconnects_with_handle(self):
        sessions = []

        def factory(env, **kw):
            scripts = [
                [{"sessionResumption": {"newHandle": "h-1"}},
                 {"goAway": {"timeLeft": "5s"}},
                 {"_closed": 1000}],
                [{"_closed": 1000}]]
            ws = FakeWS(scripts[len(sessions)])
            sess = livemod.LiveSession(
                env, ws_factory=lambda: _mk(ws), **kw)
            sessions.append(sess)
            return sess
        async def _mk(ws):
            return ws
        eng = livemod.LiveEngine(object(), {},
                                 source=callmod.CallAudioSource(),
                                 session_factory=factory)
        _run(eng.run(time.monotonic() + 50))
        self.assertEqual(len(sessions), 2)
        setup2 = sessions[1].sent[0]["setup"]
        self.assertEqual(setup2["sessionResumption"],
                         {"handle": "h-1"})


class TestFallback(unittest.TestCase):
    def test_close_1011_is_live_unavailable(self):
        f = _session([{"_closed": 1011}])
        eng = livemod.LiveEngine(object(), {},
                                 source=callmod.CallAudioSource(),
                                 session_factory=f)
        out = _run(eng.run(time.monotonic() + 50))
        self.assertEqual(out["reason"], "live_unavailable")

    def test_setup_error_is_live_unavailable(self):
        def bad_factory(env, **kw):
            class Bad(livemod.LiveSession):
                async def connect(self, **k):
                    raise livemod.LiveUnavailable("http 429")
            return Bad(env, **kw)
        eng = livemod.LiveEngine(object(), {},
                                 source=callmod.CallAudioSource(),
                                 session_factory=bad_factory)
        out = _run(eng.run(time.monotonic() + 50))
        self.assertEqual(out["reason"], "live_unavailable")

    def test_normal_close_is_not_unavailable(self):
        f = _session([
            {"serverContent": {
                "inputTranscription": {"text": "hello"}},
             },
            {"serverContent": {"turnComplete": True}},
            {"_closed": 1000}])
        eng = livemod.LiveEngine(object(), {},
                                 source=callmod.CallAudioSource(),
                                 session_factory=f)
        out = _run(eng.run(time.monotonic() + 50))
        self.assertNotEqual(out["reason"], "live_unavailable")
        self.assertEqual(out["heard"], ["hello"])
        self.assertEqual(out["engine"], "live")


class TestEngineOrder(unittest.TestCase):
    def _s(self, engine):
        return type("S", (), {"call_engine": engine})()

    def test_orders(self):
        env = {"GEMINI_API_KEY": "k"}
        self.assertEqual(livemod.engine_order(self._s("auto"), env),
                         ["live", "pipeline"])
        self.assertEqual(livemod.engine_order(self._s("live"), env),
                         ["live"])
        self.assertEqual(
            livemod.engine_order(self._s("pipeline"), env),
            ["pipeline"])

    def test_no_key_drops_live(self):
        self.assertEqual(
            livemod.engine_order(self._s("auto"), {}), ["pipeline"])


class TestConfig(unittest.TestCase):
    def test_flags(self):
        from pa.config import load_settings
        s = load_settings({})
        self.assertEqual(s.call_engine, "auto")
        self.assertEqual(s.live_model,
                         "gemini-3.1-flash-live-preview")
        self.assertEqual(s.live_voice, "Kore")
        self.assertFalse(s.live_budget)
        s = load_settings({"PA_CALL_ENGINE": "live",
                           "PA_LIVE_VOICE": "Puck",
                           "PA_LIVE_BUDGET": "true"})
        self.assertEqual(s.call_engine, "live")
        self.assertEqual(s.live_voice, "Puck")
        self.assertTrue(s.live_budget)
        with self.assertRaises(Exception):
            load_settings({"PA_CALL_ENGINE": "bogus"})


class TestLiveBudget(unittest.TestCase):
    """final-5 F3: live_min reserve/settle around a Live run."""

    def _env(self, **kw):
        from tests.helpers import TmpEnv
        return TmpEnv(**kw)

    def _ledger(self, env):
        conn = env.conn()
        row = conn.execute(
            "SELECT used, reserved FROM budget_ledger "
            "WHERE scope='call' AND resource='live_min'").fetchone()
        conn.close()
        return (row["used"], row["reserved"]) if row else (0, 0)

    def test_90s_session_charges_2_minutes(self):
        env = self._env()
        gate = livemod.LiveBudget(env.db_path, env.settings, env.env)
        self.assertTrue(gate.reserve(600))          # holds 10 min
        self.assertEqual(self._ledger(env), (0, 10))
        gate.settle(90.0, session_started=True)
        self.assertEqual(self._ledger(env), (2, 0))  # 2 used, hold gone

    def test_short_session_charges_minimum_1(self):
        env = self._env()
        gate = livemod.LiveBudget(env.db_path, env.settings, env.env)
        gate.reserve(60)
        gate.settle(4.0, session_started=True)
        self.assertEqual(self._ledger(env), (1, 0))

    def test_never_connected_charges_0(self):
        env = self._env()
        gate = livemod.LiveBudget(env.db_path, env.settings, env.env)
        gate.reserve(120)
        # setup failure before the socket: engine never reported live
        gate.settle(3.0, session_started=False)
        self.assertEqual(self._ledger(env), (0, 0))

    def test_cap_exhausted_refuses_reserve(self):
        env = self._env(PA_BUDGET_LIVE_MIN_CALL="1")
        conn = env.conn()
        from pa import budget
        # spend the single call-scope minute already
        self.assertTrue(budget.charge(conn, "call", "live_min", 1,
                                      s=env.settings, env=env.env))
        conn.close()
        gate = livemod.LiveBudget(env.db_path, env.settings, env.env)
        self.assertFalse(gate.reserve(600))
        self.assertIsNone(gate.conn)                 # nothing held

    def test_usage_row_visible(self):
        from pa import budget
        env = self._env()
        gate = livemod.LiveBudget(env.db_path, env.settings, env.env)
        gate.reserve(300)
        gate.settle(95.0, session_started=True)
        conn = env.conn()
        tops = [(r["scope"], r["resource"], r["used"])
                for r in budget.top_scopes(conn)]
        conn.close()
        self.assertIn(("call", "live_min", 2), tops)

    def test_off_flag_means_no_gate(self):
        # call.py only builds LiveBudget when settings.live_budget —
        # default off: identical to final-4 (no ledger rows written)
        env = self._env()
        self.assertFalse(env.settings.live_budget)

    def test_no_key_in_logs(self):
        import logging
        env = self._env()
        gate = livemod.LiveBudget(env.db_path, env.settings,
                                  {"GEMINI_API_KEY": "gk-secret-123"})
        logger = logging.getLogger("pa.live")
        records = []

        class Cap(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())

        cap = Cap()
        logger.addHandler(cap)
        try:
            gate.reserve(60)
            gate.settle(10.0, session_started=True)
        finally:
            logger.removeHandler(cap)
        self.assertNotIn("gk-secret-123", "\n".join(records))


if __name__ == "__main__":
    unittest.main()

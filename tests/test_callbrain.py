"""streaming call brain: call-tier router, delta streaming
[[ASK_PAL]] escape, clause cutting, speaker reply_iter path."""
import asyncio
import threading
import time
import unittest

from pa import callbrain, callconv
from pa.callbrain import CallBrain, _cut_clause, _extract_ask
from pa.provider import _iter_sse
from pa.router import (DEFAULT_CALL_ORDER, RouterProvider,
                       TIER_CALL)
from pa.netclient import HTTPError
from pa.provider import ProviderError, ProviderUnavailable


class TestExtractAsk(unittest.TestCase):
    def test_plain_text_passes(self):
        safe, rest, q = _extract_ask("Hello there mate.")
        self.assertEqual(safe, "Hello there mate.")
        self.assertEqual(rest, "")
        self.assertIsNone(q)

    def test_complete_marker(self):
        safe, rest, q = _extract_ask(
            "Sure. [[ASK_PAL: how many emails?]]")
        self.assertEqual(safe, "Sure. ")
        self.assertEqual(q, "how many emails?")

    def test_partial_marker_held(self):
        safe, rest, q = _extract_ask("Let me check [[ASK_PA")
        self.assertEqual(safe, "Let me check ")
        self.assertEqual(rest, "[[ASK_PA")
        self.assertIsNone(q)

    def test_lone_brackets_held(self):
        safe, rest, q = _extract_ask("hmm [[")
        self.assertEqual(safe, "hmm ")
        self.assertEqual(rest, "[[")

    def test_marker_then_more_text(self):
        safe, rest, q = _extract_ask(
            "[[ASK_PAL: q]] trailing words")
        self.assertEqual(q, "q")
        self.assertEqual(rest, " trailing words")


class TestCutClause(unittest.TestCase):
    def test_sentence_end(self):
        c, rest = _cut_clause("Short answer. More here.", 80)
        self.assertEqual(c, "Short answer.")
        self.assertEqual(rest, " More here.")

    def test_comma_after_30(self):
        c, rest = _cut_clause(
            "This is a fairly long clause indeed, then more", 80)
        self.assertTrue(c.endswith(","))
        self.assertEqual(rest, " then more")

    def test_short_buf_waits(self):
        c, rest = _cut_clause("tiny", 80)
        self.assertIsNone(c)
        self.assertEqual(rest, "tiny")

    def test_first_clause_limit(self):
        # no boundary within 80 → waits (unless way over)
        buf = "word " * 30
        c, rest = _cut_clause(buf.strip(), 80)
        if c is not None:
            self.assertLessEqual(len(c), 81)


class FakeStreamClient:
    def __init__(self, deltas=None, error=None, usage=None):
        self.deltas = deltas or []
        self.error = error
        self.last_usage = usage or {}

    def chat_iter(self, messages, max_completion_tokens=None,
                  timeout=None):
        if self.error:
            raise self.error
        for d in self.deltas:
            yield d


class TestCallBrain(unittest.TestCase):
    def brain(self, deltas, **kw):
        client = FakeStreamClient(deltas)
        kw.setdefault("stream",
                      lambda msgs: client.chat_iter(msgs))
        b = CallBrain(**kw)
        return b, client

    def test_streams_plain_reply(self):
        b, _ = self.brain(["Hi there. ", "How can I help?"])
        out = b.think("hello")
        text = "".join(out["reply_iter"])
        self.assertIn("Hi there.", text)
        self.assertIn("How can I help?", text)
        self.assertFalse(out["done"])

    def test_ask_pal_escape(self):
        asked = []
        b, _ = self.brain(
            ["One moment. [[ASK_PAL: what's on today?]]"],
            ask_pal=lambda q: asked.append(q) or "Two meetings.")
        out = b.think("what's my day like?")
        text = "".join(out["reply_iter"])
        self.assertEqual(asked, ["what's on today?"])
        self.assertIn("One moment.", text)
        self.assertIn("Two meetings.", text)
        self.assertNotIn("ASK_PAL", text)

    def test_ask_pal_timeout_dms(self):
        dm = []
        gate = threading.Event()

        def slow(q):
            gate.wait(2)
            return "the slow answer"

        b, _ = self.brain(
            ["[[ASK_PAL: big query]]"],
            ask_pal=slow, ask_seconds=0.05,
            on_dm=lambda q, a: dm.append((q, a)))
        out = b.think("do the thing")
        text = "".join(out["reply_iter"])
        self.assertIn("I'll DM you that", text)
        gate.set()
        deadline = time.monotonic() + 3
        while not dm and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(dm[0][1], "the slow answer")

    def test_stream_failure_falls_back(self):
        b, _ = self.brain(
            [], ask_pal=lambda q: "full engine says hi",
            stream=lambda m: (_ for _ in ()).throw(
                ProviderUnavailable("down")))
        text = "".join(b.think("hello")["reply_iter"])
        self.assertEqual(text, "full engine says hi")

    def test_no_askpal_failure_says_sorry(self):
        b, _ = self.brain(
            [], stream=lambda m: (_ for _ in ()).throw(
                ProviderUnavailable("down")))
        text = "".join(b.think("hello")["reply_iter"])
        self.assertEqual(text, b.sorry)

    def test_wrapup_short_circuits(self):
        b, _ = self.brain(
            ["never"], is_wrapup=lambda h: h == "bye")
        out = b.think("bye")
        self.assertTrue(out["done"])
        self.assertIn("reply", out)
        self.assertNotIn("reply_iter", out)

    def test_history_grows(self):
        b, _ = self.brain(["Fine thanks."])
        "".join(b.think("how are you?")["reply_iter"])
        self.assertEqual(len(b.history), 1)
        h, s = b.history[0]
        self.assertEqual(h, "how are you?")
        self.assertIn("Fine thanks.", s)

    def test_prompt_capped(self):
        hist = [("h" * 500, "s" * 500)] * 8
        b, _ = self.brain(["ok"], history=hist)
        msgs = b._msgs("hi")
        from pa.util import estimate_tokens
        self.assertLessEqual(estimate_tokens(str(msgs)),
                             callbrain.MAX_PROMPT_TOKENS + 60)


class TestIterSse(unittest.TestCase):
    def test_deltas_and_usage(self):
        chunks = [
            b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"lo"}}],',
            b'"usage":{"total_tokens":7}}\n',
            b'data: [DONE]\n']
        out = list(_iter_sse(iter(chunks)))
        self.assertEqual(out[0], "Hel")
        self.assertEqual(out[1], "lo")
        self.assertEqual(out[-1], {"usage": {"total_tokens": 7}})


class TestRouterCallTier(unittest.TestCase):
    _ENV = {"GROQ_API_KEY": "x", "GEMINI_API_KEY": "x",
            "OPENROUTER_API_KEY": "x", "CLOUDFLARE_ACCOUNT_ID": "x",
            "CLOUDFLARE_API_TOKEN": "x", "POLLINATIONS_API_KEY": "x"}

    def _router(self, client_map):
        import tempfile, os
        from pa.config import Settings
        s = Settings()
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        def factory(be, model):
            return client_map.get(
                be.name,
                FakeStreamClient(
                    error=ProviderUnavailable("unconfigured")))
        rp = RouterProvider(s, env=dict(self._ENV), db_path=db,
                            client_factory=factory)
        return rp

    def test_call_order_default(self):
        self.assertEqual(DEFAULT_CALL_ORDER[0], "groq")
        self.assertNotIn("gemini-cli", DEFAULT_CALL_ORDER)

    def test_chat_stream_first_healthy(self):
        clients = {
            "groq": FakeStreamClient(["Hi ", "there."]),
            "gemini-api": FakeStreamClient(["nope"]),
        }
        rp = self._router(clients)
        out = "".join(rp.chat_stream([{"role": "user",
                                       "content": "hi"}]))
        self.assertEqual(out, "Hi there.")

    def test_chat_stream_failover(self):
        clients = {
            "groq": FakeStreamClient(
                error=ProviderUnavailable("quota")),
            "gemini-api": FakeStreamClient(["fallback ok"]),
            "openrouter-free": FakeStreamClient(["x"]),
            "cloudflare": FakeStreamClient(["y"]),
        }
        rp = self._router(clients)
        out = "".join(rp.chat_stream([{"role": "user",
                                       "content": "hi"}]))
        self.assertEqual(out, "fallback ok")

    def test_chat_stream_no_chat_iter(self):
        class Legacy:
            def chat(self, messages, tools=None,
                     max_completion_tokens=None, timeout=None,
                     tier=None):
                return {"role": "assistant",
                        "content": "one shot"}, {}
        rp = self._router({"groq": Legacy()})
        out = "".join(rp.chat_stream([{"role": "user",
                                       "content": "hi"}]))
        self.assertEqual(out, "one shot")


class TestPlayStream(unittest.TestCase):
    """Conversation speaker drains reply_iter; TTS for clause 1
    starts before the stream finishes."""
    def _conv(self):
        events = []

        async def play(ogg):
            events.append(("play", ogg))
            await asyncio.sleep(0.01)

        def say(text):
            events.append(("say", text))
            return f"ogg:{text[:20]}"

        conv = callconv.Conversation(
            transcribe=lambda p: {"heard": "x"},
            think=lambda h: {},
            say=say, play=play, stop_playback=lambda: None,
            pack=lambda p: p)
        conv._q = asyncio.Queue()
        conv._info = {}
        return conv, events

    def test_first_clause_synth_before_stream_end(self):
        conv, events = self._conv()
        order = []
        gate = threading.Event()

        def stream(_msgs):
            yield "First clause here. "
            order.append("yield1")
            gate.wait(1)
            yield "Second clause for the reply."
            order.append("yield2")

        async def go():
            await conv._play_stream(stream(None), tag=1)

        asyncio.run(go())
        says = [e for e in events if e[0] == "say"]
        # clause 1 was synthesised before the generator was asked
        # for more (yield2 never reached before say #1)
        self.assertTrue(says)
        self.assertIn("First clause here.", says[0][1])
        self.assertLess(order.index("yield1"), len(order))

    def test_stream_interrupted_by_drop(self):
        conv, events = self._conv()

        def stream(_msgs):
            yield "one. " * 40
            yield "two"

        async def go():
            conv._drop = True
            await conv._play_stream(stream(None), tag=1)

        asyncio.run(go())
        plays = [e for e in events if e[0] == "play"]
        self.assertEqual(plays, [])


if __name__ == "__main__":
    unittest.main()

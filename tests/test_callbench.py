"""V8 call-bench: fake sessions/streams drive first-audio metrics."""
import asyncio
import tempfile
import unittest

from pa import callbench
from tests.helpers import TmpEnv


class FakeLiveEngine:
    """LiveEngine stand-in: 'model audio' lands in the source after a
    short delay."""
    delay = 0.02

    def __init__(self, source=None, **_kw):
        self.source = source

    async def run(self, deadline, opening=None):
        await asyncio.sleep(self.delay)
        if self.source is not None:
            self.source.feed(b"\x00" * 960)
        return {"reason": "ended", "turns": 1, "heard": [], "said": []}


class TestBenchPipeline(unittest.TestCase):
    def test_first_audio_measured(self):
        def stream(_msgs):
            yield "Hello there, "
            yield "this is a long clause that keeps going, "
            yield "and ends here."
        said = []
        rows = callbench.bench_pipeline(
            None, {}, n=2, stream=stream, say=said.append,
            prompts=["p1", "p2"])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["first_audio_ms"] is not None
                            for r in rows))
        self.assertEqual(len(said), 2)      # one clause synthesised

    def test_stream_error_recorded(self):
        def stream(_msgs):
            raise RuntimeError("down")
            yield
        rows = callbench.bench_pipeline(
            None, {}, n=1, stream=stream, say=lambda c: None,
            prompts=["p"])
        self.assertEqual(rows[0]["error"], "RuntimeError: down")
        self.assertIsNone(rows[0]["first_audio_ms"])

    def test_stats(self):
        rows = [{"first_audio_ms": 100, "total_ms": 300,
                 "error": None}] * 4
        st = callbench._stats(rows)
        self.assertEqual(st["n"], 4)
        self.assertIsNotNone(st["first_p50"])
        self.assertEqual(st["errors"], 0)


class TestBenchLive(unittest.TestCase):
    def test_live_first_audio(self):
        rows = callbench.bench_live(
            None, {}, n=2,
            engine_factory=lambda **kw: FakeLiveEngine(**kw),
            prompts=["p1", "p2"])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["first_audio_ms"] is not None
                            for r in rows))
        self.assertTrue(all(r["error"] is None for r in rows))


class TestRun(unittest.TestCase):
    def test_run_writes_metrics_and_prints(self):
        t = TmpEnv()
        printed = []
        res = callbench.run(
            t.settings, {}, n=2, engines=["pipeline"],
            stream=lambda m: iter(["short answer."]),
            say=lambda c: None, prompts=["a", "b"],
            out=printed.append)
        self.assertIn("pipeline", res)
        self.assertEqual(res["pipeline"]["n"], 2)
        self.assertTrue(any("p50" in p for p in printed))
        # bench rows landed in call_metrics as engine='bench'
        conn = t.conn()
        n = conn.execute(
            "SELECT COUNT(*) c FROM call_metrics WHERE engine='bench'"
        ).fetchone()["c"]
        conn.close()
        self.assertGreaterEqual(n, 1)


if __name__ == "__main__":
    unittest.main()

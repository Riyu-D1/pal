"""final-1 V1: call_metrics rows per turn + per call, and the
`/call stats` rendering (zero rows and with data)."""

import asyncio
import unittest

from pa import call as callmod
from pa import callconv
from pa import db as dbmod

from tests.helpers import TmpEnv

FRAME = callconv.FRAME_BYTES
VOICED = b"\x10\x40" * (FRAME // 2)
QUIET = b"\x00" * FRAME


class FakeVAD:
    def is_speech(self, frame):
        return frame == VOICED


def _endpointer(**kw):
    kw.setdefault("min_speech_ms", 60)
    kw.setdefault("barge_voiced_ms", 60)
    kw.setdefault("barge_window_ms", 200)
    kw.setdefault("barge_grace_s", 0)
    kw.setdefault("tail_ms", 80)
    kw.setdefault("tail_after_short_ms", 120)
    kw.setdefault("short_ms", 1000)
    return callconv.Endpointer(FakeVAD(), **kw)


class FakeIO:
    def __init__(self, heard="what's the time", reply="It's noon.",
                 noise=False):
        self.noise = noise
        self.heard = heard
        self.reply = reply
        self.plays = []
        self.stops = 0
        self._stop = None
        self._rel = None

    def bind(self):
        self._stop = None
        self._rel = None

    def release(self):
        if self._rel is not None:
            self._rel.set()

    def transcribe(self, wav):
        return {"heard": self.heard, "noise": self.noise}

    def think(self, heard):
        return {"reply": self.reply, "done": False,
                "metrics": {"llm_backend": "groq",
                            "llm_first_token_ms": 40,
                            "llm_total_ms": 110}}

    def say(self, text):
        return b"ogg:" + text.encode()

    def say_cached(self, text):
        return self.say(text)

    async def play(self, ogg):
        self.plays.append(ogg)
        if self._stop is None:
            self._stop = asyncio.Event()
            self._rel = asyncio.Event()
        self._stop.clear()
        t = asyncio.create_task(self._stop.wait())
        done, _p = await asyncio.wait({t}, timeout=0.03)
        t.cancel()

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


async def _feed_utt(conv, voiced=6, tail=8, gap=0.001):
    for _ in range(voiced):
        conv.feed_frame(VOICED)
        await asyncio.sleep(gap)
    for _ in range(tail):
        conv.feed_frame(None)
        await asyncio.sleep(gap)


async def _until(pred, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.005)
    return False


class TestTurnMetrics(unittest.TestCase):
    def test_turn_row_fields(self):
        rows = []
        io_ = FakeIO()
        io_.bind()
        conv = callconv.Conversation(
            transcribe=io_.transcribe, think=io_.think, say=io_.say,
            say_cached=io_.say_cached, play=io_.play,
            stop_playback=io_.stop_playback,
            config=_cfg(), endpointer=_endpointer(),
            metrics=rows.append,
            say_info=lambda: {"tts_provider": "cloudflare"},
            engine="pipeline")

        async def main():
            task = asyncio.create_task(
                conv.run(asyncio.get_running_loop().time() + 5))
            await _feed_utt(conv)
            self.assertTrue(await _until(lambda: io_.plays))
            await _until(lambda: rows)
            conv.request_stop("test_done")
            return await task

        asyncio.run(asyncio.wait_for(main(), timeout=8))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["heard"], "what's the time")
        self.assertEqual(r["turn_seq"], 1)
        self.assertEqual(r["eos_ms"], 120)     # tail_after_short
        self.assertIsNotNone(r["stt_ms"])
        self.assertEqual(r["llm_backend"], "groq")
        self.assertEqual(r["llm_first_token_ms"], 40)
        self.assertEqual(r["llm_total_ms"], 110)
        self.assertEqual(r["tts_provider"], "cloudflare")
        self.assertIsNotNone(r["tts_first_ms"])
        self.assertIsNotNone(r["first_audio_ms"])
        self.assertEqual(r["fillers"], 0)
        self.assertEqual(r["interrupted"], 0)

    def test_dropped_turn_recorded(self):
        rows = []
        io_ = FakeIO(heard="", noise=True)
        io_.bind()
        conv = callconv.Conversation(
            transcribe=io_.transcribe, think=io_.think, say=io_.say,
            play=io_.play, stop_playback=io_.stop_playback,
            config=_cfg(), endpointer=_endpointer(),
            metrics=rows.append)

        async def main():
            task = asyncio.create_task(
                conv.run(asyncio.get_running_loop().time() + 5))
            await _feed_utt(conv)
            self.assertTrue(await _until(lambda: rows))
            conv.request_stop("test_done")
            return await task

        asyncio.run(asyncio.wait_for(main(), timeout=8))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dropped"], "noise")
        self.assertEqual(rows[0]["heard"], "")
        self.assertIsNotNone(rows[0]["stt_ms"])


class TestMigrationAndWriter(unittest.TestCase):
    def test_migration_27_table(self):
        t = TmpEnv()
        conn = t.conn()
        self.addCleanup(conn.close)
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(call_metrics)")}
        for c in ("call_id", "kind", "engine", "turn_seq", "eos_ms",
                  "stt_ms", "llm_backend", "llm_first_token_ms",
                  "llm_total_ms", "tts_provider", "tts_first_ms",
                  "first_audio_ms", "gaps_ms", "fillers", "barge_in_ms",
                  "rx_ok", "rx_fail", "rx_silence", "rx_padded",
                  "peak_rss_mb", "ffmpeg_spawns"):
            self.assertIn(c, cols)

    def test_recorder_roundtrip(self):
        t = TmpEnv()
        conn = t.conn()
        self.addCleanup(conn.close)
        rec = callmod.CallMetrics(t.db_path, "call-abc", engine="live")
        rec.turn({"turn_seq": 1, "heard": "hi", "eos_ms": 500,
                  "stt_ms": 300, "llm_backend": "gemini",
                  "llm_total_ms": 800, "tts_provider": "azure",
                  "tts_first_ms": 200, "first_audio_ms": 1100,
                  "gaps_ms": 40, "fillers": 1, "interrupted": 0})
        rec.call({"rx_ok": 50, "rx_fail": 1, "rx_silence": 900,
                  "rx_padded": 12, "peak_rss_mb": 220,
                  "ffmpeg_spawns": 0, "call_seconds": 61.5,
                  "turns": 1, "reason": "bye"})
        rec.close()
        rows = conn.execute(
            "SELECT * FROM call_metrics WHERE call_id='call-abc'"
            " ORDER BY kind").fetchall()
        self.assertEqual(len(rows), 2)
        call_row = [r for r in rows if r["kind"] == "call"][0]
        turn_row = [r for r in rows if r["kind"] == "turn"][0]
        self.assertEqual(call_row["engine"], "live")
        self.assertEqual(call_row["rx_padded"], 12)
        self.assertEqual(call_row["ffmpeg_spawns"], 0)
        self.assertEqual(turn_row["first_audio_ms"], 1100)
        self.assertEqual(turn_row["fillers"], 1)

    def test_recorder_never_raises(self):
        rec = callmod.CallMetrics("", "x")          # no db_path
        rec.turn({"turn_seq": 1})
        rec.call({"reason": "x"})
        rec.close()


class FakeRuntime:
    """run_call fake for the CallManager metrics path."""
    def __init__(self):
        self.kw = None

    def run_call(self, **kw):
        self.kw = kw
        if kw.get("metrics") is not None:
            kw["metrics"].turn({"turn_seq": 1, "heard": "hi",
                                "first_audio_ms": 900})
        return {"reason": "bye", "turns": 1, "heard": ["hi"],
                "rx": {"ok": 10, "fail": 0, "silence": 5, "padded": 2}}

    def stop(self):
        pass


class FakeApi:
    def __init__(self):
        self.posts = []
        self.channels = [{"id": "vc-1", "type": 2, "name": "pal-call"}]

    def guild_channels(self, guild):
        return self.channels

    def open_dm(self, uid):
        return "dm-1"

    def post_message(self, channel, text, **kw):
        self.posts.append((channel, text))
        return True


class TestManagerEndToEnd(unittest.TestCase):
    def _manager(self, t, rt):
        api = FakeApi()
        cm = callmod.CallManager(
            t.settings, t.env, api, db_path=t.db_path,
            owner_id="o-1", runtime_factory=lambda *a, **k: rt)
        return cm, api

    def test_call_writes_call_row(self):
        t = TmpEnv(PA_CALL_GUILD_ID="g-1")
        conn = t.conn()
        self.addCleanup(conn.close)
        rt = FakeRuntime()
        cm, api = self._manager(t, rt)
        self.assertTrue(cm.call("hi", urgent=True)["calling"])
        from tests.helpers import wait_for
        self.assertTrue(wait_for(
            lambda: conn.execute(
                "SELECT COUNT(*) n FROM call_metrics"
                " WHERE kind='call'").fetchone()["n"] == 1))
        row = conn.execute(
            "SELECT * FROM call_metrics WHERE kind='call'").fetchone()
        self.assertEqual(row["reason"], "bye")
        self.assertEqual(row["turns"], 1)
        self.assertEqual(row["rx_silence"], 5)
        self.assertEqual(row["rx_padded"], 2)
        self.assertIsNotNone(row["peak_rss_mb"])
        self.assertIsNotNone(row["ffmpeg_spawns"])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) n FROM call_metrics"
                         " WHERE kind='turn'").fetchone()["n"], 1)
        cm.stop()

    def test_metrics_off_writes_nothing(self):
        t = TmpEnv(PA_CALL_GUILD_ID="g-1", PA_CALL_METRICS="false")
        conn = t.conn()
        self.addCleanup(conn.close)
        rt = FakeRuntime()
        cm, _api = self._manager(t, rt)
        cm.call("hi", urgent=True)
        from tests.helpers import wait_for
        wait_for(lambda: rt.kw is not None)
        self.assertIsNone(rt.kw["metrics"])
        cm.stop()


class TestStatsText(unittest.TestCase):
    def test_zero_rows(self):
        t = TmpEnv()
        conn = t.conn()
        self.addCleanup(conn.close)
        out = callmod.stats_text(conn)
        self.assertIn("no call metrics", out)

    def test_with_data(self):
        t = TmpEnv()
        conn = t.conn()
        self.addCleanup(conn.close)
        rec = callmod.CallMetrics(t.db_path, "c-1")
        for i, fa in enumerate((400, 800, 1200), start=1):
            rec.turn({"turn_seq": i, "heard": f"h{i}", "eos_ms": 500,
                      "stt_ms": 100 * i, "llm_backend": "groq",
                      "llm_total_ms": 200 * i, "tts_provider": "azure",
                      "tts_first_ms": 150, "first_audio_ms": fa,
                      "gaps_ms": 30, "fillers": 1, "interrupted": 0})
        rec.call({"rx_ok": 100, "rx_fail": 2, "rx_silence": 50,
                  "rx_padded": 7, "peak_rss_mb": 200,
                  "ffmpeg_spawns": 0, "call_seconds": 42.0,
                  "turns": 3, "reason": "bye"})
        rec.close()
        out = callmod.stats_text(conn)
        self.assertIn("last call c-1", out)
        self.assertIn("pipeline", out)
        self.assertIn("rx ok=100 fail=2 silence=50 padded=7", out)
        self.assertIn("ffmpeg spawns 0", out)
        self.assertIn("t1:", out)
        self.assertIn("first-audio: p50=800ms", out)
        self.assertIn("1 calls, 3 turns", out)
        # via the /call command path too
        from pa import discordbot
        self.assertTrue(hasattr(discordbot.DiscordBot, "_call_cmd"))


if __name__ == "__main__":
    unittest.main()

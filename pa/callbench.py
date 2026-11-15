"""python -m pa call-bench — owner-run call-latency benchmark.

N synthetic prompts through each engine, measuring what the owner
hears: time to first audio (Live: first model PCM chunk lands in the
source; pipeline: first clause's TTS returns) and total turn time.
Rows go to call_metrics with engine='bench' so /call stats can show
them; nothing here prints or logs keys. Not run in CI — tests drive
it with fake sessions/streams.
"""

import asyncio
import logging
import statistics
import time

from .callbrain import _cut_clause

log = logging.getLogger("pa.callbench")

PROMPTS = [
    "What time is it?",
    "Summarise my unread emails in one line.",
    "What's on my calendar today?",
    "Tell me a one-line joke.",
    "Remind me what we talked about earlier.",
]


def _pct(vals, p):
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    return int(statistics.quantiles(vals, n=100)[p - 1])


def _stats(rows):
    first = [r["first_audio_ms"] for r in rows
             if r.get("first_audio_ms") is not None]
    total = [r["total_ms"] for r in rows
             if r.get("total_ms") is not None]
    return {"n": len(rows), "first_p50": _pct(first, 50),
            "first_p90": _pct(first, 90), "total_p50": _pct(total, 50),
            "total_p90": _pct(total, 90),
            "errors": sum(1 for r in rows if r.get("error"))}


class _ProbeSource:
    """CallAudioSource-shaped probe: records when the first model PCM
    chunk lands (that's first audio for the owner)."""
    def __init__(self):
        self.t_first = None
        self.fed = 0
        self._mono = time.monotonic

    def feed(self, pcm):
        if self.t_first is None:
            self.t_first = self._mono()
        self.fed += len(pcm or b"")

    def clear(self):
        pass

    def close(self):
        pass


def bench_live(s, env, n=5, engine_factory=None, prompts=None,
               timeout=20.0):
    """One Live session per prompt, text-in via clientContent.
    engine_factory is injectable for tests; default LiveEngine."""
    from . import live as livemod
    prompts = (prompts or PROMPTS)[:n]
    rows = []
    for p in prompts:
        src = _ProbeSource()
        eng = (engine_factory or (lambda **kw: livemod.LiveEngine(
            s, env, **kw)))(source=src)
        t0 = time.monotonic()

        async def go():
            return await eng.run(t0 + timeout, opening=p)
        try:
            asyncio.run(asyncio.wait_for(go(), timeout=timeout + 5))
        except Exception as e:
            rows.append({"prompt": p, "first_audio_ms": None,
                         "total_ms": int((time.monotonic() - t0) * 1000),
                         "error": f"{type(e).__name__}: {e}"[:120]})
            continue
        total = int((time.monotonic() - t0) * 1000)
        first = int((src.t_first - t0) * 1000) \
            if src.t_first is not None else None
        rows.append({"prompt": p, "first_audio_ms": first,
                     "total_ms": total,
                     "error": None if first is not None
                     else "no_audio"})
    return rows


def bench_pipeline(s, env, n=5, stream=None, say=None,
                   prompts=None, router=None):
    """LLM stream -> clause cut -> TTS of clause 1 = first audio.
    stream/say injectable for tests."""
    prompts = (prompts or PROMPTS)[:n]
    if stream is None:
        if router is None:
            from .router import RouterProvider
            router = RouterProvider(s, env)
        stream = lambda msgs: router.chat_stream(
            msgs, max_completion_tokens=220)
    if say is None:
        from . import voice as voicemod
        say = voicemod.CallTTS(s, env).say
    rows = []
    for p in prompts:
        msgs = [{"role": "user", "content": p}]
        t0 = time.monotonic()
        first_audio = None
        buf = ""
        err = None
        try:
            for d in stream(msgs):
                buf += d
                cut, buf = _cut_clause(buf, 80)
                if cut:
                    say(cut)
                    first_audio = int((time.monotonic() - t0) * 1000)
                    break
            if first_audio is None and buf.strip():
                say(buf.strip())
                first_audio = int((time.monotonic() - t0) * 1000)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:120]
        total = int((time.monotonic() - t0) * 1000)
        rows.append({"prompt": p, "first_audio_ms": first_audio,
                     "total_ms": total,
                     "error": err or (None if first_audio is not None
                                      else "no_audio")})
    return rows


def run(s, env, n=5, engines=("live", "pipeline"), db_path=None,
        out=print, **kw):
    """Full bench: per-engine rows, stats, metrics rows. Returns
    {"live": stats, "pipeline": stats}."""
    from .call import CallMetrics
    results = {}
    metric = CallMetrics(db_path or s.db_path,
                         f"bench-{int(time.time())}", engine="bench") \
        if (db_path or getattr(s, "db_path", None)) else None
    for eng in engines:
        if eng == "live":
            rows = bench_live(s, env, n=n, **{
                k: v for k, v in kw.items()
                if k in ("engine_factory", "prompts", "timeout")})
        else:
            rows = bench_pipeline(s, env, n=n, **{
                k: v for k, v in kw.items()
                if k in ("stream", "say", "prompts", "router")})
        for r in rows:
            out(f"  [{eng}] {r['first_audio_ms'] or '-':>6}ms first "
                f"({r['total_ms']}ms total) {r['prompt'][:40]!r}"
                + (f"  ERR {r['error']}" if r.get("error") else ""))
            if metric is not None:
                try:
                    metric.turn({"turn_seq": 0,
                                 "heard": r["prompt"][:200],
                                 "first_audio_ms":
                                     r["first_audio_ms"],
                                 "engine": "bench"})
                except Exception:
                    pass
        results[eng] = _stats(rows)
        st = results[eng]
        out(f"[{eng}] n={st['n']} first-audio p50="
            f"{st['first_p50']}ms p90={st['first_p90']}ms · "
            f"total p50={st['total_p50']}ms p90={st['total_p90']}ms "
            f"· errors={st['errors']}")
    if metric is not None:
        try:
            metric.call({"engine": "bench"})
        except Exception:
            pass
    return results

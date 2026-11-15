"""Token-aware pacing and daily quota.

8K TPM is a per-minute *total*: two sequential tool turns can exceed it
even if each request is under 8K. So requests wait on shared buckets:

- minute buckets: tokens (TPM) and requests (RPM)
- daily counters: requests and tokens persisted in usage_days so they
  survive restarts; we stop *before* the configured ceiling.

Buckets are refreshed from x-ratelimit-* headers on every response.
"""

import datetime
import threading
import time

from . import db as dbmod
from .provider import ProviderRateLimit, parse_reset_seconds


class QuotaExhausted(Exception):
    kind = "quota_exceeded"

    def __init__(self, message: str, reset_at: str):
        super().__init__(message)
        self.reset_at = reset_at


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def tomorrow_reset() -> str:
    nxt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    return nxt.strftime("%Y-%m-%d") + "T00:00:00+00:00"


class Pacer:
    def __init__(self, settings, conn, now=None, sleep=None):
        self.s = settings
        self.conn = conn
        self._lock = threading.Lock()
        self._now = now or time.monotonic
        self._sleep = sleep or time.sleep
        now_ = self._now()
        self._minute_start = now_
        self._minute_tokens = 0
        self._minute_requests = 0
        self._extra_wait_until = now_  # learned from headers

    # -- minute buckets ----------------------------------------------------
    def _roll_minute(self, now):
        if now - self._minute_start >= 60.0:
            self._minute_start = now
            self._minute_tokens = 0
            self._minute_requests = 0

    def _minute_wait_locked(self, est_in, est_out, now):
        self._roll_minute(now)
        need = est_in + est_out
        wait = 0.0
        if self._minute_requests + 1 > self.s.rpm_limit or \
                self._minute_tokens + need > self.s.tpm_limit:
            wait = max(wait, 60.0 - (now - self._minute_start))
        if now < self._extra_wait_until:
            wait = max(wait, self._extra_wait_until - now)
        return wait

    def acquire(self, est_input_tokens: int, est_output_tokens: int,
                deadline: float = None):
        """Wait for minute-bucket capacity and check daily counters.

        Raises QuotaExhausted before the configured daily ceiling.
        deadline: monotonic time after which we give up waiting.
        Returns estimated wait used (for tests/logging)."""
        total_waited = 0.0
        while True:
            with self._lock:
                now = self._now()
                wait = self._minute_wait_locked(
                    est_input_tokens, est_output_tokens, now)
                if wait <= 0:
                    # daily ceiling check happens inside the lock so two
                    # consecutive acquires can't both pass
                    day = today()
                    u = dbmod.usage_today(self.conn, day)
                    if u["requests"] + 1 > self.s.daily_request_limit:
                        raise QuotaExhausted(
                            "daily request budget reached",
                            reset_at=tomorrow_reset())
                    if (u["tokens_in"] + u["tokens_out"] + est_input_tokens
                            + est_output_tokens) > self.s.daily_token_limit:
                        raise QuotaExhausted(
                            "daily token budget reached",
                            reset_at=tomorrow_reset())
                    self._minute_requests += 1
                    self._minute_tokens += est_input_tokens + est_output_tokens
                    return total_waited
                if deadline is not None and now + wait > deadline:
                    raise QuotaExhausted(
                        "timed out waiting for per-minute rate capacity",
                        reset_at=datetime.datetime.fromtimestamp(
                            time.time() + wait,
                            datetime.timezone.utc).isoformat(timespec="seconds"))
            slept = min(wait, 5.0)
            self._sleep(slept)
            total_waited += slept
            if deadline is not None and self._now() > deadline:
                raise QuotaExhausted(
                    "timed out waiting for per-minute rate capacity",
                    reset_at=datetime.datetime.now(
                        datetime.timezone.utc).isoformat(timespec="seconds"))

    # -- learning from responses -------------------------------------------
    def observe_headers(self, headers: dict):
        """Tighten buckets from x-ratelimit-* headers when present."""
        if not headers:
            return
        with self._lock:
            now = self._now()
            rem_t = _to_int(headers.get("x-ratelimit-remaining-tokens"))
            rem_r = _to_int(headers.get("x-ratelimit-remaining-requests"))
            if rem_t is not None:
                used = self.s.tpm_limit - rem_t
                if used > self._minute_tokens:
                    self._minute_tokens = used
            if rem_r is not None:
                used = self.s.rpm_limit - rem_r
                if used > self._minute_requests:
                    self._minute_requests = used
            for k in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
                secs = parse_reset_seconds(headers.get(k))
                if secs:
                    self._extra_wait_until = max(
                        self._extra_wait_until, now + min(secs, 120))

    def record_usage(self, usage: dict):
        """Persist reported usage into the daily counter."""
        pin = int(usage.get("prompt_tokens") or 0)
        pout = int(usage.get("completion_tokens") or 0)
        day = today()
        with self._lock:
            self.conn.execute(
                "INSERT INTO usage_days(day, requests, tokens_in, tokens_out) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "requests=requests+1, tokens_in=tokens_in+?, "
                "tokens_out=tokens_out+?",
                (day, 1, pin, pout, pin, pout))
            # reconcile minute bucket with real usage
            actual = pin + pout
            if actual < self._minute_tokens:
                self._minute_tokens = actual

    def retry_delay_for(self, exc: ProviderRateLimit, attempt: int) -> float:
        """Bounded backoff honouring retry-after."""
        base = exc.retry_after if exc.retry_after else min(2 ** attempt, 20)
        return min(base, 60.0)


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

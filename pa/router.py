"""Free-API model router ("mini Freebuff") — one provider, many backends.

An assistant pinned to provider_host='router' gets this provider. Per
call it walks an ordered list of free-tier OpenAI-compatible backends
plus optional official-CLI session sidecars:

- tier 'smart' (agent/tool steps) and 'fast' (plain chat, summaries)
  each have their own backend order and per-backend model; a call falls
  through to the other tier's list when its own is exhausted.
- Backends whose key env vars are unset read as "needs config" and are
  never called. 429 / 5xx / timeout / quota errors cool a backend down
  (honouring Retry-After and rate-limit reset headers, else exponential
  backoff) and the SAME call tries the next backend.
- Per-minute and per-day request/token usage is tracked in SQLite
  (migration 6); a backend is skipped *before* a configured cap is hit.
- Official-CLI sidecars (gemini-cli/antigravity `agy`, copilot-cli) are
  agent sessions: they get the flattened transcript and are skipped for
  calls that carry real OpenAI tool schemas (they can't emit tool_calls).

Keys come only from env vars; nothing here logs or prints them.
"""

import datetime
import logging
import os
import re
import threading
import time

from . import db as dbmod
from .evolve import incidents as incidentsmod
from .provider import (ChatProvider, ProviderAuthError,
                       ProviderBadResponse, ProviderError,
                       ProviderNotConfigured, ProviderRateLimit,
                       ProviderUnavailable, parse_reset_seconds)
from .util import estimate_tokens, json_dumps, now_iso, parse_iso, utcnow

log = logging.getLogger("pa.router")

TIER_FAST = "fast"
TIER_SMART = "smart"
TIER_ESCALATE = "escalate"
TIER_CALL = "call"


class Backend:
    """One routable backend preset.

    kind 'api':     OpenAI-compatible endpoint; `base_url` may contain
                    {ENV_VAR} placeholders filled from the environment.
                    `key_env` holds the bearer token (optional only for
                    genuinely anonymous endpoints like Pollinations).
    kind 'sidecar': official-CLI agent session behind the freebuff wire
                    protocol on loopback; `url_setting`/`token_env` point
                    at the Settings field and env var that configure it.
    """

    def __init__(self, name, kind, base_url, requires, models, caps,
                 key_env=None, url_setting=None, note=""):
        self.name = name
        self.kind = kind
        self.base_url = base_url
        self.requires = tuple(requires)
        self.models = dict(models)
        self.caps = dict(caps)
        self.key_env = key_env
        self.url_setting = url_setting
        self.note = note

    def clone(self):
        return Backend(self.name, self.kind, self.base_url, self.requires,
                       self.models, self.caps, self.key_env,
                       self.url_setting, self.note)

    def configured(self, env, settings) -> bool:
        if self.kind == "sidecar" and not getattr(
                settings, self.url_setting, ""):
            return False
        return all((env.get(k) or "").strip() for k in self.requires)

    def missing_env(self, env) -> list:
        return [k for k in self.requires if not (env.get(k) or "").strip()]

    def model_for(self, tier: str) -> str:
        return self.models.get(tier) or self.models.get(TIER_SMART) or ""

    def usable(self, tools_present: bool) -> bool:
        # Session backends take the flattened transcript; they cannot
        # answer OpenAI tool_call schemas, so they only serve tool-free
        # calls (the fenced-JSON chat protocol is plain text either way).
        return self.kind == "api" or not tools_present

    def url_for(self, env, settings) -> str:
        if self.kind == "sidecar":
            return getattr(settings, self.url_setting, "")
        return _ENV_REF.sub(lambda m: env.get(m.group(1), ""),
                            self.base_url)

    def api_key(self, env):
        return (env.get(self.key_env) or None) if self.key_env else None


_ENV_REF = re.compile(r"\{([A-Z0-9_]+)\}")

# ---------------------------------------------------------------------------
# Presets — verified against each provider's official docs on 2026-11-10
# (see docs/ROUTER.md for the per-provider citations). Caps are the
# documented free-tier numbers, deliberately slightly under where useful.
_PRESETS = [
    Backend("gemini-cli", "sidecar", "",
            ("PA_GEMINI_CLI_TOKEN",),
            {"smart": "session", "fast": "session"},
            {"rpm": 5, "rpd": 300},
            url_setting="gemini_cli_url", key_env="PA_GEMINI_CLI_TOKEN",
            note="official Antigravity CLI (agy -p) on a loopback sidecar"),
    Backend("copilot-cli", "sidecar", "",
            ("PA_COPILOT_CLI_TOKEN",),
            {"smart": "session", "fast": "session"},
            {"rpm": 5, "rpd": 300},
            url_setting="copilot_cli_url", key_env="PA_COPILOT_CLI_TOKEN",
            note="official Copilot CLI (copilot -p) on a loopback sidecar"),
    Backend("groq", "api", "https://api.groq.com/openai/v1",
            ("GROQ_API_KEY",),
            {"smart": "openai/gpt-oss-120b", "fast": "openai/gpt-oss-20b"},
            {"rpm": 30, "tpm": 8000, "rpd": 1000, "tpd": 200000},
            key_env="GROQ_API_KEY"),
    Backend("gemini-api", "api",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            ("GEMINI_API_KEY",),
            {"smart": "gemini-3.6-flash", "fast": "gemini-3.5-flash-lite"},
            {"rpm": 15, "rpd": 450},
            key_env="GEMINI_API_KEY"),
    Backend("openrouter-free", "api", "https://openrouter.ai/api/v1",
            ("OPENROUTER_API_KEY",),
            {"smart": "openrouter/free", "fast": "openrouter/free"},
            {"rpm": 20, "rpd": 50},
            key_env="OPENROUTER_API_KEY"),
    Backend("cloudflare", "api",
            "https://api.cloudflare.com/client/v4/accounts/"
            "{CLOUDFLARE_ACCOUNT_ID}/ai/v1",
            ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"),
            {"smart": "@cf/openai/gpt-oss-120b",
             "fast": "@cf/openai/gpt-oss-20b"},
            {"rpm": 20, "rpd": 150, "tpd": 400000},
            key_env="CLOUDFLARE_API_TOKEN",
            note="10K free neurons/day; caps are a rough token proxy"),
    Backend("pollinations", "api",
            "https://text.pollinations.ai/openai",
            (),
            {"smart": "openai", "fast": "openai"},
            {"rpm": 2, "rpd": 2500},
            key_env="POLLINATIONS_API_KEY",
            note="keyless anonymous tier (~1 req/30s per IP); a key is "
                 "optional and used only when present"),
    # opt-in only (no durable free tier as of 2026-11): reachable via
    # PA_ROUTER_ORDER — see docs/ROUTER.md "not in the defaults".
    Backend("cerebras", "api", "https://api.cerebras.ai/v1",
            ("CEREBRAS_API_KEY",),
            {"smart": "gpt-oss-120b", "fast": "gpt-oss-120b"},
            {"rpm": 5, "rpd": 200},
            key_env="CEREBRAS_API_KEY"),
    Backend("mistral", "api", "https://api.mistral.ai/v1",
            ("MISTRAL_API_KEY",),
            {"smart": "mistral-large-latest",
             "fast": "mistral-small-latest"},
            {"rpm": 6, "rpd": 200},
            key_env="MISTRAL_API_KEY"),
]

DEFAULT_SMART_ORDER = ["gemini-cli", "copilot-cli", "groq", "gemini-api",
                       "openrouter-free", "cloudflare", "pollinations"]
# escalate tier (PA_ROUTER_ESCALATE): the order the owner asked for
# — free Flash/thinking IDs only; github-models/cerebras/mistral are
# deliberately absent (retired or not durably free, docs/ROUTER.md).
DEFAULT_ESCALATE_ORDER = ["gemini-cli", "copilot-cli", "gemini-api",
                          "groq", "openrouter-free", "cloudflare",
                          "pollinations"]
DEFAULT_FAST_ORDER = ["groq", "gemini-api", "openrouter-free",
                      "cloudflare", "gemini-cli", "copilot-cli",
                      "pollinations"]
# call tier — the spoken-turn brain. API backends only (no
# sidecar/CLI), tiny fast models, 4 s per attempt.
DEFAULT_CALL_ORDER = ["groq", "gemini-api", "openrouter-free",
                      "cloudflare"]
DEFAULT_CALL_MODELS = {"groq": "llama-3.1-8b-instant"}

_CAP_KEYS = ("rpm", "tpm", "rpd", "tpd")
_MALFORMED_COOLDOWN_S = 90.0
_AUTH_COOLDOWN_S = 12 * 3600.0
_MAX_COOLDOWN_S = 24 * 3600.0
_MAX_WAIT_S = 60.0   # longest a call sleeps waiting out a cooldown
_MAX_WAITS = 3       # cooldown waits per call (bound total sleep)
_WAKE_MARGIN_S = 1.5 # retry-after boundaries are fuzzy; don't re-hit them
_PROBE_MAX_TOKENS = 512  # router-check budget: reasoning models (gpt-oss)
                         # think before they answer; a tiny cap starves them


def _csv(raw: str) -> list:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _model_overrides(raw: str) -> dict:
    """'groq=openai/gpt-oss-120b,cerebras=gpt-oss-120b' -> {name: model}."""
    out = {}
    for part in _csv(raw):
        name, sep, model = part.partition("=")
        if sep and name.strip() and model.strip():
            out[name.strip()] = model.strip()
    return out


def _minute_key() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M")


def _day_key() -> str:
    return utcnow().strftime("%Y-%m-%d")


class _RouterChatProvider(ChatProvider):
    """ChatProvider with quota-aware error mapping: some free endpoints
    answer 402 Payment Required on a spent free allowance (Pollinations,
    OpenRouter negative balance) — for routing purposes that is a quota
    signal like 429, not a malformed request."""

    def _raise_for(self, e):
        if e.status == 402:
            ra = e.headers.get("retry-after")
            try:
                retry_after = float(ra) if ra else None
            except ValueError:
                retry_after = parse_reset_seconds(ra)
            raise ProviderRateLimit(
                "free allowance exhausted (HTTP 402)",
                reset_at=self._reset_at(e), retry_after=retry_after)
        super()._raise_for(e)

    @staticmethod
    def _reset_at(e):
        from .provider import reset_at_iso
        return reset_at_iso(e.headers)


class RouterProvider:
    """OpenAI-compatible provider that fails over across free backends.

    Interface parity with ChatProvider: chat(messages, tools,
    max_completion_tokens, timeout, tier) -> (message, usage).
    `tier` is 'smart' | 'fast' | None; None resolves to smart when the
    caller supplied tool schemas, else fast.
    """

    kind = "chat"
    tools_enabled = True
    streaming = False

    def __init__(self, settings, env=None, db_path=None,
                 client_factory=None):
        self.s = settings
        self.env = os.environ if env is None else env
        self.db_path = db_path or settings.db_path
        self.call_seconds = settings.router_call_seconds
        self.backend_seconds = settings.router_backend_seconds
        self._factory = client_factory
        self._clients = {}
        self._conn = None
        self._lock = threading.Lock()
        self._last_backend = None
        self.last_headers = {}
        self.backends, self.orders = self._build()

    # -- config ------------------------------------------------------------
    def _build(self):
        backends = {b.name: b.clone() for b in _PRESETS}
        for tier, raw in ((TIER_SMART, self.s.router_smart_models),
                          (TIER_FAST, self.s.router_fast_models),
                          (TIER_CALL,
                           getattr(self.s, "router_call_models", ""))):
            for name, model in _model_overrides(raw).items():
                if name in backends:
                    backends[name].models[tier] = model
        for name, model in DEFAULT_CALL_MODELS.items():
            if name in backends and TIER_CALL not in backends[name].models:
                backends[name].models[TIER_CALL] = model
        for name, be in backends.items():
            for cap in _CAP_KEYS:
                v = (self.env.get(
                    f"PA_ROUTER_CAP_{name.upper().replace('-', '_')}_"
                    f"{cap.upper()}") or "").strip()
                if not v:
                    continue
                try:
                    be.caps[cap] = int(v)
                except ValueError:
                    log.warning("ignoring non-integer cap %s=%r for %s",
                                cap, v, name)
        orders = {}
        tiers = [(TIER_SMART, "router_smart_order",
                  DEFAULT_SMART_ORDER),
                 (TIER_FAST, "router_fast_order", DEFAULT_FAST_ORDER),
                 (TIER_CALL,
                  getattr(self.s, "router_call_order", ""),
                  DEFAULT_CALL_ORDER)]
        if getattr(self.s, "router_escalate", False):
            tiers.append((TIER_ESCALATE, "router_escalate_order",
                          DEFAULT_ESCALATE_ORDER))
        # retired/not-durably-free backends never enter the escalate
        # order even by explicit override (docs/EVOLVE.md #15)
        banned = {"github-models", "cerebras", "mistral"}
        for tier, attr, default in tiers:
            raw = getattr(self.s, attr, "") or self.s.router_order
            names = [n for n in _csv(raw)
                     if n in backends and
                     (tier != TIER_ESCALATE or n not in banned)] \
                or default
            for n in _csv(raw):
                if n not in backends:
                    log.warning("PA_ROUTER order names unknown backend %r",
                                n)
            orders[tier] = names
        return backends, orders

    # -- db ------------------------------------------------------------------
    def _db(self):
        if self._conn is None:
            self._conn = dbmod.connect(self.db_path)
            dbmod.migrate(self._conn)
        return self._conn

    def _cooldown(self, name):
        row = self._db().execute(
            "SELECT until, reason, strikes FROM router_cooldowns"
            " WHERE backend=?", (name,)).fetchone()
        return dict(row) if row else None

    def _mark(self, name, until_dt, reason, strikes=None):
        self._db().execute(
            "INSERT INTO router_cooldowns(backend, until, reason, strikes,"
            " updated_at) VALUES(?,?,?,?,?) ON CONFLICT(backend) DO"
            " UPDATE SET until=excluded.until, reason=excluded.reason,"
            " strikes=excluded.strikes, updated_at=excluded.updated_at",
            (name, until_dt.isoformat(timespec="seconds"), reason,
             strikes if strikes is not None else 0, now_iso()))

    def _cool(self, be, err):
        """Persist a cooldown for a failed backend; strikes drive backoff."""
        prev = self._cooldown(be.name)
        strikes = (prev["strikes"] if prev else 0) + 1
        now = utcnow()
        if isinstance(err, ProviderAuthError) or \
                isinstance(err, ProviderNotConfigured):
            until, reason = now + datetime.timedelta(
                seconds=_AUTH_COOLDOWN_S), "auth"
        elif isinstance(err, ProviderRateLimit):
            reason = "rate_limited"
            if err.retry_after:
                until = now + datetime.timedelta(
                    seconds=min(err.retry_after, 3600))
            elif err.reset_at:
                try:
                    until = parse_iso(err.reset_at)
                except ValueError:
                    until = now + self._backoff(strikes)
            else:
                until = now + self._backoff(strikes)
        elif isinstance(err, ProviderBadResponse):
            until, reason = now + datetime.timedelta(
                seconds=min(60 * strikes, 600)), "bad_response"
        else:
            until, reason = now + self._backoff(strikes), "unavailable"
        if until > now + datetime.timedelta(seconds=_MAX_COOLDOWN_S):
            until = now + datetime.timedelta(seconds=_MAX_COOLDOWN_S)
        self._mark(be.name, until, reason, strikes)
        log.info("router: %s cooling until %s (%s)", be.name,
                 until.isoformat(timespec="seconds"), reason)
        if reason == "rate_limited" and strikes >= 3:
            # repeated 429s on one backend is a quota-gap signal
            self._incident(
                "rate_limited",
                f"{be.name}: {strikes} rate-limit strikes", tool=be.name)

    def _incident(self, kind, summary, detail="", tool=""):
        try:
            incidentsmod.record(self._db(), "router", kind,
                                summary=summary, detail=detail, tool=tool)
        except Exception:
            pass

    @staticmethod
    def _backoff(strikes):
        return datetime.timedelta(
            seconds=min(30 * (2 ** (strikes - 1)), 900))

    def _usage(self, table, key_col, key, be):
        row = self._db().execute(
            f"SELECT requests, tokens_in, tokens_out FROM {table}"
            f" WHERE backend=? AND {key_col}=?", (be.name, key)).fetchone()
        return dict(row) if row else {
            "requests": 0, "tokens_in": 0, "tokens_out": 0}

    def _record(self, be, usage):
        """Persist one successful call's request + token usage."""
        pin = int(usage.get("prompt_tokens") or 0)
        pout = int(usage.get("completion_tokens") or 0)
        for table, col, key in (("router_usage_minutes", "minute",
                                 _minute_key()),
                                ("router_usage_days", "day", _day_key())):
            self._db().execute(
                f"INSERT INTO {table}(backend, {col}, requests, tokens_in,"
                f" tokens_out) VALUES(?,?,1,?,?) ON CONFLICT(backend,"
                f" {col}) DO UPDATE SET requests=requests+1,"
                f" tokens_in=tokens_in+?, tokens_out=tokens_out+?",
                (be.name, key, pin, pout, pin, pout))
        self._db().execute(
            "DELETE FROM router_cooldowns WHERE backend=?", (be.name,))
        # cheap prune of stale minute rows
        cutoff = (utcnow() - datetime.timedelta(days=2)
                  ).strftime("%Y-%m-%dT%H:%M")
        self._db().execute(
            "DELETE FROM router_usage_minutes WHERE minute < ?", (cutoff,))

    # -- escalate tier --------------------------------------------------------
    def _escalate_count(self) -> int:
        try:
            row = self._db().execute(
                "SELECT value FROM pa_settings WHERE key=?",
                (f"router_escalate:{_day_key()}",)).fetchone()
            return int(row["value"]) if row else 0
        except Exception:
            return 0

    def _count_escalate(self):
        try:
            key = f"router_escalate:{_day_key()}"
            self._db().execute(
                "INSERT INTO pa_settings(key, value, updated_at)"
                " VALUES(?, '1', ?) ON CONFLICT(key) DO UPDATE SET"
                " value=CAST(value AS INTEGER)+1,"
                " updated_at=excluded.updated_at",
                (key, now_iso()))
        except Exception:
            pass

    def escalate_used(self):
        """(used, cap) for /usage and router-status."""
        return self._escalate_count(), self.s.router_escalate_rpd

    # -- candidate selection -------------------------------------------------
    def _skip_reason(self, be, est_tokens):
        """Why this backend can't serve the next attempt, or None."""
        if not be.configured(self.env, self.s):
            return "needs config"
        cd = self._cooldown(be.name)
        if cd:
            try:
                if parse_iso(cd["until"]) > utcnow():
                    return f"cooling ({cd['reason']})"
            except ValueError:
                pass
        caps = be.caps
        day = self._usage("router_usage_days", "day", _day_key(), be)
        if caps.get("rpd") and day["requests"] + 1 > caps["rpd"]:
            return "daily cap"
        if caps.get("tpd") and day["tokens_in"] + day["tokens_out"] \
                + est_tokens > caps["tpd"]:
            return "daily token cap"
        minute = self._usage("router_usage_minutes", "minute",
                             _minute_key(), be)
        if caps.get("rpm") and minute["requests"] + 1 > caps["rpm"]:
            return "per-minute cap"
        if caps.get("tpm") and minute["tokens_in"] + minute["tokens_out"] \
                + est_tokens > caps["tpm"]:
            return "per-minute token cap"
        return None

    def _candidates(self, tier, tools_present):
        seen = set()
        if tier == TIER_ESCALATE:
            pool = self.orders.get(TIER_ESCALATE, []) \
                + self.orders[TIER_SMART]
        elif tier == TIER_CALL:
            # call brain: api backends only — a sidecar/CLI session
            # can't meet the latency budget.
            pool = [n for n in self.orders.get(TIER_CALL, [])
                    + self.orders[TIER_FAST]
                    if self.backends.get(n)
                    and self.backends[n].kind != "sidecar"]
        else:
            other = TIER_FAST if tier == TIER_SMART else TIER_SMART
            pool = self.orders[tier] + self.orders[other]
        for name in pool:
            if name in seen:
                continue
            seen.add(name)
            be = self.backends.get(name)
            if be and be.usable(tools_present):
                yield be

    # -- the call -------------------------------------------------------------
    def _client_for(self, be, model):
        key = (be.name, model or "")
        if key in self._clients:
            return self._clients[key]
        if self._factory is not None:
            c = self._factory(be, model)
        elif be.kind == "sidecar":
            from .freebuff_sidecar import SidecarSessionClient
            c = SidecarSessionClient(
                be.url_for(self.env, self.s), be.api_key(self.env),
                session_timeout=self.backend_seconds)
        else:
            c = _RouterChatProvider(
                be.url_for(self.env, self.s), model,
                api_key=be.api_key(self.env),
                timeout=self.backend_seconds)
        self._clients[key] = c
        return c

    def _call(self, be, model, messages, tools, max_tokens, timeout):
        """One attempt against one backend. Raises ProviderError."""
        client = self._client_for(be, model)
        if be.kind == "sidecar":
            from .chatagent import flatten
            prompt = flatten(messages)
            out = client.run(prompt,
                             deadline_seconds=max(1.0, timeout))
            text = out.get("text") or ""
            return {"role": "assistant", "content": text,
                    "tool_calls": []}, {
                "prompt_tokens": estimate_tokens(prompt),
                "completion_tokens": estimate_tokens(text)}
        msg, usage = client.chat(
            messages, tools=tools, max_completion_tokens=max_tokens,
            timeout=max(1.0, timeout))
        self.last_headers = getattr(client, "last_headers", {})
        return msg, usage

    def chat(self, messages, tools=None, max_completion_tokens=1024,
             timeout=None, tier=None, scope="owner-chat"):
        """One completion through the first healthy backend for `tier`.

        `scope` feeds the budget ledger — a background scope whose
        daily share is spent degrades to the fast tier instead of
        burning smart/escalate quota; owner-chat never silently
        downgrades.

        On backend failure the SAME call continues to the next backend;
        if every candidate is cooling but one wakes inside the call
        budget, the call waits for it and tries again. Raises only when
        every candidate is skipped or failed, or the total deadline
        passes.
        """
        tier = tier or (TIER_SMART if tools else TIER_FAST)
        from . import budget as _budget
        scope = _budget._norm_scope(scope)
        if scope == "background" and tier in (TIER_SMART,
                                             TIER_ESCALATE):
            try:
                conn = self._db()
                if conn is not None:
                    if _budget.would_exceed(
                            conn, scope, "llm_tokens:*", 1,
                            s=self.s, env=self.env) or \
                            _budget.would_exceed(
                                conn, scope, "escalations", 1,
                                s=self.s, env=self.env):
                        tier = TIER_FAST
            except Exception:
                pass
        if tier == TIER_ESCALATE:
            if not getattr(self.s, "router_escalate", False):
                tier = TIER_SMART            # flag off: today's path
            else:
                used = self._escalate_count()
                if used >= self.s.router_escalate_rpd:
                    self._incident(
                        "exception",
                        "router: escalate RPD cap reached "
                        f"({used}/{self.s.router_escalate_rpd})")
                    raise ProviderUnavailable(
                        "router: escalate tier is out of daily calls "
                        f"({self.s.router_escalate_rpd}/day)")
            try:
                _budget.charge(self._db(), scope, "escalations", 1,
                               s=self.s, env=self.env)
            except Exception:
                pass
        budget = self.call_seconds if timeout is None else \
            min(timeout, self.call_seconds)
        deadline = time.monotonic() + budget
        est_in = sum(estimate_tokens(json_dumps(m)) for m in messages) \
            + (estimate_tokens(json_dumps(tools)) if tools else 0)
        reasons, attempted, waits = [], 0, 0
        while True:
            next_wake = None

            def wake_of(be):
                with self._lock:
                    cd = self._cooldown(be.name)
                try:
                    return parse_iso(cd["until"]).timestamp()
                except (ValueError, TypeError, AttributeError):
                    return None

            for be in self._candidates(tier, bool(tools)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reasons.append("deadline")
                    break
                with self._lock:
                    why = self._skip_reason(be, est_in)
                if why:
                    reasons.append(f"{be.name}:{why}")
                    if why.startswith("cooling"):
                        w = wake_of(be)
                        if w is not None and (
                                next_wake is None or w < next_wake):
                            next_wake = w
                    continue
                attempted += 1
                model = be.model_for(tier)
                try:
                    msg, usage = self._call(
                        be, model, messages, tools,
                        max_completion_tokens,
                        timeout=min(remaining, self.backend_seconds))
                except ProviderError as e:
                    with self._lock:
                        self._cool(be, e)
                    reasons.append(f"{be.name}:{e.kind}")
                    w = wake_of(be)
                    if w is not None and (
                            next_wake is None or w < next_wake):
                        next_wake = w
                    log.info("router: %s failed (%s); trying next",
                             be.name, e.kind)
                    continue
                with self._lock:
                    self._record(be, usage)
                self._last_backend = be.name
                if tier == TIER_ESCALATE:
                    self._count_escalate()
                    self._incident(
                        "low_confidence",
                        f"router: escalate tier answered via {be.name}",
                        detail="; ".join(reasons[-4:]))
                return msg, usage
            # A pass that only hit cooldowns may still be served: wait
            # out the soonest cooldown (a short Retry-After, a couple of
            # times per call at most, plus a small boundary margin) and
            # rewalk. Single-backend pools depend on this; we never
            # burn the whole budget sleeping.
            remaining = deadline - time.monotonic()
            if next_wake is None or waits >= _MAX_WAITS:
                break
            delay = next_wake + _WAKE_MARGIN_S - time.time()
            if delay > min(remaining, _MAX_WAIT_S):
                break
            time.sleep(max(0.0, delay))
            waits += 1
        if not attempted:
            self._incident(
                "not_configured",
                f"router: no usable backend for tier {tier!r}",
                detail="; ".join(reasons[-8:]))
            raise ProviderNotConfigured(
                f"router: no usable backend for tier {tier!r} — every "
                f"backend needs config or is capped/cooling "
                f"({'; '.join(reasons[-8:])})")
        self._incident(
            "exception",
            f"router: all backends for tier {tier!r} failed",
            detail="; ".join(reasons[-8:]))
        raise ProviderUnavailable(
            f"router: all backends for tier {tier!r} failed "
            f"({'; '.join(reasons[-8:])})")

    def chat_stream(self, messages, max_completion_tokens=256,
                    timeout=None):
        """call tier: yield content deltas from the first healthy
        `call`-tier backend, failing over between backends (never
        mid-stream). Per-backend cap is settings.call_llm_seconds
        (default 4 s); a backend without chat_iter falls back to a
        one-shot chat() whose content arrives as a single delta."""
        be_seconds = getattr(self.s, "call_llm_seconds", 4.0)
        deadline = time.monotonic() + (timeout or be_seconds * 4)
        est_in = sum(estimate_tokens(json_dumps(m)) for m in messages)
        reasons, attempted = [], 0

        def attempt():
            nonlocal attempted
            for be in self._candidates(TIER_CALL, False):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reasons.append("deadline")
                    break
                with self._lock:
                    why = self._skip_reason(be, est_in)
                if why:
                    reasons.append(f"{be.name}:{why}")
                    continue
                attempted += 1
                model = be.models.get(TIER_CALL) \
                    or be.model_for(TIER_FAST)
                client = self._client_for(be, model)
                try:
                    if hasattr(client, "chat_iter"):
                        for d in client.chat_iter(
                                messages,
                                max_completion_tokens=
                                max_completion_tokens,
                                timeout=min(remaining, be_seconds)):
                            yield d
                        usage = getattr(client, "last_usage", {}) or {}
                    else:
                        msg, usage = self._call(
                            be, model, messages, None,
                            max_completion_tokens,
                            timeout=min(remaining, be_seconds))
                        if msg.get("content"):
                            yield msg["content"]
                except ProviderError as e:
                    with self._lock:
                        self._cool(be, e)
                    reasons.append(f"{be.name}:{e.kind}")
                    log.info("router call tier: %s failed (%s)",
                             be.name, e.kind)
                    continue
                with self._lock:
                    self._record(be, usage)
                self._last_backend = be.name
                return
            if attempted:
                raise ProviderUnavailable(
                    "router: all call-tier backends failed "
                    f"({'; '.join(reasons[-6:])})")
            raise ProviderNotConfigured(
                "router: no usable call-tier backend "
                f"({'; '.join(reasons[-6:])})")

        return attempt()

    # -- hooks ----------------------------------------------------------------
    def note_malformed(self):
        """The caller couldn't parse this backend's output — cool it
        briefly so the next call in the same turn tries a different
        backend (weak free models often break fenced-JSON protocol)."""
        name = self._last_backend
        if not name or name not in self.backends:
            return
        with self._lock:
            prev = self._cooldown(name)
            self._mark(name,
                       utcnow() + datetime.timedelta(
                           seconds=_MALFORMED_COOLDOWN_S),
                       "malformed_json",
                       (prev["strikes"] if prev else 0) + 1)
        log.info("router: %s produced malformed output; cooling %ss",
                 name, _MALFORMED_COOLDOWN_S)

    # -- introspection for `pa router-status` / `pa router-check` -------------
    def status_rows(self):
        """Per-backend state for the CLI (no keys, metadata only)."""
        rows = []
        for name in dict.fromkeys(
                self.orders[TIER_SMART] + self.orders[TIER_FAST]
                + list(self.backends)):
            be = self.backends[name]
            cd = self._cooldown(name)
            cooling = ""
            if cd:
                try:
                    if parse_iso(cd["until"]) > utcnow():
                        cooling = f"{cd['until']} ({cd['reason']})"
                except ValueError:
                    pass
            day = self._usage("router_usage_days", "day", _day_key(), be)
            rows.append({
                "name": name, "kind": be.kind,
                "configured": be.configured(self.env, self.s),
                "missing": be.missing_env(self.env),
                "smart_model": be.model_for(TIER_SMART),
                "fast_model": be.model_for(TIER_FAST),
                "cooling": cooling,
                "requests": day["requests"],
                "tokens": day["tokens_in"] + day["tokens_out"],
                "caps": be.caps,
            })
        return rows

    def check_backend(self, name, timeout=30.0):
        """One tiny live call against ONE backend (no failover).
        Returns (ok, latency_ms, detail). Never prints secrets."""
        be = self.backends.get(name)
        if be is None:
            return False, 0.0, "unknown backend"
        if not be.configured(self.env, self.s):
            return None, 0.0, "needs config: " + ", ".join(
                be.missing_env(self.env))
        prompt = [{"role": "user",
                   "content": "Reply with the single word: ok"}]
        started = time.monotonic()
        try:
            model = be.model_for(TIER_FAST)
            client = self._client_for(be, model)
            msg, _u = self._call(be, model, prompt, None,
                                 _PROBE_MAX_TOKENS, timeout=timeout)
            text = (msg.get("content") or "").strip()
            ms = int((time.monotonic() - started) * 1000)
            if text:
                return True, ms, text[:60]
            if getattr(client, "last_finish_reason", None) == "length":
                return False, ms, (
                    "empty reply — finish_reason=length (reasoning used "
                    f"the whole {_PROBE_MAX_TOKENS}-token budget)")
            return False, ms, "empty reply"
        except ProviderError as e:
            ms = int((time.monotonic() - started) * 1000)
            return False, ms, str(e)[:160]

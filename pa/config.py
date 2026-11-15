"""Environment-driven configuration.

Security-relevant rules live here so they are checked in one place:

- PA_ENV: development | test | production
- Bind safety: in development/test the app may only bind loopback. In
  production a non-loopback bind is only allowed in the documented Fly.io
  topology (FLY_APP_NAME present): the Fly proxy is the public edge, but
  sibling machines in the org can still reach the machine over private
  IPv6, which is why every request is authenticated anyway. Any other
  production setup must keep the app on loopback behind a TLS terminator
  such as Caddy. An env flag alone never counts as proof of TLS.
- The inference endpoint is pinned server-side via LLM_BASE_URL /
  LLM_MODEL / LLM_API_KEY. It is never user-, model- or web-controllable.
  Local-network/loopback endpoints are allowed (the owner chose them),
  but the API key is only ever sent to that one configured endpoint.
- A key being present never enables anything by itself.
"""

import ipaddress
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field

ENV_DEVELOPMENT = "development"
ENV_TEST = "test"
ENV_PRODUCTION = "production"

TAVILY_HOST = "api.tavily.com"
TAVILY_BASE_URL = "https://api.tavily.com"

DEFAULT_TIMEZONE = "Europe/London"

DEV_PROVIDER_HOSTS = ("localhost", "127.0.0.1", "::1")

GMAIL_API_BASE = "https://gmail.googleapis.com"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GCAL_API_BASE = "https://www.googleapis.com"
GCAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"
# what `pa gmail-auth` asks consent for now: read + drafts + send + calendar
GMAIL_AUTH_SCOPES = (GMAIL_SCOPE, GMAIL_COMPOSE_SCOPE, GMAIL_SEND_SCOPE,
                     GCAL_SCOPE)

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Voice/TTS provider bases (keys come from env; see docs/VOICE.md)
GEMINI_TTS_BASE = "https://generativelanguage.googleapis.com"
FISH_TTS_BASE = "https://api.fish.audio"
GROQ_WHISPER_BASE = "https://api.groq.com/openai/v1"
CF_AI_BASE = "https://api.cloudflare.com/client/v4"

# Composio REST API v3.1 (official API — used directly so no SDK dep).
COMPOSIO_API_BASE = "https://backend.composio.dev/api/v3.1"
# Free "Hobby" platform tier: 100k tool calls/month total, but only
# 20k of them may run on Composio-managed apps — the connect-link flow
# uses managed apps, so the 20k sub-cap is the operative limit.
COMPOSIO_MONTHLY_LIMIT = 20000

# Hostname-style sentinel for the free-API router (pa.router). Only
# resolvable while PA_ROUTER_ENABLED=true.
ROUTER_HOST = "router"


def _bool(env, name, default=False):
    v = env.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(env, name, default):
    v = env.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {v!r}")


def _float(env, name, default):
    v = env.get(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {v!r}")


class ConfigError(Exception):
    pass


def is_loopback_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_private_host(host: str) -> bool:
    """Loopback or RFC-1918/link-local/ULA — the owner's own network.
    Hostnames that aren't literal IPs are treated as non-private."""
    if host in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


_TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")


def is_tailnet_host(host: str) -> bool:
    """Literal IP in the Tailscale CGNAT range (100.64.0.0/10). NOT the
    same as is_private_host: Python's ipaddress marks CGNAT as neither
    private nor global, so it needs its own check."""
    try:
        return ipaddress.ip_address(host) in _TAILNET_NET
    except ValueError:
        return False


def _validate_agent_url(url: str, varname: str) -> str:
    """Shared rule for PA_COMPUTER_URL and PA_NODES entries: https to
    any host; plain http only to loopback/private/tailnet hosts (the
    bearer token must never cross a public network unencrypted)."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise ConfigError(f"{varname} is not a parseable URL")
    if parts.scheme not in ("http", "https"):
        raise ConfigError(
            f"{varname} scheme {parts.scheme!r} must be http "
            "or https")
    if parts.username or parts.password:
        raise ConfigError(f"{varname} must not contain credentials")
    host = (parts.hostname or "").lower()
    if not host:
        raise ConfigError(f"{varname} has no host")
    if parts.scheme == "http" and not (
            is_private_host(host) or is_tailnet_host(host)):
        raise ConfigError(
            f"{varname} uses plain http to a public host {host!r}: "
            "refusing (the token would cross the internet unencrypted). "
            "Use https, or a loopback/private/tailnet (100.64/10) "
            "endpoint.")
    return url.rstrip("/")


def validate_computer_url(url: str) -> str:
    """PA_COMPUTER_URL — the remote computer-agent base URL."""
    return _validate_agent_url(url, "PA_COMPUTER_URL")


_NODE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


def node_token_env(name: str) -> str:
    """PA_NODE_<NAME>_TOKEN — token env var for a configured node."""
    return "PA_NODE_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_TOKEN"


def parse_nodes(env) -> dict:
    """PA_NODES=name=url,... -> {name: url}. Each url obeys the
    computer-agent rule; each name needs PA_NODE_<NAME>_TOKEN set."""
    out = {}
    raw = (env.get("PA_NODES") or "").strip()
    if not raw:
        return out
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, url = item.partition("=")
        name = name.strip().lower()
        if not sep or not _NODE_NAME_RE.match(name):
            raise ConfigError(
                f"PA_NODES entry {item!r} must look like "
                "name=url (name: [a-z0-9_-], starts with a letter)")
        if name in out:
            raise ConfigError(f"PA_NODES lists node {name!r} twice")
        out[name] = _validate_agent_url(url, f"PA_NODES[{name}]")
        if not (env.get(node_token_env(name)) or "").strip():
            raise ConfigError(
                f"PA_NODES configures node {name!r} but "
                f"{node_token_env(name)} is empty — the agent needs "
                "a shared token")
    return out


def validate_endpoint_url(url: str, name: str = "LLM_BASE_URL") -> str:
    """Validate an owner-pinned endpoint URL.

    https is allowed to any host. Plain http is allowed only for
    loopback/private addresses — an API key must never cross the public
    internet unencrypted. No credentials in the URL."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise ConfigError(f"{name} is not a parseable URL")
    if parts.scheme not in ("http", "https"):
        raise ConfigError(
            f"{name} scheme {parts.scheme!r} must be http or https")
    if parts.username or parts.password:
        raise ConfigError(f"{name} must not contain credentials")
    host = parts.hostname
    if not host:
        raise ConfigError(f"{name} has no host")
    if parts.scheme == "http" and not is_private_host(host.lower()):
        raise ConfigError(
            f"{name} uses plain http to a public host {host!r}: "
            "refusing (the API key would cross the internet unencrypted). "
            "Use https, or a loopback/private endpoint.")
    return url.rstrip("/")


def validate_llm_base_url(url: str) -> str:
    return validate_endpoint_url(url, "LLM_BASE_URL")


def parse_channel_map(raw: str) -> dict:
    """DISCORD_CHANNELS: JSON map {assistant name or id: channel id}."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except ValueError:
        raise ConfigError("DISCORD_CHANNELS must be a JSON object "
                          '{"assistant": "channel-id"}')
    if not isinstance(obj, dict):
        raise ConfigError("DISCORD_CHANNELS must be a JSON object")
    out = {}
    for k, v in obj.items():
        out[str(k).strip()] = str(v).strip()
    return {k: v for k, v in out.items() if k and v}


@dataclass
class Settings:
    env: str = ENV_DEVELOPMENT
    data_dir: str = "data"
    bind_host: str = "127.0.0.1"
    port: int = 8080
    timezone: str = DEFAULT_TIMEZONE

    # inference provider (owner-pinned OpenAI-compatible endpoint)
    llm_base_url: str = ""
    llm_model: str = ""
    llm_streaming: bool = False
    llm_tools: bool = True
    llm_completions_path: str = "/chat/completions"
    dev_ollama_base_url: str = "http://localhost:11434/v1"
    dev_model: str = "qwen2.5:1.5b"

    # freebuff agent-session provider (OFF by default; not an OpenAI
    # endpoint — drives the official freebuff CLI in a tmux PTY with the
    # owner's own sign-in; see docs/FREEBUFF_CONNECTION.md)
    freebuff_enabled: bool = False
    freebuff_cli: str = "freebuff"
    freebuff_tmux: str = "tmux"
    freebuff_home: str = ""
    freebuff_workdir: str = ""
    freebuff_session_timeout: int = 300
    freebuff_ready_timeout: int = 45
    freebuff_max_output_chars: int = 60_000
    freebuff_allow_paid: bool = False
    freebuff_assume_ready: bool = False
    freebuff_live_check: bool = False
    # remote mode: delegate sessions to a home sidecar instead of a
    # local tmux/CLI (cloud-IP fix — the session runs on owner hardware)
    freebuff_remote_url: str = ""
    freebuff_remote_token: str = ""
    freebuff_sidecar_host: str = "127.0.0.1"
    freebuff_sidecar_port: int = 8777

    # free-API router (pa.router): ordered failover across free-tier LLM
    # endpoints. OFF by default; enable to pin assistants to host 'router'.
    router_enabled: bool = False
    router_escalate: bool = False     # PA_ROUTER_ESCALATE — 'think
    # harder' tier; OFF = today's behaviour exactly (docs/EVOLVE.md)
    router_escalate_order: str = ""   # PA_ROUTER_ESCALATE_ORDER CSV
    router_escalate_rpd: int = 20     # PA_ROUTER_ESCALATE_RPD/day
    router_order: str = ""            # CSV backend names, both tiers
    router_smart_order: str = ""      # CSV; falls back to router_order
    router_fast_order: str = ""       # CSV; falls back to router_order
    router_smart_models: str = ""     # CSV name=model overrides
    router_fast_models: str = ""      # CSV name=model overrides
    router_call_order: str = ""       # V5 CSV; falls back to router_order
    router_call_models: str = ""      # V5 CSV name=model overrides
    call_llm_seconds: float = 4.0     # V5 per-backend cap in a call turn
    tts_chars_day: int = 0            # V7 global/day cap; 0 = per-provider
    browser_owner_domains: str = ""   # P1 CSV domain=owner|pal map
    policy_engine: bool = True        # P3 one approval floor; off = legacy
    policy_vault_unattended: bool = True  # vault writes stay unattended-free
    tool_topk: int = 0                # L1: 0=off; >0 prunes the catalogue
    router_call_seconds: int = 180    # whole-call deadline across backends
    router_backend_seconds: int = 60  # per-attempt cap inside the call
    # official-CLI session sidecars (loopback + bearer; see docs/ROUTER.md)
    gemini_cli_url: str = "http://127.0.0.1:8781"
    gemini_cli_token: str = ""
    gemini_cli_bin: str = "agy"
    copilot_cli_url: str = "http://127.0.0.1:8782"
    copilot_cli_token: str = ""
    copilot_cli_bin: str = "copilot"

    # search (optional Tavily)
    search_enabled: bool = False

    # discord
    discord_enabled: bool = True
    discord_owner_id: str = ""
    discord_channels: dict = field(default_factory=dict)
    discord_gateway_url: str = DISCORD_GATEWAY_URL
    discord_api_base: str = DISCORD_API_BASE
    # channel where every owner message is treated as chat (no mention
    # needed); empty = mention-or-DM only
    discord_chat_channel_id: str = ""

    # conversational agent ("chat turns"): ReAct loop over an
    # agent-session or chat-completions provider
    chat_assistant: str = "pal"
    chat_max_steps: int = 8
    chat_turn_seconds: int = 300
    chat_history_turns: int = 8
    chat_working_seconds: float = 8.0
    chat_approval_wait: int = 3600
    chat_sandbox: str = ""          # default <data_dir>/chat-sandbox
    shell_timeout: float = 15.0
    shell_output_cap: int = 8000
    approval_ttl_seconds: int = 24 * 3600
    # inbound attachments (images/PDFs land in chat turns, item 3)
    attach_max_bytes: int = 8 * 1024 * 1024
    pdf_max_pages: int = 20
    pdf_max_chars: int = 50_000
    # vision captions (item 4): off only if PA_VISION=false or no
    # backend key is configured at all
    vision: bool = True
    vision_order: str = "groq,gemini,cloudflare"
    vision_max_bytes: int = 4 * 1024 * 1024
    video_enabled: bool = False       # PA_VIDEO — off by default

    # Telegram mirror (docs/PARITY.md #26): PA_TELEGRAM — off by
    # default. When on, inbound owner messages on the linked chat run
    # chat turns answered on Telegram, and Discord-DM replies mirror.
    telegram_enabled: bool = False
    telegram_chat_id: str = ""        # TELEGRAM_CHAT_ID (owner's chat)
    video_max_mb: int = 50
    # memory (item 5): owner-fact writes always need approval;
    # PA_MEMORY_SAVE_APPROVAL also gates per-assistant memory_save;
    # PA_MEMORY_AUTO_PROPOSE lets a fast pass PROPOSE facts end-of-turn
    memory_save_approval: bool = False
    memory_auto_propose: bool = False
    memory_propose_max: int = 3
    # live turn progress: 👀 on the owner's message + an editable
    # "on it — <tool>…" status line, torn down when the reply lands
    # (docs/PARITY.md). False -> typing-only + one "working on it" note.
    chat_progress: bool = True

    # self-evolution loop (docs/EVOLVE.md): incidents are always
    # written; everything that *acts* on them (detect, reflect,
    # auto-apply, self-heal) requires PA_EVOLVE=true plus its own
    # flag or seeded-disabled job.
    evolve: bool = False
    evolve_auto_apply: bool = False
    evolve_self_heal: bool = False
    evolve_plan_calls_per_day: int = 10
    evolve_reflect_max: int = 3
    evolve_detect_threshold: int = 3
    evolve_detect_max_items: int = 3
    evolve_health_seconds: int = 60
    # final-4 E1: versioned improvement archive — every skill/snippet/
    # config/made-tool/memory-block change lands as a scored variant;
    # Track A promotes only candidates that beat live on the eval set.
    evolve_archive: bool = False
    evolve_practice: bool = False    # PA_EVOLVE_PRACTICE (final-4 E4)

    # owner's Mac over SSH (tailscale); every mac command needs approval
    mac_ssh_host: str = ""
    mac_ssh_user: str = ""
    mac_ssh_key: str = ""
    mac_ssh_timeout: float = 20.0

    # pal's own computer (docs/COMPUTER.md): persistent-profile Chromium
    # + a tailnet-only remote desktop. OFF by default; every risky
    # browser action is approval-gated via the normal Discord buttons.
    computer_enabled: bool = False
    computer_profile: str = ""        # default <data_dir>/browser-profile
    computer_max_tabs: int = 3
    computer_idle_seconds: int = 300
    computer_memory_mb: int = 600     # self-check; cgroup is the real cap
    computer_lock_wait: float = 30.0  # seconds a 2nd caller waits
    computer_url: str = ""            # remote computer-agent base URL
    computer_token: str = ""          # shared bearer (env only)
    computer_agent_bind: str = "127.0.0.1"
    computer_agent_port: int = 8788
    computer_download_max: int = 20 * 1024 * 1024
    computer_browser_bin: str = ""    # chromium override for /desktop
    # /desktop remote session: TTL minutes (hard cap 120), noVNC port
    desktop_minutes: int = 30
    desktop_port: int = 6080
    # read_url's Jina reader fallback — defaults to computer_enabled
    jina_reader: bool = False

    # voice: TTS provider chain (see docs/VOICE.md). Keys live in env;
    # a provider whose keys are absent is skipped automatically.
    tts_order: str = "gemini,fish,azure,cloudflare"
    tts_gemini_model: str = "gemini-3.8-flash-tts"
    tts_gemini_voice: str = "Kore"
    tts_gemini_base: str = GEMINI_TTS_BASE
    tts_fish_model: str = "s2.1-pro-free"
    tts_fish_voice: str = ""
    tts_fish_base: str = FISH_TTS_BASE
    tts_azure_voice: str = "en-GB-SoniaNeural"
    tts_azure_format: str = "audio-24khz-96kbitrate-mono-mp3"
    tts_cf_speaker: str = "luna"
    cloudflare_ai_base: str = CF_AI_BASE
    groq_whisper_base: str = GROQ_WHISPER_BASE
    whisper_model: str = "whisper-large-v3-turbo"
    cf_whisper_model: str = "@cf/openai/whisper-large-v3-turbo"
    ffmpeg_bin: str = "ffmpeg"
    # /voice on|off|auto stores pa_settings.voice_notes; this env is the
    # fallback before the owner ever toggles it ('auto' when unset+false)
    voice_notes_default: bool = False

    # Discord "call" (docs/VOICE.md): voice-channel pings for urgent
    # things. Needs the discord.py voice extras on the VM.
    call_guild_id: str = ""
    call_channel_id: str = ""
    call_max_per_hour: int = 3
    call_join_seconds: int = 120
    call_listen_seconds: int = 30
    call_max_seconds: int = 600
    # utterance endpointing: auto = webrtcvad when installed, else the
    # adaptive energy gate; "webrtc" / "energy" force one
    call_vad: str = "auto"
    # in-call TTS: ONE provider locked per call (separate from the
    # voice-note PA_TTS_ORDER); barge-in toggle; on-disk clip cache
    # for static lines (fillers) so they synthesise once ever
    # V7: Gemini TTS is reserved for voice notes (it 429s under call
    # load); calls default to azure -> cloudflare -> gemini.
    call_tts_order: str = "azure,cloudflare,gemini"
    call_barge_in: bool = True
    voice_cache_dir: str = ""   # default <data_dir>/voice-cache
    # per-turn/per-call timing rows into call_metrics (/call stats);
    # default on — records only, changes nothing the owner hears
    call_metrics: bool = True
    # playback path: 'inproc' = one long-lived AudioSource per call
    # fed with in-process PCM (gapless, zero ffmpeg at play time);
    # 'ffmpeg' = legacy per-clip FFmpegPCMAudio
    call_audio: str = "inproc"
    # end-of-speech tails (V3): shorter = snappier answers; the longer
    # 'short' tail holds after brief bursts (owner probably mid-thought)
    call_tail_ms: int = 500
    call_tail_short_ms: int = 700
    # call engine (V4): auto = Gemini Live first, pipeline fallback;
    # live = Live only; pipeline = STT->LLM->TTS chain only
    call_engine: str = "auto"
    call_stream_brain: bool = True   # V5 streamed call-tier brain
    live_model: str = "gemini-3.1-flash-live-preview"
    live_voice: str = "Kore"
    live_budget: bool = False      # PA_LIVE_BUDGET — charge live_min
    # 'HH-HH' local (settings.timezone): inside it, only urgent=True
    # calls go through
    quiet_hours: str = "23-07"

    # inbox watcher (docs/JARVIS.md): per-run classify cap, per-day call
    # cap, and how often the 'important mail' batch may DM the owner
    watch_max_classify: int = 12
    watch_max_calls_per_day: int = 6
    watch_batch_seconds: int = 3600

    # morning briefing: London weather via open-meteo (no key)
    open_meteo_base: str = "https://api.open-meteo.com"
    briefing_city: str = "London"
    briefing_lat: float = 51.5074
    briefing_lon: float = -0.1278

    # Discord attachment cap for chat-history imports (zip/json/md)
    import_max_bytes: int = 60 * 1024 * 1024

    # vault mirror: git commit+push after agent writes (opt-in)
    vault_git_sync: bool = False

    # connections library: public base for the OAuth callback (required
    # for /connect — e.g. the cloudflared URL, or http://127.0.0.1:8080
    # in dev); token encryption key lives in PA_SECRETS_KEY (env only,
    # see pa.secretsbox); MCP calls share one timeout
    public_url: str = ""
    mcp_timeout: float = 20.0

    # google calendar (oauth reuse of the GMAIL_* grant)
    gcal_api_base: str = GCAL_API_BASE
    google_auth_url: str = GOOGLE_AUTH_URL

    # composio fallback catalog (only consulted when COMPOSIO_API_KEY is
    # set and the app is not in the self-hosted MCP catalog)
    composio_api_base: str = COMPOSIO_API_BASE
    composio_user_id: str = "owner"
    composio_monthly_limit: int = COMPOSIO_MONTHLY_LIMIT

    # nodes (docs/NODES.md): PA_NODES=phone=http://127.0.0.1:8791,... —
    # pal's remote hands. Each node needs PA_NODE_<NAME>_TOKEN.
    # Empty = the whole feature is off.
    nodes: dict = field(default_factory=dict)
    node_tokens: dict = field(default_factory=dict)
    node_connect_seconds: float = 10.0
    node_health_seconds: float = 30.0   # cache /health this long
    # phone: may an unattended (job) turn read the owner's SMS inbox?
    # off by default — attended chat reads are free, jobs queue
    node_sms_read_unattended: bool = False

    # file workspace (docs/WORKSPACE.md): durable per-project files
    # with versions under <data_dir>/workspace/. Off by default; when
    # off, file_write keeps its attach-only base behaviour.
    workspace: bool = False
    workspace_mb: int = 500               # byte cap, all versions
    workspace_project: str = "main"       # default project name

    # K1 tool making (docs/TOOLMAKING.md): pal generates+tests+
    # registers small pure-Python tools under <data>/tools/.
    # Off by default — the tool_* chat tools stay hidden.
    madetools: bool = False
    integrations: bool = False
    subagents: bool = False
    subagent_max: int = 2
    research: bool = False
    skillextract: bool = False

    # keyless web-search fallback when Tavily is unset
    ddg_base: str = "https://html.duckduckgo.com"

    # gmail (oauth client material lives in env; see pa.gmail)
    gmail_api_base: str = GMAIL_API_BASE
    gmail_token_url: str = GMAIL_TOKEN_URL

    # obsidian vault mirror (read-only)
    vault_path: str = ""
    vault_max_file_bytes: int = 256 * 1024
    vault_max_files: int = 5000
    vault_max_results: int = 8
    vault_note_cap: int = 12000

    # budgets
    max_input_tokens: int = 5500
    max_completion_tokens: int = 1024
    tpm_limit: int = 8000
    rpm_limit: int = 30
    daily_request_limit: int = 1000
    daily_token_limit: int = 200_000
    rate_max_retries: int = 3

    # feature flags (docs/FEATURES.md): goals, proactive read-only work, the
    # memory OS and triage. Everything defaults OFF — with all flags
    # off behaviour matches the base branch. Background work is
    # read-only on owner accounts (pa/policy.py enforces that floor).
    goals: bool = False            # PA_GOALS — standing goals + /goals
    proactive: bool = False        # PA_PROACTIVE — proactive-tick job
    proactive_goal_tokens: int = 8000  # per-goal token cap per tick
    activity_feed: bool = False    # PA_ACTIVITY — activity feed + /activity
    activity_greeting: bool = False  # PA_ACTIVITY_GREETING — "while away"
    automation: bool = False       # PA_AUTOMATION — automation_propose
    briefing_goals: bool = False   # PA_BRIEFING_GOALS — goals section
    memory_blocks: bool = False    # PA_MEMORY_BLOCKS — pinned blocks
    sleeptime: bool = False        # PA_SLEEPTIME — nightly curator
    recall: bool = False           # PA_RECALL — recall_fts search
    kg: bool = False               # PA_KG — vault knowledge graph
    triage: bool = False           # PA_TRIAGE — inbox triage labels
    cal_triage: bool = False       # PA_CAL_TRIAGE — calendar prep
    cal_soon: bool = False         # PA_CAL_SOON — 15m/5m meeting pings
    cal_soon_leads: str = "15,5"   # PA_CAL_SOON_LEADS — lead minutes
    leave_now: bool = False        # PA_LEAVE_NOW — TfL leave-by pings
    leave_now_phone_loc: bool = False  # PA_LEAVE_NOW_USE_PHONE_LOC —
                                   # may use a recent phone-node fix
    home_lat: str = ""             # PA_HOME_LAT — leave-now origin
    home_lon: str = ""             # PA_HOME_LON   (default: briefing)
    tfl_base: str = "https://api.tfl.gov.uk"  # PA_TFL_BASE
    secret_capture: bool = True    # PA_SECRET_CAPTURE — modal/HTTPS
                                   # credential capture (default on)
    notify_phone: bool = False     # PA_NOTIFY_PHONE — high/urgent
                                   # also ping a healthy phone node
    mail_inbound: bool = False     # PA_MAIL_INBOUND — pal's own Gmail
                                   # -> owner turns (allowlist only)
    owner_email: str = ""          # PA_OWNER_EMAIL — the owner's
                                   # address for the mail allowlist
    mail_inbound_senders: str = "" # PA_MAIL_INBOUND_SENDERS — extra
                                   # allowed From: addresses (csv)
    github_hook_agent: bool = False  # PA_GITHUB_HOOK_AGENT — github
                                   # hook payloads run an agent turn
                                   # (off: CI/PR events -> DM cards)
    coursework: bool = False       # PA_COURSEWORK — Classroom/Docs
                                   # reads + deadline watch
    notion_index: bool = False     # PA_NOTION_INDEX — Notion -> recall
                                   # FTS + KG (read-only, hourly)
    notify_v2: bool = False        # PA_NOTIFY_V2 — notify() routing
    notify_per_hour: int = 6       # PA_NOTIFY_PER_HOUR — DM cap
    projects: bool = False         # PA_PROJECTS — multi-day plans

    # agent
    max_turns: int = 10
    task_deadline_seconds: int = 600
    lease_ttl_seconds: float = 90.0
    heartbeat_seconds: float = 20.0
    worker_poll_seconds: float = 2.0

    # web
    session_idle_seconds: int = 24 * 3600
    session_absolute_seconds: int = 7 * 24 * 3600
    login_window_seconds: int = 900
    login_max_failures: int = 5
    max_body_bytes: int = 64 * 1024

    # fetch
    fetch_max_bytes: int = 5 * 1024 * 1024
    fetch_timeout_seconds: float = 10.0
    fetch_max_redirects: int = 3

    fly_app_name: str = field(default="")
    container_bind: bool = False

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "pa.db")

    @property
    def is_production(self) -> bool:
        return self.env == ENV_PRODUCTION

    @property
    def fly_topology(self) -> bool:
        return bool(self.fly_app_name)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    # -- provider allowlist ------------------------------------------------
    def provider_allowlist(self):
        """Return [(host, base_url, [models], key_env)].

        Production: exactly the owner-pinned LLM_* endpoint (or nothing —
        an unconfigured endpoint resolves to provider_not_configured, not
        a silent fallback). Outside production the dev Ollama endpoint is
        additionally allowed so tests/dev run without real keys.
        A key's presence plays no part here."""
        entries = []
        if self.llm_configured:
            host = (urllib.parse.urlsplit(self.llm_base_url).hostname
                    or "").lower()
            entries.append((host, self.llm_base_url, [self.llm_model],
                            "LLM_API_KEY"))
        if not self.is_production:
            entries.append((
                "localhost", self.dev_ollama_base_url, [self.dev_model],
                None))
        if self.freebuff_enabled:
            # Agent-session provider: pin is (sentinel host, "default") —
            # the session uses whichever model the signed-in CLI selects.
            # Never a fallback: only reachable by explicit assistant pin.
            entries.append((
                "freebuff-cli", "freebuff://cli", ["default"], None))
        if self.router_enabled:
            # Multi-backend failover provider: pin is (sentinel host,
            # "default") — the router picks the concrete backend+model
            # per call and per tier.
            entries.append((
                ROUTER_HOST, "router://pool", ["default"], None))
        return entries

    def resolve_provider(self, host: str, model: str):
        """Return (base_url, api_key_env) for an allowed (host, model)
        pair, or raise ConfigError."""
        host = (host or "").strip().lower()
        for allowed_host, base_url, models, key_env in \
                self.provider_allowlist():
            names = {allowed_host}
            if allowed_host == "localhost":
                names = set(DEV_PROVIDER_HOSTS)
            if host in names and model in models:
                return base_url, key_env
        raise ConfigError(
            f"provider (host={host!r}, model={model!r}) is not configured; "
            f"set LLM_BASE_URL/LLM_MODEL or use a configured pair")

    def default_provider_pair(self):
        """(host, model) for new assistants: the configured LLM endpoint,
        else the dev fallback outside production, else ('','')."""
        al = self.provider_allowlist()
        if not al:
            return "", ""
        host, _url, models, _key = al[0]
        return host, models[0]

    # -- discord -------------------------------------------------------------
    def discord_token(self, env=None) -> str:
        env = os.environ if env is None else env
        return (env.get("DISCORD_BOT_TOKEN") or "").strip()

    def google_client(self, env=None):
        """(client_id, client_secret) for `/connect google`: PA_GOOGLE_*
        wins, then the existing GMAIL_* pair (same OAuth client)."""
        env = os.environ if env is None else env
        cid = (env.get("PA_GOOGLE_CLIENT_ID")
               or env.get("GMAIL_CLIENT_ID") or "").strip()
        csec = (env.get("PA_GOOGLE_CLIENT_SECRET")
                or env.get("GMAIL_CLIENT_SECRET") or "").strip()
        return cid, csec

    def github_client(self, env=None):
        """(client_id, client_secret) for a GitHub OAuth App the owner
        registers by hand — GitHub's AS has no dynamic registration."""
        env = os.environ if env is None else env
        return ((env.get("PA_GITHUB_CLIENT_ID") or "").strip(),
                (env.get("PA_GITHUB_CLIENT_SECRET") or "").strip())

    def composio_key(self, env=None) -> str:
        env = os.environ if env is None else env
        return (env.get("COMPOSIO_API_KEY") or "").strip()

    def oauth_redirect_uri(self) -> str:
        """The public callback URL providers redirect to; '' when
        PA_PUBLIC_URL is unset (connect commands refuse with a hint)."""
        if not self.public_url:
            return ""
        return self.public_url + "/oauth/callback"

    def discord_channel_for(self, assistant) -> str:
        """Routed channel for an assistant row, or '' (unrouted)."""
        if not assistant:
            return ""
        for key in (str(assistant["id"]), str(assistant["name"])):
            if key in self.discord_channels:
                return self.discord_channels[key]
        return ""

    # -- bind safety -------------------------------------------------------
    def check_bind(self, host: str, port: int) -> None:
        """Enforce the network-boundary policy. Raises ConfigError."""
        host = (host or "").strip()
        if is_loopback_host(host):
            return
        if not self.is_production:
            raise ConfigError(
                f"refusing to bind non-loopback address {host!r} outside "
                f"production; use 127.0.0.1 in development")
        if self.fly_topology:
            # Documented Fly topology: the proxy is the edge; the machine is
            # still reachable on the org's private IPv6 network, so every
            # request must authenticate (enforced by the web app).
            return
        if self.container_bind:
            # Documented container topology: the app binds the container
            # interface because Docker's publish rules are the network
            # boundary (compose pins `127.0.0.1:8080:8080` — host loopback).
            # Publishing a non-loopback host IP is the owner's explicit
            # decision; every request still authenticates.
            return
        raise ConfigError(
            f"refusing to bind {host!r} in production: no recognized TLS "
            f"topology (set FLY_APP_NAME on Fly.io, PA_CONTAINER_BIND=true "
            f"inside a container whose publish rules are the boundary, or "
            f"bind 127.0.0.1 behind Caddy). An env flag alone is not proof "
            f"of TLS.")

    def trusted_client_ip(self, environ) -> str:
        """Client IP used ONLY for logging and rate-limit buckets.

        Fly-Client-IP is trusted solely in the documented Fly topology.
        Anywhere else we fall back to REMOTE_ADDR. The login throttle also
        has a global bucket, so forged headers cannot buy extra attempts.
        """
        if self.fly_topology:
            ip = (environ.get("HTTP_FLY_CLIENT_IP") or "").strip()
            if ip:
                return ip
        return environ.get("REMOTE_ADDR") or "unknown"


def load_settings(env=None) -> Settings:
    env = os.environ if env is None else env
    s = Settings()
    s.env = env.get("PA_ENV", ENV_DEVELOPMENT).strip().lower() or ENV_DEVELOPMENT
    if s.env not in (ENV_DEVELOPMENT, ENV_TEST, ENV_PRODUCTION):
        raise ConfigError(f"PA_ENV must be development|test|production, got {s.env!r}")
    s.data_dir = env.get("PA_DATA_DIR", s.data_dir)
    s.bind_host = env.get("PA_BIND", s.bind_host)
    s.container_bind = _bool(env, "PA_CONTAINER_BIND", False)
    s.port = _int(env, "PA_PORT", s.port)
    s.timezone = env.get("PA_TIMEZONE", s.timezone).strip() or DEFAULT_TIMEZONE

    s.llm_base_url = validate_llm_base_url(env.get("LLM_BASE_URL", ""))
    s.llm_model = env.get("LLM_MODEL", "").strip()
    s.llm_streaming = _bool(env, "LLM_STREAMING", False)
    s.llm_tools = _bool(env, "LLM_TOOLS", True)
    s.llm_completions_path = env.get(
        "LLM_COMPLETIONS_PATH", "/chat/completions").strip()
    if s.llm_completions_path and not s.llm_completions_path.startswith("/"):
        raise ConfigError(
            "LLM_COMPLETIONS_PATH must start with '/' or be empty")
    if s.llm_base_url and not s.llm_model:
        raise ConfigError("LLM_BASE_URL is set but LLM_MODEL is empty")
    s.dev_ollama_base_url = env.get("PA_OLLAMA_BASE_URL", s.dev_ollama_base_url)
    s.dev_model = env.get("PA_DEV_MODEL", s.dev_model)

    s.freebuff_enabled = _bool(env, "PA_FREEBUFF_ENABLED", False)
    s.freebuff_cli = env.get("PA_FREEBUFF_CLI", s.freebuff_cli).strip() \
        or s.freebuff_cli
    s.freebuff_tmux = env.get("PA_FREEBUFF_TMUX", s.freebuff_tmux).strip() \
        or s.freebuff_tmux
    s.freebuff_home = env.get("PA_FREEBUFF_HOME", "").strip() or \
        os.path.join(os.path.expanduser("~"), ".config", "manicode")
    s.freebuff_workdir = env.get("PA_FREEBUFF_WORKDIR", "").strip() or \
        os.path.join(s.data_dir, "freebuff-work")
    s.freebuff_session_timeout = _int(
        env, "PA_FREEBUFF_SESSION_TIMEOUT", s.freebuff_session_timeout)
    s.freebuff_ready_timeout = _int(
        env, "PA_FREEBUFF_READY_TIMEOUT", s.freebuff_ready_timeout)
    s.freebuff_max_output_chars = _int(
        env, "PA_FREEBUFF_MAX_OUTPUT_CHARS", s.freebuff_max_output_chars)
    s.freebuff_allow_paid = _bool(env, "PA_FREEBUFF_ALLOW_PAID", False)
    s.freebuff_assume_ready = _bool(env, "PA_FREEBUFF_ASSUME_READY", False)
    s.freebuff_live_check = _bool(env, "PA_FREEBUFF_LIVE_CHECK", False)
    s.freebuff_remote_url = validate_endpoint_url(
        env.get("PA_FREEBUFF_REMOTE_URL", ""), "PA_FREEBUFF_REMOTE_URL")
    s.freebuff_remote_token = env.get("PA_FREEBUFF_REMOTE_TOKEN", "")
    s.freebuff_sidecar_host = env.get(
        "PA_FREEBUFF_SIDECAR_HOST", s.freebuff_sidecar_host).strip() or \
        s.freebuff_sidecar_host
    s.freebuff_sidecar_port = _int(
        env, "PA_FREEBUFF_SIDECAR_PORT", s.freebuff_sidecar_port)
    if s.freebuff_remote_url and not s.freebuff_remote_token:
        raise ConfigError(
            "PA_FREEBUFF_REMOTE_URL is set but PA_FREEBUFF_REMOTE_TOKEN "
            "is empty — the sidecar needs a shared token")

    s.router_enabled = _bool(env, "PA_ROUTER_ENABLED", False)
    s.router_escalate = _bool(env, "PA_ROUTER_ESCALATE", False)
    s.router_escalate_order = env.get(
        "PA_ROUTER_ESCALATE_ORDER", "").strip()
    s.router_escalate_rpd = _int(
        env, "PA_ROUTER_ESCALATE_RPD", s.router_escalate_rpd)
    s.router_order = env.get("PA_ROUTER_ORDER", "").strip()
    s.router_smart_order = env.get("PA_ROUTER_SMART_ORDER", "").strip()
    s.router_fast_order = env.get("PA_ROUTER_FAST_ORDER", "").strip()
    s.router_smart_models = env.get("PA_ROUTER_SMART_MODELS", "").strip()
    s.router_fast_models = env.get("PA_ROUTER_FAST_MODELS", "").strip()
    s.router_call_order = env.get("PA_ROUTER_CALL_ORDER", "").strip()
    s.router_call_models = env.get("PA_ROUTER_CALL_MODELS", "").strip()
    s.call_llm_seconds = _float(
        env, "PA_CALL_LLM_SECONDS", s.call_llm_seconds)
    s.router_call_seconds = _int(
        env, "PA_ROUTER_CALL_SECONDS", s.router_call_seconds)
    s.router_backend_seconds = _int(
        env, "PA_ROUTER_BACKEND_SECONDS", s.router_backend_seconds)
    s.gemini_cli_url = validate_endpoint_url(
        env.get("PA_GEMINI_CLI_URL", s.gemini_cli_url),
        "PA_GEMINI_CLI_URL") or s.gemini_cli_url
    s.gemini_cli_token = env.get("PA_GEMINI_CLI_TOKEN", "").strip()
    s.gemini_cli_bin = env.get(
        "PA_GEMINI_CLI_BIN", s.gemini_cli_bin).strip() or s.gemini_cli_bin
    s.copilot_cli_url = validate_endpoint_url(
        env.get("PA_COPILOT_CLI_URL", s.copilot_cli_url),
        "PA_COPILOT_CLI_URL") or s.copilot_cli_url
    s.copilot_cli_token = env.get("PA_COPILOT_CLI_TOKEN", "").strip()
    s.copilot_cli_bin = env.get(
        "PA_COPILOT_CLI_BIN", s.copilot_cli_bin).strip() or s.copilot_cli_bin

    s.search_enabled = _bool(env, "SEARCH_ENABLED", False)

    s.discord_enabled = _bool(env, "DISCORD_ENABLED", True)
    s.discord_owner_id = env.get("DISCORD_OWNER_ID", "").strip()
    s.discord_channels = parse_channel_map(env.get("DISCORD_CHANNELS", ""))
    s.discord_gateway_url = env.get(
        "DISCORD_GATEWAY_URL", s.discord_gateway_url).strip()
    s.discord_api_base = env.get(
        "DISCORD_API_BASE", s.discord_api_base).strip().rstrip("/")
    s.discord_chat_channel_id = env.get(
        "DISCORD_CHAT_CHANNEL_ID", "").strip()

    s.chat_assistant = env.get(
        "PA_CHAT_ASSISTANT", s.chat_assistant).strip() or s.chat_assistant
    s.chat_max_steps = _int(env, "PA_CHAT_MAX_STEPS", s.chat_max_steps)
    s.chat_turn_seconds = _int(
        env, "PA_CHAT_TURN_SECONDS", s.chat_turn_seconds)
    s.chat_history_turns = _int(
        env, "PA_CHAT_HISTORY_TURNS", s.chat_history_turns)
    s.chat_working_seconds = _float(
        env, "PA_CHAT_WORKING_SECONDS", s.chat_working_seconds)
    s.chat_approval_wait = _int(
        env, "PA_CHAT_APPROVAL_WAIT", s.chat_approval_wait)
    s.chat_sandbox = env.get("PA_CHAT_SANDBOX", "").strip() or \
        os.path.join(s.data_dir, "chat-sandbox")
    s.shell_timeout = _float(env, "PA_SHELL_TIMEOUT", s.shell_timeout)
    s.shell_output_cap = _int(
        env, "PA_SHELL_OUTPUT_CAP", s.shell_output_cap)
    s.approval_ttl_seconds = _int(
        env, "PA_APPROVAL_TTL_SECONDS", s.approval_ttl_seconds)
    s.chat_progress = _bool(env, "PA_CHAT_PROGRESS", s.chat_progress)
    s.attach_max_bytes = _int(
        env, "PA_ATTACH_MAX_BYTES", s.attach_max_bytes)
    s.pdf_max_pages = _int(env, "PA_PDF_MAX_PAGES", s.pdf_max_pages)
    s.pdf_max_chars = _int(env, "PA_PDF_MAX_CHARS", s.pdf_max_chars)
    s.vision = _bool(env, "PA_VISION", s.vision)
    s.vision_order = env.get(
        "PA_VISION_ORDER", s.vision_order).strip()
    s.vision_max_bytes = _int(
        env, "PA_VISION_MAX_BYTES", s.vision_max_bytes)
    s.video_enabled = _bool(env, "PA_VIDEO", s.video_enabled)
    s.telegram_enabled = _bool(env, "PA_TELEGRAM", s.telegram_enabled)
    s.telegram_chat_id = env.get(
        "TELEGRAM_CHAT_ID", s.telegram_chat_id).strip()
    s.video_max_mb = _int(env, "PA_VIDEO_MAX_MB", s.video_max_mb)
    s.memory_save_approval = _bool(
        env, "PA_MEMORY_SAVE_APPROVAL", s.memory_save_approval)
    s.memory_auto_propose = _bool(
        env, "PA_MEMORY_AUTO_PROPOSE", s.memory_auto_propose)
    s.memory_propose_max = _int(
        env, "PA_MEMORY_PROPOSE_MAX", s.memory_propose_max)

    s.mac_ssh_host = env.get("MAC_SSH_HOST", "").strip()
    s.mac_ssh_user = env.get("MAC_SSH_USER", "").strip()
    s.mac_ssh_key = env.get("MAC_SSH_KEY", "").strip()
    s.mac_ssh_timeout = _float(env, "MAC_SSH_TIMEOUT", s.mac_ssh_timeout)

    s.computer_enabled = _bool(env, "PA_COMPUTER_ENABLED", False)
    s.computer_profile = env.get("PA_COMPUTER_PROFILE", "").strip() or \
        os.path.join(s.data_dir, "browser-profile")
    s.computer_max_tabs = _int(
        env, "PA_COMPUTER_MAX_TABS", s.computer_max_tabs)
    s.computer_idle_seconds = _int(
        env, "PA_COMPUTER_IDLE_SECONDS", s.computer_idle_seconds)
    s.computer_memory_mb = _int(
        env, "PA_COMPUTER_MEMORY_MB", s.computer_memory_mb)
    s.computer_lock_wait = _float(
        env, "PA_COMPUTER_LOCK_WAIT", s.computer_lock_wait)
    s.computer_url = validate_computer_url(
        env.get("PA_COMPUTER_URL", ""))
    s.computer_token = env.get("PA_COMPUTER_TOKEN", "")
    if s.computer_url and not s.computer_token:
        raise ConfigError(
            "PA_COMPUTER_URL is set but PA_COMPUTER_TOKEN is empty — "
            "the agent needs a shared token")
    s.computer_agent_bind = env.get(
        "PA_COMPUTER_AGENT_BIND", s.computer_agent_bind).strip() or \
        s.computer_agent_bind
    s.computer_agent_port = _int(
        env, "PA_COMPUTER_AGENT_PORT", s.computer_agent_port)
    s.computer_download_max = _int(
        env, "PA_COMPUTER_DOWNLOAD_MAX", s.computer_download_max)
    s.computer_browser_bin = env.get(
        "PA_COMPUTER_BROWSER_BIN", "").strip()
    s.desktop_minutes = min(
        120, _int(env, "PA_DESKTOP_MINUTES", s.desktop_minutes))
    s.desktop_port = _int(env, "PA_DESKTOP_PORT", s.desktop_port)
    s.jina_reader = _bool(env, "PA_JINA_READER", s.computer_enabled)

    s.nodes = parse_nodes(env)
    s.node_tokens = {name: env.get(node_token_env(name), "").strip()
                     for name in s.nodes}
    s.node_connect_seconds = _float(
        env, "PA_NODE_CONNECT_SECONDS", s.node_connect_seconds)
    s.node_health_seconds = _float(
        env, "PA_NODE_HEALTH_SECONDS", s.node_health_seconds)
    s.node_sms_read_unattended = _bool(
        env, "PA_NODE_SMS_READ_UNATTENDED",
        s.node_sms_read_unattended)

    s.workspace = _bool(env, "PA_WORKSPACE", s.workspace)
    s.workspace_mb = max(1, _int(
        env, "PA_WORKSPACE_MB", s.workspace_mb))
    s.workspace_project = env.get(
        "PA_WORKSPACE_PROJECT", s.workspace_project).strip() or \
        s.workspace_project
    s.madetools = _bool(env, "PA_MADETOOLS", s.madetools)
    s.integrations = _bool(env, "PA_INTEGRATIONS", s.integrations)
    s.subagents = _bool(env, "PA_SUBAGENTS", s.subagents)
    s.subagent_max = _int(env, "PA_SUBAGENT_MAX", s.subagent_max)
    s.research = _bool(env, "PA_RESEARCH", s.research)
    s.skillextract = _bool(env, "PA_SKILLEXTRACT", s.skillextract)

    s.tts_order = env.get("PA_TTS_ORDER", s.tts_order).strip() or \
        s.tts_order
    s.tts_gemini_model = env.get(
        "PA_TTS_GEMINI_MODEL", s.tts_gemini_model).strip() or \
        s.tts_gemini_model
    s.tts_gemini_voice = env.get(
        "PA_TTS_GEMINI_VOICE", s.tts_gemini_voice).strip() or \
        s.tts_gemini_voice
    s.tts_gemini_base = env.get(
        "PA_TTS_GEMINI_BASE", s.tts_gemini_base).strip().rstrip("/")
    s.tts_fish_model = env.get(
        "PA_TTS_FISH_MODEL", s.tts_fish_model).strip() or s.tts_fish_model
    s.tts_fish_voice = env.get("PA_TTS_FISH_VOICE", "").strip()
    s.tts_fish_base = env.get(
        "PA_TTS_FISH_BASE", s.tts_fish_base).strip().rstrip("/")
    s.tts_azure_voice = env.get(
        "PA_TTS_AZURE_VOICE", s.tts_azure_voice).strip() or \
        s.tts_azure_voice
    s.tts_azure_format = env.get(
        "PA_TTS_AZURE_FORMAT", s.tts_azure_format).strip() or \
        s.tts_azure_format
    s.tts_cf_speaker = env.get(
        "PA_TTS_CF_SPEAKER", s.tts_cf_speaker).strip() or s.tts_cf_speaker
    s.cloudflare_ai_base = env.get(
        "CLOUDFLARE_AI_BASE", s.cloudflare_ai_base).strip().rstrip("/")
    s.groq_whisper_base = env.get(
        "GROQ_WHISPER_BASE", s.groq_whisper_base).strip().rstrip("/")
    s.whisper_model = env.get(
        "PA_WHISPER_MODEL", s.whisper_model).strip() or s.whisper_model
    s.cf_whisper_model = env.get(
        "PA_CF_WHISPER_MODEL", s.cf_whisper_model).strip() or \
        s.cf_whisper_model
    s.ffmpeg_bin = env.get("PA_FFMPEG", s.ffmpeg_bin).strip() or \
        s.ffmpeg_bin
    s.voice_notes_default = _bool(env, "PA_VOICE_NOTES", False)

    s.call_guild_id = env.get("PA_CALL_GUILD_ID", "").strip()
    s.call_channel_id = env.get("PA_CALL_CHANNEL_ID", "").strip()
    s.call_max_per_hour = _int(
        env, "PA_CALL_MAX_PER_HOUR", s.call_max_per_hour)
    s.call_join_seconds = _int(
        env, "PA_CALL_JOIN_SECONDS", s.call_join_seconds)
    s.call_listen_seconds = _int(
        env, "PA_CALL_LISTEN_SECONDS", s.call_listen_seconds)
    s.call_max_seconds = _int(
        env, "PA_CALL_MAX_SECONDS", s.call_max_seconds)
    s.call_vad = env.get("PA_CALL_VAD", s.call_vad).strip().lower() or \
        s.call_vad
    s.call_tts_order = env.get(
        "PA_CALL_TTS_ORDER", s.call_tts_order).strip() or \
        s.call_tts_order
    s.tts_chars_day = _int(
        env, "PA_TTS_CHARS_DAY", s.tts_chars_day)
    s.browser_owner_domains = env.get(
        "PA_BROWSER_OWNER_DOMAINS", "").strip()
    s.policy_engine = _bool(
        env, "PA_POLICY_ENGINE", s.policy_engine)
    s.policy_vault_unattended = _bool(
        env, "PA_POLICY_VAULT_UNATTENDED", s.policy_vault_unattended)
    s.tool_topk = _int(env, "PA_TOOL_TOPK", s.tool_topk)
    s.call_barge_in = _bool(env, "PA_CALL_BARGE_IN", True)
    s.call_metrics = _bool(env, "PA_CALL_METRICS", s.call_metrics)
    s.call_audio = env.get(
        "PA_CALL_AUDIO", s.call_audio).strip().lower() or s.call_audio
    if s.call_audio not in ("inproc", "ffmpeg"):
        raise ConfigError(
            f"PA_CALL_AUDIO must be inproc|ffmpeg, got {s.call_audio!r}")
    s.call_tail_ms = _int(env, "PA_CALL_TAIL_MS", s.call_tail_ms)
    s.call_tail_short_ms = _int(
        env, "PA_CALL_TAIL_SHORT_MS", s.call_tail_short_ms)
    s.call_stream_brain = _bool(env, "PA_CALL_STREAM_BRAIN",
                                s.call_stream_brain)
    s.call_engine = env.get(
        "PA_CALL_ENGINE", s.call_engine).strip().lower() or s.call_engine
    if s.call_engine not in ("auto", "live", "pipeline"):
        raise ConfigError(
            "PA_CALL_ENGINE must be auto|live|pipeline, got "
            f"{s.call_engine!r}")
    s.live_model = env.get("PA_LIVE_MODEL", s.live_model).strip() or \
        s.live_model
    s.live_voice = env.get("PA_LIVE_VOICE", s.live_voice).strip() or \
        s.live_voice
    s.live_budget = _bool(env, "PA_LIVE_BUDGET", s.live_budget)
    s.voice_cache_dir = env.get("PA_VOICE_CACHE_DIR", "").strip() or \
        os.path.join(s.data_dir, "voice-cache")
    s.quiet_hours = env.get(
        "PA_QUIET_HOURS", s.quiet_hours).strip() or s.quiet_hours

    s.watch_max_classify = _int(
        env, "PA_WATCH_MAX_CLASSIFY", s.watch_max_classify)
    s.watch_max_calls_per_day = _int(
        env, "PA_WATCH_MAX_CALLS_PER_DAY", s.watch_max_calls_per_day)
    s.watch_batch_seconds = _int(
        env, "PA_WATCH_IMPORTANT_BATCH_SECONDS", s.watch_batch_seconds)
    s.open_meteo_base = env.get(
        "PA_OPEN_METEO_BASE", s.open_meteo_base).strip().rstrip("/")
    s.briefing_city = env.get(
        "PA_BRIEFING_CITY", s.briefing_city).strip() or s.briefing_city
    s.briefing_lat = _float(env, "PA_BRIEFING_LAT", s.briefing_lat)
    s.briefing_lon = _float(env, "PA_BRIEFING_LON", s.briefing_lon)
    s.import_max_bytes = _int(
        env, "PA_IMPORT_MAX_BYTES", s.import_max_bytes)

    s.evolve = _bool(env, "PA_EVOLVE", s.evolve)
    s.evolve_auto_apply = _bool(
        env, "PA_EVOLVE_AUTO_APPLY", s.evolve_auto_apply)
    s.evolve_self_heal = _bool(
        env, "PA_EVOLVE_SELF_HEAL", s.evolve_self_heal)
    s.evolve_plan_calls_per_day = _int(
        env, "PA_EVOLVE_PLAN_CALLS", s.evolve_plan_calls_per_day)
    s.evolve_reflect_max = _int(
        env, "PA_EVOLVE_REFLECT_MAX", s.evolve_reflect_max)
    s.evolve_detect_threshold = _int(
        env, "PA_EVOLVE_DETECT_THRESHOLD", s.evolve_detect_threshold)
    s.evolve_detect_max_items = _int(
        env, "PA_EVOLVE_DETECT_MAX_ITEMS", s.evolve_detect_max_items)
    s.evolve_health_seconds = _int(
        env, "PA_EVOLVE_HEALTH_SECONDS", s.evolve_health_seconds)
    s.evolve_archive = _bool(
        env, "PA_EVOLVE_ARCHIVE", s.evolve_archive)
    s.evolve_practice = _bool(
        env, "PA_EVOLVE_PRACTICE", s.evolve_practice)

    s.vault_git_sync = _bool(env, "VAULT_GIT_SYNC", False)

    s.public_url = validate_endpoint_url(
        env.get("PA_PUBLIC_URL", ""), "PA_PUBLIC_URL")
    s.mcp_timeout = _float(env, "PA_MCP_TIMEOUT", s.mcp_timeout)

    s.composio_api_base = env.get(
        "COMPOSIO_API_BASE", s.composio_api_base).strip().rstrip("/")
    s.composio_user_id = env.get(
        "COMPOSIO_USER_ID", s.composio_user_id).strip() or \
        s.composio_user_id
    s.composio_monthly_limit = _int(
        env, "PA_COMPOSIO_MONTHLY_LIMIT", s.composio_monthly_limit)
    s.google_auth_url = env.get(
        "GOOGLE_AUTH_URL", s.google_auth_url).strip()

    s.gcal_api_base = env.get(
        "GCAL_API_BASE", s.gcal_api_base).strip().rstrip("/")
    s.ddg_base = env.get("DDG_SEARCH_BASE", s.ddg_base).strip().rstrip("/")

    s.gmail_api_base = env.get(
        "GMAIL_API_BASE", s.gmail_api_base).strip().rstrip("/")
    s.gmail_token_url = env.get("GMAIL_TOKEN_URL", s.gmail_token_url).strip()

    s.vault_path = env.get("VAULT_PATH", "").strip()
    s.vault_max_file_bytes = _int(
        env, "VAULT_MAX_FILE_BYTES", s.vault_max_file_bytes)
    s.vault_max_files = _int(env, "VAULT_MAX_FILES", s.vault_max_files)
    s.vault_max_results = _int(
        env, "VAULT_MAX_RESULTS", s.vault_max_results)
    s.vault_note_cap = _int(env, "VAULT_NOTE_CAP", s.vault_note_cap)

    s.max_input_tokens = _int(env, "PA_MAX_INPUT_TOKENS", s.max_input_tokens)
    s.max_completion_tokens = _int(
        env, "PA_MAX_COMPLETION_TOKENS", s.max_completion_tokens)
    s.tpm_limit = _int(env, "PA_TPM_LIMIT", s.tpm_limit)
    s.rpm_limit = _int(env, "PA_RPM_LIMIT", s.rpm_limit)
    s.daily_request_limit = _int(
        env, "PA_DAILY_REQUEST_LIMIT", s.daily_request_limit)
    s.daily_token_limit = _int(
        env, "PA_DAILY_TOKEN_LIMIT", s.daily_token_limit)
    s.rate_max_retries = _int(
        env, "PA_RATE_MAX_RETRIES", s.rate_max_retries)

    s.max_turns = _int(env, "PA_MAX_TURNS", s.max_turns)
    s.task_deadline_seconds = _int(
        env, "PA_TASK_DEADLINE_SECONDS", s.task_deadline_seconds)
    s.lease_ttl_seconds = _float(
        env, "PA_LEASE_TTL_SECONDS", s.lease_ttl_seconds)
    s.heartbeat_seconds = _float(
        env, "PA_HEARTBEAT_SECONDS", s.heartbeat_seconds)
    s.worker_poll_seconds = _float(
        env, "PA_WORKER_POLL_SECONDS", s.worker_poll_seconds)

    s.session_idle_seconds = _int(
        env, "PA_SESSION_IDLE_SECONDS", s.session_idle_seconds)
    s.session_absolute_seconds = _int(
        env, "PA_SESSION_ABSOLUTE_SECONDS", s.session_absolute_seconds)
    s.login_window_seconds = _int(
        env, "PA_LOGIN_WINDOW_SECONDS", s.login_window_seconds)
    s.login_max_failures = _int(
        env, "PA_LOGIN_MAX_FAILURES", s.login_max_failures)
    s.max_body_bytes = _int(env, "PA_MAX_BODY_BYTES", s.max_body_bytes)

    # final-2
    s.goals = _bool(env, "PA_GOALS", s.goals)
    s.proactive = _bool(env, "PA_PROACTIVE", s.proactive)
    s.proactive_goal_tokens = _int(
        env, "PA_PROACTIVE_GOAL_TOKENS", s.proactive_goal_tokens)
    s.activity_feed = _bool(env, "PA_ACTIVITY", s.activity_feed)
    s.activity_greeting = _bool(
        env, "PA_ACTIVITY_GREETING", s.activity_greeting)
    s.automation = _bool(env, "PA_AUTOMATION", s.automation)
    s.briefing_goals = _bool(
        env, "PA_BRIEFING_GOALS", s.briefing_goals)
    s.memory_blocks = _bool(env, "PA_MEMORY_BLOCKS", s.memory_blocks)
    s.sleeptime = _bool(env, "PA_SLEEPTIME", s.sleeptime)
    s.recall = _bool(env, "PA_RECALL", s.recall)
    s.kg = _bool(env, "PA_KG", s.kg)
    s.triage = _bool(env, "PA_TRIAGE", s.triage)
    s.cal_triage = _bool(env, "PA_CAL_TRIAGE", s.cal_triage)
    s.cal_soon = _bool(env, "PA_CAL_SOON", s.cal_soon)
    s.cal_soon_leads = env.get(
        "PA_CAL_SOON_LEADS", s.cal_soon_leads).strip() or "15,5"
    s.leave_now = _bool(env, "PA_LEAVE_NOW", s.leave_now)
    s.leave_now_phone_loc = _bool(
        env, "PA_LEAVE_NOW_USE_PHONE_LOC", s.leave_now_phone_loc)
    s.home_lat = env.get("PA_HOME_LAT", s.home_lat).strip()
    s.home_lon = env.get("PA_HOME_LON", s.home_lon).strip()
    s.tfl_base = env.get(
        "PA_TFL_BASE", s.tfl_base).strip().rstrip("/")
    s.secret_capture = _bool(
        env, "PA_SECRET_CAPTURE", s.secret_capture)
    s.notify_phone = _bool(env, "PA_NOTIFY_PHONE", s.notify_phone)
    s.mail_inbound = _bool(env, "PA_MAIL_INBOUND", s.mail_inbound)
    s.owner_email = env.get(
        "PA_OWNER_EMAIL", s.owner_email).strip().lower()
    s.mail_inbound_senders = env.get(
        "PA_MAIL_INBOUND_SENDERS", s.mail_inbound_senders).strip()
    s.github_hook_agent = _bool(
        env, "PA_GITHUB_HOOK_AGENT", s.github_hook_agent)
    s.coursework = _bool(env, "PA_COURSEWORK", s.coursework)
    s.notion_index = _bool(env, "PA_NOTION_INDEX", s.notion_index)
    s.notify_v2 = _bool(env, "PA_NOTIFY_V2", s.notify_v2)
    s.notify_per_hour = _int(
        env, "PA_NOTIFY_PER_HOUR", s.notify_per_hour)
    s.projects = _bool(env, "PA_PROJECTS", s.projects)

    s.fetch_max_bytes = _int(env, "PA_FETCH_MAX_BYTES", s.fetch_max_bytes)
    s.fetch_timeout_seconds = _float(
        env, "PA_FETCH_TIMEOUT_SECONDS", s.fetch_timeout_seconds)
    s.fetch_max_redirects = _int(
        env, "PA_FETCH_MAX_REDIRECTS", s.fetch_max_redirects)

    s.fly_app_name = env.get("FLY_APP_NAME", "").strip()
    return s

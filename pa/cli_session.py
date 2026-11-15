"""Official-CLI session provider — spawn a signed-in CLI in headless
mode, one process per prompt.

Like FreebuffSessionProvider this is NOT an OpenAI-compatible endpoint
(``kind == "agent_session"``): prompt in, plain text out. Unlike it,
modern CLIs have a documented non-interactive print mode, so no tmux is
needed — one ``subprocess.run`` per prompt:

- ``agy -p "<prompt>"``         — Google Antigravity CLI (personal Google
  accounts incl. Google AI Pro; the old `gemini` CLI no longer serves
  consumer sign-ins, but works with a Code Assist seat — override the
  binary via PA_GEMINI_CLI_BIN).
- ``copilot -p "<prompt>" -s --no-ask-user`` — GitHub Copilot CLI
  (documented headless mode; uses the signed-in Copilot subscription —
  Student Pack = Copilot Pro).

Both CLIs are driven only through their documented flags; we never read
their credential stores. Served to the router by ``pa cli-sidecar``
(loopback + bearer token, same wire protocol as the freebuff sidecar).
"""

import logging
import shutil
import subprocess
import time

from .provider import (ProviderNotConfigured, ProviderRateLimit,
                       ProviderUnavailable)
from .util import truncate_middle

log = logging.getLogger("pa.cli_session")

MAX_PROMPT_CHARS = 60_000
MAX_OUTPUT_CHARS = 60_000
MAX_SESSION_SECONDS = 300.0

_AUTH_HINTS = ("login", "sign in", "signin", "authenticate",
               "authentication required", "not logged in", "oauth")
_QUOTA_HINTS = ("rate limit", "rate_limit", "too many requests",
                "quota", "429", "usage limit", "exhausted")


class CliSessionProvider:
    """One prompt -> one headless CLI call -> one text answer."""

    kind = "agent_session"
    tools_enabled = False
    streaming = False
    last_headers: dict = {}

    def __init__(self, argv, session_timeout=MAX_SESSION_SECONDS,
                 workdir=None):
        self.argv = list(argv)
        self.session_timeout = session_timeout
        self.workdir = workdir
        self.binary = self.argv[0] if self.argv else ""

    # -- introspection (sidecar /healthz + router-check) -------------------
    def check_installation(self):
        path = shutil.which(self.binary)
        ok = bool(path)
        ver = None
        if ok:
            try:
                p = subprocess.run([self.binary, "--version"],
                                   capture_output=True, text=True,
                                   timeout=15)
                ver = (p.stdout or p.stderr or "").strip().splitlines()[0]
            except (OSError, subprocess.TimeoutExpired, IndexError):
                ver = None
        return [("cli", ok,
                 f"{self.binary} -> {path}"
                 + (f" ({ver})" if ver else "") if ok else
                 f"{self.binary} not on PATH")]

    def version(self):
        checks = self.check_installation()
        return checks[0][2] if checks else None

    # freebuff_sidecar.serve() reports this on /healthz
    _installed_version = version

    # -- main entry ---------------------------------------------------------
    def run(self, prompt, deadline_seconds=None, check_cancel=None):
        if check_cancel:
            check_cancel()
        prompt = (prompt or "").strip()
        if not prompt:
            raise ProviderUnavailable("empty prompt")
        if not self.binary or not shutil.which(self.binary):
            raise ProviderNotConfigured(
                f"CLI {self.binary!r} not on PATH — install it and sign "
                "in once on the host (see docs/ROUTER.md)")
        timeout = min(deadline_seconds or self.session_timeout,
                      self.session_timeout)
        started = time.monotonic()
        try:
            p = subprocess.run(
                self.argv + [truncate_middle(prompt, MAX_PROMPT_CHARS)],
                capture_output=True, text=True, timeout=timeout,
                cwd=self.workdir or None)
        except subprocess.TimeoutExpired:
            raise ProviderUnavailable(
                f"{self.binary} headless run exceeded {timeout:.0f}s")
        except OSError as e:
            raise ProviderNotConfigured(
                f"cannot run {self.binary}: {e}")
        elapsed = round(time.monotonic() - started, 1)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if p.returncode != 0:
            raise self._classify(err or out)
        if not out:
            raise ProviderUnavailable(
                f"{self.binary} returned no output"
                + (f": {err[:200]}" if err else ""))
        return {"text": truncate_middle(out, MAX_OUTPUT_CHARS),
                "reasoning": None,
                "meta": {"cli": self.binary, "duration_s": elapsed}}

    def _classify(self, err):
        low = (err or "").lower()
        if any(h in low for h in _AUTH_HINTS):
            return ProviderNotConfigured(
                f"{self.binary} is not signed in — log it in once on the "
                f"host ({err[:160]})")
        if any(h in low for h in _QUOTA_HINTS):
            return ProviderRateLimit(
                f"{self.binary} hit a quota/rate limit: {err[:160]}",
                retry_after=60.0)
        return ProviderUnavailable(
            f"{self.binary} exited non-zero: {err[:200]}")

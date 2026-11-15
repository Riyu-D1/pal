"""Freebuff agent-session provider — prompt in, CLI session transcript out.

This is NOT an OpenAI-compatible chat endpoint. It drives the owner's own
official ``freebuff`` CLI through its normal terminal interface — the CLI
runs inside a tmux pseudo-terminal and the prompt is entered through the
same input path a human at the keyboard uses (bracketed paste + Enter,
the method the upstream repo's own ``scripts/tmux`` test helpers use).
The answer is read from the transcript files the CLI itself writes
(``<config_home>/projects/<dir>/chats/<chat>/chat-messages.json``).

What this deliberately does NOT do:

- no backend/API calls of its own, no session tokens read from app
  storage, no fingerprint or client spoofing — the official binary does
  all of that itself through its own login;
- no tool-calling surface on our side: a session answer is plain text
  (``kind == "agent_session"``). The freebuff agent has its own built-in
  tools inside its workdir, which is why every run gets a fresh empty
  working directory and the prompt instructs it not to modify files or
  run commands. That is an instruction, not a sandbox — see
  docs/FREEBUFF_CONNECTION.md for the residual-risk discussion;
- no retries that would multiply session spend. One task = at most one
  admitted session. A refused admission fails the task visibly.

UI prompts the driver answers through the normal interface, exactly as a
user at the keyboard would:

- the profile-survey overlay -> Escape ("not now");
- a stale live-session takeover prompt -> Enter (the owner's own session
  being reclaimed);
- the wallet-spend "Enter: confirm and send" -> Enter ONLY when
  ``PA_FREEBUFF_ALLOW_PAID=true``. By default the task fails instead of
  silently spending wallet hours;
- "Enter: retry" / "choose another model" / refusal notices -> never
  pressed: they fail the task with the CLI's own reason.
"""

import glob
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time

from .provider import (ProviderError, ProviderNotConfigured,
                       ProviderRateLimit, ProviderUnavailable)
from .util import json_loads_bounded, new_id, truncate_middle

log = logging.getLogger("pa.freebuff")

# Hostname-style sentinel used in assistants.provider_host. It is only
# resolvable while PA_FREEBUFF_ENABLED=true (see Settings.provider_allowlist).
FREEBUFF_HOST = "freebuff-cli"
# provider_model value for freebuff assistants: the session runs whatever
# model the signed-in CLI is configured with; we do not drive the picker.
FREEBUFF_MODEL_DEFAULT = "default"

MAX_PROMPT_CHARS = 24_000
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
REASONING_CAP = 4_000
# Version this adapter's screen markers and transcript handling were
# live-verified against; a drift is warned about, not refused.
TESTED_CLI_VERSION = "0.2.36"

# <<<UNTRUSTED x >>> / <<<END UNTRUSTED x>>> made the model backend
# return a 400 provider_error in live testing (see
# docs/FREEBUFF_CONNECTION.md). The markers are translated to bracket
# form before a prompt is sent — the untrusted-data boundary and its
# policy text survive; only the delimiter glyphs change.
_UNTRUSTED_RE = re.compile(r"<<<\s*((?:END\s+)?UNTRUSTED[^>]*?)\s*>>>")

READY_POLL = 0.4
WAIT_POLL = 0.75
QUIT_TIMEOUT = 15.0
TMUX_CALL_TIMEOUT = 10.0

# Screen markers observed on freebuff 0.2.36 (see docs/FREEBUFF_CONNECTION.md).
# Ready/login markers are the same strings the upstream repo's own e2e
# tests wait on (freebuff/e2e/utils/freebuff-session.ts) — the set is
# intentionally redundant because the boot screen varies by build/auth.
_MARK_INPUT = ("Enter a coding task", "for commands",
               "/model to change", "Your first message starts the session.")
_MARK_SURVEY = ("quick question", "Doesn't apply")
_MARK_CONFIRM_SPEND = "Enter: confirm and send"
_MARK_TAKEOVER = "Enter: take over"
_MARK_RETRY = "Enter: retry"
_MARK_CHOOSE_MODEL = ("choose another model", "Enter: change model")
_MARK_LOGIN = ("log in", "login to", "sign in", "authenticate",
               "press enter to login", "open this url")
_REFUSAL_MARKS = (
    "session limit reached",
    "too many freebuff sessions",
    "account is suspended",
    "disable vpn, proxy or tor",
    "could not verify your location",
    "not enough hours for",
    "unavailable in ",
)


class FreebuffNotConfigured(ProviderNotConfigured):
    pass


class FreebuffSessionError(ProviderError):
    pass


class FreebuffRateLimited(ProviderRateLimit):
    """Freebuff refused admission: quota/session/network gate."""


class FreebuffUnavailable(ProviderUnavailable):
    """The local harness (tmux/CLI process) failed, not the service."""


class Tmux:
    """Thin wrapper over one private tmux socket.

    A dedicated ``-L`` socket keeps us off the owner's own tmux server and
    makes the driver collision-free per provider instance.
    """

    def __init__(self, binary="tmux", socket="pa-freebuff"):
        self.binary = binary
        self.socket = socket

    def _cmd(self, args, timeout=TMUX_CALL_TIMEOUT):
        try:
            return subprocess.run(
                [self.binary, "-L", self.socket, *args],
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise FreebuffUnavailable(f"tmux {' '.join(args[:2])} timed out")
        except OSError as e:
            raise FreebuffNotConfigured(f"cannot run tmux: {e}")

    def has_session(self, name):
        return self._cmd(["has-session", "-t", name]).returncode == 0

    def new_session(self, name, shell_cmd, cols=140, lines=40):
        r = self._cmd(["new-session", "-d", "-s", name,
                       "-x", str(cols), "-y", str(lines), shell_cmd])
        if r.returncode != 0:
            raise FreebuffUnavailable(
                f"tmux new-session failed: {(r.stderr or '').strip()[:200]}")

    def send_key(self, name, key):
        self._cmd(["send-keys", "-t", name, key])

    def send_paste(self, name, text):
        # Bracketed paste: TUI input is read char-by-char and plain send-keys
        # drops characters; \e[200~ ... \e[201~ is processed atomically.
        self._cmd(["send-keys", "-t", name, "C-u"])
        self._cmd(["send-keys", "-t", name, f"\x1b[200~{text}\x1b[201~"])

    def capture(self, name):
        r = self._cmd(["capture-pane", "-p", "-t", name])
        return r.stdout or ""

    def kill(self, name):
        self._cmd(["kill-session", "-t", name])


def project_dir_for(workdir: str, config_home: str) -> str:
    """Where the CLI files transcripts for a run rooted at `workdir`:
    <config_home>/projects/<basename(workdir)>."""
    base = os.path.basename(os.path.normpath(workdir))
    return os.path.join(config_home, "projects", base)


def newest_chat_messages(projects_dir: str):
    """Newest chats/*/chat-messages.json under a project dir (by mtime)."""
    pat = os.path.join(projects_dir, "chats", "*", "chat-messages.json")
    files = [p for p in glob.glob(pat) if os.path.isfile(p)]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _read_transcript(path: str):
    with open(path, "rb") as f:
        raw = f.read(MAX_TRANSCRIPT_BYTES + 1)
    if len(raw) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("transcript exceeds cap")
    return json_loads_bounded(raw.decode("utf-8", "replace"),
                              max_len=MAX_TRANSCRIPT_BYTES)


def extract_answer(messages):
    """Fold chat-messages.json into (answer, reasoning, meta) or None.

    The CLI writes one object per turn item: user items, ai turns and
    dividers. An ai turn carries ``isComplete: true`` once the run ends;
    its ``text`` blocks split into ``textType: 'reasoning'`` (thinking)
    and ``textType: 'text'`` (the answer)."""
    if not isinstance(messages, list):
        return None
    complete = [m for m in messages
                if isinstance(m, dict) and m.get("variant") == "ai"
                and m.get("isComplete")]
    if not complete:
        return None
    last = complete[-1]
    texts, reasoning = [], []
    for b in last.get("blocks") or []:
        if not isinstance(b, dict) or b.get("type") != "text":
            continue
        if b.get("textType") == "reasoning":
            reasoning.append(b.get("content") or "")
        elif b.get("textType") == "text":
            texts.append(b.get("content") or "")
    return {
        "text": "\n".join(t for t in texts if t).strip(),
        "reasoning": "\n".join(r for r in reasoning if r).strip() or None,
        # set when the CLI completes a turn by failing it (e.g. a backend
        # 400) — the turn is done and must not be waited on further
        "user_error": last.get("userError"),
        "completion_time": last.get("completionTime"),
        "credits": last.get("credits"),
        "message_id": last.get("id"),
    }


def classify_screen(text: str):
    """Detect interactive states in the captured TUI screen.

    Returns (state, detail). States:
      'ready'         input box mounted (pre-send only)
      'survey'        profile survey overlay -> send Escape
      'confirm_spend' wallet-spend confirmation -> Enter iff allow_paid
      'takeover'      stale live session -> Enter reclaims it
      'retry'         admission failed with retry offered -> fail
      'choose_model'  admission wants a different model -> fail
      'refused'       quota/network ban text on screen -> fail
      'working'       a turn is visibly in progress
      'other'         anything else
    """
    low = text.lower()
    if _MARK_CONFIRM_SPEND in text:
        return "confirm_spend", _MARK_CONFIRM_SPEND
    if _MARK_TAKEOVER in text:
        return "takeover", _MARK_TAKEOVER
    if _MARK_RETRY in text:
        return "retry", _MARK_RETRY
    if any(m.lower() in low for m in _MARK_CHOOSE_MODEL):
        return "choose_model", _MARK_CHOOSE_MODEL[0]
    for mark in _REFUSAL_MARKS:
        if mark in low:
            line = next((ln.strip() for ln in text.splitlines()
                         if mark in ln.lower()), mark)
            return "refused", line[:200]
    if any(m.lower() in low for m in _MARK_SURVEY):
        return "survey", _MARK_SURVEY[0]
    if any(m in text for m in _MARK_INPUT):
        return "ready", _MARK_INPUT[0]
    if any(m in low for m in _MARK_LOGIN):
        return "login", "login prompt"
    return "other", ""


def _translate_untrusted_markers(text: str) -> str:
    return _UNTRUSTED_RE.sub(r"[\1]", text)


_SESSION_LOCK = threading.Lock()


class FreebuffSessionProvider:
    """One prompt -> one official-CLI agent session -> one text answer.

    Constructed only via provider_for()/freebuff_provider_for(). All
    tunables come from Settings (PA_FREEBUFF_*), never from a task or a
    prompt.
    """

    kind = "agent_session"
    # An agent session cannot emit OpenAI-style tool_calls; the worker
    # runs the single-shot session path for this provider instead.
    tools_enabled = False
    streaming = False
    last_headers: dict = {}

    def __init__(self, settings):
        s = settings
        self.cli = s.freebuff_cli
        self.tmux = Tmux(s.freebuff_tmux)
        self.config_home = s.freebuff_home
        self.workdir_base = s.freebuff_workdir
        self.session_timeout = s.freebuff_session_timeout
        self.ready_timeout = s.freebuff_ready_timeout
        self.max_output_chars = s.freebuff_max_output_chars
        self.allow_paid = s.freebuff_allow_paid
        self.assume_ready = s.freebuff_assume_ready
        self.live_check = s.freebuff_live_check
        self.model = FREEBUFF_MODEL_DEFAULT

    # -- introspection (used by `pa provider-check`) ----------------------
    def check_installation(self):
        checks = []
        cli_path = shutil.which(self.cli)
        checks.append(("cli", bool(cli_path),
                       f"{self.cli} -> {cli_path}" if cli_path else
                       f"{self.cli} not on PATH"))
        tmux_path = shutil.which(self.tmux.binary)
        checks.append(("tmux", bool(tmux_path),
                       f"tmux -> {tmux_path}" if tmux_path else
                       "tmux not on PATH"))
        home = self.config_home
        checks.append(("config_home", os.path.isdir(home),
                       home if os.path.isdir(home) else
                       f"{home} missing — run `freebuff login` once"))
        ver = self._installed_version()
        checks.append(("cli_version", True,
                       f"{ver} (live-tested: {TESTED_CLI_VERSION})"
                       if ver == TESTED_CLI_VERSION else
                       f"{ver or 'unknown'} — markers were verified "
                       f"against {TESTED_CLI_VERSION}; a TUI redesign "
                       f"can break them"))
        # Presence check only. The file is never opened or read.
        creds = os.path.join(home, "credentials.json")
        checks.append(("credentials", os.path.isfile(creds),
                       "credentials.json present (not read)" if
                       os.path.isfile(creds) else
                       "no credentials.json — run `freebuff login`"))
        return checks

    def _installed_version(self):
        """Launcher-written metadata (version info only — no secrets)."""
        try:
            meta = json_loads_bounded(open(os.path.join(
                self.config_home, "freebuff-metadata.json")).read())
            return meta.get("version") if isinstance(meta, dict) else None
        except (OSError, ValueError):
            return None

    def _warn_on_version_drift(self):
        ver = self._installed_version()
        if ver and ver != TESTED_CLI_VERSION:
            log.warning(
                "freebuff %s differs from live-tested %s — TUI markers "
                "may need review", ver, TESTED_CLI_VERSION)

    # -- tmux plumbing ----------------------------------------------------
    def _launch(self, name, workdir):
        # FREEBUFF_CONFIG_DIR is the CLI's official override for its config
        # home; we only set it when the owner overrode PA_FREEBUFF_HOME so
        # both sides always agree on where transcripts land.
        env_prefix = ""
        if self.config_home != _default_config_home():
            env_prefix = (f"FREEBUFF_CONFIG_DIR={shlex.quote(self.config_home)}"
                          " ")
        cmd = f"{env_prefix}{shlex.quote(self.cli)} --cwd {shlex.quote(workdir)}"
        self.tmux.new_session(name, cmd)

    def _quit(self, name):
        """Leave the session the way a user does: /exit, then force-kill
        the pane only if the process ignores the clean path."""
        try:
            if not self.tmux.has_session(name):
                return
            try:
                self.tmux.send_paste(name, "/exit")
                self.tmux.send_key(name, "Enter")
            except ProviderError:
                pass
            deadline = time.monotonic() + QUIT_TIMEOUT
            while time.monotonic() < deadline:
                if not self.tmux.has_session(name):
                    return
                time.sleep(0.5)
            self.tmux.kill(name)
        except ProviderError:
            pass
        except Exception:
            log.exception("freebuff quit failed for session %s", name)

    # -- main entry --------------------------------------------------------
    def run(self, prompt, deadline_seconds=None, check_cancel=None):
        """Send one prompt through the official TUI and return its answer.

        ``check_cancel`` is a callable polled between ticks; it may raise
        (Cancelled / DeadlineReached / AbortAttempt). ``deadline_seconds``
        additionally caps the wall clock at min(task deadline,
        PA_FREEBUFF_SESSION_TIMEOUT). At most one CLI session runs at a
        time; a second caller queues on the session lock for as long as
        its own deadline allows instead of failing instantly."""
        queue_budget = deadline_seconds or self.session_timeout
        started = time.monotonic()
        while not _SESSION_LOCK.acquire(timeout=1.0):
            if check_cancel:
                check_cancel()
            if time.monotonic() - started >= queue_budget:
                raise FreebuffUnavailable(
                    f"queued behind another freebuff session for "
                    f"{queue_budget:.0f}s")
        try:
            waited = time.monotonic() - started
            remaining = (deadline_seconds - waited
                         if deadline_seconds is not None else None)
            if remaining is not None and remaining <= 0:
                raise FreebuffUnavailable(
                    "task deadline elapsed while queued for freebuff")
            return self._run_locked(prompt, remaining, check_cancel)
        finally:
            _SESSION_LOCK.release()

    def _run_locked(self, prompt, deadline_seconds, check_cancel):
        if not shutil.which(self.cli):
            raise FreebuffNotConfigured(
                f"freebuff CLI {self.cli!r} not on PATH")
        if not shutil.which(self.tmux.binary):
            raise FreebuffNotConfigured("tmux not on PATH")
        prompt = (prompt or "").strip()
        if not prompt:
            raise FreebuffSessionError("empty prompt")
        prompt = _translate_untrusted_markers(prompt)
        prompt = truncate_middle(prompt, MAX_PROMPT_CHARS)
        self._warn_on_version_drift()

        run_id = new_id(4)
        name = f"pafb-{run_id}"
        workdir = os.path.join(self.workdir_base, f"fb-{run_id}")
        os.makedirs(workdir, exist_ok=True)
        proj_dir = project_dir_for(workdir, self.config_home)

        timeout = min(deadline_seconds or self.session_timeout,
                      self.session_timeout)
        deadline = time.monotonic() + timeout
        meta = {"run_id": run_id, "workdir": workdir, "gates": []}
        started = time.monotonic()

        def tick():
            if check_cancel:
                check_cancel()
            if time.monotonic() > deadline:
                raise FreebuffUnavailable(
                    f"freebuff session exceeded {timeout:.0f}s cap")

        try:
            self._launch(name, workdir)
            self._await_ready(name, deadline, tick)
            self.tmux.send_paste(name, prompt)
            time.sleep(0.3)
            self.tmux.send_key(name, "Enter")
            answer = self._await_answer(name, proj_dir, deadline, tick, meta)
        finally:
            self._quit(name)

        meta["duration_s"] = round(time.monotonic() - started, 1)
        meta["chat_id"] = answer.get("chat_id")
        meta["completion_time"] = answer.get("completion_time")
        meta["credits"] = answer.get("credits")
        return {
            "text": truncate_middle(answer["text"], self.max_output_chars),
            "reasoning": truncate_middle(answer["reasoning"] or "",
                                         REASONING_CAP) or None,
            "meta": meta,
        }

    def _await_ready(self, name, deadline, tick):
        """Wait for the TUI input to mount, or fail on a login wall."""
        last_screen = ""
        last_esc = 0.0
        ready_deadline = min(deadline, time.monotonic() + self.ready_timeout)
        while time.monotonic() < ready_deadline:
            tick()
            if not self.tmux.has_session(name):
                raise FreebuffUnavailable(
                    f"freebuff exited during startup; screen tail: "
                    f"{last_screen[-400:]!r}")
            screen = self.tmux.capture(name)
            if screen and screen != last_screen:
                last_screen = screen
            state, detail = classify_screen(screen)
            if state == "ready":
                return
            if state == "login":
                raise FreebuffNotConfigured(
                    "freebuff CLI is not signed in — run `freebuff login` "
                    "once as the owner")
            if state == "survey" and \
                    time.monotonic() - last_esc > 2.0:
                self.tmux.send_key(name, "Escape")
                last_esc = time.monotonic()
            time.sleep(READY_POLL)
        if self.assume_ready and last_screen.strip():
            # Owner escape hatch for marker drift after a CLI update:
            # paste into a live, non-empty, unrecognized screen anyway.
            log.warning("freebuff ready marker absent; "
                        "PA_FREEBUFF_ASSUME_READY — proceeding blind")
            return
        raise FreebuffUnavailable(
            f"TUI not ready within cap; screen tail: {last_screen[-400:]!r}")

    def _await_answer(self, name, proj_dir, deadline, tick, meta):
        """Poll the CLI transcript for a completed ai turn; handle UI
        prompts through the normal interface while waiting."""
        last_survey_esc = 0.0
        saw_transcript = False
        while True:
            tick()
            chat_file = newest_chat_messages(proj_dir)
            if chat_file:
                saw_transcript = True
                try:
                    answer = extract_answer(_read_transcript(chat_file))
                except (OSError, ValueError):
                    answer = None  # mid-write; try again next tick
                if answer and answer.get("user_error"):
                    raise FreebuffSessionError(
                        f"freebuff turn failed: {answer['user_error']}")
                if answer and answer["text"]:
                    answer["chat_id"] = os.path.basename(
                        os.path.dirname(chat_file))
                    return answer
            if not self.tmux.has_session(name):
                raise FreebuffUnavailable(
                    "freebuff exited before producing an answer"
                    + ("" if saw_transcript else
                       " (no transcript was written)"))
            screen = self.tmux.capture(name)
            state, detail = classify_screen(screen)
            if state == "survey" and \
                    time.monotonic() - last_survey_esc > 2.0:
                self.tmux.send_key(name, "Escape")
                last_survey_esc = time.monotonic()
                meta["gates"].append("survey_dismissed")
            elif state == "confirm_spend":
                if not self.allow_paid:
                    raise FreebuffRateLimited(
                        "freebuff asked to confirm wallet spend "
                        "(Enter: confirm and send); refusing — set "
                        "PA_FREEBUFF_ALLOW_PAID=true to allow",
                        reset_at=None, retry_after=None)
                self.tmux.send_key(name, "Enter")
                meta["gates"].append("spend_confirmed")
            elif state == "takeover":
                self.tmux.send_key(name, "Enter")
                meta["gates"].append("takeover")
            elif state == "retry":
                raise FreebuffRateLimited(
                    f"freebuff admission failed and offered retry; "
                    f"refusing to evade the limit: {detail}",
                    reset_at=None, retry_after=None)
            elif state == "choose_model":
                raise FreebuffRateLimited(
                    f"freebuff requires a different (likely paid) model: "
                    f"{detail}", reset_at=None, retry_after=None)
            elif state == "refused":
                raise FreebuffRateLimited(
                    f"freebuff refused the session: {detail}",
                    reset_at=None, retry_after=None)
            time.sleep(WAIT_POLL)


def _default_config_home():
    return os.path.join(os.path.expanduser("~"), ".config", "manicode")


def freebuff_provider_for(settings):
    """Build the session provider; raises ProviderNotConfigured when the
    owner has not enabled freebuff.

    With PA_FREEBUFF_REMOTE_URL set, returns the remote provider that
    delegates to the home sidecar — same agent_session contract, the
    CLI just runs on owner hardware rather than this host."""
    if not getattr(settings, "freebuff_enabled", False):
        raise ProviderNotConfigured(
            "freebuff provider not enabled — set PA_FREEBUFF_ENABLED=true "
            "(see docs/FREEBUFF_CONNECTION.md)")
    if getattr(settings, "freebuff_remote_url", ""):
        from .freebuff_sidecar import FreebuffRemoteProvider
        return FreebuffRemoteProvider(
            settings.freebuff_remote_url, settings.freebuff_remote_token,
            session_timeout=settings.freebuff_session_timeout)
    return FreebuffSessionProvider(settings)

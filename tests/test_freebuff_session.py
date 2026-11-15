"""Freebuff agent-session provider tests.

Deterministic: a fake `freebuff` CLI script and an in-memory FakeTmux
cover the orchestration; where a real tmux binary exists the driver is
also exercised end-to-end against the fake CLI. Nothing here starts a
real freebuff session — fake success is NOT live success (the live
smoke result is recorded in docs/FREEBUFF_CONNECTION.md).
"""

import json
import os
import shutil
import stat
import unittest

from pa import freebuff_session as fbs
from pa import worker as workmod
from pa.agent import Cancelled, build_session_prompt
from pa.config import load_settings
from pa.freebuff_session import (FREEBUFF_HOST, FreebuffRateLimited,
                                 FreebuffSessionError,
                                 FreebuffSessionProvider,
                                 FreebuffUnavailable, Tmux,
                                 classify_screen, extract_answer,
                                 project_dir_for)
from pa.provider import ProviderNotConfigured, provider_for
from tests.helpers import TmpEnv

READY = "Glyph Cluster · /tmp/wd · /model to change · Chat: New chat\n" \
        " ▍Enter a coding task or / for commands\n"
SURVEY = "quick question 1/11\nWho pays for your dev tools?\n" \
         "0 Doesn't apply\n"
CONFIRM = "Glyph X takes 1h from your balance.\nEnter: confirm and send\n"
TAKEOVER = "This session is live elsewhere.\nEnter: take over\n"
REFUSED = "Too many Freebuff sessions on this network. Try again after " \
          "another session finishes.\n"


def make_transcript(ai_blocks, complete=True):
    ai = {"variant": "ai", "blocks": ai_blocks, "isComplete": complete,
          "completionTime": "5s", "credits": 0, "id": "m-ai"}
    return [{"variant": "ai",
             "blocks": [{"type": "mode-divider", "mode": "LITE"}]},
            {"variant": "user", "content": "the prompt"}, ai,
            {"variant": "ai", "blocks": []}]


ANSWER_BLOCKS = [
    {"type": "text", "textType": "reasoning", "content": "thinking"},
    {"type": "text", "textType": "text", "content": "Hello"},
    {"type": "text", "textType": "text", "content": "world"},
]


def _env(**over):
    return TmpEnv(PA_FREEBUFF_ENABLED="true", **over)


def _fake_cli(path):
    """Executable stand-in for the freebuff binary — unit tests only
    check that `which` resolves it; it is never run."""
    with open(path, "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP |
             stat.S_IXOTH)
    return path


class TestExtract(unittest.TestCase):
    def test_last_complete_ai_text_blocks(self):
        out = extract_answer(make_transcript(ANSWER_BLOCKS))
        self.assertEqual(out["text"], "Hello\nworld")
        self.assertEqual(out["reasoning"], "thinking")
        self.assertEqual(out["completion_time"], "5s")
        self.assertEqual(out["credits"], 0)

    def test_no_complete_ai_returns_none(self):
        self.assertIsNone(extract_answer(
            make_transcript(ANSWER_BLOCKS, complete=False)))
        self.assertIsNone(extract_answer([]))
        self.assertIsNone(extract_answer({"not": "a list"}))

    def test_skips_non_text_blocks(self):
        blocks = [{"type": "tool", "name": "run_terminal_cmd"},
                  {"type": "text", "textType": "text", "content": "done"}]
        self.assertEqual(extract_answer(make_transcript(blocks))["text"],
                         "done")

    def test_user_error_surfaced(self):
        # a turn the CLI completes by failing it (backend error) carries
        # userError and must not be treated as still-running
        t = make_transcript([], complete=True)
        t[2]["userError"] = "The stealth model could not complete " \
                            "the request."
        out = extract_answer(t)
        self.assertEqual(out["user_error"],
                         "The stealth model could not complete "
                         "the request.")
        self.assertEqual(out["text"], "")


class TestMarkerTranslation(unittest.TestCase):
    def test_angle_markers_become_brackets(self):
        # live finding: <<<...>>> markers triggered a backend 400; the
        # boundary survives translated to plain brackets
        self.assertEqual(
            fbs._translate_untrusted_markers(
                "<<<UNTRUSTED EMAIL hi >>>\nbody\n<<<END UNTRUSTED EMAIL>>>"),
            "[UNTRUSTED EMAIL hi]\nbody\n[END UNTRUSTED EMAIL]")
        self.assertEqual(
            fbs._translate_untrusted_markers("inside <<<UNTRUSTED ...>>>"),
            "inside [UNTRUSTED ...]")

    def test_pasted_prompt_is_translated(self):
        env = _env()
        s = env.settings
        s.freebuff_cli = _fake_cli(os.path.join(env.dir, "freebuff"))
        s.freebuff_home = os.path.join(env.dir, "manicode")
        s.freebuff_workdir = os.path.join(env.dir, "fbwork")
        os.makedirs(s.freebuff_home, exist_ok=True)
        p = FreebuffSessionProvider(s)
        p.tmux = FakeTmux(s.freebuff_home, [READY],
                          make_transcript(ANSWER_BLOCKS))
        p.tmux.binary = s.freebuff_cli
        p.run("<<<UNTRUSTED x >>>data<<<END UNTRUSTED x>>>")
        self.assertIn(("paste", "[UNTRUSTED x]data[END UNTRUSTED x]"),
                      p.tmux.sent)


class TestClassify(unittest.TestCase):
    def test_states(self):
        self.assertEqual(classify_screen(READY)[0], "ready")
        self.assertEqual(classify_screen(SURVEY)[0], "survey")
        self.assertEqual(classify_screen(CONFIRM)[0], "confirm_spend")
        self.assertEqual(classify_screen(TAKEOVER)[0], "takeover")
        self.assertEqual(classify_screen("Enter: retry")[0], "retry")
        self.assertEqual(classify_screen("Enter: change model")[0],
                         "choose_model")
        self.assertEqual(classify_screen(
            "Your first message starts the session.")[0], "ready")
        self.assertEqual(classify_screen("Press ENTER to login")[0],
                         "login")
        self.assertEqual(classify_screen(
            "Enter: choose another model")[0], "choose_model")
        self.assertEqual(classify_screen(REFUSED)[0], "refused")
        self.assertEqual(classify_screen(
            "log in at freebuff.com/login to start")[0], "login")
        self.assertEqual(classify_screen("thinking...")[0], "other")


class TestProjectDir(unittest.TestCase):
    def test_basename_mapping(self):
        self.assertEqual(
            project_dir_for("/tmp/fb-abc", "/h"),
            "/h/projects/fb-abc")
        self.assertEqual(
            project_dir_for("/a/b/wd", "/h"), "/h/projects/wd")


class TestConfig(unittest.TestCase):
    def test_disabled_by_default(self):
        s = load_settings({"PA_ENV": "test"})
        self.assertFalse(s.freebuff_enabled)
        with self.assertRaises(ProviderNotConfigured):
            provider_for(s, FREEBUFF_HOST, "default")

    def test_enabled_joins_allowlist(self):
        s = _env().settings
        self.assertIn(("freebuff-cli", "freebuff://cli", ["default"],
                       None), s.provider_allowlist())
        p = provider_for(s, FREEBUFF_HOST, "default")
        self.assertEqual(p.kind, "agent_session")
        self.assertFalse(p.tools_enabled)

    def test_wrong_model_rejected(self):
        s = _env().settings
        with self.assertRaises(ProviderNotConfigured):
            provider_for(s, FREEBUFF_HOST, "glm-5.3-flash")


class FakeTmux:
    """In-memory Tmux replacement: scripted screens + a transcript
    written (once) when the prompt paste arrives."""

    def __init__(self, home, screens=(), transcript=None, write_after=1):
        self.home = home
        self.binary = "tmux"          # set to the fake CLI path in _provider
        self.screens = list(screens)
        self.screen = self.screens[-1] if self.screens else READY
        self.transcript = transcript
        # transcript lands after N capture() calls — gates are visible
        # while the run is still in flight, like the real TUI.
        self.write_after = write_after
        self.sent = []
        self.alive = True
        self._captures = 0
        self.workdir = ""

    def has_session(self, name):
        return self.alive

    def new_session(self, name, shell_cmd, cols=140, lines=40):
        self.sent.append(("new", shell_cmd))
        # workdir the launch was given: "<cli> --cwd <dir>"
        args = shell_cmd.split()
        self.workdir = args[args.index("--cwd") + 1] if "--cwd" in args \
            else ""

    def send_key(self, name, key):
        self.sent.append(("key", key))

    def send_paste(self, name, text):
        self.sent.append(("paste", text))
        if text == "/exit":
            self.alive = False        # clean quit ends the pane

    def _write_transcript(self):
        proj = project_dir_for(self.workdir, self.home)
        chat = os.path.join(proj, "chats", "chat-1")
        os.makedirs(chat, exist_ok=True)
        with open(os.path.join(chat, "chat-messages.json"), "w") as f:
            json.dump(self.transcript, f)

    def capture(self, name):
        if self.screens:
            self.screen = self.screens.pop(0)
        self._captures += 1
        if self.transcript is not None and \
                self._captures >= self.write_after:
            chat = os.path.join(
                project_dir_for(self.workdir, self.home),
                "chats", "chat-1")
            if not os.path.exists(
                    os.path.join(chat, "chat-messages.json")):
                self._write_transcript()
        return self.screen

    def kill(self, name):
        self.alive = False


class TestRunOrchestration(unittest.TestCase):
    def setUp(self):
        self.env = _env()
        s = self.env.settings
        s.freebuff_cli = _fake_cli(os.path.join(self.env.dir, "freebuff"))
        s.freebuff_home = os.path.join(self.env.dir, "manicode")
        s.freebuff_workdir = os.path.join(self.env.dir, "fbwork")
        os.makedirs(s.freebuff_home, exist_ok=True)

    def _provider(self, screens, transcript=None, write_after=2, **over):
        p = FreebuffSessionProvider(self.env.settings)
        for k, v in over.items():
            setattr(p, k, v)
        p.tmux = FakeTmux(self.env.settings.freebuff_home, screens,
                          transcript, write_after)
        # the `which` preflight must resolve — reuse the fake CLI path
        p.tmux.binary = self.env.settings.freebuff_cli
        return p, p.tmux

    def test_happy_path(self):
        p, tm = self._provider([READY],
                               transcript=make_transcript(ANSWER_BLOCKS))
        out = p.run("summarise this")
        self.assertEqual(out["text"], "Hello\nworld")
        self.assertEqual(out["reasoning"], "thinking")
        self.assertEqual(out["meta"]["completion_time"], "5s")
        self.assertEqual(out["meta"]["chat_id"], "chat-1")
        pastes = [t for k, t in tm.sent if k == "paste"]
        self.assertEqual(pastes[0], "summarise this")
        self.assertIn("/exit", pastes)
        self.assertIn(("key", "Enter"), tm.sent)

    def test_prompt_pasted_through_normal_input(self):
        p, tm = self._provider([READY],
                               transcript=make_transcript(ANSWER_BLOCKS))
        p.run("the task")
        # the only model-visible input path is the TUI's own paste entry
        self.assertIn(("paste", "the task"), tm.sent)
        self.assertIn(("key", "Enter"), tm.sent)

    def test_survey_dismissed(self):
        p, tm = self._provider([READY, SURVEY, SURVEY],
                               transcript=make_transcript(ANSWER_BLOCKS),
                               write_after=4)
        out = p.run("x")
        self.assertEqual(out["text"], "Hello\nworld")
        self.assertIn("survey_dismissed", out["meta"]["gates"])
        self.assertIn(("key", "Escape"), tm.sent)

    def test_confirm_spend_refused_by_default(self):
        p, _ = self._provider([READY, CONFIRM, CONFIRM, CONFIRM])
        with self.assertRaises(FreebuffRateLimited):
            p.run("x")

    def test_confirm_spend_allowed(self):
        p, tm = self._provider([READY, CONFIRM, CONFIRM, CONFIRM],
                               transcript=make_transcript(ANSWER_BLOCKS),
                               write_after=3)
        p.allow_paid = True
        out = p.run("x")
        self.assertEqual(out["text"], "Hello\nworld")
        self.assertIn("spend_confirmed", out["meta"]["gates"])
        self.assertIn(("key", "Enter"), tm.sent)

    def test_failed_turn_fails_fast(self):
        # live-smoke regression: CLI completed the turn with userError;
        # the driver must raise immediately, not poll to the cap
        t = make_transcript([], complete=True)
        t[2]["userError"] = "The stealth model could not complete " \
                            "the request."
        p, _ = self._provider([READY], transcript=t)
        with self.assertRaises(FreebuffSessionError):
            p.run("x")

    def test_assume_ready_escape_hatch(self):
        # marker drift after a CLI update: unrecognized but live screen;
        # PA_FREEBUFF_ASSUME_READY lets the owner proceed anyway
        p, tm = self._provider(["??? unrecognized screen ???"],
                               transcript=make_transcript(ANSWER_BLOCKS),
                               ready_timeout=0.5)
        p.assume_ready = True
        out = p.run("x")
        self.assertEqual(out["text"], "Hello\nworld")
        self.assertIn(("paste", "x"), tm.sent)

    def test_unknown_screen_without_escape_fails(self):
        p, _ = self._provider(["??? unrecognized screen ???"],
                              transcript=make_transcript(ANSWER_BLOCKS),
                              ready_timeout=0.5)
        with self.assertRaises(FreebuffUnavailable):
            p.run("x")

    def test_queue_wait_bounded_by_deadline(self):
        # a second caller waits on the session lock only as long as its
        # own deadline allows, then fails clearly
        p, _ = self._provider([READY])
        self.assertTrue(fbs._SESSION_LOCK.acquire(blocking=False))
        try:
            with self.assertRaises(FreebuffUnavailable):
                p.run("x", deadline_seconds=0.3)
        finally:
            fbs._SESSION_LOCK.release()

    def test_takeover_entered(self):
        p, tm = self._provider([READY, TAKEOVER, TAKEOVER],
                               transcript=make_transcript(ANSWER_BLOCKS),
                               write_after=3)
        out = p.run("x")
        self.assertIn("takeover", out["meta"]["gates"])

    def test_retry_choose_refusal_fail(self):
        for screen in ("Enter: retry", REFUSED,
                       "Enter: choose another model"):
            p, _ = self._provider([READY, screen, screen, screen])
            with self.assertRaises(FreebuffRateLimited):
                p.run("x")

    def test_cancel_propagates_and_quits(self):
        calls = []

        def cancel():
            calls.append(1)
            if len(calls) > 2:
                raise Cancelled()

        p, tm = self._provider([READY],
                               transcript=make_transcript(ANSWER_BLOCKS,
                                                          complete=False))
        with self.assertRaises(Cancelled):
            p.run("x", check_cancel=cancel)
        self.assertIn(("paste", "/exit"), tm.sent)

    def test_session_cap(self):
        p, _ = self._provider([READY],
                              transcript=make_transcript(ANSWER_BLOCKS,
                                                         complete=False))
        p.session_timeout = 1
        with self.assertRaises(FreebuffUnavailable):
            p.run("x")

    def test_login_wall(self):
        p, _ = self._provider(
            ["Welcome. log in at freebuff.com/login to start"])
        with self.assertRaises(ProviderNotConfigured):
            p.run("x")


class StubSessionProvider:
    kind = "agent_session"
    tools_enabled = False
    streaming = False
    last_headers = {}

    def __init__(self, out=None, err=None):
        self.out = out or {"text": "session answer", "reasoning": None,
                           "meta": {"run_id": "r1"}}
        self.err = err
        self.calls = []

    def run(self, prompt, deadline_seconds=None, check_cancel=None):
        self.calls.append(prompt)
        if check_cancel:
            check_cancel()
        if self.err:
            raise self.err
        return self.out


class TestAgentSessionPath(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_FREEBUFF_ENABLED="true")
        self.conn = self.env.conn()
        self.aid = self.env.add_assistant(host=FREEBUFF_HOST,
                                        model="default")

    def _worker(self, provider):
        return workmod.Worker(self.env.db_path, self.env.settings,
                              provider_factory=lambda a: provider)

    def test_task_succeeds_with_text_result(self):
        tid = workmod.create_task(self.conn, self.aid, "t", "do a thing")
        stub = StubSessionProvider()
        self._worker(stub).run_once()
        t = self.conn.execute("SELECT * FROM tasks WHERE id=?",
                              (tid,)).fetchone()
        self.assertEqual(t["state"], "succeeded")
        res = json.loads(t["result_json"])
        self.assertEqual(res["text"], "session answer")
        self.assertEqual(res["provider"], "agent_session")
        self.assertEqual(res["session"]["run_id"], "r1")
        steps = self.conn.execute(
            "SELECT kind, status FROM steps WHERE task_id=?", (tid,)
        ).fetchall()
        self.assertEqual([(s["kind"], s["status"]) for s in steps],
                         [("model_call", "done")])
        self.assertIn("do a thing", stub.calls[0])
        self.assertIn("UNTRUSTED", stub.calls[0])

    def test_rate_limited_maps_to_quota(self):
        tid = workmod.create_task(self.conn, self.aid, "t", "x")
        stub = StubSessionProvider(err=FreebuffRateLimited(
            "session limit reached", reset_at=None, retry_after=None))
        self._worker(stub).run_once()
        t = self.conn.execute("SELECT * FROM tasks WHERE id=?",
                              (tid,)).fetchone()
        self.assertEqual(t["state"], "failed")
        self.assertEqual(t["error_kind"], "quota_exceeded")

    def test_session_error_fails_task(self):
        tid = workmod.create_task(self.conn, self.aid, "t", "x")
        stub = StubSessionProvider(err=FreebuffSessionError("boom"))
        self._worker(stub).run_once()
        t = self.conn.execute("SELECT * FROM tasks WHERE id=?",
                              (tid,)).fetchone()
        self.assertEqual(t["state"], "failed")
        self.assertEqual(t["error_kind"], "provider_error")

    def test_done_step_replays_without_rerun(self):
        tid = workmod.create_task(self.conn, self.aid, "t", "do a thing")
        stub = StubSessionProvider()
        self._worker(stub).run_once()
        self.assertEqual(len(stub.calls), 1)
        self.assertTrue(workmod.request_retry(self.conn, tid))
        self._worker(stub).run_once()
        self.assertEqual(len(stub.calls), 1)  # replayed, not re-run


class TestSessionPrompt(unittest.TestCase):
    def test_prompt_contents(self):
        a = {"name": "A1", "instructions": "be terse"}
        p = build_session_prompt(a, [{"key": "tz", "value": "UTC"}],
                                 "task body")
        self.assertIn("be terse", p)
        self.assertIn("tz: UTC", p)
        self.assertIn("task body", p)
        self.assertIn("no task tools", p)


# --- real tmux + fake CLI -------------------------------------------------

FAKE_CLI = r"""#!/usr/bin/env bash
# Minimal stand-in for the freebuff TUI: prints the ready marker, reads
# pasted lines, writes a transcript on the first prompt, exits on /exit.
CWD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --cwd) CWD="$2"; shift 2 ;;
    *) shift ;;
  esac
done
CWD="${CWD:-$PWD}"
HOME_DIR="${FREEBUFF_CONFIG_DIR:-$HOME/.config/manicode}"
printf 'Enter a coding task or / for commands\n'
while IFS= read -r line; do
  case "$line" in
    */exit*) exit 0 ;;
    *$'\033'[200~*)
      PROJ="$HOME_DIR/projects/$(basename "$CWD")"
      mkdir -p "$PROJ/chats/fake-chat-1"
      cat > "$PROJ/chats/fake-chat-1/chat-messages.json" <<'EOF'
[{"variant":"user","content":"x"},
 {"variant":"ai","isComplete":true,"completionTime":"1s","credits":0,
  "id":"m1","blocks":[
   {"type":"text","textType":"reasoning","content":"fake think"},
   {"type":"text","textType":"text","content":"FAKE SESSION ANSWER"}]}]
EOF
      ;;
  esac
done
"""


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TestRealTmux(unittest.TestCase):
    """Drive the real tmux/PTY path against the fake CLI script."""

    def test_run_against_fake_cli(self):
        env = _env()
        s = env.settings
        home = os.path.join(env.dir, "manicode")
        work = os.path.join(env.dir, "fbwork")
        os.makedirs(home, exist_ok=True)
        cli = os.path.join(env.dir, "fake-freebuff")
        with open(cli, "w") as f:
            f.write(FAKE_CLI)
        os.chmod(cli, 0o755)
        s.freebuff_cli = cli
        s.freebuff_home = home
        s.freebuff_workdir = work
        s.freebuff_ready_timeout = 15
        s.freebuff_session_timeout = 40
        p = FreebuffSessionProvider(s)
        # private socket per run — never touches the owner's tmux server
        p.tmux = Tmux(s.freebuff_tmux, socket="pa-fb-test")
        out = p.run("say the fake answer")
        self.assertEqual(out["text"], "FAKE SESSION ANSWER")
        self.assertEqual(out["reasoning"], "fake think")
        self.assertEqual(out["meta"]["chat_id"], "fake-chat-1")


if __name__ == "__main__":
    unittest.main()

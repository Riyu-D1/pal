"""PA_CHAT_PROGRESS: one editable status line + reactions per turn.

- 👀 on the owner's message, ONE 'on it — …' message posted and edited
  (PATCH) as tools run, deleted when the answer lands; reaction flips
  to ✅ (⚠️ on failure).
- The old typing + 'working on it…' path still works when the flag is
  off.
- Tool names may appear in the status line; args never do.
"""

import threading
import time
import unittest
import urllib.parse

from pa import discordbot
from tests.helpers import FakeGateway, Fixture, TmpEnv, wait_for

OWNER = "owner-1"
BOT = "999"


def _event(t, data, seq):
    return {"op": 0, "s": seq, "t": t, "d": data}


def _dm(user, cid, mid, content):
    return {"id": mid, "channel_id": cid, "channel_type": 1,
            "author": {"id": user}, "content": content}


class ToolEngine:
    """Fake engine that reports one tool through the progress cb."""
    def __init__(self, answer="the answer"):
        self.answer = answer
        self.calls = []

    def run_turn(self, channel_id, text, deadline_seconds=None,
                 progress=None, **kw):
        self.calls.append((channel_id, text))
        if progress:
            progress("web_search")
            progress("read_url")
        return {"text": self.answer, "steps": 1, "trace": [],
                "elapsed_s": 0.01}


class Base(unittest.TestCase):
    env_kw = {}

    def setUp(self):
        self.gw = FakeGateway()
        self.fx = Fixture()
        kw = {"DISCORD_BOT_TOKEN": "fake-bot-token",
              "DISCORD_OWNER_ID": OWNER,
              "DISCORD_API_BASE": self.fx.web_base,
              "DISCORD_GATEWAY_URL": self.gw.url,
              "PA_CHAT_WORKING_SECONDS": "0.2"}
        kw.update(self.env_kw)
        self.env = TmpEnv(**kw)
        self.env.conn().close()
        self.engine = ToolEngine()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False,
            engine=self.engine)
        self.t = None

    def tearDown(self):
        self.bot.stop()
        self.gw.close()
        self.fx.close()
        if self.t:
            self.t.join(timeout=5)

    def _drive(self, script):
        self.gw.scripts.append(script)
        self.t = threading.Thread(target=self.bot.run_forever,
                                  daemon=True)
        self.t.start()

    def _hello_ready(self, g):
        g.send({"op": 10, "d": {"heartbeat_interval": 30000}})
        g.recv()
        g.send(_event("READY", {"session_id": "s1",
                                "resume_gateway_url": self.gw.url,
                                "application": {"id": "app1"},
                                "user": {"id": BOT}}, 1))

    def _calls(self, method=None, prefix=None):
        out = []
        for c in self.fx.calls:
            if method and c["method"] != method:
                continue
            if prefix and not c["path"].startswith(prefix):
                continue
            out.append(c)
        return out

    def _posts(self):
        return [c for c in self._calls("POST", "/channels/")
                if c["path"].endswith("/messages")]

    def _patches(self):
        return self._calls("PATCH")

    def _deletes(self):
        return self._calls("DELETE")

    def _reactions(self):
        return [c for c in self._calls("PUT")
                if "/reactions/" in c["path"]]

    def _push_status_ids(self, mid="status-1", n=12):
        # every REST call (command sync, reactions, typing, PATCH,
        # DELETE) consumes a pushed "post" response; only the status
        # POST reads .id, so giving every item the same id is enough
        for _ in range(n):
            self.fx.push_post({"id": mid})


class TestLiveProgress(Base):
    def test_status_line_edit_and_reactions(self):
        self._push_status_ids()

        def script(g):
            self._hello_ready(g)
            g.send(_event("MESSAGE_CREATE",
                          _dm(OWNER, "dm1", "m9", "hi there"), 2))
            wait_for(lambda: self.engine.calls, timeout=10)
            wait_for(lambda: self._deletes(), timeout=10)
            time.sleep(0.3)
            g.drop()

        self._drive(script)
        wait_for(lambda: self._deletes(), timeout=10)
        time.sleep(0.4)

        # 👀 added, then ✅ added, 👀 removed
        reactions = [c["path"] for c in self._reactions()]
        enc_eyes = urllib.parse.quote("👀", safe="")
        enc_done = urllib.parse.quote("✅", safe="")
        self.assertTrue(any(enc_eyes in p for p in reactions))
        self.assertTrue(any(enc_done in p for p in reactions))
        dels = [c["path"] for c in self._deletes()]
        self.assertTrue(any(enc_eyes in p for p in dels))
        # exactly one status message posted (plus the reply)
        posts = self._posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["body"]["content"],
                         "on it — working…")
        # the reply, not a second status line
        self.assertEqual(posts[-1]["body"]["content"], "the answer")
        # the status line was PATCHed with a tool verb, then DELETEd
        patches = self._patches()
        self.assertEqual(len(patches), 1)
        self.assertIn("/channels/dm1/messages/status-1",
                      patches[0]["path"])
        self.assertIn("searching the web…",
                      patches[0]["body"]["content"])
        self.assertIn("/channels/dm1/messages/status-1", dels)

    def test_no_tool_args_in_status(self):
        self._push_status_ids("s-2")

        class ArgEngine(ToolEngine):
            def run_turn(self, channel_id, text, deadline_seconds=None,
                         progress=None, **kw):
                if progress:
                    progress("gmail@pal.GMAIL_SEND_EMAIL")
                return {"text": "ok", "steps": 0, "trace": []}

        self.engine = ArgEngine()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False,
            engine=self.engine)

        def script(g):
            self._hello_ready(g)
            g.send(_event("MESSAGE_CREATE",
                          _dm(OWNER, "dm1", "m10", "mail mum"), 2))
            wait_for(lambda: self._deletes(), timeout=10)
            time.sleep(0.3)
            g.drop()

        self._drive(script)
        wait_for(lambda: self._deletes(), timeout=10)
        for c in self._patches():
            self.assertNotIn("@", c["body"]["content"] or "")
            self.assertNotIn("password", c["body"]["content"] or "")


class TestTypingFallback(Base):
    env_kw = {"PA_CHAT_PROGRESS": "false"}

    def test_working_on_it_and_no_edits(self):
        class SlowEngine(ToolEngine):
            def run_turn(self, channel_id, text, deadline_seconds=None,
                         progress=None, **kw):
                self.calls.append((channel_id, text))
                time.sleep(0.5)     # long enough for the 0.2s note
                return {"text": self.answer, "steps": 0, "trace": []}

        self.engine = SlowEngine()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False,
            engine=self.engine)

        def script(g):
            self._hello_ready(g)
            g.send(_event("MESSAGE_CREATE",
                          _dm(OWNER, "dm1", "m11", "hey"), 2))
            wait_for(lambda: self.engine.calls, timeout=10)
            time.sleep(0.6)
            g.drop()

        self._drive(script)
        wait_for(lambda: self.engine.calls, timeout=10)
        time.sleep(0.6)
        posts = self._posts()
        self.assertTrue(any(
            p["body"]["content"] == "working on it…" for p in posts))
        self.assertEqual(self._patches(), [])
        self.assertEqual(self._reactions(), [])


class TestVoiceProgress(Base):
    def test_voice_note_gets_status_too(self):
        # transcribe path: download_attachment + transcribe are faked
        import pa.voice as voicemod
        orig_dl, orig_tr = voicemod.download_attachment, \
            voicemod.transcribe
        voicemod.download_attachment = lambda s, url, **kw: b"ogg"
        voicemod.transcribe = lambda s, env, audio, fn, ct: ("hi", "x")
        self._push_status_ids("vs-1")
        att = {"url": "https://cdn.discordapp.com/a/v.ogg",
               "filename": "v.ogg", "content_type": "audio/ogg"}
        msg = {"id": "mv1", "channel_id": "dm1", "channel_type": 1,
               "author": {"id": OWNER}, "content": "",
               "attachments": [att]}

        def script(g):
            self._hello_ready(g)
            g.send(_event("MESSAGE_CREATE", msg, 2))
            wait_for(lambda: self.engine.calls, timeout=10)
            wait_for(lambda: self._deletes(), timeout=10)
            time.sleep(0.3)
            g.drop()

        try:
            self._drive(script)
            wait_for(lambda: self.engine.calls, timeout=10)
            wait_for(lambda: self._deletes(), timeout=10)
            time.sleep(0.3)
        finally:
            voicemod.download_attachment = orig_dl
            voicemod.transcribe = orig_tr
        posts = self._posts()
        self.assertTrue(any(
            "transcribing" in (p["body"]["content"] or "")
            for p in posts))


if __name__ == "__main__":
    unittest.main()

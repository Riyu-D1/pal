"""Conversational Discord routing: DM/mention/chat-channel -> chat
engine, commands still work, non-owners ignored, reply splitting,
approval buttons, and the scheduled chat_inbox pump.

Real bot against the scripted fake gateway + REST fixture; the chat
engine is a fake. Scripts are appended BEFORE the bot connects so the
first accepted connection always has a script (helpers.FakeGateway
closes scriptless connections).
"""

import json
import threading
import time
import unittest

from pa import approvals, discordbot
from tests.helpers import FakeGateway, Fixture, TmpEnv, wait_for

OWNER = "owner-1"
BOT = "999"


def _event(t, data, seq):
    return {"op": 0, "s": seq, "t": t, "d": data}


def _dm(user, cid, mid, content):
    return {"id": mid, "channel_id": cid, "channel_type": 1,
            "author": {"id": user}, "content": content}


def _guild_msg(user, cid, mid, content, gid="g-1"):
    return {"id": mid, "channel_id": cid, "guild_id": gid,
            "author": {"id": user}, "content": content}


def _component(user, iid, custom_id, token="tok"):
    return {"id": iid, "token": token, "type": 3,
            "member": {"user": {"id": user}},
            "message": {"id": "mm-1"},
            "data": {"component_type": 2, "custom_id": custom_id}}


class FakeEngine:
    def __init__(self, answer="the answer"):
        self.answer = answer
        self.calls = []

    def run_turn(self, channel_id, text, deadline_seconds=None,
                 progress=None, **kw):
        self.calls.append((channel_id, text))
        return {"text": self.answer, "steps": 0, "trace": [],
                "elapsed_s": 0.01}


class ChatBase(unittest.TestCase):
    chat_channel = None

    def setUp(self):
        self.gw = FakeGateway()
        self.fx = Fixture()
        kw = {}
        if self.chat_channel:
            kw["DISCORD_CHAT_CHANNEL_ID"] = self.chat_channel
        self.env = TmpEnv(
            DISCORD_BOT_TOKEN="fake-bot-token",
            DISCORD_OWNER_ID=OWNER,
            DISCORD_API_BASE=self.fx.web_base,
            DISCORD_GATEWAY_URL=self.gw.url,
            **kw)
        self.env.conn().close()          # migrate before events arrive
        self.engine = FakeEngine()
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
        """Register the gateway script, then connect the bot."""
        self.gw.scripts.append(script)
        self.t = threading.Thread(target=self.bot.run_forever,
                                  daemon=True)
        self.t.start()

    def _rest(self, prefix):
        return [c for c in self.fx.calls
                if c["method"] == "POST" and c["path"].startswith(prefix)]

    def _channel_posts(self):
        return [c for c in self._rest("/channels/")
                if c["path"].endswith("/messages")]

    def _hello_ready(self, g):
        g.send({"op": 10, "d": {"heartbeat_interval": 30000}})
        g.recv()
        g.send(_event("READY", {"session_id": "s1",
                                "resume_gateway_url": self.gw.url,
                                "application": {"id": "app1"},
                                "user": {"id": BOT}}, 1))


class TestRouting(ChatBase):
    def test_dm_is_chat_and_dm_commands_still_work(self):
        def script(g):
            self._hello_ready(g)
            g.send(_event("MESSAGE_CREATE",
                          _dm(OWNER, "dm1", "m1", "assistants"), 2))
            wait_for(lambda: self._channel_posts(), timeout=10)
            g.send(_event("MESSAGE_CREATE",
                          _dm(OWNER, "dm1", "m2", "how are you?"), 3))
            wait_for(lambda: self.engine.calls, timeout=10)
            time.sleep(0.4)
            g.drop()

        self._drive(script)
        wait_for(lambda: self.engine.calls, timeout=10)
        wait_for(lambda: len(self._channel_posts()) >= 2, timeout=10)
        # 'assistants' went to the command path, not the engine
        self.assertEqual(self.engine.calls, [("dm1", "how are you?")])
        posts = self._channel_posts()
        self.assertIn("assistants", posts[0]["body"]["content"])
        self.assertEqual(posts[-1]["body"]["content"], "the answer")
        # reply references the triggering message
        ref = posts[-1]["body"].get("message_reference") or {}
        self.assertEqual(ref.get("message_id"), "m2")

    def test_mention_in_guild_routes_and_strips_mention(self):
        def script(g):
            self._hello_ready(g)
            g.send(_event("MESSAGE_CREATE",
                          _guild_msg(OWNER, "chan-7", "m1",
                                     f"<@{BOT}> tell me a joke"), 2))
            # unmentioned guild traffic: ignored
            g.send(_event("MESSAGE_CREATE",
                          _guild_msg(OWNER, "chan-7", "m2",
                                     "hello channel"), 3))
            wait_for(lambda: self.engine.calls, timeout=10)
            time.sleep(0.4)
            g.drop()

        self._drive(script)
        wait_for(lambda: self.engine.calls, timeout=10)
        self.assertEqual(self.engine.calls,
                         [("chan-7", "tell me a joke")])

    def test_non_owner_ignored_everywhere(self):
        def script(g):
            self._hello_ready(g)
            g.send(_event("MESSAGE_CREATE",
                          _dm("intruder", "dmX", "m1", "hi bot"), 2))
            g.send(_event("MESSAGE_CREATE",
                          _guild_msg("intruder", "chan-7", "m2",
                                     f"<@{BOT}> do a thing"), 3))
            g.send(_event("INTERACTION_CREATE",
                          _component("intruder", "i9",
                                     "chatappr:abc:y"), 4))
            time.sleep(0.8)
            g.drop()

        self._drive(script)
        wait_for(lambda: self.gw.connections >= 1, timeout=10)
        time.sleep(0.8)
        self.assertEqual(self.engine.calls, [])
        # only the unauthorized interaction reply (ephemeral)
        cbs = self._rest("/interactions/")
        self.assertEqual(len(cbs), 1)
        self.assertTrue(cbs[0]["body"]["data"]["flags"] & 64)
        self.assertEqual(self._channel_posts(), [])

    def test_bot_and_self_messages_ignored(self):
        def script(g):
            self._hello_ready(g)
            g.send(_event("MESSAGE_CREATE",
                          {"id": "mb", "channel_id": "dm1",
                           "channel_type": 1,
                           "author": {"id": BOT}, "content": "echo"}, 2))
            g.send(_event("MESSAGE_CREATE",
                          _dm(OWNER, "dm1", "m3",
                              "what is up"), 3))
            wait_for(lambda: self.engine.calls, timeout=10)
            time.sleep(0.3)
            g.drop()

        self._drive(script)
        wait_for(lambda: self.engine.calls, timeout=10)
        self.assertEqual(self.engine.calls, [("dm1", "what is up")])


class TestChatChannel(ChatBase):
    chat_channel = "chan-9"

    def test_configured_channel_is_chat(self):
        def script(g):
            self._hello_ready(g)
            g.send(_event("MESSAGE_CREATE",
                          _guild_msg(OWNER, "chan-9", "m1",
                                     "morning briefing please"), 2))
            g.send(_event("MESSAGE_CREATE",
                          _guild_msg(OWNER, "chan-8", "m2",
                                     "not this channel"), 3))
            wait_for(lambda: self.engine.calls, timeout=10)
            time.sleep(0.4)
            g.drop()

        self._drive(script)
        wait_for(lambda: self.engine.calls, timeout=10)
        self.assertEqual(self.engine.calls,
                         [("chan-9", "morning briefing please")])


class TestComponentsAndInbox(ChatBase):
    def test_approve_button_decides_and_updates(self):
        conn = self.env.conn()
        aid = approvals.create_approval(
            conn, "chat_action",
            {"tool": "send_email", "args": {"to": "x@y.z"},
             "channel_id": "dm1"})
        conn.close()

        def script(g):
            self._hello_ready(g)
            g.send(_event("INTERACTION_CREATE",
                          _component(OWNER, "i1",
                                     f"chatappr:{aid}:y"), 2))
            wait_for(lambda: self._rest("/interactions/"), timeout=10)
            g.drop()

        self._drive(script)
        wait_for(lambda: self._rest("/interactions/"), timeout=10)
        time.sleep(0.4)
        cb = self._rest("/interactions/")[0]["body"]
        self.assertEqual(cb["type"], 7)
        self.assertIn("Approved", cb["data"]["content"])
        self.assertEqual(cb["data"]["components"], [])
        conn = self.env.conn()
        self.assertEqual(approvals.get(conn, aid)["status"], "approved")
        conn.close()

    def test_request_approval_dms_buttons_and_waits(self):
        self.fx.push_post({"id": "dm-77", "ok": True})   # open_dm reply
        result = {}

        # create the approval the callback waits on
        conn = self.env.conn()
        aid = approvals.create_approval(conn, "chat_action",
                                        {"tool": "send_email"})
        conn.close()
        th = threading.Thread(
            target=lambda: result.setdefault(
                "d", self.bot._request_chat_approval(
                    "send_email", {"to": "x@y.z"}, aid)),
            daemon=True)
        th.start()
        wait_for(lambda: self._channel_posts(), timeout=10)
        posts = self._channel_posts()
        body = posts[0]["body"]
        self.assertEqual(posts[0]["path"], "/channels/dm-77/messages")
        btns = body["components"][0]["components"]
        self.assertEqual({b["custom_id"] for b in btns},
                         {f"chatappr:{aid}:y", f"chatappr:{aid}:n"})
        self.assertFalse(result.get("d"))
        conn = self.env.conn()
        approvals.decide(conn, aid, False)
        conn.close()
        th.join(timeout=10)
        self.assertEqual(result["d"], "denied")

    def test_inbox_pump_runs_scheduled_prompt(self):
        self.engine.answer = "scheduled answer"
        conn = self.env.conn()
        conn.execute(
            "INSERT INTO chat_inbox(channel_id, prompt, source, status,"
            " created_at) VALUES('chan-5','stretch your legs',"
            " 'schedule:remind-x','queued','2026-01-01T00:00:00Z')")
        conn.close()
        self.bot.tick_chat_inbox()
        wait_for(lambda: self.engine.calls, timeout=10)
        wait_for(lambda: self._channel_posts(), timeout=10)
        self.assertEqual(self.engine.calls,
                         [("chan-5", "stretch your legs")])
        msg = [c for c in self._channel_posts()
               if c["body"].get("content")]
        # PA_CHAT_PROGRESS posts (then deletes) an "on it" status line
        # first; the answer is the last message POST
        self.assertEqual(msg[-1]["body"]["content"], "scheduled answer")
        conn = self.env.conn()
        row = conn.execute("SELECT * FROM chat_inbox").fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["reply"], "scheduled answer")
        conn.close()


class TestSplitting(unittest.TestCase):
    def test_short(self):
        self.assertEqual(discordbot.split_message("hi"), ["hi"])

    def test_splits_on_lines(self):
        lines = [f"line {i} " + "x" * 100 for i in range(40)]
        text = "\n".join(lines)
        chunks = discordbot.split_message(text, limit=1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("\n".join(chunks).count("line"), 40)

    def test_long_single_line_hard_splits(self):
        chunks = discordbot.split_message("a" * 4500, limit=1000)
        self.assertEqual([len(c) for c in chunks],
                         [1000, 1000, 1000, 1000, 500])


if __name__ == "__main__":
    unittest.main()

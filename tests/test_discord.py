"""Discord gateway bot: identify, owner allowlist, dedup, reconnect+resume.

Runs the real WSClient/bot against a scripted fake gateway plus the
Fixture REST stub. No live Discord traffic.
"""

import json
import os
import threading
import time
import unittest

from pa import discordbot, worker as workmod
from tests.helpers import FakeGateway, Fixture, TmpEnv, wait_for


def _event(t, data, seq):
    return {"op": 0, "s": seq, "t": t, "d": data}


def _interaction(user, iid, name, options=None, i_type=2, token="tok"):
    data = {"name": name}
    if options:
        data["options"] = options
    payload = {"id": iid, "token": token, "type": i_type, "data": data}
    if user:
        payload["member"] = {"user": {"id": user}}
    return payload


def _dm(user, cid, mid, content):
    return {"id": mid, "channel_id": cid, "channel_type": 1,
            "author": {"id": user}, "content": content}


class BotTestBase(unittest.TestCase):
    OWNER = "owner-1"

    def setUp(self):
        self.gw = FakeGateway()
        self.fx = Fixture()
        self.env = TmpEnv(
            DISCORD_BOT_TOKEN="fake-bot-token",
            DISCORD_OWNER_ID=self.OWNER,
            DISCORD_API_BASE=self.fx.web_base,
            DISCORD_GATEWAY_URL=self.gw.url,
        )
        self.aid = self.env.add_assistant(
            name="email",
            tools=["email_list", "vault_search"])
        conn = self.env.conn()
        self.tid = workmod.create_task(conn, self.aid, "inbox triage",
                                       "check the inbox")
        conn.close()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)
        self.t = threading.Thread(target=self.bot.run_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.bot.stop()
        self.gw.close()
        self.fx.close()
        self.t.join(timeout=5)

    def _rest(self, prefix):
        return [c for c in self.fx.calls
                if c["method"] == "POST"
                and c["path"].startswith(prefix)]

    def _channel_posts(self):
        return [c for c in self._rest("/channels/")]


class TestSession(BotTestBase):
    def test_identify_commands_dedup(self):
        """conn1: IDENTIFY, owner /tasks command handled once even when
        the gateway redelivers the interaction; DM message handled."""

        def script(g):
            g.send({"op": 10, "d": {"heartbeat_interval": 30000}})
            ident = g.recv()
            self.assertEqual(ident["op"], 2)
            self.assertIn("fake-bot-token",
                          ident["d"]["token"])
            g.send(_event("READY", {"session_id": "s1",
                                    "resume_gateway_url": self.gw.url,
                                    "application": {"id": "app1"}}, 1))
            ev = _event("INTERACTION_CREATE",
                        _interaction(self.OWNER, "i1", "tasks"), 2)
            g.send(ev)
            wait_for(lambda: self._rest("/interactions/"), timeout=10)
            # gateway redelivers the same interaction -> deduped
            g.send(ev)
            g.send(_event("MESSAGE_CREATE",
                          _dm(self.OWNER, "dm1", "m1", "status 1"), 3))
            wait_for(lambda: self._channel_posts(), timeout=10)
            time.sleep(0.3)
            g.drop()

        self.gw.scripts.append(script)
        wait_for(lambda: self.gw.connections >= 1, timeout=10)
        wait_for(lambda: self._channel_posts(), timeout=10)
        time.sleep(0.5)
        callbacks = self._rest("/interactions/")
        self.assertEqual(len(callbacks), 1)
        body = callbacks[0]["body"]
        self.assertIn("inbox triage", json.dumps(body))
        post = self._channel_posts()[0]["body"]
        self.assertIn("#1 [queued] inbox triage", post["content"])
        # dedup rows recorded
        conn = self.env.conn()
        n = conn.execute("SELECT COUNT(*) c FROM discord_events") \
            .fetchone()["c"]
        conn.close()
        self.assertGreaterEqual(n, 2)

    def test_intruder_fail_closed(self):
        def script(g):
            g.send({"op": 10, "d": {"heartbeat_interval": 30000}})
            g.recv()
            g.send(_event("READY", {"session_id": "s1",
                                    "resume_gateway_url": self.gw.url},
                          1))
            g.send(_event("INTERACTION_CREATE",
                          _interaction("intruder", "i9", "tasks"), 2))
            wait_for(lambda: self._rest("/interactions/"), timeout=10)
            g.send(_event("MESSAGE_CREATE",
                          _dm("intruder", "dmX", "m9", "tasks"), 3))
            time.sleep(0.5)
            g.drop()

        self.gw.scripts.append(script)
        wait_for(lambda: self._rest("/interactions/"), timeout=10)
        time.sleep(0.6)
        cb = self._rest("/interactions/")[0]["body"]
        self.assertIn("authorized", cb["data"]["content"].lower())
        self.assertTrue(cb["data"]["flags"] & 64)  # ephemeral
        self.assertEqual(self._channel_posts(), [])

    def test_resume_after_drop(self):
        """conn2 must use RESUME (op 6) with the saved session id."""
        ops = []

        def script1(g):
            g.send({"op": 10, "d": {"heartbeat_interval": 30000}})
            g.recv()
            g.send(_event("READY", {"session_id": "s1",
                                    "resume_gateway_url": self.gw.url},
                          1))
            g.send(_event("MESSAGE_CREATE",
                          _dm(self.OWNER, "dm1", "m1", "status 1"), 2))
            wait_for(lambda: self._channel_posts(), timeout=10)
            g.drop()

        def script2(g):
            g.send({"op": 10, "d": {"heartbeat_interval": 30000}})
            msg = g.recv(timeout=10)
            ops.append(msg["op"])
            self.assertEqual(msg["d"]["session_id"], "s1")
            g.send(_event("RESUMED", {}, 3))
            g.send(_event("MESSAGE_CREATE",
                          _dm(self.OWNER, "dm1", "m2", "tasks"), 4))
            wait_for(lambda: len(self._channel_posts()) >= 2,
                     timeout=10)
            g.drop()

        self.gw.scripts.extend([script1, script2])
        wait_for(lambda: self.gw.connections >= 2, timeout=15)
        wait_for(lambda: len(self._channel_posts()) >= 2, timeout=10)
        self.assertIn(6, ops)


class TestNoOwnerConfigured(unittest.TestCase):
    def test_refuses_everyone(self):
        gw = FakeGateway()
        fx = Fixture()
        env = TmpEnv(DISCORD_BOT_TOKEN="fake-bot-token",
                     DISCORD_API_BASE=fx.web_base,
                     DISCORD_GATEWAY_URL=gw.url)
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False)
        t = threading.Thread(target=bot.run_forever, daemon=True)
        t.start()

        def script(g):
            g.send({"op": 10, "d": {"heartbeat_interval": 30000}})
            g.recv()
            g.send(_event("READY", {"session_id": "s",
                                    "resume_gateway_url": gw.url}, 1))
            g.send(_event("INTERACTION_CREATE",
                          _interaction("owner-1", "i1", "tasks"), 2))
            wait_for(lambda: [c for c in fx.calls
                              if c["path"].startswith("/interactions/")],
                     timeout=10)
            g.drop()

        gw.scripts.append(script)
        wait_for(lambda: [c for c in fx.calls
                          if c["path"].startswith("/interactions/")],
                 timeout=10)
        cb = [c for c in fx.calls
                  if c["path"].startswith("/interactions/")][0]["body"]
        self.assertIn("authorized", cb["data"]["content"].lower())
        bot.stop()
        gw.close()
        fx.close()
        t.join(timeout=5)


class TestAuthFailure(unittest.TestCase):
    """A rejected token must say so in one line — not just retry."""

    def _bot(self, gw, fx, token="fake-bot-token"):
        env = TmpEnv(DISCORD_BOT_TOKEN=token,
                     DISCORD_API_BASE=fx.web_base,
                     DISCORD_GATEWAY_URL=gw.url)
        return env, discordbot.DiscordBot(
            env.settings, env=env.env, notifier=False)

    def test_close_4004_logs_token_line(self):
        gw = FakeGateway()
        fx = Fixture()
        env, bot = self._bot(gw, fx)

        def script(g):
            g.send({"op": 10, "d": {"heartbeat_interval": 30000}})
            g.recv()
            g.close(4004)              # Discord 'authentication failed'

        gw.scripts.append(script)
        with self.assertLogs("pa.discord", level="ERROR") as cm:
            t = threading.Thread(target=bot.run_forever, daemon=True)
            t.start()
            wait_for(lambda: any("Discord token rejected" in m
                                 for m in cm.output), timeout=10)
        bot.stop()
        gw.close()
        fx.close()
        t.join(timeout=5)
        self.assertTrue(any(
            "Discord token rejected (401) — check DISCORD_BOT_TOKEN"
            in m for m in cm.output))
        # keeps retrying gracefully rather than crashing
        self.assertGreaterEqual(gw.connections, 1)

    def test_handshake_401_marks_auth_failure(self):
        fx = Fixture()
        fx.page("/gw", code=401, body=b"{}")
        from pa.wsclient import WSClient, WSError
        with self.assertRaises(WSError) as ectx:
            WSClient.connect(f"ws://127.0.0.1:{fx.port}/gw", timeout=5)
        fx.close()
        self.assertEqual(ectx.exception.code, 401)
        self.assertTrue(discordbot._auth_failed(ectx.exception))

    def test_missing_token_start_log(self):
        fx = Fixture()
        env = TmpEnv(DISCORD_API_BASE=fx.web_base)
        env.env.pop("DISCORD_BOT_TOKEN", None)
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False)
        with self.assertLogs("pa.discord", level="WARNING") as cm:
            self.assertFalse(bot.start())
        fx.close()
        self.assertTrue(any("DISCORD_BOT_TOKEN not set" in m
                            for m in cm.output))


class TestNotifier(BotTestBase):
    def test_result_and_approval_notifications(self):
        self.gw.scripts = [lambda g: g.drop()]  # no gateway chatter
        self.env.settings.discord_channels = {"email": "chan-42"}
        conn = self.env.conn()
        # terminal task on the routed assistant
        tid = workmod.create_task(conn, self.aid, "Daily email summary",
                                "body")
        conn.execute(
            "UPDATE tasks SET state=?, result_json=? WHERE id=?",
            ("succeeded",
             json.dumps({"summary": "2 unread messages; trip note found",
                         "claims": []}), tid))
        # a pending approval on the same assistant
        t2 = workmod.create_task(conn, self.aid, "mutate", "body")
        from pa import approvals
        approvals.create_approval(conn, "owner_fact",
                                  {"key": "k", "value": "v",
                                   "assistant_name": "email",
                                   "delete": False},
                                  task_id=t2)
        for _ in range(3):
            self.fx.push_post({"id": "dm-77", "ok": True})
        self.bot.tick_notifications()
        posts = self._channel_posts()
        self.assertTrue(any(p["path"] == "/channels/chan-42/messages"
                            for p in posts), posts)
        # DM flow: open_dm then post to the dm channel
        self.assertTrue(any(p.get("path") == "/users/@me/channels"
                            for p in self.fx.calls))
        self.assertTrue(any(p["path"] == "/channels/dm-77/messages"
                            for p in posts))
        # durable dedup: a second tick sends nothing new
        n = len(posts)
        self.bot.tick_notifications()
        self.assertEqual(len(self._channel_posts()), n)
        # notified column persisted
        marked = conn.execute(
            "SELECT COUNT(*) c FROM tasks"
            " WHERE discord_notified_at IS NOT NULL").fetchone()["c"]
        self.assertGreaterEqual(marked, 1)


if __name__ == "__main__":
    unittest.main()

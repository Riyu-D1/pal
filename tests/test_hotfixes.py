"""Regression tests for the three VM hotfixes carried into this branch.

1. Gateway socket timeout must be heartbeat_interval + slack, not the
   20s connect timeout (the gateway dropped every ~30s and ate events).
2. APIClient.post_json must accept an empty (204) body instead of
   raising on json.loads (Discord interaction callbacks return 204).
3. tick_notifications must fall back to the owner DM when a task's
   assistant has no routed channel, instead of silently marking it
   notified.
"""

import json
import unittest
from unittest import mock

from pa import discordbot, worker as workmod
from pa.netclient import APIClient
from pa.wsclient import WSClosed
from tests.helpers import Fixture, TmpEnv, wait_for


class _StubWS:
    """Records settimeout() calls; serves one HELLO then drops."""
    def __init__(self, heartbeat_ms):
        self.heartbeat_ms = heartbeat_ms
        self.timeouts = []
        self._hello = True

    def recv(self):
        if self._hello:
            self._hello = False
            return json.dumps({"op": 10, "d": {
                "heartbeat_interval": self.heartbeat_ms}})
        raise WSClosed("stub closed")

    def settimeout(self, t):
        self.timeouts.append(t)

    def send_text(self, _t):
        pass

    def close(self):
        pass


class TestGatewayTimeoutHotfix(unittest.TestCase):
    def test_socket_timeout_is_heartbeat_plus_slack(self):
        env = TmpEnv(DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID="o")
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False)
        ws = _StubWS(heartbeat_ms=41250)
        with mock.patch.object(discordbot.WSClient, "connect",
                               return_value=ws):
            with self.assertRaises(WSClosed):
                bot._session()
        self.assertEqual(ws.timeouts, [41250 / 1000.0 + 30])


class TestPostJsonEmptyBody(unittest.TestCase):
    def test_204_returns_none_body(self):
        fx = Fixture()
        try:
            fx.push_post((204, {}, b""))
            client = APIClient(fx.web_base, timeout=5)
            headers, body = client.post_json("/interactions/1/t/callback",
                                             {"type": 4})
            self.assertIsNone(body)
        finally:
            fx.close()

    def test_normal_body_still_parsed(self):
        fx = Fixture()
        try:
            fx.push_post({"id": "chan-1"})
            client = APIClient(fx.web_base, timeout=5)
            _h, body = client.post_json("/users/@me/channels", {})
            self.assertEqual(body, {"id": "chan-1"})
        finally:
            fx.close()


class TestNotifierDmFallback(unittest.TestCase):
    OWNER = "owner-1"

    def test_unrouted_task_result_dms_owner(self):
        fx = Fixture()
        env = TmpEnv(DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID=self.OWNER,
                     DISCORD_API_BASE=fx.web_base)
        try:
            aid = env.add_assistant(name="email", tools=[])
            conn = env.conn()
            tid = workmod.create_task(conn, aid, "triage", "body")
            conn.execute(
                "UPDATE tasks SET state='succeeded', result_json=?"
                " WHERE id=?",
                (json.dumps({"summary": "all done"}), tid))
            conn.close()
            # no DISCORD_CHANNELS route -> must fall back to owner DM
            fx.push_post({"id": "dm-55"})   # open_dm response
            fx.push_post({"id": "m1"})      # post_message response
            bot = discordbot.DiscordBot(env.settings, env=env.env,
                                        notifier=False)
            bot.tick_notifications()
            paths = [c["path"] for c in fx.calls if c["method"] == "POST"]
            self.assertIn("/users/@me/channels", paths)
            posts = [c for c in fx.calls
                     if c["path"] == "/channels/dm-55/messages"]
            self.assertTrue(posts, fx.calls)
            self.assertIn("all done", posts[0]["body"]["content"])
            conn = env.conn()
            marked = conn.execute(
                "SELECT discord_notified_at FROM tasks WHERE id=?",
                (tid,)).fetchone()["discord_notified_at"]
            conn.close()
            self.assertIsNotNone(marked)
        finally:
            fx.close()

    def test_unroutable_task_keeps_retrying(self):
        """No route AND no owner DM -> not marked; retried next tick."""
        fx = Fixture()
        env = TmpEnv(DISCORD_BOT_TOKEN="t",
                     DISCORD_API_BASE=fx.web_base)  # no owner id
        try:
            aid = env.add_assistant(name="email", tools=[])
            conn = env.conn()
            tid = workmod.create_task(conn, aid, "triage", "body")
            conn.execute(
                "UPDATE tasks SET state='succeeded' WHERE id=?", (tid,))
            conn.close()
            bot = discordbot.DiscordBot(env.settings, env=env.env,
                                        notifier=False)
            bot.tick_notifications()
            conn = env.conn()
            row = conn.execute(
                "SELECT discord_notified_at, discord_notify_attempts"
                " FROM tasks WHERE id=?", (tid,)).fetchone()
            conn.close()
            self.assertIsNone(row["discord_notified_at"])
            self.assertEqual(row["discord_notify_attempts"], 1)
        finally:
            fx.close()


if __name__ == "__main__":
    unittest.main()

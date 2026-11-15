"""ask_owner question cards: ctx.ask -> 'ask' approval -> Discord
choice buttons -> clicked label back as the tool result.

- No choices -> Yes/No pair; up to 5 short choices -> a button each.
- Same 24h TTL as approvals; unattended turns get {skipped}.
- Non-owner clicks are ignored by the interaction gate.
"""

import threading
import time
import unittest

from pa import approvals, chattools, discordbot
from tests.helpers import FakeGateway, Fixture, TmpEnv, wait_for

OWNER = "owner-1"
BOT = "999"


def _event(t, data, seq):
    return {"op": 0, "s": seq, "t": t, "d": data}


def _component(user, iid, custom_id, token="tok"):
    return {"id": iid, "token": token, "type": 3,
            "member": {"user": {"id": user}},
            "message": {"id": "mm-1"},
            "data": {"component_type": 2, "custom_id": custom_id}}


def _ctx(env, **kw):
    conn = env.conn()
    return chattools.ChatToolContext(
        conn, env.settings, None, "chan-1", env=env.env, **kw)


class TestCtxAsk(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()

    def test_defaults_to_yes_no(self):
        seen = {}

        def fake_ask(question, choices, aid):
            seen.update(q=question, choices=list(choices), aid=aid)
            return {"answer": "Yes"}

        ctx = _ctx(self.env, request_ask=fake_ask)
        out = chattools.run(ctx, "ask_owner", {"question": "go ahead?"})
        self.assertEqual(out, {"answer": "Yes"})
        self.assertEqual(seen["choices"], ["Yes", "No"])
        row = approvals.get(self.env.conn(), seen["aid"])
        self.assertEqual(row["kind"], "ask")
        self.assertEqual(row["status"], "pending")

    def test_choices_capped_at_five_and_80_chars(self):
        seen = {}

        def fake_ask(question, choices, aid):
            seen["choices"] = list(choices)
            return {"answer": choices[0]}

        ctx = _ctx(self.env, request_ask=fake_ask)
        out = chattools.run(
            ctx, "ask_owner",
            {"question": "pick",
             "choices": ["a" * 100, "b", "c", "d", "e", "f"]})
        self.assertEqual(out["answer"], "a" * 80)
        self.assertEqual(len(seen["choices"]), 5)
        self.assertEqual(len(seen["choices"][0]), 80)

    def test_unattended_skips_without_row(self):
        ctx = _ctx(self.env, unattended=True)
        out = chattools.run(ctx, "ask_owner", {"question": "anyone?"})
        self.assertEqual(out, {"skipped": True, "reason": "unattended"})
        conn = self.env.conn()
        rows = conn.execute(
            "SELECT * FROM approvals WHERE kind='ask'").fetchall()
        self.assertEqual(rows, [])
        conn.close()

    def test_no_channel_wired_is_error_not_block(self):
        ctx = _ctx(self.env)
        out = chattools.run(ctx, "ask_owner", {"question": "hm?"})
        self.assertEqual(out["error"], "unavailable")


class TestAskCard(unittest.TestCase):
    def setUp(self):
        self.gw = FakeGateway()
        self.fx = Fixture()
        self.env = TmpEnv(
            DISCORD_BOT_TOKEN="fake-bot-token",
            DISCORD_OWNER_ID=OWNER,
            DISCORD_API_BASE=self.fx.web_base,
            DISCORD_GATEWAY_URL=self.gw.url)
        self.env.conn().close()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False,
            engine=object())
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

    def _channel_posts(self):
        return [c for c in self.fx.calls
                if c.get("method") == "POST" and
                str(c.get("path", "")).endswith("/messages")]

    def _rest(self, prefix):
        return [c for c in self.fx.calls
                if str(c.get("path", "")).startswith(prefix)]

    def test_card_shape_and_click_answer(self):
        self.fx.push_post({"id": "dm-77"})   # open_dm reply
        conn = self.env.conn()
        aid = approvals.create_approval(
            conn, "ask",
            {"question": "tea or coffee?",
             "choices": ["tea", "coffee", "water"],
             "channel_id": "dm1"})
        conn.close()
        result = {}
        th = threading.Thread(
            target=lambda: result.setdefault(
                "r", self.bot._request_chat_ask(
                    "tea or coffee?", ["tea", "coffee", "water"], aid)),
            daemon=True)
        th.start()

        def script(g):
            self._hello_ready(g)
            wait_for(lambda: self._channel_posts(), timeout=10)
            g.send(_event("INTERACTION_CREATE",
                          _component(OWNER, "i1", f"chatask:{aid}:1"),
                          2))
            wait_for(lambda: self._rest("/interactions/"), timeout=10)
            g.drop()

        self._drive(script)
        th.join(timeout=15)
        self.assertEqual(result["r"], {"answer": "coffee"})
        posts = [p for p in self._channel_posts()
                 if p["path"] == "/channels/dm-77/messages"]
        body = posts[0]["body"]
        self.assertIn("tea or coffee?", body["content"])
        btns = body["components"][0]["components"]
        self.assertEqual([b["label"] for b in btns],
                         ["tea", "coffee", "water"])
        self.assertEqual(
            [b["custom_id"] for b in btns],
            [f"chatask:{aid}:{i}" for i in range(3)])
        cb = self._rest("/interactions/")[0]["body"]
        self.assertEqual(cb["type"], 7)
        self.assertIn("coffee", cb["data"]["content"])
        conn = self.env.conn()
        row = approvals.get(conn, aid)
        self.assertEqual(row["status"], "approved")
        conn.close()

    def test_second_click_is_ignored(self):
        conn = self.env.conn()
        aid = approvals.create_approval(
            conn, "ask", {"question": "q", "choices": ["x", "y"],
                          "channel_id": "dm1"})
        approvals.decide(conn, aid, True)
        conn.close()

        def script(g):
            self._hello_ready(g)
            g.send(_event("INTERACTION_CREATE",
                          _component(OWNER, "i1", f"chatask:{aid}:0"),
                          2))
            wait_for(lambda: self._rest("/interactions/"), timeout=10)
            g.drop()

        self._drive(script)
        wait_for(lambda: self._rest("/interactions/"), timeout=10)
        time.sleep(0.3)
        cb = self._rest("/interactions/")[0]["body"]
        self.assertIn("already answered", cb["data"]["content"])

    def test_expired_card_refuses(self):
        conn = self.env.conn()
        aid = approvals.create_approval(
            conn, "ask", {"question": "q", "choices": ["x", "y"],
                          "channel_id": "dm1"})
        conn.execute(
            "UPDATE approvals SET created_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", aid))
        conn.close()

        def script(g):
            self._hello_ready(g)
            g.send(_event("INTERACTION_CREATE",
                          _component(OWNER, "i1", f"chatask:{aid}:0"),
                          2))
            wait_for(lambda: self._rest("/interactions/"), timeout=10)
            g.drop()

        self._drive(script)
        wait_for(lambda: self._rest("/interactions/"), timeout=10)
        time.sleep(0.3)
        cb = self._rest("/interactions/")[0]["body"]
        self.assertIn("expired", cb["data"]["content"].lower())

    def test_non_owner_click_ignored(self):
        conn = self.env.conn()
        aid = approvals.create_approval(
            conn, "ask", {"question": "q", "choices": ["x", "y"],
                          "channel_id": "dm1"})
        conn.close()

        def script(g):
            self._hello_ready(g)
            g.send(_event("INTERACTION_CREATE",
                          _component("intruder", "i9",
                                     f"chatask:{aid}:1"), 2))
            time.sleep(0.4)
            g.drop()

        self._drive(script)
        time.sleep(0.6)
        cbs = self._rest("/interactions/")
        self.assertEqual(len(cbs), 1)
        self.assertEqual(cbs[0]["body"]["data"]["content"],
                         "Not authorized.")
        conn = self.env.conn()
        self.assertEqual(approvals.get(conn, aid)["status"], "pending")
        conn.close()


if __name__ == "__main__":
    unittest.main()

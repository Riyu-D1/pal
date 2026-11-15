"""Item 11: auto-review labels on approval cards."""

import unittest

from pa import review


class TestLabel(unittest.TestCase):
    def test_high_risk_tools(self):
        for tool in ("send_email", "shell", "mac", "run_python",
                     "browse_fill_secret", "code_task",
                     "calendar_create"):
            self.assertEqual(review.label(tool, {})["level"], "high",
                             tool)

    def test_review_tools(self):
        for tool in ("owner_fact", "watch_add", "skill_save",
                     "memory_save"):
            self.assertEqual(review.label(tool, {})["level"],
                             "review", tool)

    def test_low_default(self):
        self.assertEqual(review.label("mystery_tool", {})["level"],
                         "low")

    def test_reasons_are_specific(self):
        self.assertIn("example.com", review.label(
            "send_email", {"to": "a@example.com"})["reason"])
        self.assertIn("deletes", review.label(
            "owner_fact", {"delete": True, "key": "x"})["reason"])
        self.assertIn("shared owner profile", review.label(
            "owner_fact", {"key": "x"})["reason"])

    def test_card_body_carries_label(self):
        from tests.helpers import TmpEnv
        from pa import discordbot
        env = TmpEnv(DISCORD_OWNER_ID="42")
        conn = env.conn()
        posts = []

        class API:
            def open_dm(self, uid):
                return "DM1"

            def post_message(self, ch, body, **kw):
                posts.append(body)
                return {"id": "1"}

        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False, engine=object())
        bot.api = API()
        # don't wait: pretend nobody answers
        bot.s.chat_approval_wait = 0.01
        bot._request_chat_approval("send_email",
                                   {"to": "a@b.com"}, "a" * 32)
        self.assertIn("🔴 high", posts[0])
        self.assertIn("b.com", posts[0])


if __name__ == "__main__":
    unittest.main()

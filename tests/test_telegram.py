"""Telegram mirror — off by default."""

import unittest
from unittest import mock

from pa import telegram as tg
from tests.helpers import TmpEnv


class TestTelegram(unittest.TestCase):
    def test_disabled_by_default(self):
        env = TmpEnv()
        self.assertFalse(env.settings.telegram_enabled)
        self.assertFalse(tg.enabled(env.settings,
                                    {"TELEGRAM_BOT_TOKEN": "t"}))

    def test_enabled_needs_all_three(self):
        env = TmpEnv(PA_TELEGRAM="true", TELEGRAM_CHAT_ID="42")
        s = env.settings
        self.assertFalse(tg.enabled(s, {}))
        self.assertTrue(tg.enabled(s, {"TELEGRAM_BOT_TOKEN": "abc"}))

    def test_send_calls_bot_api(self):
        env = TmpEnv(PA_TELEGRAM="true", TELEGRAM_CHAT_ID="42")
        calls = []
        with mock.patch.object(tg, "_call",
                               side_effect=lambda e, m, p, timeout:
                               calls.append((m, p)) or {"ok": True}):
            self.assertTrue(tg.send(
                env.settings, {"TELEGRAM_BOT_TOKEN": "k"}, "hello"))
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["chat_id"], "42")
        self.assertEqual(calls[0][1]["text"], "hello")

    def test_send_long_text_splits(self):
        env = TmpEnv(PA_TELEGRAM="true", TELEGRAM_CHAT_ID="42")
        calls = []
        with mock.patch.object(tg, "_call",
                               side_effect=lambda e, m, p, timeout:
                               calls.append(p) or {"ok": True}):
            tg.send(env.settings, {"TELEGRAM_BOT_TOKEN": "k"},
                    "x" * (tg.MAX_LEN + 50))
        self.assertEqual(len(calls), 2)

    def test_poll_parses_updates(self):
        env = TmpEnv(PA_TELEGRAM="true", TELEGRAM_CHAT_ID="42")
        fake = {"result": [
            {"update_id": 7,
             "message": {"text": "hi", "chat": {"id": 42}}},
            {"update_id": 8,
             "message": {"chat": {"id": 42}}}]}
        with mock.patch.object(tg, "_call", return_value=fake):
            msgs = tg.poll({"TELEGRAM_BOT_TOKEN": "k"}, offset=1)
        self.assertEqual([m["id"] for m in msgs], [7, 8])
        self.assertEqual(msgs[0]["text"], "hi")
        self.assertEqual(msgs[0]["chat_id"], "42")


if __name__ == "__main__":
    unittest.main()

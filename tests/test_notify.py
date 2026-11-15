"""notifier — quiet-hour holds flush as one batch
6h dedupe, per-hour cap, urgent passthrough, dm_and_voice routing
behind PA_NOTIFY_V2."""

import unittest
from unittest import mock

from pa import notify, sysjobs
from tests.helpers import TmpEnv


class API:
    def __init__(self):
        self.posts = []

    def post_message(self, dm, text):
        self.posts.append((dm, text))


class Bot:
    def __init__(self, env):
        self.env = env
        self.s = env.settings
        self.api = API()
        self._voice = None

    @property
    def db_path(self):
        return self.env.db_path

    def _owner_dm_id(self):
        return "dm1"


class TestNotify(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_NOTIFY_V2="true")
        self.bot = Bot(self.env)
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def _quiet(self, val):
        return mock.patch.object(
            __import__("pa.call", fromlist=["in_quiet_hours"]),
            "in_quiet_hours", return_value=val)

    def test_normal_sends_now(self):
        with self._quiet(False):
            self.assertEqual(
                notify.notify(self.bot, self.conn, "dm", "normal",
                              "hi there"), "sent")
        self.assertEqual(self.bot.api.posts[0][1], "hi there")

    def test_dedupe_six_hours(self):
        with self._quiet(False):
            notify.notify(self.bot, self.conn, "dm", "normal",
                          "same text", dedupe_key="k1")
            out = notify.notify(self.bot, self.conn, "dm", "normal",
                                "same text", dedupe_key="k1")
        self.assertEqual(out, "deduped")
        self.assertEqual(len(self.bot.api.posts), 1)

    def test_quiet_holds_then_flushes_as_batch(self):
        with self._quiet(True):
            notify.notify(self.bot, self.conn, "dm", "normal", "a")
            notify.notify(self.bot, self.conn, "dm", "low", "b")
        self.assertEqual(self.bot.api.posts, [])
        with self._quiet(False):
            notify.notify(self.bot, self.conn, "dm", "normal", "c")
        self.assertEqual(len(self.bot.api.posts), 2)
        batch = self.bot.api.posts[0][1]
        self.assertIn("2 held update", batch)
        self.assertIn("a", batch) and self.assertIn("b", batch)

    def test_urgent_passthrough_in_quiet(self):
        with self._quiet(True):
            self.assertEqual(
                notify.notify(self.bot, self.conn, "dm", "urgent",
                              "house is on fire"), "sent")
        self.assertIn("fire", self.bot.api.posts[0][1])

    def test_per_hour_cap_holds(self):
        self.env.settings.notify_per_hour = 2
        with self._quiet(False):
            notify.notify(self.bot, self.conn, "dm", "normal",
                          "one", dedupe_key="1")
            notify.notify(self.bot, self.conn, "dm", "normal",
                          "two", dedupe_key="2")
            out = notify.notify(self.bot, self.conn, "dm", "normal",
                                "three", dedupe_key="3")
        self.assertEqual(out, "held")

    def test_dm_and_voice_routes_when_flag_on(self):
        with self._quiet(False):
            self.assertEqual(
                sysjobs.dm_and_voice(self.bot, "via notifier"), "sent")
        self.assertIn("via notifier", self.bot.api.posts[0][1])
        self.env.settings.notify_v2 = False
        with self._quiet(False):
            out = sysjobs.dm_and_voice(self.bot, "direct dm")
        self.assertEqual(out, "dm1")


if __name__ == "__main__":
    unittest.main()

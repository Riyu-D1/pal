import re
import unittest

from pa.discordbot import _COMMANDS

_NAME = re.compile(r"^[-_a-z0-9]{1,32}$")


def _walk(opts):
    for o in opts or []:
        yield o
        yield from _walk(o.get("options"))


class TestDiscordCommandLimits(unittest.TestCase):
    """Discord rejects the whole bulk PUT (HTTP 400) if any command breaks
    its limits, which silently leaves every slash command stale."""

    def test_names_and_descriptions_fit_discord_limits(self):
        self.assertLessEqual(len(_COMMANDS), 100)
        for item in _walk(_COMMANDS):
            with self.subTest(name=item.get("name")):
                self.assertRegex(item["name"], _NAME)
                self.assertTrue(1 <= len(item["description"]) <= 100)
                self.assertLessEqual(len(item.get("options") or []), 25)


if __name__ == "__main__":
    unittest.main()

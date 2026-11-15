"""Evolve item 10: Track A auto-apply with snapshot rollback."""

import json
import unittest
from unittest import mock

from pa.evolve import apply, changelog, eval as evalmod, items, \
    snippets
from tests.helpers import TmpEnv


class _Bot:
    def __init__(self):
        self.posts = []
        self.api = self

    def _owner_dm_id(self):
        return "DM1"

    def post_message(self, channel, content, **kw):
        self.posts.append((channel, content, kw))


def _item(conn, shape, status="planned", artifacts=None, title="t",
          dedupe="d1"):
    iid = items.open_item(conn, shape, "low", title=title,
                          dedupe_key=dedupe, artifacts=artifacts)
    items.set_status(conn, iid, status)
    return iid


class TestTrackA(unittest.TestCase):
    def test_happy_path_apply(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true")
        conn = env.conn()
        iid = _item(conn, "lesson", title="reflect: be terse")
        st = apply.maybe_apply(conn, env.settings, iid,
                               bot=_Bot())
        item = items.get(conn, iid)
        events = changelog.recent(conn, verbs=("applied",))
        conn.close()
        self.assertEqual(st, "applied")
        self.assertEqual(item["eval_score"], 1.0)
        self.assertTrue(events)                      # changelog row
        self.assertIsNotNone(snippets.get(env.conn(), item["id"] and
                                          f"lesson-{iid}"))

    def test_eval_fail_rolls_back(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true")
        conn = env.conn()
        iid = _item(conn, "prompt", title="tone: be terse",
                    artifacts={"name": "tone", "body": "be terse"})
        with mock.patch.object(evalmod, "run",
                               return_value={"score": 0.5,
                                             "results": [],
                                             "regressions": ["x"]}):
            st = apply.maybe_apply(conn, env.settings, iid,
                                   bot=_Bot())
        item = items.get(conn, iid)
        cur = snippets.get(conn, "tone")
        inc = conn.execute(
            "SELECT kind FROM incidents WHERE kind='eval_regress'"
            ).fetchall()
        conn.close()
        self.assertEqual(st, "rolled_back")
        self.assertIsNone(cur)            # brand-new snippet removed
        self.assertTrue(inc)

    def test_existing_snippet_restored(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true")
        conn = env.conn()
        snippets.save_snippet(conn, "tone", "be polite")
        iid = _item(conn, "prompt", title="tone",
                    artifacts={"name": "tone", "body": "be rude"})
        with mock.patch.object(evalmod, "run",
                               return_value={"score": 0.0,
                                             "results": [],
                                             "regressions": ["x"]}):
            apply.maybe_apply(conn, env.settings, iid, bot=_Bot())
        cur = snippets.get(conn, "tone")
        conn.close()
        self.assertEqual(cur["body"], "be polite")     # snapshot back

    def test_ineligible_shape_gets_card(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true")
        conn = env.conn()
        bot = _Bot()
        iid = _item(conn, "fix", title="code fix")
        st = apply.maybe_apply(conn, env.settings, iid, bot=bot)
        conn.close()
        self.assertEqual(st, "awaiting_approval")
        self.assertTrue(bot.posts)
        comps = bot.posts[0][2].get("components")
        self.assertIn("evappr:", json.dumps(comps))

    def test_flag_off_posts_card_no_write(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="false")
        conn = env.conn()
        bot = _Bot()
        iid = _item(conn, "prompt", title="tone",
                    artifacts={"name": "tone", "body": "be terse"})
        st = apply.maybe_apply(conn, env.settings, iid, bot=bot)
        self.assertEqual(st, "awaiting_approval")
        self.assertIsNone(snippets.get(conn, "tone"))   # not written
        self.assertTrue(bot.posts)
        # owner's click applies it anyway (the click IS approval)
        apply.apply_approved(conn, env.settings, iid, bot=bot)
        item = items.get(conn, iid)
        conn.close()
        self.assertEqual(item["status"], "applied")

    def test_config_allowlist_rejects_bind(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true")
        conn = env.conn()
        iid = _item(conn, "config", title="bind 0.0.0.0",
                    artifacts={"kind": "bind_address",
                               "value": "0.0.0.0"})
        st = apply.maybe_apply(conn, env.settings, iid)
        conn.close()
        self.assertEqual(st, "awaiting_approval")

    def test_new_skill_name_needs_card(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true")
        conn = env.conn()
        iid = _item(conn, "skill", title="new skill",
                    artifacts={"name": "brand-new", "body": "do it"})
        st = apply.maybe_apply(conn, env.settings, iid)
        conn.close()
        self.assertEqual(st, "awaiting_approval")


if __name__ == "__main__":
    unittest.main()

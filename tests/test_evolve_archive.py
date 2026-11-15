"""final-4 E1: improvement archive — lineage, promote/revert,
Track A candidate gating."""

import unittest
from unittest import mock

from pa import discordbot, memblocks, skills as skillsmod
from pa.evolve import apply, archive, evaluator, items, snippets
from tests.helpers import FakeGateway, Fixture, TmpEnv
from tests.test_discordchat import OWNER


def _item(conn, shape, status="planned", artifacts=None, title="t",
          dedupe="d1"):
    iid = items.open_item(conn, shape, "low", title=title,
                          dedupe_key=dedupe, artifacts=artifacts)
    items.set_status(conn, iid, status)
    return iid


class _Bot:
    def __init__(self):
        self.posts = []
        self.api = self

    def _owner_dm_id(self):
        return "DM1"

    def post_message(self, channel, content, **kw):
        self.posts.append((channel, content, kw))


def _ev(score, h="h1"):
    return {"score": score, "results": [], "regressions": [],
            "passed": int(score * 4), "total": 4,
            "eval_set_hash": h}


class TestVariants(unittest.TestCase):
    def test_every_change_becomes_a_variant(self):
        env = TmpEnv(PA_EVOLVE_ARCHIVE="true")
        conn = env.conn()
        s = env.settings
        snippets.save_snippet(conn, "tone", "be terse", s=s)
        skillsmod.save_skill(conn, s, "brew", "tea", "boil water")
        memblocks.set_block(conn, "world", "owner is in London",
                            edited_by="owner", s=s)
        kinds = {r["kind"] for r in archive.variants(conn, limit=50)}
        conn.close()
        self.assertEqual(
            kinds, {"snippet", "skill", "memory_block"})

    def test_config_change_promotes_a_variant(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true",
                     PA_EVOLVE_ARCHIVE="true")
        conn = env.conn()
        iid = _item(conn, "config", title="slower monitor",
                    artifacts={"kind": "monitor_every",
                               "name": "web", "minutes": 30})
        with mock.patch.object(evaluator, "run",
                               side_effect=[_ev(1.0), _ev(1.0)]):
            st = apply.maybe_apply(conn, env.settings, iid, bot=_Bot())
        live = archive.live(conn, "config", "monitor_every:web")
        from pa import db as dbmod
        minutes = dbmod.get_setting(conn, "monitor_every:web")
        conn.close()
        self.assertEqual(st, "applied")
        self.assertIsNotNone(live)
        self.assertEqual(minutes, "30")

    def test_lineage_and_live_pointer(self):
        env = TmpEnv(PA_EVOLVE_ARCHIVE="true")
        conn = env.conn()
        s = env.settings
        snippets.save_snippet(conn, "tone", "v1 body", s=s)
        snippets.save_snippet(conn, "tone", "v2 body", s=s)
        rows = list(reversed(
            archive.variants(conn, "snippet", "tone")))
        cur = snippets.get(conn, "tone")
        live = archive.live(conn, "snippet", "tone")
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "retired")
        self.assertEqual(rows[1]["status"], "live")
        self.assertEqual(rows[1]["parent_id"], rows[0]["id"])
        self.assertEqual(live["content"], cur["body"])

    def test_flag_off_writes_no_variants(self):
        env = TmpEnv(PA_EVOLVE_ARCHIVE="false")
        conn = env.conn()
        snippets.save_snippet(conn, "tone", "body", s=env.settings)
        skillsmod.save_skill(conn, env.settings, "b", "d", "body")
        rows = archive.variants(conn, limit=50)
        conn.close()
        self.assertEqual(rows, [])

    def test_revert_flips_live_and_restores_store(self):
        env = TmpEnv(PA_EVOLVE_ARCHIVE="true")
        conn = env.conn()
        s = env.settings
        snippets.save_snippet(conn, "tone", "v1 body", s=s)
        v1 = archive.live(conn, "snippet", "tone")["id"]
        snippets.save_snippet(conn, "tone", "v2 body", s=s)
        out = archive.revert(conn, s, v1)
        cur = snippets.get(conn, "tone")
        live = archive.live(conn, "snippet", "tone")
        rows = archive.variants(conn, "snippet", "tone")
        conn.close()
        self.assertTrue(out["ok"])
        self.assertEqual(cur["body"], "v1 body")
        self.assertEqual(live["id"], v1)
        self.assertEqual(len(rows), 2)          # no extra row

    def test_dedupe_same_live_content(self):
        env = TmpEnv(PA_EVOLVE_ARCHIVE="true")
        conn = env.conn()
        s = env.settings
        snippets.save_snippet(conn, "tone", "same", s=s)
        snippets.save_snippet(conn, "tone", "same", s=s)
        rows = archive.variants(conn, "snippet", "tone")
        conn.close()
        self.assertEqual(len(rows), 1)


class TestTrackACandidates(unittest.TestCase):
    def test_green_candidate_promotes(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true",
                     PA_EVOLVE_ARCHIVE="true")
        conn = env.conn()
        iid = _item(conn, "prompt", title="tone",
                    artifacts={"name": "tone", "body": "be terse"})
        with mock.patch.object(evaluator, "run",
                               side_effect=[_ev(1.0), _ev(1.0)]):
            st = apply.maybe_apply(conn, env.settings, iid, bot=_Bot())
        item = items.get(conn, iid)
        live = archive.live(conn, "snippet", "tone")
        conn.close()
        self.assertEqual(st, "applied")
        self.assertIsNotNone(live)
        self.assertEqual(live["content"], "be terse")
        self.assertEqual(item["status"], "applied")

    def test_no_promotion_on_worse_score(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true",
                     PA_EVOLVE_ARCHIVE="true")
        conn = env.conn()
        snippets.save_snippet(conn, "tone", "v1", s=env.settings)
        iid = _item(conn, "prompt", title="tone",
                    artifacts={"name": "tone", "body": "v2"})
        with mock.patch.object(evaluator, "run",
                               side_effect=[_ev(1.0), _ev(0.5)]):
            st = apply.maybe_apply(conn, env.settings, iid, bot=_Bot())
        item = items.get(conn, iid)
        cand = archive.variants(conn, "snippet", "tone")
        cur = snippets.get(conn, "tone")
        conn.close()
        self.assertEqual(st, "rolled_back")
        self.assertEqual(cur["body"], "v1")            # store restored
        statuses = {r["status"] for r in cand}
        self.assertIn("candidate", statuses)           # scored, on file
        self.assertIn("live", statuses)                # v1 still live

    def test_equal_score_below_green_not_promoted(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true",
                     PA_EVOLVE_ARCHIVE="true")
        conn = env.conn()
        iid = _item(conn, "prompt", title="tone",
                    artifacts={"name": "tone", "body": "v2"})
        with mock.patch.object(evaluator, "run",
                               side_effect=[_ev(0.5), _ev(0.5)]):
            st = apply.maybe_apply(conn, env.settings, iid, bot=_Bot())
        cand = archive.variants(conn, "snippet", "tone")
        live = archive.live(conn, "snippet", "tone")
        conn.close()
        self.assertEqual(st, "rolled_back")
        self.assertEqual(cand[0]["status"], "candidate")
        self.assertIsNone(live)

    def test_flag_off_track_a_unchanged(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_AUTO_APPLY="true",
                     PA_EVOLVE_ARCHIVE="false")
        conn = env.conn()
        iid = _item(conn, "prompt", title="tone",
                    artifacts={"name": "tone", "body": "be terse"})
        st = apply.maybe_apply(conn, env.settings, iid, bot=_Bot())
        rows = archive.variants(conn, "snippet", "tone")
        conn.close()
        self.assertEqual(st, "applied")
        self.assertEqual(rows, [])                     # base behaviour


class TestArchiveCommand(unittest.TestCase):
    def setUp(self):
        self.gw = FakeGateway()
        self.fx = Fixture()
        self.env = TmpEnv(
            DISCORD_BOT_TOKEN="fake-bot-token",
            DISCORD_OWNER_ID=OWNER,
            DISCORD_API_BASE=self.fx.web_base,
            DISCORD_GATEWAY_URL=self.gw.url,
            PA_EVOLVE_ARCHIVE="true")
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False,
            engine=object())

    def tearDown(self):
        self.bot.stop()
        self.gw.close()
        self.fx.close()

    def test_archive_and_revert_commands(self):
        conn = self.env.conn()
        snippets.save_snippet(conn, "tone", "v1", s=self.env.settings)
        v1 = archive.live(conn, "snippet", "tone")["id"]
        snippets.save_snippet(conn, "tone", "v2", s=self.env.settings)
        out = self.bot.execute(conn, ["evolve", "archive"], "dm1")
        self.assertIn("snippet:tone", out)
        out = self.bot.execute(
            conn, ["evolve", "archive", "snippet:tone"], "dm1")
        self.assertIn("live", out)
        self.assertIn(f"#{v1}", out)
        out = self.bot.execute(
            conn, ["evolve", "revert", str(v1)], "dm1")
        cur = snippets.get(conn, "tone")
        conn.close()
        self.assertIn("now variant", out)
        self.assertEqual(cur["body"], "v1")

    def test_archive_off_says_so(self):
        env = TmpEnv(DISCORD_BOT_TOKEN="t", DISCORD_OWNER_ID=OWNER)
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False, engine=object())
        conn = env.conn()
        out = bot.execute(conn, ["evolve", "archive"], "dm1")
        conn.close()
        bot.stop()
        self.assertIn("off", out)


if __name__ == "__main__":
    unittest.main()

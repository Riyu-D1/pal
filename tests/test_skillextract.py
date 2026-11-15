"""K3 acceptance: skill auto-extraction + retrieval.

A clean 'ok' turn with >=3 successful tool steps asks the fast model
for a reusable skill; the proposal is deduped (slug + keyword overlap
+ content-hash marker) and queued as a held skill_save job_action —
the only write path is the owner's Approve (execute_approved).
suggest_for() ranks saved skills for a new turn by keyword overlap.
"""

import json
import unittest

from pa import approvals, chattools, db as dbmod, skills, \
    skillextract as sx
from tests.helpers import TmpEnv


class Brain:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete(self, messages, remaining, tier=None,
                 scope="owner-chat"):
        self.calls += 1
        return self.replies.pop(0)


class ExtEnv(TmpEnv):
    def __init__(self, **kw):
        kw.setdefault("PA_SKILLEXTRACT", "1")
        super().__init__(**kw)


TRACE = [
    {"tool": "web_search", "args": {"q": "x"}, "result": {"ok": 1}},
    {"tool": "fetch_url", "args": {"url": "https://a.x"},
     "result": {"text": "t"}},
    {"tool": "memory_save", "args": {"key": "k"},
     "result": {"saved": True}},
]

PROPOSAL = json.dumps({
    "name": "find-and-save-fact",
    "description": "look up a fact online and store it",
    "body": "1. web_search the question\n2. fetch_url the best hit\n"
            "3. memory_save the distilled fact"})


def _ctx(env):
    conn = env.conn()
    ast = conn.execute(
        "SELECT * FROM assistants WHERE name='pal'").fetchone()
    return chattools.ChatToolContext(conn, env.settings, ast, "c1",
                                     env=env.env)


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.env = ExtEnv()
        self.env.add_assistant(name="pal", tools=[])
        self.conn = self.env.conn()

    def test_proposal_queued_not_saved(self):
        out = sx.maybe_extract(
            Brain([PROPOSAL]), self.conn, self.env.settings, "c1",
            "remember the mayor's name", TRACE, "done — saved it")
        self.assertEqual(out["proposed"], "find-and-save-fact")
        # nothing in skills yet; one held job_action instead
        self.assertEqual(skills.list_skills(self.conn,
                                            self.env.settings), [])
        pend = approvals.list_pending(self.conn)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["kind"], "job_action")
        payload = json.loads(pend[0]["payload_json"])
        self.assertEqual(payload["tool"], "skill_save")
        self.assertEqual(payload["args"]["name"],
                         "find-and-save-fact")

    def test_approve_replay_saves_skill(self):
        sx.maybe_extract(Brain([PROPOSAL]), self.conn,
                         self.env.settings, "c1", "r", TRACE, "ok")
        ctx = _ctx(self.env)
        payload = json.loads(
            approvals.list_pending(self.conn)[0]["payload_json"])
        out = chattools.execute_approved(
            ctx, payload["tool"], payload["args"])
        self.assertTrue(out["saved"], out)
        self.assertEqual(
            [r["name"] for r in
             skills.list_skills(ctx.conn, self.env.settings)],
            ["find-and-save-fact"])
        # replay made no second approval row
        self.assertEqual(len(approvals.list_pending(ctx.conn)), 1)

    def test_under_three_steps_skips(self):
        b = Brain([PROPOSAL])
        out = sx.maybe_extract(b, self.conn, self.env.settings,
                               "c1", "r", TRACE[:2], "ok")
        self.assertEqual(out, {})
        self.assertEqual(b.calls, 0)   # no model call at all

    def test_flag_off_no_call(self):
        env = TmpEnv()
        b = Brain([PROPOSAL])
        self.assertEqual(sx.maybe_extract(
            b, env.conn(), env.settings, "c1", "r", TRACE, "ok"), {})
        self.assertEqual(b.calls, 0)

    def test_model_skip(self):
        out = sx.maybe_extract(
            Brain(['{"skip": true}']), self.conn, self.env.settings,
            "c1", "r", TRACE, "ok")
        self.assertEqual(out, {})
        self.assertEqual(approvals.list_pending(self.conn), [])

    def test_dedupe_existing_skill(self):
        skills.save_skill(self.conn, self.env.settings,
                          "find-and-save-fact", "look up + store",
                          "existing body")
        out = sx.maybe_extract(
            Brain([PROPOSAL]), self.conn, self.env.settings,
            "c1", "r", TRACE, "ok")
        self.assertEqual(out, {})
        self.assertEqual(approvals.list_pending(self.conn), [])

    def test_dedupe_keyword_overlap(self):
        skills.save_skill(self.conn, self.env.settings,
                          "web-fact-keeper",
                          "look up a fact online and store it",
                          "existing")
        out = sx.maybe_extract(
            Brain([PROPOSAL]), self.conn, self.env.settings,
            "c1", "r", TRACE, "ok")
        self.assertEqual(out, {})

    def test_same_proposal_not_requeued(self):
        sx.maybe_extract(Brain([PROPOSAL]), self.conn,
                         self.env.settings, "c1", "r", TRACE, "ok")
        out = sx.maybe_extract(
            Brain([PROPOSAL]), self.conn, self.env.settings,
            "c1", "r2", TRACE, "ok")
        self.assertEqual(out, {})
        self.assertEqual(len(approvals.list_pending(self.conn)), 1)

    def test_failed_steps_dont_count(self):
        trace = TRACE[:2] + [{"tool": "x", "args": {},
                              "result": {"error": "denied"}}]
        b = Brain([PROPOSAL])
        self.assertEqual(
            sx.maybe_extract(b, self.conn, self.env.settings,
                             "c1", "r", trace, "ok"), {})
        self.assertEqual(b.calls, 0)


class TestSuggest(unittest.TestCase):
    def setUp(self):
        self.env = ExtEnv()
        self.env.add_assistant(name="pal", tools=[])
        self.conn = self.env.conn()
        skills.save_skill(self.conn, self.env.settings,
                          "weather-report", "daily weather briefing",
                          "steps")
        skills.save_skill(self.conn, self.env.settings,
                          "deploy-watch", "watch deploy health",
                          "steps")
        skills.save_skill(self.conn, self.env.settings,
                          "photo-backup", "back up phone photos",
                          "steps")

    def test_top3_by_keyword(self):
        hits = sx.suggest_for(self.conn, self.env.settings,
                              "what's the weather like today?")
        self.assertEqual(hits, ["weather-report"])

    def test_no_match(self):
        self.assertEqual(
            sx.suggest_for(self.conn, self.env.settings,
                           "zzz qqq"), [])

    def test_k_limit(self):
        for i in range(5):
            skills.save_skill(self.conn, self.env.settings,
                              f"weather-x{i}", "weather thing",
                              "b")
        hits = sx.suggest_for(self.conn, self.env.settings, "weather")
        self.assertLessEqual(len(hits), 3)


if __name__ == "__main__":
    unittest.main()

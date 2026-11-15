"""Evolve item 2: turn outcomes, /good /bad, reply buttons,
correction detection, and the feedback chat tool."""

import json
import threading
import unittest

from pa import chatagent, discordbot
from pa.evolve import outcomes
from tests.helpers import FakeGateway, Fixture, TmpEnv, wait_for
from tests.test_discordchat import OWNER, _component, _dm, _event


def fenced(obj):
    return "```json\n" + json.dumps(obj) + "\n```"


def _outcomes(conn):
    return conn.execute("SELECT * FROM turn_outcomes ORDER BY id") \
        .fetchall()


def _incidents(conn):
    return conn.execute("SELECT * FROM incidents ORDER BY id") \
        .fetchall()


class TestHeuristic(unittest.TestCase):
    def test_outcome_categories(self):
        self.assertEqual(outcomes.heuristic("(model error: down)"),
                         "error")
        self.assertEqual(
            outcomes.heuristic("(not configured: no brain)"), "error")
        self.assertEqual(outcomes.heuristic("(empty reply)"),
                         "low_confidence")
        self.assertEqual(
            outcomes.heuristic("I can't do that for you"),
            "low_confidence")
        self.assertEqual(outcomes.heuristic("here is your answer"),
                         "ok")

    def test_two_tool_errors_is_low_confidence(self):
        trace = [
            {"tool": "x", "result": {"error": "fetch_failed"}},
            {"tool": "y", "result": {"error": "bad_arguments"}},
        ]
        self.assertEqual(outcomes.heuristic("all done", trace),
                         "low_confidence")
        self.assertEqual(
            outcomes.heuristic("all done", trace[:1]), "ok")


class TestTurnOutcomes(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_CHAT_MAX_STEPS="4")
        self.env.add_assistant(name="pal", tools=[])

    def _engine(self, script, **kw):
        def brain(messages):
            return script.pop(0) if script else fenced({"final": "d"})
        return chatagent.ChatEngine(self.env.settings, env=self.env.env,
                                    brain=brain, **kw)

    def test_flag_off_turn_still_answers_and_records_outcome(self):
        eng = self._engine([fenced({"final": "42"})])
        out = eng.run_turn("c1", "meaning?")
        self.assertEqual(out["text"], "42")
        rows = _outcomes(self.env.conn())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "ok")
        self.assertEqual(rows[0]["channel_id"], "c1")

    def test_error_reply_records_error_outcome(self):
        eng = chatagent.ChatEngine(
            self.env.settings, env=self.env.env,
            brain=lambda m: (_ for _ in ()).throw(
                chatagent.ProviderError("nope")))
        out = eng.run_turn("c1", "hi")
        self.assertIn("model error", out["text"])
        rows = _outcomes(self.env.conn())
        self.assertEqual(rows[0]["outcome"], "error")

    def test_correction_message_records_incident(self):
        eng = self._engine([fenced({"final": "got it"})])
        eng.run_turn("c1", "no, I meant the other folder")
        rows = _incidents(self.env.conn())
        kinds = [r["kind"] for r in rows]
        self.assertIn("owner_correction", kinds)
        # a plain 'no thanks' is NOT a correction
        eng.run_turn("c1", "no worries, all good")
        kinds = [r["kind"] for r in _incidents(self.env.conn())]
        self.assertEqual(kinds.count("owner_correction"), 1)

    def test_feedback_tool_writes_outcome_and_incident(self):
        eng = self._engine([
            fenced({"tool": "feedback",
                    "args": {"rating": "bad", "note": "wrong city"}}),
            fenced({"final": "noted"}),
        ])
        eng.run_turn("c1", "that was wrong")
        oc = _outcomes(self.env.conn())
        inc = _incidents(self.env.conn())
        self.assertIn("feedback_bad", [r["outcome"] for r in oc])
        self.assertIn("feedback_bad", [r["kind"] for r in inc])
        self.assertIn("wrong city", inc[-1]["detail"])

    def test_evolve_grader_can_downgrade_outcome(self):
        env = TmpEnv(PA_EVOLVE="true", PA_CHAT_MAX_STEPS="4")
        env.add_assistant(name="pal", tools=[])
        script = [fenced({"final": "a weak answer"}), "bad"]

        def brain(messages):
            return script.pop(0) if script else fenced({"final": "d"})
        eng = chatagent.ChatEngine(env.settings, env=env.env,
                                   brain=brain)
        eng.run_turn("c1", "hi")
        wait_for(lambda: _outcomes(env.conn())[0]["outcome"]
                 == "low_confidence", timeout=5)
        rows = _outcomes(env.conn())
        self.assertEqual(rows[0]["outcome"], "low_confidence")
        self.assertIn("low_confidence",
                      [r["kind"] for r in _incidents(env.conn())])


class TestDiscordFeedback(unittest.TestCase):
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

    def tearDown(self):
        self.bot.stop()
        self.gw.close()
        self.fx.close()

    def test_good_bad_commands_write_feedback(self):
        conn = self.env.conn()
        out = self.bot.execute(conn, ["good"], "dm1")
        self.assertIn("👍", out)
        out = self.bot.execute(conn, ["bad", "wrong", "city"], "dm1")
        self.assertIn("👎", out)
        ocs = [r["outcome"] for r in _outcomes(conn)]
        self.assertEqual(ocs, ["feedback_good", "feedback_bad"])
        inc = _incidents(conn)
        self.assertEqual([r["kind"] for r in inc],
                         ["feedback_good", "feedback_bad"])
        self.assertIn("wrong city", inc[-1]["detail"])
        conn.close()

    def test_thumbs_component_records_feedback(self):
        conn = self.env.conn()
        d = _component(OWNER, "i1", "fbdn")
        d["channel_id"] = "dm1"
        self.bot._handle_component(conn, d)
        conn.close()
        ocs = [r["outcome"] for r in _outcomes(self.env.conn())]
        self.assertEqual(ocs, ["feedback_bad"])

    def test_non_owner_component_is_refused_silently(self):
        d = _component("intruder", "i9", "fbup")
        d["channel_id"] = "dmX"
        self.bot._handle_event("INTERACTION_CREATE", d)
        self.assertEqual(_outcomes(self.env.conn()), [])
        self.assertEqual(_incidents(self.env.conn()), [])
        # exactly one ephemeral refusal, no other posts
        cbs = [c for c in self.fx.calls
               if c["path"].startswith("/interactions/")]
        self.assertEqual(len(cbs), 1)
        self.assertTrue(cbs[0]["body"]["data"]["flags"] & 64)


if __name__ == "__main__":
    unittest.main()

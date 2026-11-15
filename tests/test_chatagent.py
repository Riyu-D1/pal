"""Conversational agent: JSON action parsing + the ReAct loop.

All brains here are fakes (callables); no provider or network involved.
"""

import json
import unittest

from pa import chatagent
from pa.provider import ProviderRateLimit, ProviderUnavailable
from tests.helpers import TmpEnv


def fenced(obj):
    return "```json\n" + json.dumps(obj) + "\n```"


class TestParseAction(unittest.TestCase):
    def test_tool_block(self):
        out = chatagent.parse_action(
            fenced({"tool": "web_search", "args": {"query": "x"}}))
        self.assertEqual(out, ("tool", "web_search", {"query": "x"}))

    def test_final_block(self):
        out = chatagent.parse_action(fenced({"final": "all done"}))
        self.assertEqual(out, ("final", "all done"))

    def test_bare_json(self):
        out = chatagent.parse_action('{"final": "hi"}')
        self.assertEqual(out, ("final", "hi"))

    def test_tool_without_args(self):
        out = chatagent.parse_action(fenced({"tool": "memory_search"}))
        self.assertEqual(out, ("tool", "memory_search", {}))

    def test_answer_alias_and_arguments(self):
        out = chatagent.parse_action(fenced(
            {"answer": "ok"}))
        self.assertEqual(out, ("final", "ok"))
        out = chatagent.parse_action(fenced(
            {"tool": "shell", "arguments": {"command": "ls"}}))
        self.assertEqual(out, ("tool", "shell", {"command": "ls"}))

    def test_last_valid_block_wins(self):
        text = ("sure! like this: ```json\n{\"tool\": \"example\"}\n``` "
                "but actually:\n" +
                fenced({"final": "real answer"}))
        self.assertEqual(chatagent.parse_action(text),
                         ("final", "real answer"))

    def test_prose_is_malformed(self):
        self.assertIsNone(chatagent.parse_action("just chatting, no json"))
        self.assertIsNone(chatagent.parse_action(""))
        self.assertIsNone(chatagent.parse_action("```json\n{bad\n```"))

    def test_unknown_shape(self):
        self.assertIsNone(chatagent.parse_action(fenced({"foo": 1})))
        self.assertIsNone(chatagent.parse_action(fenced([1, 2])))


class ChatEngineBase(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_CHAT_MAX_STEPS="4")
        self.aid = self.env.add_assistant(name="pal", tools=[])
        self.seen = []

    def engine(self, script, **kw):
        calls = self.seen

        def brain(messages):
            calls.append(messages)
            return script.pop(0) if script else fenced({"final": "default"})

        return chatagent.ChatEngine(self.env.settings, env=self.env.env,
                                    brain=brain, **kw)


class TestLoop(ChatEngineBase):
    def test_multi_step_tool_then_final(self):
        eng = self.engine([
            fenced({"tool": "memory_save",
                    "args": {"content": "likes tea"}}),
            fenced({"tool": "memory_search", "args": {"query": "tea"}}),
            fenced({"final": "noted — you like tea"}),
        ])
        out = eng.run_turn("chan1", "remember I like tea")
        self.assertEqual(out["text"], "noted — you like tea")
        self.assertEqual([s["tool"] for s in out["trace"]],
                         ["memory_save", "memory_search"])
        conn = self.env.conn()
        mem = conn.execute("SELECT content FROM memories").fetchall()
        hist = conn.execute(
            "SELECT role FROM chat_messages WHERE channel_id='chan1'"
            " ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(mem[0]["content"], "likes tea")
        self.assertEqual([r["role"] for r in hist],
                         ["user", "assistant"])

    def test_malformed_retry_once(self):
        eng = self.engine(["sorry what?", fenced({"final": "recovered"})])
        out = eng.run_turn("c", "hi")
        self.assertEqual(out["text"], "recovered")
        self.assertEqual(len(self.seen), 2)
        nudge = self.seen[1][-1]["content"]
        self.assertIn("not a single fenced json block", nudge)

    def test_double_malformed_uses_raw_text(self):
        eng = self.engine(["blah", "still no json here"])
        out = eng.run_turn("c", "hi")
        self.assertEqual(out["text"], "still no json here")

    def test_step_cap(self):
        eng = self.engine([
            fenced({"tool": "memory_search", "args": {"query": "x"}}),
            fenced({"tool": "memory_search", "args": {"query": "y"}}),
            fenced({"tool": "memory_search", "args": {"query": "z"}}),
            fenced({"tool": "memory_search", "args": {"query": "w"}}),
            fenced({"tool": "memory_search", "args": {"query": "v"}}),
        ])
        out = eng.run_turn("c", "hi")
        self.assertIn("step limit", out["text"])
        self.assertEqual(len(out["trace"]), 4)  # PA_CHAT_MAX_STEPS

    def test_unknown_tool_reported(self):
        eng = self.engine([
            fenced({"tool": "nonsense", "args": {}}),
            fenced({"final": "fine"}),
        ])
        out = eng.run_turn("c", "hi")
        self.assertEqual(out["text"], "fine")
        self.assertEqual(out["trace"][0]["result"]["error"],
                         "unknown_tool")

    def test_fallback_on_primary_error(self):
        def bad(messages):
            raise ProviderUnavailable("sidecar down")

        def good(messages):
            return fenced({"final": "fallback answer"})

        eng = chatagent.ChatEngine(
            self.env.settings, env=self.env.env, brain=bad,
            fallback_brain=good)
        out = eng.run_turn("c", "hi")
        self.assertEqual(out["text"], "fallback answer")

    def test_both_brains_fail_gives_error_text(self):
        def bad(messages):
            raise ProviderRateLimit("slow down")

        def bad2(messages):
            raise ProviderUnavailable("also down")

        eng = chatagent.ChatEngine(
            self.env.settings, env=self.env.env, brain=bad,
            fallback_brain=bad2)
        out = eng.run_turn("c", "hi")
        self.assertIn("model error", out["text"])

    def test_history_followups(self):
        eng = self.engine([fenced({"final": "four"})])
        eng.run_turn("c", "what is 2+2")
        eng2 = self.engine([fenced({"final": "you said four"})])
        eng2.run_turn("c", "and 3+3?")
        # second turn's prompt carried the first exchange
        msgs = self.seen[-1]
        roles = [m["role"] for m in msgs]
        self.assertIn("assistant", roles)
        contents = json.dumps(msgs)
        self.assertIn("four", contents)


if __name__ == "__main__":
    unittest.main()

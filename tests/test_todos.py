"""chat todos + /stop + mid-turn steering."""

import unittest

from pa import chattools
from tests.helpers import TmpEnv


class TestTodos(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        aid = self.env.add_assistant(name="pal", tools=[])
        self.conn = self.env.conn()
        ast = self.conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        self.ctx = chattools.ChatToolContext(
            self.conn, self.env.settings, ast, "chan-1",
            env=self.env.env)

    def test_add_list_done(self):
        chattools.run(self.ctx, "todo_add", {"text": "buy milk"})
        chattools.run(self.ctx, "todo_add", {"text": "write tests"})
        out = chattools.run(self.ctx, "todo_list", {})
        self.assertEqual([t["text"] for t in out["todos"]],
                         ["buy milk", "write tests"])
        out = chattools.run(self.ctx, "todo_done", {"num": 1})
        self.assertTrue(out["todos"][0]["done"])
        self.assertFalse(out["todos"][1]["done"])
        self.assertEqual(
            chattools.run(self.ctx, "todo_done", {"num": 9})
            ["error"], "not_found")

    def test_scoped_to_channel(self):
        chattools.run(self.ctx, "todo_add", {"text": "mine"})
        ctx2 = chattools.ChatToolContext(
            self.conn, self.env.settings, self.ctx.assistant,
            "chan-2", env=self.env.env)
        self.assertEqual(
            chattools.run(ctx2, "todo_list", {})["todos"], [])

    def test_empty_add(self):
        self.assertEqual(
            chattools.run(self.ctx, "todo_add", {"text": " "})
            ["error"], "bad_arguments")


class TestStopSteer(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        aid = self.env.add_assistant(name="pal", tools=[])
        self.conn = self.env.conn()

    def test_engine_stop_and_steer(self):
        """stop_check ends the turn; steer_pull injects notes."""
        import json
        from pa import chatagent

        seen = []
        steered = ["skip the todos, just say hi"]
        stops = []

        def brain(messages):
            seen.append(
                [m["content"] for m in messages
                 if "steered" in m.get("content", "")])
            if len(seen) == 1:
                return "```json\n" + json.dumps(
                    {"tool": "todo_list", "args": {}}) + "\n```"
            return "```json\n" + json.dumps(
                {"final": "hello"}) + "\n```"

        def stop_check():
            return bool(stops)

        def steer_pull():
            out, steered[:] = list(steered), []
            stops.append(True)          # stop right after steering
            return out

        eng = chatagent.ChatEngine(
            self.env.settings, env=self.env.env, brain=brain)
        out = eng.run_turn("chan-1", "hi", stop_check=stop_check,
                           steer_pull=steer_pull)
        self.assertIn("stopped", out["text"])
        self.assertIn("steered mid-turn", str(seen))

    def test_steer_injected_as_user_note(self):
        import json
        from pa import chatagent

        seen = []
        notes = [["just one more thing"]]

        def brain(messages):
            seen.append(list(messages))
            return "```json\n" + json.dumps(
                {"final": "ok"}) + "\n```"

        eng = chatagent.ChatEngine(
            self.env.settings, env=self.env.env, brain=brain)
        out = eng.run_turn(
            "chan-1", "hi",
            steer_pull=lambda: notes.pop(0) if notes else [])
        self.assertEqual(out["text"], "ok")
        steers = [m["content"] for m in seen[0]
                  if "steered mid-turn" in m.get("content", "")]
        self.assertEqual(len(steers), 1)


if __name__ == "__main__":
    unittest.main()

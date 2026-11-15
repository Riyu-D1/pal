"""final-2 M1: pinned memory blocks — schema, caps, versioned
history + undo, always-on prompt injection (chat/call/job share
_system), the human-block owner gate and /memory blocks."""

import unittest

from pa import chattools, memblocks
from tests.helpers import TmpEnv


def _env(**flags):
    return TmpEnv(PA_MEMORY_BLOCKS="true", **flags)


class TestSchema(unittest.TestCase):
    def test_tables(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            for t in ("memory_blocks", "memory_block_history"):
                conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
        finally:
            conn.close()


class TestBlocks(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def test_set_get_cap_history(self):
        b = memblocks.set_block(self.conn, "persona", "be terse")
        self.assertEqual(b["version"], 1)
        self.assertEqual(b["content"], "be terse")
        # cap enforcement
        memblocks.set_block(self.conn, "persona", "x" * 5000)
        self.assertEqual(len(memblocks.get(self.conn, "persona")
                             ["content"]), 1200)
        # history has both previous versions
        h = memblocks.history(self.conn, "persona")
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0]["content"], "be terse")
        # unknown name refused
        self.assertIn("error",
                      memblocks.set_block(self.conn, "evil", "x"))

    def test_undo_latest_and_by_id(self):
        memblocks.set_block(self.conn, "world", "v1")
        memblocks.set_block(self.conn, "world", "v2")
        out = memblocks.undo(self.conn)
        self.assertEqual(out["content"], "v1")
        self.assertEqual(out["version"], 3)   # undo is itself a write
        memblocks.set_block(self.conn, "world", "v3")
        # undo a specific history row restores ITS replaced content
        hid = next(h["id"] for h in memblocks.history(
            self.conn, "world") if h["content"] == "v2")
        out = memblocks.undo(self.conn, hid)
        self.assertEqual(out["content"], "v2")
        self.assertIn("error", memblocks.undo(self.conn, 9999))

    def test_prompt_block_empty_and_full(self):
        self.assertEqual(memblocks.prompt_block(self.conn), "")
        memblocks.set_block(self.conn, "persona", "dry wit")
        memblocks.set_block(self.conn, "human", "Alex, busy")
        out = memblocks.prompt_block(self.conn)
        self.assertIn("[How you sound]", out)
        self.assertIn("dry wit", out)
        self.assertIn("[The owner]", out)
        self.assertNotIn("[World context]", out)


class TestInjection(unittest.TestCase):
    def test_system_prompt_includes_blocks(self):
        env = _env()
        conn = env.conn()
        try:
            memblocks.set_block(conn, "persona", "be terse")
            memblocks.set_block(conn, "goals", "job hunt")
            from pa import chatagent
            eng = chatagent.ChatEngine(env.settings, env=env.env,
                                       db_path=env.settings.db_path)
            sys = eng._system(conn, None, None)
            self.assertIn("be terse", sys)
            self.assertIn("job hunt", sys)
            # call prompt shares the same path
            sys2 = eng._system(conn, None, None, call=True)
            self.assertIn("be terse", sys2)
            # unattended too
            sys3 = eng._system(conn, None, None, unattended=True)
            self.assertIn("be terse", sys3)
        finally:
            conn.close()


class TestTool(unittest.TestCase):
    def setUp(self):
        self.env = _env()
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def _ctx(self, **kw):
        return chattools.ChatToolContext(
            self.conn, self.env.settings, None, "chan",
            env=self.env.env, **kw)

    def test_flag_off(self):
        env = TmpEnv()
        conn = env.conn()
        try:
            ctx = chattools.ChatToolContext(
                conn, env.settings, None, "c", env=env.env)
            r = chattools.run(ctx, "memory_block_set",
                              {"name": "persona", "content": "x"})
            self.assertEqual(r["error"], "disabled")
        finally:
            conn.close()

    def test_pal_sets_persona_freely(self):
        ctx = self._ctx()
        r = chattools.run(ctx, "memory_block_set",
                          {"name": "persona", "content": "be terse"})
        self.assertEqual(r["version"], 1)
        self.assertEqual(memblocks.get(self.conn, "persona")
                         ["content"], "be terse")

    def test_human_needs_owner(self):
        calls = []
        ctx = self._ctx(
            request_approval=lambda n, a, i:
                calls.append(n) or "denied")
        r = chattools.run(ctx, "memory_block_set",
                          {"name": "human", "content": "Alex"})
        self.assertEqual(calls, ["memory_block_set"])
        self.assertEqual(r["error"], "not_approved")
        self.assertEqual(memblocks.get(self.conn, "human")
                         ["content"], "")
        # approved -> writes
        ctx2 = self._ctx(request_approval=lambda *a: "approved")
        r = chattools.run(ctx2, "memory_block_set",
                          {"name": "human", "content": "Alex"})
        self.assertEqual(r["version"], 1)

    def test_tainted_gate(self):
        calls = []
        ctx = self._ctx(
            request_approval=lambda n, a, i:
                calls.append(n) or "approved")
        ctx.tainted = True
        ctx.taint_source = "web"
        chattools.run(ctx, "memory_block_set",
                      {"name": "world", "content": "x"})
        self.assertEqual(calls, ["memory_block_set"])


class TestCommand(unittest.TestCase):
    def setUp(self):
        self.env = _env()
        from pa import discordbot
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)

    def _cmd(self, *argv):
        conn = self.env.conn()
        try:
            return self.bot.execute(conn, list(argv), "chan-1")
        finally:
            conn.close()

    def test_blocks_set_undo(self):
        out = self._cmd("memory", "blocks")
        self.assertIn("persona", out)
        out = self._cmd("memory", "block", "persona",
                        "speak plainly")
        self.assertIn("v1", out)
        out = self._cmd("memory", "block", "persona", "be terse")
        self.assertIn("v2", out)
        out = self._cmd("memory", "undo")
        self.assertIn("undone", out)
        conn = self.env.conn()
        try:
            self.assertEqual(memblocks.get(conn, "persona")
                             ["content"], "speak plainly")
        finally:
            conn.close()

    def test_flag_off(self):
        env = TmpEnv()
        from pa import discordbot
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False)
        conn = env.conn()
        try:
            out = bot.execute(conn, ["memory", "blocks"], "c")
        finally:
            conn.close()
        self.assertIn("PA_MEMORY_BLOCKS", out)


if __name__ == "__main__":
    unittest.main()

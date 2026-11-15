"""W1 acceptance: workspace CRUD + versions + cap + traversal +
chat-tool gating + /files."""

import os
import unittest

from pa import chattools, workspace as wsmod
from tests.helpers import TmpEnv


class WsEnv(TmpEnv):
    def __init__(self, **kw):
        kw.setdefault("PA_WORKSPACE", "1")
        super().__init__(**kw)


def _ctx(t, channel="c1"):
    class C:
        pass
    c = C()
    c.s = t.settings
    c.conn = t.conn()
    c.channel_id = channel
    c.env = {}
    c.unattended = False
    c.tainted = None
    c.pending = []
    c.waited = 0.0
    c.progress = None
    c.computer = None
    c.attach = lambda name, ctype, data, cap=8 * 1024 * 1024: {
        "attached": name}
    return c


class TestPutReadVersions(unittest.TestCase):
    def setUp(self):
        self.t = WsEnv()
        self.conn = self.t.conn()

    def test_put_then_read_latest(self):
        out = wsmod.put(self.conn, self.t.settings, "proj",
                        "notes/todo.md", b"v1 body")
        self.assertEqual(out["version"], 1)
        r = wsmod.read(self.conn, self.t.settings, "proj",
                       "notes/todo.md")
        self.assertEqual(r["content"], "v1 body")
        self.assertEqual(r["version"], 1)

    def test_write_bumps_version_and_keeps_old(self):
        wsmod.put(self.conn, self.t.settings, "proj", "a.txt", b"one")
        wsmod.put(self.conn, self.t.settings, "proj", "a.txt", b"two")
        wsmod.put(self.conn, self.t.settings, "proj", "a.txt", b"three")
        vs = wsmod.versions(self.conn, self.t.settings, "proj", "a.txt")
        self.assertEqual([v["version"] for v in vs], [1, 2, 3])
        self.assertEqual(
            wsmod.read(self.conn, self.t.settings, "proj", "a.txt",
                       version=1)["content"], "one")
        self.assertEqual(
            wsmod.read(self.conn, self.t.settings, "proj", "a.txt",
                       version=2)["content"], "two")
        self.assertEqual(
            wsmod.read(self.conn, self.t.settings, "proj",
                       "a.txt")["content"], "three")

    def test_version_files_on_disk(self):
        wsmod.put(self.conn, self.t.settings, "proj", "a.txt", b"one")
        wsmod.put(self.conn, self.t.settings, "proj", "a.txt", b"two")
        vpath = os.path.join(
            wsmod.project_dir(self.t.settings, "proj"),
            ".versions", "a.txt.v1")
        self.assertTrue(os.path.exists(vpath))
        with open(vpath, "rb") as f:
            self.assertEqual(f.read(), b"one")

    def test_delete_tombstones_but_keeps_history(self):
        wsmod.put(self.conn, self.t.settings, "proj", "a.txt", b"one")
        out = wsmod.remove(self.conn, self.t.settings, "proj", "a.txt")
        self.assertTrue(out["deleted"])
        with self.assertRaises(wsmod.WorkspaceError) as e:
            wsmod.read(self.conn, self.t.settings, "proj", "a.txt")
        self.assertIn("not_found", str(e.exception))
        # v1 still readable explicitly
        r = wsmod.read(self.conn, self.t.settings, "proj", "a.txt",
                       version=1)
        self.assertEqual(r["content"], "one")
        self.assertEqual(
            wsmod.list_files(self.conn, self.t.settings, "proj"), [])

    def test_list_files_latest_only(self):
        wsmod.put(self.conn, self.t.settings, "proj", "a.txt", b"1")
        wsmod.put(self.conn, self.t.settings, "proj", "a.txt", b"2")
        wsmod.put(self.conn, self.t.settings, "proj", "b/c.txt", b"3")
        wsmod.put(self.conn, self.t.settings, "other", "a.txt", b"x")
        files = wsmod.list_files(self.conn, self.t.settings, "proj")
        paths = {(f["path"], f["version"]) for f in files}
        self.assertEqual(paths, {("a.txt", 2), ("b/c.txt", 1)})
        allp = wsmod.list_files(self.conn, self.t.settings)
        self.assertEqual(len(allp), 3)


class TestGuards(unittest.TestCase):
    def setUp(self):
        self.t = WsEnv()
        self.conn = self.t.conn()

    def test_path_traversal_blocked(self):
        for bad in ("../x", "a/../../x", "/etc/passwd", ".versions/x",
                    "a/./b", "a//b", "a\\..\\x"):
            with self.assertRaises(wsmod.WorkspaceError,
                                   msg=bad):
                wsmod._safe_relpath(bad)

    def test_put_traversal_blocked(self):
        with self.assertRaises(wsmod.WorkspaceError):
            wsmod.put(self.conn, self.t.settings, "proj",
                      "../../escape.txt", b"x")
        self.assertFalse(os.path.exists(
            os.path.join(self.t.dir, "escape.txt")))

    def test_bad_project_rejected(self):
        for bad in ("Has Space", "UPPER!", "-lead", "a" * 62):
            with self.assertRaises(wsmod.WorkspaceError, msg=bad):
                wsmod.put(self.conn, self.t.settings, bad, "a.txt", b"x")

    def test_empty_project_uses_default(self):
        out = wsmod.put(self.conn, self.t.settings, "", "a.txt", b"x")
        self.assertEqual(out["project"], "main")

    def test_missing_file_not_found(self):
        with self.assertRaises(wsmod.WorkspaceError) as e:
            wsmod.read(self.conn, self.t.settings, "proj", "none.txt")
        self.assertIn("not_found", str(e.exception))

    def test_cap_exceeded(self):
        t = WsEnv(PA_WORKSPACE_MB="1")     # 1 MiB cap
        conn = t.conn()
        big = b"x" * (1024 * 1024)
        wsmod.put(conn, t.settings, "proj", "big.bin", big)
        with self.assertRaises(wsmod.WorkspaceError) as e:
            wsmod.put(conn, t.settings, "proj", "big2.bin", big)
        self.assertIn("cap_exceeded", str(e.exception))

    def test_single_file_cap(self):
        with self.assertRaises(wsmod.WorkspaceError) as e:
            wsmod.put(self.conn, self.t.settings, "proj", "huge.bin",
                      b"x" * (wsmod.MAX_FILE_BYTES + 1))
        self.assertIn("too_large", str(e.exception))

    def test_read_caps_content(self):
        wsmod.put(self.conn, self.t.settings, "proj", "a.txt",
                  b"y" * 1000)
        r = wsmod.read(self.conn, self.t.settings, "proj", "a.txt",
                       max_bytes=100)
        self.assertTrue(r["truncated"])
        self.assertEqual(len(r["content"]), 100)


class TestChatTools(unittest.TestCase):
    def test_tools_gated_off(self):
        t = TmpEnv()                    # PA_WORKSPACE unset
        out = chattools.prompt_lines(t.settings)
        for name in chattools.WORKSPACE_TOOLS:
            self.assertNotIn(f"- {name} ", out)
        # file_write still there — it predates the workspace
        self.assertIn("- file_write ", out)

    def test_tools_listed_when_on(self):
        t = WsEnv()
        out = chattools.prompt_lines(t.settings)
        for name in ("file_list", "file_read", "file_version",
                     "file_delete"):
            self.assertIn(f"- {name} ", out)

    def test_fns_answer_off(self):
        t = TmpEnv()
        ctx = _ctx(t)
        out = chattools.run(ctx, "file_list", {})
        self.assertEqual(out["error"], "not_configured")

    def test_roundtrip_through_chat_tools(self):
        t = WsEnv()
        ctx = _ctx(t)
        out = chattools.run(ctx, "file_write",
                            {"filename": "report.md",
                             "content": "hello ws", "attach": False})
        self.assertEqual(out["version"], 1)
        self.assertEqual(out["workspace_path"], "report.md")
        self.assertNotIn("attached", out)
        r = chattools.run(ctx, "file_read", {"path": "report.md"})
        self.assertEqual(r["content"], "hello ws")
        v = chattools.run(ctx, "file_version", {"path": "report.md"})
        self.assertEqual(len(v["versions"]), 1)
        d = chattools.run(ctx, "file_delete", {"path": "report.md"})
        self.assertTrue(d["deleted"])

    def test_file_write_still_attaches_by_default(self):
        t = WsEnv()
        ctx = _ctx(t)
        out = chattools.run(ctx, "file_write",
                            {"filename": "a.md", "content": "x"})
        self.assertEqual(out.get("attached"), "a.md")
        self.assertEqual(out.get("version"), 1)

    def test_files_command(self):
        from pa.discordbot import DiscordBot
        t = WsEnv()
        bot = DiscordBot.__new__(DiscordBot)
        bot.s = t.settings
        conn = t.conn()
        wsmod.put(conn, t.settings, "main", "a.txt", b"one")
        out = bot._files(conn, [])
        self.assertIn("a.txt", out)
        self.assertIn("workspace:", out)

    def test_files_command_off(self):
        from pa.discordbot import DiscordBot
        t = TmpEnv()
        bot = DiscordBot.__new__(DiscordBot)
        bot.s = t.settings
        self.assertIn("off", bot._files(t.conn(), []))


if __name__ == "__main__":
    unittest.main()

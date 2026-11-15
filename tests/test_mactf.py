import unittest
from pa import chattools
from tests.helpers import TmpEnv


class R:
    def __init__(self, argv, **kw):
        self.argv, self.kw = argv, kw
        calls.append(argv)
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


calls = []


class TestMacTransfer(unittest.TestCase):
    def setUp(self):
        calls.clear()
        self.env = TmpEnv(MAC_SSH_HOST="mac.local",
                          MAC_SSH_USER="me")
        aid = self.env.add_assistant(name="pal", tools=[])
        self.conn = self.env.conn()
        ast = self.conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        self.ctx = chattools.ChatToolContext(
            self.conn, self.env.settings, ast, "c1",
            env=self.env.env, mac_runner=R,
            request_approval=lambda *a: "approved")

    def test_put_and_get(self):
        import os
        sd = os.path.join(self.env.settings.chat_sandbox, "c1")
        os.makedirs(sd, exist_ok=True)
        src = os.path.join(sd, "note.txt")
        open(src, "w").write("hello")
        out = chattools.run(self.ctx, "mac_put",
                            {"sandbox_path": src,
                             "remote_path": "/tmp/note.txt"})
        self.assertTrue(out["ok"])
        self.assertEqual(calls[0][0], "scp")
        self.assertEqual(calls[0][-1], "me@mac.local:/tmp/note.txt")
        # a path with spaces gets shell-quoted for the remote side
        chattools.run(self.ctx, "mac_put",
                      {"sandbox_path": src,
                       "remote_path": "/tmp/my dir/x.txt"})
        self.assertIn("'/tmp/my dir/x.txt'", calls[1][-1])

        # get: runner fakes the landing file
        def fake_run(argv, **kw):
            calls.append(argv)
            local = argv[-1]
            open(local, "wb").write(b"macdata")
            return type("P", (), {"returncode": 0,
                                  "stdout": "", "stderr": ""})()
        self.ctx._mac_runner = fake_run
        out = chattools.run(self.ctx, "mac_get",
                            {"remote_path": "/Users/me/photo.png"})
        self.assertTrue(out["local_path"].endswith("photo.png"))
        self.assertTrue(os.path.exists(out["local_path"]))

    def test_put_rejects_outside_sandbox(self):
        out = chattools.run(self.ctx, "mac_put",
                            {"sandbox_path": "/etc/passwd",
                             "remote_path": "/tmp/x"})
        self.assertEqual(out["error"], "bad_arguments")

    def test_gated(self):
        self.assertTrue(chattools.CHAT_TOOLS["mac_get"]["approval"])
        self.assertTrue(chattools.CHAT_TOOLS["mac_put"]["approval"])


if __name__ == "__main__":
    unittest.main()

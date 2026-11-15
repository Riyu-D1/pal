"""generate_image (Cloudflare / Pollinations)."""

import base64
import unittest

from pa import chattools, imagegen
from tests.helpers import TmpEnv

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


class TestGenerate(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()

    def test_empty_prompt(self):
        self.assertEqual(
            imagegen.generate(self.env.settings, self.env.env,
                              "  ")["error"], "bad_arguments")

    def test_pollinations_via_egress(self):
        import pa.egress as egressmod
        orig = egressmod.guarded_fetch
        egressmod.guarded_fetch = lambda url, **kw: {
            "body": PNG, "content_type": "image/png", "url": url}
        try:
            out = imagegen.generate(self.env.settings, self.env.env,
                                    "a cat")
            self.assertEqual(out["backend"], "pollinations")
            self.assertTrue(out["data"].startswith(b"\x89PNG"))
        finally:
            egressmod.guarded_fetch = orig

    def test_cloudflare_first_when_keyed(self):
        import pa.egress as egressmod
        called = []

        class FakeClient:
            def __init__(self, base, timeout=30):
                pass

            def post_json(self, path, body, headers=None):
                called.append(path)
                return {}, {"result": {"image": base64.b64encode(
                    PNG).decode()}}

        env = dict(self.env.env)
        env["CLOUDFLARE_ACCOUNT_ID"] = "a"
        env["CLOUDFLARE_API_TOKEN"] = "t"
        orig_c = imagegen.APIClient
        orig_e = egressmod.guarded_fetch
        imagegen.APIClient = FakeClient
        egressmod.guarded_fetch = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("pollinations must not be reached"))
        try:
            out = imagegen.generate(self.env.settings, env, "a cat")
            self.assertEqual(out["backend"], "cloudflare")
            self.assertIn("flux-1-schnell", called[0])
        finally:
            imagegen.APIClient = orig_c
            egressmod.guarded_fetch = orig_e

    def test_bad_bytes_rejected(self):
        import pa.egress as egressmod
        orig = egressmod.guarded_fetch
        egressmod.guarded_fetch = lambda *a, **k: {
            "body": b"<html>nope</html>"}
        try:
            out = imagegen.generate(self.env.settings, self.env.env,
                                    "x")
            self.assertEqual(out["error"], "generate_failed")
        finally:
            egressmod.guarded_fetch = orig


class TestTool(unittest.TestCase):
    def test_tool_attaches_result(self):
        env = TmpEnv()
        aid = env.add_assistant(name="pal", tools=[])
        conn = env.conn()
        ast = conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        ctx = chattools.ChatToolContext(
            conn, env.settings, ast, "c", env=env.env)
        posted = []
        ctx.post_file = lambda ch, fn, ct, data: (
            posted.append((fn, ct, data)), True)[1]
        import pa.egress as egressmod
        orig = egressmod.guarded_fetch
        egressmod.guarded_fetch = lambda *a, **k: {
            "body": PNG, "content_type": "image/png"}
        try:
            out = chattools.run(ctx, "generate_image",
                                {"prompt": "a cat"})
            self.assertEqual(out["attached"], "generated.png")
            self.assertEqual(posted[0][1], "image/png")
            self.assertEqual(out["backend"], "pollinations")
        finally:
            egressmod.guarded_fetch = orig


if __name__ == "__main__":
    unittest.main()

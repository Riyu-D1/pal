"""Vision captions: backend chain, model probing, cooldowns, fences,
the `look` tool — all providers mocked via a fake APIClient."""

import os
import unittest

from pa import vision
from pa.netclient import HTTPError, TransportError
from tests.helpers import TmpEnv

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
       + (10).to_bytes(4, "big") + (8).to_bytes(4, "big")
       + b"\x08\x06\x00\x00\x00" + b"rest")


class FakeClient:
    """Scripted stand-in for APIClient; records post_json bodies."""
    calls = []
    script = []          # items: dict reply | int status | Exception

    def __init__(self, base, timeout=30, **kw):
        self.base = base

    def post_json(self, path, body, headers=None):
        FakeClient.calls.append({"base": self.base, "path": path,
                                 "body": body, "headers": headers})
        item = FakeClient.script.pop(0) if FakeClient.script else \
            {"choices": [{"message": {"content": "a red cat"}}]}
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            raise HTTPError(item, {}, b"err")
        return {}, item

    @classmethod
    def reset(cls, script=()):
        cls.calls = []
        cls.script = list(script)


class Base(unittest.TestCase):
    env_kw = {"GROQ_API_KEY": "gk-test"}

    def setUp(self):
        self.env = TmpEnv(**self.env_kw)
        self.env.conn().close()
        vision._resolved.clear()
        self._cli = vision.APIClient
        vision.APIClient = FakeClient

    def tearDown(self):
        vision.APIClient = self._cli
        FakeClient.reset()

    def _caption(self, data=PNG):
        return vision.describe_data(self.env.settings, self.env.env,
                                    data, "x.png")


class TestCaption(Base):
    def test_payload_shape_and_fence(self):
        FakeClient.reset([{"choices": [{"message": {
            "content": "a red cat on a mat"}}]}])
        out = self._caption()
        self.assertIn("<<<UNTRUSTED VISION CAPTION>>>", out)
        self.assertIn("a red cat on a mat", out)
        call = FakeClient.calls[0]
        self.assertEqual(call["base"], "https://api.groq.com/openai/v1")
        self.assertEqual(call["path"], "/chat/completions")
        self.assertEqual(call["headers"]["Authorization"],
                         "Bearer gk-test")
        content = call["body"]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith(
            "data:image/png;base64,"))
        self.assertEqual(call["body"]["model"],
                         "meta-llama/llama-4-scout-17b-16e-instruct")

    def test_model_probe_walks_past_400(self):
        FakeClient.reset([
            400,
            {"choices": [{"message": {"content": "ok caption"}}]}])
        out = self._caption()
        self.assertIn("ok caption", out)
        self.assertEqual(len(FakeClient.calls), 2)
        self.assertNotEqual(FakeClient.calls[0]["body"]["model"],
                            FakeClient.calls[1]["body"]["model"])

    def test_model_override_first(self):
        env = dict(self.env.env)
        env["PA_VISION_MODEL_GROQ"] = "custom/v1"
        FakeClient.reset([{"choices": [{"message": {
            "content": "cap"}}]}])
        vision.describe_data(self.env.settings, env, PNG, "x.png")
        self.assertEqual(FakeClient.calls[0]["body"]["model"],
                         "custom/v1")

    def test_rate_limit_marks_cooldown_then_gemini(self):
        env = dict(self.env.env)
        env["GEMINI_API_KEY"] = "gm-test"
        FakeClient.reset([
            429,
            {"choices": [{"message": {"content": "gemini says"}}]}])
        out = vision.describe_data(self.env.settings, env, PNG, "x.png")
        self.assertIn("gemini says", out)
        self.assertEqual(
            FakeClient.calls[1]["base"],
            "https://generativelanguage.googleapis.com/v1beta/openai")
        # second call: groq is cooling, goes straight to gemini
        FakeClient.reset(
            [{"choices": [{"message": {"content": "again"}}]}])
        vision.describe_data(self.env.settings, env, PNG, "x.png")
        self.assertEqual(len(FakeClient.calls), 1)
        self.assertIn("generativelanguage", FakeClient.calls[0]["base"])

    def test_oversize_and_bad_bytes_no_call(self):
        FakeClient.reset()
        big = b"x" * (self.env.settings.vision_max_bytes + 1)
        self.assertEqual(self._caption(big), "")
        self.assertEqual(
            vision.describe_data(self.env.settings, self.env.env,
                                 b"not an image", "x.bin"), "")
        self.assertEqual(FakeClient.calls, [])


class TestDisabled(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(GROQ_API_KEY="gk", PA_VISION="false")
        self.env.conn().close()
        self._cli = vision.APIClient
        vision.APIClient = FakeClient
        FakeClient.reset()

    def tearDown(self):
        vision.APIClient = self._cli

    def test_off_means_no_calls(self):
        out = vision.describe_data(self.env.settings, self.env.env,
                                   PNG, "x.png")
        self.assertEqual(out, "")
        self.assertEqual(FakeClient.calls, [])


class TestUnconfigured(unittest.TestCase):
    def test_no_keys_means_empty_caption(self):
        env = TmpEnv()
        out = vision.describe_data(env.settings, env.env, PNG, "x.png")
        self.assertEqual(out, "")


class TestLook(Base):
    def test_sandbox_path_escape_refused(self):
        out = vision.look(self.env.settings, self.env.env,
                          "/etc/passwd")
        self.assertEqual(out["error"], "bad_arguments")

    def test_sandbox_file_caption(self):
        FakeClient.reset([{"choices": [{"message": {
            "content": "blue square"}}]}])
        sb = self.env.settings.chat_sandbox
        os.makedirs(sb, exist_ok=True)
        path = os.path.join(sb, "sq.png")
        with open(path, "wb") as f:
            f.write(PNG)
        out = vision.look(self.env.settings, self.env.env, path)
        self.assertIn("blue square", out["caption"])

    def test_url_goes_through_egress(self):
        seen = {}

        def fake_get(url, s=None, **kw):
            seen.update(url=url, allowed=kw.get("allowed"))
            return {"body": PNG}
        orig = vision.egress.guarded_get
        vision.egress.guarded_get = fake_get
        try:
            FakeClient.reset([{"choices": [{"message": {
                "content": "from the web"}}]}])
            out = vision.look(self.env.settings, self.env.env,
                              "https://example.com/x.png")
        finally:
            vision.egress.guarded_get = orig
        self.assertIn("from the web", out["caption"])
        self.assertIn("image/png", seen["allowed"])

    def test_disabled_flag(self):
        env = TmpEnv(PA_VISION="false", GROQ_API_KEY="gk")
        out = vision.look(env.settings, env.env, "x.png")
        self.assertEqual(out["error"], "not_configured")


if __name__ == "__main__":
    unittest.main()

"""pa provider-check conformance: models, text completion, tool round trip."""

import unittest

from pa import provider_check
from tests.helpers import Fixture, TmpEnv


class TestProviderCheck(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.env = TmpEnv(LLM_BASE_URL=self.fx.base_url,
                          LLM_MODEL="fixture-model")

    def tearDown(self):
        self.fx.close()

    def test_full_pass(self):
        self.fx.push_chat(self.fx.chat_text("pong"))
        self.fx.push_chat(self.fx.chat_tool("echo_check", {"x": 1},
                                            call_id="c1"))
        self.fx.push_chat(self.fx.chat_text("tool done"))
        rep = provider_check.run_check(self.env.settings)
        self.assertTrue(rep["ok"], rep)
        names = [c["name"] for c in rep["checks"]]
        self.assertIn("models", names)
        self.assertIn("chat", names)
        self.assertIn("tool_call", names)
        self.assertTrue(all(c["ok"] for c in rep["checks"]))
        models = [c for c in rep["checks"] if c["name"] == "models"][0]
        self.assertIn("fixture-model", models["detail"])

    def test_missing_models_endpoint_still_passes(self):
        class NoModels(Fixture):
            pass
        self.fx.models = None  # /v1/models -> 404
        self.fx.push_chat(self.fx.chat_text("pong"))
        self.fx.push_chat(self.fx.chat_tool("echo_check", {}, call_id="c1"))
        self.fx.push_chat(self.fx.chat_text("done"))
        rep = provider_check.run_check(self.env.settings)
        models = [c for c in rep["checks"] if c["name"] == "models"][0]
        self.assertTrue(models["ok"])           # optional endpoint
        self.assertIn("does not expose", models["detail"])
        self.assertTrue(rep["ok"])

    def test_failing_endpoint_fails_cleanly(self):
        self.fx.push_chat((500, {}, {"error": "boom"}))
        rep = provider_check.run_check(self.env.settings)
        self.assertFalse(rep["ok"])
        self.assertTrue(any(not c["ok"] for c in rep["checks"]))

    def test_reasoning_model_empty_length_explained(self):
        # gpt-oss-style endpoint: HTTP 200, empty content, whole budget
        # spent on hidden reasoning — the detail must name
        # finish_reason=length, not a bare "empty completion".
        self.fx.push_chat({"choices": [{
            "message": {"role": "assistant", "content": ""},
            "finish_reason": "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 512}})
        rep = provider_check.run_check(self.env.settings)
        chat = [c for c in rep["checks"] if c["name"] == "chat"][0]
        self.assertFalse(chat["ok"])
        self.assertIn("finish_reason=length", chat["detail"])
        self.assertGreaterEqual(
            self.fx.calls[0]["max_completion_tokens"], 400)


if __name__ == "__main__":
    unittest.main()

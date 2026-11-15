"""GitHub CI/PR webhook events -> owner-DM cards, no agent turn."""

import json
import unittest

from pa import ghcards, webhooks
from pa.webapp import App
from tests.helpers import TmpEnv, WSGIClient


def _wf(conclusion="failure"):
    return json.dumps({
        "action": "completed",
        "workflow_run": {
            "name": "tests", "conclusion": conclusion,
            "head_branch": "main",
            "head_sha": "abc1234567890",
            "html_url": "https://gh.example/run/1",
            "actor": {"login": "devin"}},
        "repository": {"full_name": "me/repo",
                       "html_url": "https://gh.example/me/repo"},
    }).encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.conn = self.env.conn()
        self.app = App(self.env.settings, env=self.env.env)
        self.c = WSGIClient(self.app)
        self.secret = webhooks.create(self.conn, "github")["secret"]

    def _post(self, body, path="/hooks/github", sig=None,
              headers=None):
        h = {"X-Hub-Signature-256": sig or webhooks.signature(
            self.secret, body)}
        h.update(headers or {})
        return self.c.request("POST", path, data=body,
                              ctype="application/json", headers=h)

    def _inbox(self):
        return self.conn.execute(
            "SELECT source, prompt, status FROM chat_inbox"
            " ORDER BY id").fetchall()


class TestCards(Base):
    def test_workflow_run_posts_card_no_agent_turn(self):
        status, _, body = self._post(
            _wf(), headers={"X-GitHub-Event": "workflow_run"})
        self.assertTrue(status.startswith("200"))
        self.assertTrue(json.loads(body)["card"])
        rows = self._inbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "ghcard")   # not webhook:
        self.assertIn("CI failure", rows[0]["prompt"])
        self.assertIn("me/repo", rows[0]["prompt"])
        self.assertIn("abc1234", rows[0]["prompt"])
        self.assertIn("devin", rows[0]["prompt"])
        self.assertNotIn("UNTRUSTED", rows[0]["prompt"])

    def test_payload_detected_without_header(self):
        status, _, _ = self._post(_wf("success"))
        rows = self._inbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "ghcard")
        self.assertIn("CI success", rows[0]["prompt"])

    def test_pull_request_card(self):
        body = json.dumps({
            "action": "opened",
            "pull_request": {
                "number": 17, "title": "final-5 branch",
                "html_url": "https://gh.example/pr/17",
                "head": {"sha": "feedbeef99"},
                "user": {"login": "devin"}},
            "repository": {"full_name": "me/repo"},
        }).encode()
        status, _, resp = self._post(
            body, headers={"X-GitHub-Event": "pull_request"})
        rows = self._inbox()
        self.assertEqual(rows[0]["source"], "ghcard")
        self.assertIn("PR opened", rows[0]["prompt"])
        self.assertIn("#17", rows[0]["prompt"])

    def test_non_github_body_ignored_not_crash(self):
        status, _, body = self._post(b'{"random": "payload"}')
        self.assertTrue(status.startswith("200"))
        self.assertTrue(json.loads(body)["ignored"])
        self.assertEqual(self._inbox(), [])

    def test_bad_signature_still_401(self):
        status, _, _ = self._post(_wf(), sig="sha256=deadbeef")
        self.assertTrue(status.startswith("401"))
        self.assertEqual(self._inbox(), [])

    def test_agent_flag_restores_generic_path(self):
        self.env.settings.github_hook_agent = True
        app = App(self.env.settings, env=self.env.env)
        c = WSGIClient(app)
        status, _, body = c.request(
            "POST", "/hooks/github", data=_wf(),
            ctype="application/json",
            headers={"X-Hub-Signature-256": webhooks.signature(
                self.secret, _wf())})
        self.assertTrue(json.loads(body)["queued"])
        rows = self._inbox()
        self.assertEqual(rows[0]["source"], "webhook:github")
        self.assertIn("UNTRUSTED", rows[0]["prompt"])

    def test_other_hooks_unaffected(self):
        webhooks.create(self.conn, "gh")
        secret2 = webhooks.create(self.conn, "gh")["secret"]
        status, _, body = self.c.request(
            "POST", "/hooks/gh", data=_wf(), ctype="application/json",
            headers={"X-PA-Signature": webhooks.signature(
                secret2, _wf())})
        self.assertTrue(json.loads(body)["queued"])
        row = self.conn.execute(
            "SELECT source FROM chat_inbox").fetchone()
        self.assertEqual(row["source"], "webhook:gh")


class TestRender(unittest.TestCase):
    def test_check_suite(self):
        text = ghcards.render("check_suite", {
            "check_suite": {"conclusion": "failure",
                            "head_branch": "main",
                            "head_sha": "abc1234xx",
                            "app": {"slug": "ci-app"}},
            "repository": {"full_name": "me/r",
                           "html_url": "https://x/r"}})
        self.assertIn("checks failure", text)
        self.assertIn("ci-app", text)

    def test_issue_comment_only_on_pr(self):
        base = {"issue": {"number": 3, "title": "t"},
                "comment": {"user": {"login": "u"},
                            "html_url": "https://x"},
                "repository": {"full_name": "me/r"}}
        self.assertEqual(ghcards.render("issue_comment", base), "")
        base["issue"]["pull_request"] = {"url": "x"}
        self.assertIn("PR comment", ghcards.render(
            "issue_comment", base))

    def test_weird_payload_no_crash(self):
        self.assertEqual(ghcards.render("workflow_run", {}), "")
        self.assertEqual(ghcards.render("workflow_run",
                                        {"workflow_run": None}), "")
        self.assertEqual(ghcards.detect("", b"not json"), "")
        self.assertEqual(ghcards.detect("", b"[]"), "")


class TestHelper(unittest.TestCase):
    def test_hooks_add_github_shows_steps_once(self):
        env = TmpEnv()
        conn = env.conn()
        from pa import discordbot
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False, engine=object())
        out = bot._hooks(conn, ["add", "github"])
        self.assertIn("shown once", out)
        self.assertIn("GitHub setup", out)
        self.assertIn("/hooks/github", out)
        listed = bot._hooks(conn, [])
        self.assertIn("github", listed)
        self.assertNotIn("Secret:", listed)   # list never re-shows


if __name__ == "__main__":
    unittest.main()

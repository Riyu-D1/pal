"""End-to-end: scheduled daily email summary, fixture-tested.

fake LLM endpoint + synthetic Gmail inbox + sample vault -> one
read-only summary task -> Discord REST stub gets the delivery.

No live provider/Gmail/Discord anywhere in this path.
"""

import json
import os
import tempfile
import unittest

from pa import discordbot, scheduler as sch, worker as workmod
from tests.helpers import Fixture, TmpEnv, wait_for


class TestDailySummaryE2E(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.vault_dir = tempfile.mkdtemp(prefix="pa-e2e-vault-")
        with open(os.path.join(self.vault_dir, "trip.md"), "w") as f:
            f.write("# Trip\nTrain departs 09:15 platform 4.\n")
        self.env = TmpEnv(
            PA_OLLAMA_BASE_URL=self.fx.base_url,
            GMAIL_API_BASE=self.fx.web_base,
            GMAIL_TOKEN_URL=self.fx.web_base + "/token",
            VAULT_PATH=self.vault_dir,
            DISCORD_API_BASE=self.fx.web_base,
            DISCORD_CHANNELS='{"email": "chan-email"}',
        )
        self._env_before = dict(os.environ)
        os.environ.update({
            "GMAIL_CLIENT_ID": "cid",
            "GMAIL_CLIENT_SECRET": "csec",
            "GMAIL_REFRESH_TOKEN": "rtok",
            "DISCORD_BOT_TOKEN": "fake-bot-token",
        })
        self.aid = self.env.add_assistant(
            name="email",
            tools=["email_list", "email_read", "vault_search",
                   "vault_read", "memory_search"])
        self.conn = self.env.conn()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_before)
        self.fx.close()
        self.conn.close()

    def _gmail_queue(self):
        self.fx.push_post({"access_token": "at1", "expires_in": 3600})
        self.fx.push_gmail({"messages": [{"id": "m1"}, {"id": "m2"}]})
        for subj in ("Booking confirmed", "Newsletter"):
            self.fx.push_gmail({
                "id": "m1", "snippet": "snippet for " + subj,
                "labelIds": ["INBOX", "UNREAD"],
                "payload": {"headers": [
                    {"name": "Subject", "value": subj},
                    {"name": "From", "value": "s@x.test"},
                    {"name": "Date", "value": "Mon, 5 Jan 2026"}]}})

    def test_daily_summary_flow(self):
        # 1) the shipped job is DISABLED until owner config exists;
        #    the owner triggers it manually here (fire_now).
        enabled = sch.list_schedules(self.conn)
        job = [s for s in enabled if s["name"] == "daily-email-summary"]
        self.assertEqual(len(job), 1)
        self.assertFalse(job[0]["enabled"])
        res = sch.fire_now(self.conn, "daily-email-summary")
        self.assertTrue(res["fired"])
        tid = res["task_id"]

        # 2) provider script: list unread -> search vault -> finish
        self._gmail_queue()
        self.fx.push_chat(self.fx.chat_tool(
            "email_list",
            {"query": "is:unread newer_than:1d"}, call_id="c1"))
        self.fx.push_chat(self.fx.chat_tool(
            "vault_search", {"query": "train"}, call_id="c2"))
        self.fx.push_chat(self.fx.chat_tool(
            "finish",
            {"claims": [{"text": "2 unread messages", "source_ids": []},
                        {"text": "trip note found", "source_ids": []}],
             "summary": "Inbox: 2 unread. Vault: trip note found."},
            call_id="c3"))

        # 3) worker runs the task end-to-end
        w = workmod.Worker(self.env.db_path, self.env.settings)
        wait_for(lambda: self._task_state(tid) == "succeeded"
                 or not w.run_once(), timeout=30)
        state, result = self._task_state(tid, result=True)
        self.assertEqual(state, "succeeded")
        self.assertIn("2 unread", result["summary"])

        # 4) the email/vault tools ran read-only against the fakes
        paths = [c["path"] for c in self.fx.calls
                 if isinstance(c, dict)
                 and str(c.get("path", "")).startswith("/gmail/")]
        self.assertTrue(any("messages?" in p for p in paths))
        notes = self.conn.execute(
            "SELECT COUNT(*) c FROM vault_notes").fetchone()["c"]
        self.assertGreaterEqual(notes, 1)
        runs = self.conn.execute(
            "SELECT status FROM schedule_runs WHERE task_id=?",
            (tid,)).fetchall()
        self.assertEqual(runs[0]["status"], "manual")

        # 5) Discord delivery: routed to the assistant's channel
        bot = discordbot.DiscordBot(self.env.settings, env=os.environ,
                                    notifier=False)
        self.fx.push_post({"ok": True})
        bot.tick_notifications()
        posts = [c for c in self.fx.calls
                 if c.get("path") == "/channels/chan-email/messages"]
        self.assertEqual(len(posts), 1)
        body = posts[0]["body"]
        self.assertIn("Daily email summary", body["content"])
        self.assertIn("2 unread", body["content"])
        # dedup: second tick posts nothing more
        self.fx.push_post({"ok": True})
        bot.tick_notifications()
        posts = [c for c in self.fx.calls
                 if c.get("path") == "/channels/chan-email/messages"]
        self.assertEqual(len(posts), 1)

    def _task_state(self, tid, result=False):
        row = self.conn.execute(
            "SELECT state, result_json FROM tasks WHERE id=?",
            (tid,)).fetchone()
        if result:
            return row["state"], json.loads(row["result_json"] or "{}")
        return row["state"]


if __name__ == "__main__":
    unittest.main()

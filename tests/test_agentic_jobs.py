"""Agentic jobs + multi-account connections.

- /connect autocomplete merges built-in MCP servers with the live
  Composio toolkit catalog (cached briefly — not one API call per
  keystroke).
- A second connection to the same app carries an account label
  ('gmail@pal'); tools route by longest provider prefix so
  'gmail@pal.GMAIL_SEND_EMAIL' hits pal's mailbox, not the owner's.
- email_owner sends ONLY to PA_OWNER_EMAIL from the pal mailbox and
  never asks for approval; every other send/write stays gated.
- monthly schedules clamp day_of_month to the month's real last day
  and keep local-time/DST semantics.
- Unattended job runs: read-only tools + vault writes + email_owner
  run free; other gated actions become 'job_action' approvals instead
  of blocking.
- Jobs are created/listed/disabled from chat tools and /jobs.
"""

import datetime
import json
import os
import tempfile
import unittest

from pa import approvals, chattools, composio as composiomod
from pa import connectflows, discordbot, scheduler as sch
from tests.helpers import TmpEnv
from tests.test_connections import FakeComposio, SECRETS

UTC = datetime.timezone.utc


def _now(y, m, d, hh=0, mm=0):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=UTC)


def ctx_for(env, assistant=None, unattended=False, **kw):
    conn = env.conn()
    return chattools.ChatToolContext(
        conn, env.settings, assistant, "chan-1", env=env.env,
        unattended=unattended, **kw)


# ---------------------------------------------------------------------------
# /connect autocomplete — live Composio catalog merged with built-ins

class TestToolkitAutocomplete(unittest.TestCase):
    def setUp(self):
        self.cx = FakeComposio()
        self.addCleanup(self.cx.close)
        composiomod._TOOLKIT_CACHE.clear()
        self.addCleanup(composiomod._TOOLKIT_CACHE.clear)
        self.env = TmpEnv(
            COMPOSIO_API_KEY="ck-test",
            COMPOSIO_API_BASE=self.cx.base,
            **SECRETS)
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False)

    def test_merges_builtin_and_live_catalog(self):
        choices = self.bot._connect_choices("")
        values = {v for _n, v in choices}
        # built-in catalog entries stay
        self.assertIn("notion", values)
        self.assertIn("url:https://", values)
        self.assertIn("google", values)
        # live Composio toolkits show up too
        self.assertIn("trello", values)
        self.assertIn("airtable", values)
        # one catalog call served all of it
        self.assertEqual(len(self.cx.toolkit_queries), 1)

    def test_search_reaches_the_api_and_results_dedupe(self):
        choices = self.bot._connect_choices("trel")
        values = {v for _n, v in choices}
        self.assertIn("trello", values)
        self.assertNotIn("spotify", values)
        self.assertEqual(
            self.cx.toolkit_queries[0]["search"], ["trel"])

    def test_keystrokes_hit_the_brief_cache(self):
        self.bot._connect_choices("trel")
        self.bot._connect_choices("trel")
        self.bot._connect_choices("trel")
        self.assertEqual(len(self.cx.toolkit_queries), 1)

    def test_api_down_falls_back_to_common_toolkits(self):
        self.cx.close()  # nothing listening anymore
        env = TmpEnv(
            COMPOSIO_API_KEY="ck-test",
            COMPOSIO_API_BASE=f"http://127.0.0.1:{self.cx.port}/api/v3.1",
            **SECRETS)
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False)
        values = {v for _n, v in bot._connect_choices("")}
        self.assertIn("notion", values)      # built-ins still offered
        self.assertIn("gmail", values)       # static fallback
        self.assertIn("trello", values)


# ---------------------------------------------------------------------------
# multiple accounts per app

class TestAccountLabels(unittest.TestCase):
    def test_split_and_provider(self):
        self.assertEqual(connectflows.split_account("gmail@pal"),
                         ("gmail", "pal"))
        self.assertEqual(connectflows.split_account("gmail"),
                         ("gmail", "owner"))
        self.assertEqual(connectflows.split_account("url:host@x"),
                         ("url:host@x", "owner"))
        self.assertEqual(connectflows.provider_for("gmail", "pal"),
                         "gmail@pal")
        self.assertEqual(connectflows.provider_for("gmail", ""),
                         "gmail")

    def test_label_normalization(self):
        self.assertEqual(connectflows._clean_account("PAL"), "pal")
        self.assertEqual(connectflows._clean_account("owner"), "")
        self.assertEqual(connectflows._clean_account(""), "")
        self.assertEqual(connectflows._clean_account("default"), "")
        with self.assertRaises(connectflows.ConnectError):
            connectflows._clean_account("bad label!")

    def test_composio_connect_with_label(self):
        cx = FakeComposio()
        self.addCleanup(cx.close)
        env = TmpEnv(COMPOSIO_API_KEY="ck-test",
                     COMPOSIO_API_BASE=cx.base, **SECRETS)
        conn = env.conn()
        try:
            res = connectflows.start_connect(
                conn, env.settings, env.env, "gmail", account="pal")
            self.assertEqual(res["provider"], "gmail@pal")
            self.assertEqual(res["meta"]["toolkit"], "gmail")
            connectflows.activate_composio(
                conn, "gmail@pal", res["meta"])
            row = connectflows.get(conn, "gmail@pal")
            self.assertEqual(row["account"], "pal")
            self.assertEqual(row["status"], "active")
            # toolkit stays the base slug — tools resolve to the
            # labelled provider
            tools = connectflows.list_tools(
                conn, env.settings, env.env, row)
            self.assertIn("GMAIL_SEND_EMAIL",
                          {t["name"] for t in tools})
            self.assertIn({"toolkit_slug": ["gmail"], "limit": ["200"]},
                          cx.tool_queries)
        finally:
            conn.close()

    def test_labelled_tool_routes_to_labelled_row(self):
        env = TmpEnv(**SECRETS)
        conn = env.conn()
        try:
            connectflows.activate_composio(
                conn, "gmail", {"connected_account_id": "ca-owner",
                                "toolkit": "gmail"})
            connectflows.activate_composio(
                conn, "gmail@pal", {"connected_account_id": "ca-pal",
                                    "toolkit": "gmail"})
            row, tool = connectflows.find_connection_for_tool(
                conn, "gmail@pal.GMAIL_SEND_EMAIL")
            self.assertEqual(row["provider"], "gmail@pal")
            self.assertEqual(tool, "GMAIL_SEND_EMAIL")
            row, tool = connectflows.find_connection_for_tool(
                conn, "gmail.GMAIL_FETCH_EMAILS")
            self.assertEqual(row["provider"], "gmail")
            self.assertEqual(tool, "GMAIL_FETCH_EMAILS")
        finally:
            conn.close()

    def test_legacy_rows_default_to_owner(self):
        env = TmpEnv(**SECRETS)
        conn = env.conn()
        try:
            # pre-migration-8 shape: no account column set -> 'owner'
            row = connectflows.get(conn, "weekly-brain-update")
            self.assertIsNone(row)
            conn.execute(
                "INSERT INTO connections(provider, kind, status,"
                " created_at, updated_at) VALUES('notion','mcp',"
                " 'active','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
            row = connectflows.get(conn, "notion")
            self.assertEqual(row["account"], "owner")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# email_owner — fixed recipient, no approval

class TestEmailOwner(unittest.TestCase):
    def setUp(self):
        self.cx = FakeComposio()
        self.addCleanup(self.cx.close)
        self.env = TmpEnv(
            COMPOSIO_API_KEY="ck-test",
            COMPOSIO_API_BASE=self.cx.base,
            PA_OWNER_EMAIL="owner@example.com",
            **SECRETS)

    def _ctx(self, conn, **kw):
        return chattools.ChatToolContext(
            conn, self.env.settings, None, "chan-1", env=self.env.env,
            **kw)

    def _pal_ctx(self, conn, unattended=False):
        connectflows.activate_composio(
            conn, "gmail@pal", {"connected_account_id": "ca-pal",
                                "toolkit": "gmail"})
        return self._ctx(conn, unattended=unattended,
                         request_approval=lambda n, a, i: "denied")

    def _executed(self):
        out = []
        for c in self.cx.calls:
            if "execute" in c["path"]:
                out.append((c["path"].rsplit("/", 1)[-1], c["body"]))
        return out

    def test_sends_to_owner_without_approval(self):
        conn = self.env.conn()
        try:
            ctx = self._pal_ctx(conn)
            r = chattools.run(ctx, "email_owner",
                              {"subject": "digest", "body": "hi"})
            self.assertEqual(r["sent_to_owner"], "owner@example.com")
            slug, body = self._executed()[0]
            self.assertEqual(slug, "GMAIL_SEND_EMAIL")
            self.assertEqual(body["arguments"]["recipient_email"],
                             "owner@example.com")
            self.assertEqual(
                body["connected_account_id"], "ca-pal")
            # zero approval rows — this send never asks
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) c FROM approvals").fetchone()["c"], 0)
        finally:
            conn.close()

    def test_cannot_redirect_to_anyone_else(self):
        """Even when the model smuggles a 'to'/'recipient' arg, the
        recipient is always PA_OWNER_EMAIL — the tool schema has no
        recipient field at all."""
        conn = self.env.conn()
        try:
            ctx = self._pal_ctx(conn)
            r = chattools.run(
                ctx, "email_owner",
                {"subject": "s", "body": "b",
                 "to": "attacker@evil.example",
                 "recipient_email": "attacker@evil.example"})
            self.assertEqual(r["sent_to_owner"], "owner@example.com")
            _slug, body = self._executed()[0]
            self.assertEqual(body["arguments"]["recipient_email"],
                             "owner@example.com")
        finally:
            conn.close()

    def test_needs_owner_email_and_pal_mailbox(self):
        env = TmpEnv(COMPOSIO_API_KEY="ck-test",
                     COMPOSIO_API_BASE=self.cx.base, **SECRETS)
        conn = env.conn()
        try:
            # no PA_OWNER_EMAIL at all
            ctx = chattools.ChatToolContext(
                conn, env.settings, None, "c", env=env.env)
            connectflows.activate_composio(
                conn, "gmail@pal", {"connected_account_id": "ca-pal",
                                    "toolkit": "gmail"})
            r = chattools.run(ctx, "email_owner",
                              {"subject": "s", "body": "b"})
            self.assertEqual(r["error"], "owner_email_not_set")
            # owner email set but no pal mailbox
            conn2 = self.env.conn()
            ctx2 = self._ctx(conn2)
            r = chattools.run(ctx2, "email_owner",
                              {"subject": "s", "body": "b"})
            self.assertEqual(r["error"], "pal_mailbox_not_connected")
            conn2.close()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# monthly schedules

class TestMonthly(unittest.TestCase):
    def _sched(self, dom, tod="09:00", tz="Europe/London"):
        return {"timezone": tz, "time_of_day": tod, "freq": "monthly",
                "day_of_week": None, "day_of_month": dom, "id": 1,
                "payload_json": "{}"}

    def test_day31_clamps_to_feb28(self):
        s = self._sched(31)
        key = sch.latest_occurrence(s, _now(2026, 2, 20, 12))
        self.assertEqual(key, "2026-01-31T09:00")
        key = sch.latest_occurrence(s, _now(2026, 3, 1, 12))
        self.assertEqual(key, "2026-02-28T09:00")

    def test_leap_day(self):
        s = self._sched(31)
        key = sch.latest_occurrence(s, _now(2024, 3, 5, 12))
        self.assertEqual(key, "2024-02-29T09:00")

    def test_rollover_to_previous_year(self):
        s = self._sched(15)
        key = sch.latest_occurrence(s, _now(2026, 1, 10, 12))
        self.assertEqual(key, "2025-12-15T09:00")

    def test_before_slot_same_month_uses_last_month(self):
        s = self._sched(10, tod="18:00")
        # 09:00 on the 10th — today's 18:00 hasn't happened yet
        key = sch.latest_occurrence(
            s, _now(2026, 4, 10, 12))
        self.assertEqual(key, "2026-03-10T18:00")

    def test_dst_local_time_preserved(self):
        s = self._sched(15, tod="07:30")
        key = sch.latest_occurrence(s, _now(2026, 1, 20, 12))
        self.assertEqual(key, "2026-01-15T07:30")
        inst = sch.occurrence_instant(s, key)
        self.assertEqual((inst.hour, inst.minute), (7, 30))  # GMT
        key = sch.latest_occurrence(s, _now(2026, 7, 20, 12))
        inst = sch.occurrence_instant(s, key)
        self.assertEqual((inst.hour, inst.minute), (6, 30))  # BST

    def test_validation(self):
        env = TmpEnv(**SECRETS)
        conn = env.conn()
        try:
            payload = {"prompt": "x", "channel_id": "c"}
            with self.assertRaises(ValueError):
                sch.add_schedule(conn, "m1", "monthly", "09:00",
                                 "Europe/London", payload,
                                 kind="agent_job")
            with self.assertRaises(ValueError):
                sch.add_schedule(conn, "m2", "monthly", "09:00",
                                 "Europe/London", payload,
                                 day_of_month=0, kind="agent_job")
            with self.assertRaises(ValueError):
                sch.add_schedule(conn, "m3", "monthly", "09:00",
                                 "Europe/London", payload,
                                 day_of_month=32, kind="agent_job")
            sid = sch.add_schedule(conn, "ok", "monthly", "09:00",
                                   "Europe/London", payload,
                                   day_of_month=1, kind="agent_job")
            self.assertIsInstance(sid, int)
        finally:
            conn.close()

    def test_monthly_agent_job_queues_inbox(self):
        env = TmpEnv(**SECRETS)
        conn = env.conn()
        try:
            sch.add_schedule(conn, "m-job", "monthly", "09:00",
                             "Europe/London",
                             {"prompt": "do the thing", "channel_id": ""},
                             day_of_month=15, enabled=True,
                             kind="agent_job")
            out = sch.tick(conn, env.settings, _now(2026, 1, 15, 12))
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["detail"], "job queued")
            row = conn.execute("SELECT * FROM chat_inbox").fetchone()
            self.assertEqual(row["source"], "job:m-job")
            self.assertIn("do the thing", row["prompt"])
            # reminder schedules keep their distinct source
            sch.add_schedule(conn, "rem", "daily", "09:00",
                             "Europe/London",
                             {"prompt": "r", "channel_id": "c1"},
                             enabled=True, kind="chat_prompt")
            sch.tick(conn, env.settings, _now(2026, 1, 15, 13))
            srcs = {r["source"] for r in
                    conn.execute("SELECT source FROM chat_inbox")}
            self.assertIn("schedule:rem", srcs)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# unattended run policy

class TestUnattendedPolicy(unittest.TestCase):
    def setUp(self):
        self.cx = FakeComposio()
        self.addCleanup(self.cx.close)
        self.env = TmpEnv(
            COMPOSIO_API_KEY="ck-test",
            COMPOSIO_API_BASE=self.cx.base,
            PA_OWNER_EMAIL="owner@example.com",
            **SECRETS)
        self.vault = tempfile.mkdtemp(prefix="pa-vault-")
        self.env.settings.vault_path = self.vault

    def _ctx(self, conn, unattended=True):
        connectflows.activate_composio(
            conn, "gmail", {"connected_account_id": "ca-owner",
                            "toolkit": "gmail"})
        connectflows.activate_composio(
            conn, "gmail@pal", {"connected_account_id": "ca-pal",
                                "toolkit": "gmail"})
        return chattools.ChatToolContext(
            conn, self.env.settings, None, "chan-1",
            env=self.env.env, unattended=unattended,
            request_approval=lambda n, a, i: "denied")

    def _pending(self, conn):
        return conn.execute(
            "SELECT * FROM approvals WHERE kind='job_action'"
            " AND status='pending'").fetchall()

    def test_read_runs_vault_runs_email_owner_runs_write_queues(self):
        conn = self.env.conn()
        try:
            ctx = self._ctx(conn)
            # read-only app tool: executes immediately
            r = chattools.run(ctx, "gmail.GMAIL_FETCH_EMAILS",
                              {"max_results": 1})
            self.assertIn("result", r)
            self.assertEqual(ctx.pending, [])
            # vault write: allowed, file on disk, no approval row
            r = chattools.run(ctx, "vault_write",
                              {"path": "pal/weekly/x.md",
                               "content": "# w\nhi"})
            self.assertTrue(r.get("ok", True), r)
            self.assertTrue(os.path.exists(
                os.path.join(self.vault, "pal/weekly/x.md")))
            self.assertEqual(ctx.pending, [])
            # email_owner: allowed, went out via composio pal account
            r = chattools.run(ctx, "email_owner",
                              {"subject": "s", "body": "b"})
            self.assertEqual(r["sent_to_owner"], "owner@example.com")
            self.assertEqual(ctx.pending, [])
            # gated write (send_email): queued, NOT executed, approval
            # row is a job_action
            gmail_calls_before = len([
                c for c in self.cx.calls if "execute" in c["path"]])
            r = chattools.run(ctx, "send_email",
                              {"to": "x@y.z", "subject": "s",
                               "body": "b"})
            self.assertTrue(r["queued"])
            self.assertEqual(len(ctx.pending), 1)
            self.assertEqual(ctx.pending[0]["tool"], "send_email")
            self.assertEqual(len(self._pending(conn)), 1)
            self.assertEqual(
                len([c for c in self.cx.calls
                     if "execute" in c["path"]]),
                gmail_calls_before)  # nothing executed for send_email
            # gated app tool likewise queues
            r = chattools.run(ctx, "gmail.GMAIL_SEND_EMAIL",
                              {"recipient_email": "x@y.z",
                               "subject": "s", "body": "b"})
            self.assertTrue(r["queued"])
            self.assertEqual(len(ctx.pending), 2)
            self.assertEqual(len(self._pending(conn)), 2)
        finally:
            conn.close()

    def test_execute_approved_runs_held_action_once(self):
        conn = self.env.conn()
        try:
            ctx = self._ctx(conn)
            r = chattools.run(ctx, "gmail.GMAIL_SEND_EMAIL",
                              {"recipient_email": "x@y.z",
                               "subject": "s", "body": "b"})
            self.assertTrue(r["queued"])
            # owner approves -> execute_approved runs it for real
            out = chattools.execute_approved(
                ctx, "gmail.GMAIL_SEND_EMAIL",
                {"recipient_email": "x@y.z", "subject": "s",
                 "body": "b"})
            self.assertEqual(out["result"]["executed"],
                             "GMAIL_SEND_EMAIL")
        finally:
            conn.close()

    def test_interactive_turn_still_waits(self):
        conn = self.env.conn()
        try:
            ctx = self._ctx(conn, unattended=False)
            r = chattools.run(ctx, "send_email",
                              {"to": "x@y.z", "subject": "s",
                               "body": "b"})
            self.assertEqual(r["error"], "not_approved")
            self.assertEqual(r["decision"], "denied")
            rows = conn.execute(
                "SELECT kind FROM approvals").fetchall()
            self.assertEqual({r["kind"] for r in rows},
                             {"chat_action"})
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# job management via chat tools + /jobs

class TestJobTools(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(**SECRETS)

    def _ctx(self, conn):
        return chattools.ChatToolContext(
            conn, self.env.settings, None, "chan-1", env=self.env.env)

    def test_create_list_disable_enable(self):
        conn = self.env.conn()
        try:
            ctx = self._ctx(conn)
            r = chattools.run(ctx, "job_add", {
                "prompt": "go through my emails and update my brain",
                "when": "weekly sun 18:00",
                "name": "weekly-brain"})
            self.assertEqual(r["scheduled"], "weekly-brain")
            self.assertEqual(r["freq"], "weekly")
            self.assertTrue(r["enabled"])
            row = sch.get_schedule(conn, "weekly-brain")
            self.assertEqual(row["kind"], "agent_job")
            self.assertEqual(row["day_of_week"], 6)

            r = chattools.run(ctx, "job_add", {
                "prompt": "first-of-month tidy",
                "when": "monthly 1 09:00"})
            self.assertEqual(r["freq"], "monthly")
            row = sch.get_schedule(conn, r["scheduled"])
            self.assertEqual(row["day_of_month"], 1)

            out = chattools.run(ctx, "job_list", {})
            names = {j["name"] for j in out["jobs"]}
            self.assertIn("weekly-brain", names)
            self.assertIn("weekly-brain-update", names)  # shipped job
            self.assertTrue(all(
                j["kind"] if "kind" in j else True for j in out["jobs"]))

            r = chattools.run(ctx, "job_set",
                              {"name": "weekly-brain", "enabled": False})
            self.assertFalse(r["enabled"])
            self.assertFalse(
                sch.get_schedule(conn, "weekly-brain")["enabled"])
            r = chattools.run(ctx, "job_set",
                              {"name": "weekly-brain", "enabled": True})
            self.assertTrue(r["enabled"])
        finally:
            conn.close()

    def test_update_existing_job(self):
        conn = self.env.conn()
        try:
            ctx = self._ctx(conn)
            chattools.run(ctx, "job_add", {
                "prompt": "v1", "when": "daily 07:00",
                "name": "tidy"})
            r = chattools.run(ctx, "job_add", {
                "prompt": "v2", "when": "weekly fri 18:00",
                "name": "tidy", "enabled": False})
            self.assertIn("updated", r)
            row = sch.get_schedule(conn, "tidy")
            self.assertEqual(row["freq"], "weekly")
            self.assertEqual(row["day_of_week"], 4)
            self.assertFalse(row["enabled"])
            self.assertIn("v2", row["payload_json"])
        finally:
            conn.close()

    def test_bad_when_and_name_collision(self):
        conn = self.env.conn()
        try:
            ctx = self._ctx(conn)
            r = chattools.run(ctx, "job_add",
                              {"prompt": "p", "when": "someday"})
            self.assertEqual(r["error"], "bad_arguments")
            # a reminder schedule with the same name can't be hijacked
            sch.add_schedule(conn, "rem", "daily", "09:00",
                             "Europe/London",
                             {"prompt": "r", "channel_id": "c"},
                             kind="chat_prompt")
            r = chattools.run(ctx, "job_add", {
                "prompt": "p", "when": "daily 07:00", "name": "rem"})
            self.assertEqual(r["error"], "name_taken")
        finally:
            conn.close()

    def test_shipped_job_is_disabled_agent_job(self):
        conn = self.env.conn()
        try:
            row = sch.get_schedule(conn, "weekly-brain-update")
            self.assertIsNotNone(row)
            self.assertEqual(row["kind"], "agent_job")
            self.assertFalse(row["enabled"])
            self.assertEqual(row["freq"], "weekly")
            payload = json.loads(row["payload_json"])
            self.assertIn("email_owner", payload["prompt"])
            # /jobs command can enable it
            bot = discordbot.DiscordBot(
                self.env.settings, env=self.env.env, notifier=False)
            out = bot._jobs(conn, [])
            self.assertIn("weekly-brain-update", out)
            out = bot._jobs(conn, ["weekly-brain-update", "on"])
            self.assertIn("enabled", out)
            self.assertTrue(
                sch.get_schedule(conn, "weekly-brain-update")["enabled"])
            out = bot._jobs(conn, ["weekly-brain-update", "off"])
            self.assertFalse(
                sch.get_schedule(conn, "weekly-brain-update")["enabled"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

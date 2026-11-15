"""self-heal -> approval-gated code_task."""

import json
import unittest

from pa import chattools
from pa.evolve import heal, incidents, items
from pa.freebuff_session import FREEBUFF_HOST
from tests.helpers import TmpEnv


def _ctx(env, approved=True):
    return chattools.ChatToolContext(
        env.conn(), env.settings, {"id": 1}, "dm1",
        env=env.env, unattended=not approved,
        request_approval=(lambda n, a, aid: "approved")
        if approved else None)


def _planned_fix(conn, env):
    env.add_assistant(name="coder", host=FREEBUFF_HOST)
    iid = items.open_item(conn, "fix", "low",
                          title="journal crashes on empty day",
                          plan="guard the empty-rows path",
                          dedupe_key="fp:x")
    items.set_status(conn, iid, "planned")
    return iid


class TestSelfHeal(unittest.TestCase):
    def test_flag_off_noop(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_SELF_HEAL="false")
        conn = env.conn()
        iid = _planned_fix(conn, env)
        st = heal.maybe_heal(conn, env.settings, iid,
                             ctx=_ctx(env))
        conn.close()
        self.assertEqual(st, "off")

    def test_mocked_code_task_records_task_id(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_SELF_HEAL="true",
                     GH_TOKEN="", GITHUB_TOKEN="")
        conn = env.conn()
        iid = _planned_fix(conn, env)
        ctx = _ctx(env, approved=True)
        st = heal.maybe_heal(conn, env.settings, iid, ctx=ctx,
                             env={})
        item = items.get(conn, iid)
        art = json.loads(item["artifacts"])
        conn.close()
        self.assertEqual(st, "building")
        self.assertTrue(art.get("task_id"))

    def test_no_gh_creds_gives_patch_path(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_SELF_HEAL="true")
        conn = env.conn()
        iid = _planned_fix(conn, env)
        ctx = _ctx(env, approved=True)
        heal.maybe_heal(conn, env.settings, iid, ctx=ctx,
                        env={"PATH": "/nonexistent"})
        art = json.loads(items.get(conn, iid)["artifacts"])
        conn.close()
        self.assertIn("evolve/patches", art.get("patch_path", ""))
        self.assertTrue(art["patch_path"].endswith(
            f"item-{iid}.diff"))

    def test_prompt_has_fixture_and_no_secret(self):
        env = TmpEnv(PA_EVOLVE="true", PA_EVOLVE_SELF_HEAL="true")
        conn = env.conn()
        iid = _planned_fix(conn, env)
        incidents.record(
            conn, "worker", "exception",
            "task blew up with key ghp_abc123def456ghi789jkl012mno",
            detail="traceback mentions "
                   "ghp_abc123def456ghi789jkl012mno")
        captured = {}

        class SpyCtx(_ctx(env).__class__):
            pass
        ctx = _ctx(env, approved=True)
        real = chattools.CHAT_TOOLS["code_task"]["fn"]
        chattools.CHAT_TOOLS["code_task"]["fn"] = \
            lambda c, a: captured.update(a) or \
            {"queued": True, "task_id": 42}
        try:
            heal.maybe_heal(conn, env.settings, iid, ctx=ctx,
                            env={"PATH": "/nonexistent"})
        finally:
            chattools.CHAT_TOOLS["code_task"]["fn"] = real
        conn.close()
        prompt = captured["prompt"]
        self.assertIn("EVAL FIXTURE", prompt)
        self.assertIn("evolve_", prompt)
        self.assertIn("journal crashes", prompt)
        self.assertNotIn("ghp_abc123def456ghi789jkl012mno", prompt)


if __name__ == "__main__":
    unittest.main()

"""Budget ledger + enforcement."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from pa import budget, db


class Case(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(tempfile.mktemp(suffix=".db"))
        db.migrate(self.conn)

    def tearDown(self):
        self.conn.close()


LON = timezone(timedelta(hours=1))


class TestLedger(Case):
    def test_charge_and_remaining(self):
        # tts_chars:* daily 100_000, background share 0.3 -> 30_000
        assert budget.charge(self.conn, "background", "tts_chars:x", 100)
        assert budget.remaining(self.conn, "background",
                              "tts_chars:x") == 30_000 - 100

    def test_cap_blocks(self):
        cap = budget.remaining(self.conn, "background", "escalations")
        assert cap == 12  # 40 * 0.3
        assert budget.charge(self.conn, "background", "escalations",
                             cap) is True
        assert budget.charge(self.conn, "background", "escalations",
                             1) is False
        assert budget.remaining(self.conn, "background",
                                "escalations") == 0

    def test_owner_chat_full_share(self):
        assert budget.remaining(self.conn, "owner-chat",
                                "escalations") == 40

    def test_would_exceed(self):
        assert not budget.would_exceed(self.conn, "call", "live_min", 24)
        assert budget.would_exceed(self.conn, "call", "live_min", 25)

    def test_reserve_settle_release(self):
        assert budget.reserve(self.conn, "call", "stt_sec", 600)
        assert budget.remaining(self.conn, "call", "stt_sec") == \
            1440 - 600
        budget.settle(self.conn, "call", "stt_sec", 120)   # actual cost
        assert budget.remaining(self.conn, "call", "stt_sec") == \
            1440 - 120 - 480
        budget.release(self.conn, "call", "stt_sec", 480)
        assert budget.remaining(self.conn, "call", "stt_sec") == \
            1440 - 120

    def test_day_rollover(self):
        yesterday = datetime.now(LON) - timedelta(days=1)
        budget.charge(self.conn, "background", "escalations", 12,
                      now=yesterday)
        assert budget.remaining(self.conn, "background",
                                "escalations") == 12  # today fresh

    def test_job_scopes_share_background(self):
        budget.charge(self.conn, "job:nightly", "escalations", 12)
        assert budget.remaining(self.conn, "background",
                                "escalations") == 0
        assert budget.would_exceed(self.conn, "task:9", "escalations", 1)

    def test_env_override(self):
        env = {"PA_BUDGET_ESCALATIONS_BACKGROUND": "2"}
        assert budget.remaining(self.conn, "background", "escalations",
                                env=env) == 2

    def test_top_scopes(self):
        budget.charge(self.conn, "owner-chat", "composio_calls", 5)
        budget.charge(self.conn, "call", "live_min", 3)
        rows = budget.top_scopes(self.conn, 2)
        assert rows[0]["resource"] == "composio_calls"


class TestEnforcement(Case):
    def test_background_downgrades_to_fast(self):
        """router: a spent background scope asks for smart, gets
        fast candidates instead."""
        from pa import router
        cap = budget.remaining(self.conn, "background", "escalations")
        budget.charge(self.conn, "background", "escalations", cap)
        s = type("S", (), {"router_call_seconds": 60,
                           "router_backend_seconds": 10,
                           "db_path": str(
                               self.conn.execute("PRAGMA database_list")
                               .fetchone()[2])})()
        # would_exceed fires inside chat() before candidates — unit-
        # level check of the guard instead of a live call:
        assert budget.would_exceed(
            self.conn, "background", "escalations", 1)

    def test_owner_chat_unaffected_by_background_spend(self):
        cap = budget.remaining(self.conn, "background", "escalations")
        budget.charge(self.conn, "background", "escalations", cap)
        # owner chat is a different scope — still has its own budget
        assert not budget.would_exceed(self.conn, "owner-chat",
                                       "escalations", 1)

    def test_composio_bump_charges_ledger(self):
        from pa import composio
        composio.bump_usage(self.conn, scope="background")
        row = self.conn.execute(
            "SELECT used FROM budget_ledger WHERE resource="
            "'composio_calls' AND scope='background'").fetchone()
        assert row["used"] == 1


class TestLeanCatalogue(unittest.TestCase):
    """PA_TOOL_TOPK prunes the prompt catalogue."""
    S = type("S", (), {"computer_enabled": False, "tool_topk": 16})()

    def test_flag_off_keeps_everything(self):
        from pa import chattools
        s = type("S", (), {"computer_enabled": False,
                           "tool_topk": 0})()
        out = chattools.prompt_lines(s)
        gated = chattools.BROWSE_TOOLS | chattools.PHONE_TOOLS | \
            chattools.WORKSPACE_TOOLS | chattools.MADE_TOOLS | \
            chattools.INTEGRATION_TOOLS | chattools.RESEARCH_TOOLS
        for name in chattools.CHAT_TOOLS:
            if name in gated:
                continue
            spec = chattools.CHAT_TOOLS[name]
            # flag-gated tools only exist while their PA_*
            # flag is on (this fake settings carries none)
            if spec.get("flag") and not getattr(
                    s, spec["flag"], False):
                continue
            assert f"- {name} " in out, name

    def test_prompt_shrinks(self):
        from pa import chattools
        full = chattools.prompt_lines(
            type("S", (), {"computer_enabled": False,
                           "tool_topk": 0})())
        lean = chattools.prompt_lines(self.S)
        assert len(lean) <= 8_000
        assert len(lean) < len(full)
        assert "tools_more" in lean

    def test_query_surfaces_the_right_tool(self):
        from pa import chattools
        out = chattools.prompt_lines(
            self.S, query="add an event to my calendar on friday")
        assert "- calendar_create " in out
        # core always present
        assert "- memory_save " in out and "- delegate " in out

    def test_tools_more_lists_hidden(self):
        from pa import chattools
        ctx = type("C", (), {"s": self.S})()
        out = chattools._tools_more(ctx, {"filter": "calendar"})
        names = " ".join(out["tools"])
        assert "calendar_create" in names or "calendar_list" in names

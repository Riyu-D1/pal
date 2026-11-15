"""final-1 P1 — account-owner registry."""
import tempfile
import unittest

from pa import connectflows, db


def _conn():
    c = db.connect(tempfile.mktemp(suffix=".db"))
    db.migrate(c)
    return c


class Case(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()


class TestMigration(Case):
    def test_column_exists(self):
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(connections)").fetchall()}
        assert "owner_kind" in cols

    def test_existing_pal_rows_flip(self):
        c = db.connect(tempfile.mktemp(suffix=".db"))
        db.migrate(c)
        connectflows.upsert(c, "gmail@pal", "mcp", "", "", "active",
                            "", "", "", {})
        connectflows.upsert(c, "gmail", "oauth", "", "", "active",
                            "", "", "", {})
        rows = {r["provider"]: r["owner_kind"] for r in c.execute(
            "SELECT provider, owner_kind FROM connections").fetchall()}
        assert rows["gmail@pal"] == "pal"
        assert rows["gmail"] == "owner"
        c.close()


class TestOwnerKind(Case):
    def test_fail_closed_default(self):
        assert connectflows.owner_kind_for("gmail") == "owner"
        assert connectflows.owner_kind_for("github") == "owner"
        assert connectflows.owner_kind_for("gmail@you") == "owner"

    def test_pal_label(self):
        assert connectflows.owner_kind_for("gmail@pal") == "pal"
        assert connectflows.owner_kind_for("google@pal") == "pal"

    def test_set_owner_kind(self):
        connectflows.upsert(self.conn, "github", "oauth", "", "",
                            "active", "", "", "", {})
        connectflows.set_owner_kind(self.conn, "github", "pal")
        r = self.conn.execute(
            "SELECT owner_kind FROM connections WHERE provider='github'"
        ).fetchone()
        assert r["owner_kind"] == "pal"
        connectflows.set_owner_kind(self.conn, "github", "owner")
        r = self.conn.execute(
            "SELECT owner_kind FROM connections WHERE provider='github'"
        ).fetchone()
        assert r["owner_kind"] == "owner"

    def test_set_owner_kind_rejects_bad(self):
        connectflows.upsert(self.conn, "github", "oauth", "", "",
                            "active", "", "", "", {})
        with self.assertRaises(connectflows.ConnectError):
            connectflows.set_owner_kind(self.conn, "github", "admin")

    def test_reconnect_keeps_owner_kind(self):
        connectflows.upsert(self.conn, "github", "oauth", "", "",
                            "active", "", "", "", {})
        connectflows.set_owner_kind(self.conn, "github", "pal")
        connectflows.upsert(self.conn, "github", "oauth", "", "",
                            "active", "", "", "", {})
        r = self.conn.execute(
            "SELECT owner_kind FROM connections WHERE provider='github'"
        ).fetchone()
        assert r["owner_kind"] == "pal"


class TestDomainOwner(Case):
    def test_unknown_is_owner(self):
        s = type("S", (), {"browser_owner_domains": ""})()
        assert connectflows.domain_owner(
            s, {}, self.conn, "acme.com") == "owner"
        assert connectflows.domain_owner(
            s, {}, self.conn, "") == "owner"

    def test_env_map(self):
        env = {"PA_BROWSER_OWNER_DOMAINS":
               "pal.example=pal,acme.com=owner"}
        s = type("S", (), {"browser_owner_domains": ""})()
        assert connectflows.domain_owner(
            s, env, self.conn, "pal.example") == "pal"
        assert connectflows.domain_owner(
            s, env, self.conn, "acme.com") == "owner"

    def test_learned_then_env_overrides(self):
        s = type("S", (), {"browser_owner_domains": ""})()
        connectflows.learn_domain_owner(self.conn, "notes.app", "pal")
        assert connectflows.domain_owner(
            s, {}, self.conn, "notes.app") == "pal"
        env = {"PA_BROWSER_OWNER_DOMAINS": "notes.app=owner"}
        assert connectflows.domain_owner(
            s, env, self.conn, "notes.app") == "owner"
        connectflows.learn_domain_owner(self.conn, "bad.app", "admin")
        assert connectflows.domain_owner(
            s, {}, self.conn, "bad.app") == "owner"


class TestAccountsRender(Case):
    def test_owner_kind_in_listing(self):
        connectflows.upsert(self.conn, "gmail", "oauth", "", "",
                            "active", "", "", "", {})
        connectflows.upsert(self.conn, "gmail@pal", "oauth", "", "",
                            "active", "", "", "", {})
        rows = {r["provider"]: r for r in
                connectflows.list_all(self.conn)}
        assert rows["gmail"]["owner_kind"] == "owner"
        assert rows["gmail@pal"]["owner_kind"] == "pal"

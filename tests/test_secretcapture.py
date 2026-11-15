"""secure password/OTP capture — modal, one-time HTTPS form
DM paste redaction, and removal of the plaintext command path.

Everything external is faked: the Discord REST layer is a recording
stub, the web form is driven through the WSGI test client.
"""

import json
import unittest
import urllib.parse

from pa import chattools, credvault, discordbot, secretform
from pa.logsetup import ScrubFilter
from pa.webapp import App
from tests.helpers import TmpEnv, WSGIClient

import io
import logging

KEY = "ab" * 32                       # PA_SECRETS_KEY hex
OWNER = "owner-1"
SECRET = "P@ss-Test-DontLeak-917"     # planted — must appear nowhere


class _FakeAPI:
    """Records every Discord REST call; returns truthy for all."""
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def f(*a, **kw):
            self.calls.append((name, a, kw))
            return True
        return f

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


def _bot(env):
    bot = discordbot.DiscordBot(env.settings, env=env.env,
                                notifier=False, engine=object())
    bot.api = _FakeAPI()
    return bot


def _dm(mid, content, cid="dm1"):
    return {"id": mid, "channel_id": cid, "channel_type": 1,
            "author": {"id": OWNER}, "content": content}


def _modal_submit(iid, cid_field_values, custom_id="credmodal:pw"):
    comps = [{"type": 1, "components": [
        {"type": 4, "custom_id": k, "value": v}]}
        for k, v in cid_field_values.items()]
    return {"id": iid, "token": "tok", "type": 5, "channel_id": "dm1",
            "member": {"user": {"id": OWNER}},
            "data": {"custom_id": custom_id, "components": comps}}


def _slash(iid, name, options=None):
    data = {"name": name}
    if options:
        data["options"] = [{"name": k, "type": 3, "value": v}
                           for k, v in options.items()]
    return {"id": iid, "token": "tok", "type": 2, "channel_id": "dm1",
            "member": {"user": {"id": OWNER}}, "data": data}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv(PA_SECRETS_KEY=KEY,
                          DISCORD_OWNER_ID=OWNER,
                          DISCORD_BOT_TOKEN="x",
                          PA_SECRET_CAPTURE="true")
        self.conn = self.env.conn()


class TestSecretform(Base):
    def test_issue_peek_consume_single_use(self):
        tok = secretform.issue(self.conn, "password")
        self.assertEqual(secretform.peek(self.conn, tok)["kind"],
                         "password")
        self.assertIsNotNone(secretform.consume(self.conn, tok))
        self.assertIsNone(secretform.consume(self.conn, tok))
        self.assertIsNone(secretform.peek(self.conn, tok))

    def test_expired_token(self):
        tok = secretform.issue(self.conn)
        self.conn.execute(
            "UPDATE pa_settings SET value=? WHERE key=?",
            (json.dumps({"kind": "password",
                         "at": "2020-01-01T00:00:00"}),
             f"secform:{tok}"))
        self.assertIsNone(secretform.peek(self.conn, tok))

    def test_pending_roundtrip_and_stale(self):
        self.assertIsNone(secretform.pending(self.conn))
        secretform.mark_pending(self.conn, "otp")
        self.assertEqual(secretform.pending(self.conn), "otp")
        secretform.clear_pending(self.conn)
        self.assertIsNone(secretform.pending(self.conn))
        secretform.mark_pending(self.conn)
        self.conn.execute(
            "UPDATE pa_settings SET value=? WHERE key='cap_pending'",
            (json.dumps({"kind": "password",
                         "at": "2020-01-01T00:00:00"}),))
        self.assertIsNone(secretform.pending(self.conn))

    def test_url_requires_https_or_loopback(self):
        self.env.settings.public_url = "https://pa.example.com"
        self.assertTrue(secretform.url(self.env.settings, "t").
                        startswith("https://pa.example.com/secret/"))
        self.env.settings.public_url = "http://pa.example.com"
        self.assertEqual(secretform.url(self.env.settings, "t"), "")
        self.env.settings.public_url = "http://127.0.0.1:8080"
        self.assertTrue(secretform.url(self.env.settings, "t"))


class TestOtpVault(Base):
    def test_add_read_one_shot(self):
        credvault.add_otp(self.conn, self.env.env, "x.com", SECRET)
        self.assertEqual(
            credvault.read_otp(self.conn, self.env.env, "x.com"),
            SECRET)
        self.assertIsNone(
            credvault.read_otp(self.conn, self.env.env, "x.com"))

    def test_otp_expired(self):
        credvault.add_otp(self.conn, self.env.env, "x.com", SECRET,
                          ttl=-1)
        self.assertIsNone(
            credvault.read_otp(self.conn, self.env.env, "x.com"))
        # row was still burned
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM credentials WHERE domain='otp:x.com'"
        ).fetchone()["c"], 0)

    def test_otp_suffix_match_and_phish(self):
        credvault.add_otp(self.conn, self.env.env, "example.com", "111")
        self.assertEqual(credvault.read_otp(
            self.conn, self.env.env, "app.example.com"), "111")
        credvault.add_otp(self.conn, self.env.env, "example.com", "222")
        self.assertIsNone(credvault.read_otp(
            self.conn, self.env.env, "evil-example.com"))

    def test_otp_slot_invisible_to_password_lookup(self):
        credvault.add_otp(self.conn, self.env.env, "example.com", "999")
        self.assertIsNone(credvault._lookup(
            self.conn, self.env.env, "example.com"))
        self.assertIsNone(credvault._lookup(
            self.conn, self.env.env, "app.example.com"))

    def test_list_shows_no_secret(self):
        credvault.add_otp(self.conn, self.env.env, "x.com", SECRET)
        self.assertNotIn(SECRET, str(
            credvault.list_credentials(self.conn)))


class _FakeSvc:
    def __init__(self, nodes, url):
        self.calls = []
        self._nodes, self._url = nodes, url

    def call(self, action, args=None):
        self.calls.append((action, args))
        if action == "snapshot":
            return {"url": self._url, "nodes": self._nodes, "text": ""}
        return {"ok": True}

    def typed(self):
        return [a for act, a in self.calls if act == "type"]


class TestOtpFill(Base):
    def _ctx(self):
        aid = self.env.add_assistant(name="pal", tools=[])
        ast = self.conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        return chattools.ChatToolContext(
            self.conn, self.env.settings, ast, "c", env=self.env.env)

    def test_fill_secret_consumes_otp(self):
        credvault.add(self.conn, self.env.env, "example.com", "u", "pw")
        credvault.add_otp(self.conn, self.env.env, "example.com",
                          SECRET)
        ctx = self._ctx()
        ctx.computer = _FakeSvc(
            ["@e1 input type=text \"Email\"",
             "@e2 input type=password \"Password\"",
             "@e3 input type=text \"One-time code\""],
            "https://app.example.com/login")
        out = credvault.fill_secret(ctx, "")
        self.assertIn("otp", out["fields"])
        self.assertEqual(ctx.computer.typed()[2]["text"], SECRET)
        self.assertNotIn(SECRET, str(out))
        # code was burned — a second fill cannot replay it
        self.assertIsNone(
            credvault.read_otp(self.conn, self.env.env,
                               "example.com"))


class TestDiscordCapture(Base):
    def setUp(self):
        super().setUp()
        self.bot = _bot(self.env)

    def test_slash_add_opens_modal(self):
        self.bot._handle_event(
            "INTERACTION_CREATE",
            _slash("i1", "credentials", {"action": "add"}))
        calls = self.bot.api.named("interaction_modal")
        self.assertEqual(len(calls), 1)
        modal = calls[0][1][2]
        self.assertEqual(modal["custom_id"], "credmodal:pw")
        self.assertEqual(secretform.pending(self.env.conn()),
                         "password")

    def test_slash_otp_opens_otp_modal(self):
        self.bot._handle_event(
            "INTERACTION_CREATE",
            _slash("i2", "credentials", {"action": "otp"}))
        modal = self.bot.api.named("interaction_modal")[0][1][2]
        self.assertEqual(modal["custom_id"], "credmodal:otp")

    def test_modal_submit_seals_domain_only_confirm(self):
        self.bot._handle_event(
            "INTERACTION_CREATE",
            _modal_submit("i3", {"domain": "x.com", "username": "u",
                                 "secret": SECRET}))
        replies = self.bot.api.named("interaction_reply")
        self.assertEqual(len(replies), 1)
        body = json.dumps(replies[0][1][2])
        self.assertIn("x.com", body)
        self.assertNotIn(SECRET, body)
        self.assertTrue(replies[0][2].get("ephemeral"))
        # sealed — readable only via the vault, never re-rendered
        rows = credvault.list_credentials(self.env.conn())
        self.assertEqual(rows[0]["domain"], "x.com")
        self.assertNotIn(SECRET, str(rows))
        self.assertNotIn(SECRET, str(self.bot.api.calls))
        audits = self.env.conn().execute(
            "SELECT detail FROM audit_log").fetchall()
        self.assertNotIn(SECRET,
                         " ".join(r["detail"] or "" for r in audits))

    def test_modal_submit_otp(self):
        self.bot._handle_event(
            "INTERACTION_CREATE",
            _modal_submit("i4", {"domain": "x.com", "secret": SECRET},
                          custom_id="credmodal:otp"))
        self.assertEqual(
            credvault.read_otp(self.env.conn(), self.env.env, "x.com"),
            SECRET)

    def test_component_button_opens_modal(self):
        d = {"id": "i5", "token": "t", "type": 3, "channel_id": "dm1",
             "member": {"user": {"id": OWNER}},
             "data": {"custom_id": "credmodal:otp"}}
        self.bot._handle_event("INTERACTION_CREATE", d)
        modal = self.bot.api.named("interaction_modal")[0][1][2]
        self.assertEqual(modal["custom_id"], "credmodal:otp")

    def test_text_add_no_pending_refused_and_deleted(self):
        self.bot._handle_event(
            "MESSAGE_CREATE",
            _dm("m1", f"credentials add x.com me {SECRET}"))
        # message deletion attempted, nothing sealed
        self.assertEqual(len(self.bot.api.named("delete_message")), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM credentials").fetchone()["c"],
            0)
        posts = self.bot.api.named("post_message")
        body = json.dumps(posts)
        self.assertIn("nothing", body.lower())
        self.assertNotIn(SECRET, body)

    def test_text_add_pending_seals_and_deletes(self):
        secretform.mark_pending(self.conn, "password")
        self.bot._handle_event(
            "MESSAGE_CREATE",
            _dm("m2", f"credentials add x.com me {SECRET}"))
        self.assertEqual(len(self.bot.api.named("delete_message")), 1)
        rows = credvault.list_credentials(self.env.conn())
        self.assertEqual(rows[0]["domain"], "x.com")
        self.assertNotIn(SECRET, json.dumps(self.bot.api.calls))
        self.assertIsNone(secretform.pending(self.env.conn()))

    def test_text_add_alone_arms_pending(self):
        self.bot._handle_event(
            "MESSAGE_CREATE", _dm("m3", "credentials add"))
        self.assertEqual(secretform.pending(self.env.conn()),
                         "password")
        body = json.dumps(self.bot.api.named("post_message"))
        self.assertIn("capture", body.lower())

    def test_pending_dm_paste_sealed_deleted_not_journaled(self):
        secretform.mark_pending(self.conn, "password")
        self.bot._handle_event(
            "MESSAGE_CREATE",
            _dm("m4", f"x.com me@x.com {SECRET}"))
        self.assertEqual(len(self.bot.api.named("delete_message")), 1)
        rows = credvault.list_credentials(self.env.conn())
        self.assertEqual(rows[0]["domain"], "x.com")
        n = self.env.conn().execute(
            "SELECT COUNT(*) c FROM chat_messages").fetchone()["c"]
        self.assertEqual(n, 0)
        # nothing in the api call stream echoes the secret
        self.assertNotIn(SECRET, json.dumps(self.bot.api.calls))

    def test_pending_otp_paste(self):
        secretform.mark_pending(self.conn, "otp")
        self.bot._handle_event(
            "MESSAGE_CREATE", _dm("m5", f"x.com {SECRET}"))
        self.assertEqual(
            credvault.read_otp(self.env.conn(), self.env.env, "x.com"),
            SECRET)

    def test_normal_chat_not_eaten_while_pending(self):
        secretform.mark_pending(self.conn, "password")
        captured = {}
        self.bot._start_chat = lambda d: captured.setdefault("d", d)
        self.bot._handle_event(
            "MESSAGE_CREATE", _dm("m6", "what is the weather today?"))
        self.assertIn("d", captured)
        self.assertEqual(self.bot.api.named("delete_message"), [])

    def test_no_pending_paste_passes_to_chat(self):
        captured = {}
        self.bot._start_chat = lambda d: captured.setdefault("d", d)
        self.bot._handle_event(
            "MESSAGE_CREATE", _dm("m7", f"x.com me {SECRET}"))
        # no pending capture — treated as an ordinary chat turn
        self.assertIn("d", captured)

    def test_form_command_link(self):
        self.env.settings.public_url = "https://pa.example.com"
        out = self.bot._credentials(self.conn, ["form"])
        self.assertIn("https://pa.example.com/secret/", out)
        tok = out.rsplit("/secret/", 1)[1].split()[0]
        self.assertIsNotNone(secretform.peek(self.conn, tok))
        self.assertEqual(secretform.pending(self.conn), "password")

    def test_form_command_no_public_url(self):
        self.env.settings.public_url = ""
        out = self.bot._credentials(self.conn, ["form"])
        self.assertIn("PA_PUBLIC_URL", out)

    def test_flag_off_legacy_path(self):
        env2 = TmpEnv(PA_SECRETS_KEY=KEY, PA_SECRET_CAPTURE="false")
        conn2 = env2.conn()
        bot = _bot(env2)
        out = bot._credentials(conn2, ["add", "x.com", "u", SECRET])
        self.assertIn("saved", out)
        self.assertFalse(bot._maybe_text_secret(
            conn2, _dm("m8", ""), ["credentials", "add", "x", "u", "p"]))
        self.assertFalse(bot._maybe_secret_paste(
            conn2, _dm("m9", "x.com u p")))


class TestWebForm(Base):
    def setUp(self):
        super().setUp()
        self.env.settings.public_url = "https://pa.example.com"
        self.app = App(self.env.settings, env=self.env.env)
        self.c = WSGIClient(self.app)

    def _issue(self, kind="password"):
        conn = self.env.conn()
        tok = secretform.issue(conn, kind)
        conn.close()
        return tok

    def test_get_renders_form_no_secret(self):
        tok = self._issue()
        st, hdr, body = self.c.get(f"/secret/{tok}")
        self.assertEqual(st.split()[0], "200")
        self.assertIn(b'name=secret', body)
        self.assertIn(b'name=domain', body)
        self.assertNotIn(SECRET.encode(), body)
        self.assertIn(b'name=referrer content=never', body)

    def test_post_seals_and_burns(self):
        tok = self._issue()
        st, _, body = self.c.post("/secret", {
            "token": tok, "domain": "x.com", "username": "u",
            "secret": SECRET})
        self.assertIn(b"Saved", body)
        self.assertNotIn(SECRET.encode(), body)
        # second POST with the same token is rejected
        st, _, body = self.c.post("/secret", {
            "token": tok, "domain": "x.com", "username": "u",
            "secret": SECRET})
        self.assertIn(b"expired", body.lower())
        # sealed — only domain visible in list + audit
        rows = credvault.list_credentials(self.env.conn())
        self.assertEqual(rows[0]["domain"], "x.com")
        audits = self.env.conn().execute(
            "SELECT detail FROM audit_log").fetchall()
        self.assertNotIn(SECRET,
                         " ".join(r["detail"] or "" for r in audits))

    def test_expired_token_rejected(self):
        st, _, body = self.c.get("/secret/not-a-token")
        self.assertIn(b"expired", body.lower())
        st, _, body = self.c.post("/secret", {
            "token": "not-a-token", "domain": "x.com",
            "secret": SECRET})
        self.assertIn(b"expired", body.lower())
        self.assertEqual(
            self.env.conn().execute(
                "SELECT COUNT(*) c FROM credentials").fetchone()["c"],
            0)

    def test_https_required(self):
        env2 = TmpEnv(PA_SECRETS_KEY=KEY)      # no public_url
        conn2 = env2.conn()
        tok = secretform.issue(conn2)
        conn2.close()
        app2 = App(env2.settings, env=env2.env)
        c2 = WSGIClient(app2)
        st, _, _ = c2.get(f"/secret/{tok}")
        self.assertEqual(st.split()[0], "403")
        # forwarded-proto header (tunnel) passes
        st, _, body = c2.get(f"/secret/{tok}",
                             headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(st.split()[0], "200")
        self.assertIn(b'name=secret', body)

    def test_flag_off_404s(self):
        env2 = TmpEnv(PA_SECRETS_KEY=KEY, PA_SECRET_CAPTURE="false")
        conn2 = env2.conn()
        tok = secretform.issue(conn2)
        conn2.close()
        app2 = App(env2.settings, env=env2.env)
        st, _, _ = WSGIClient(app2).get(f"/secret/{tok}")
        self.assertEqual(st.split()[0], "404")

    def test_otp_form_seals_otp_slot(self):
        tok = self._issue("otp")
        self.c.post("/secret", {"token": tok, "domain": "x.com",
                                "secret": SECRET})
        self.assertEqual(
            credvault.read_otp(self.env.conn(), self.env.env, "x.com"),
            SECRET)


class TestScrubbed(Base):
    def test_secret_absent_from_logs(self):
        captured = io.StringIO()
        h = logging.StreamHandler(captured)
        h.addFilter(ScrubFilter())
        logging.getLogger().addHandler(h)
        try:
            bot = _bot(self.env)
            bot._handle_event(
                "INTERACTION_CREATE",
                _modal_submit("i9", {"domain": "x.com", "username": "u",
                                     "secret": SECRET}))
            self.env.settings.public_url = "https://pa.example.com"
            app = App(self.env.settings, env=self.env.env)
            c = WSGIClient(app)
            conn = self.env.conn()
            tok = secretform.issue(conn)
            conn.close()
            c.post("/secret", {"token": tok, "domain": "x.com",
                               "username": "u", "secret": SECRET})
            self.assertNotIn(SECRET, captured.getvalue())
            # the full db dump must not contain it either (sealed)
            self.assertNotIn(SECRET,
                             "\n".join(self.env.conn().iterdump()))
        finally:
            logging.getLogger().removeHandler(h)


if __name__ == "__main__":
    unittest.main()

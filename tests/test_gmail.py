"""Gmail read-only adapter against the Fixture HTTP stub.

- token exchange posts the refresh grant to the configured token URL
- list/get use the bearer token on the configured API base
- messages are returned wrapped as UNTRUSTED data
- missing/loose credentials fail closed into 'needs config'
"""

import base64
import os
import stat
import tempfile
import unittest

from pa import gmail as gmod
from tests.helpers import Fixture, TmpEnv


def _hdr_msg(subj, frm, snippet="hi"):
    return {"id": "m1", "snippet": snippet,
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {"headers": [
                {"name": "Subject", "value": subj},
                {"name": "From", "value": frm},
                {"name": "Date", "value": "Mon, 5 Jan 2026 09:00:00 +0000"}]}}


class GmailBase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.env = TmpEnv(
            GMAIL_API_BASE=self.fx.web_base,
            GMAIL_TOKEN_URL=self.fx.web_base + "/token",
        )
        os.environ["GMAIL_CLIENT_ID"] = "cid"
        os.environ["GMAIL_CLIENT_SECRET"] = "csec"
        os.environ["GMAIL_REFRESH_TOKEN"] = "rtok"

    def tearDown(self):
        for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
                  "GMAIL_REFRESH_TOKEN"):
            os.environ.pop(k, None)
        self.fx.close()

    def _client(self):
        return gmod.GmailClient(self.env.settings)


class TestAuthAndList(GmailBase):
    def test_refresh_then_list(self):
        self.fx.push_post({"access_token": "at1", "expires_in": 3600})
        self.fx.push_gmail({"messages": [{"id": "m1"}, {"id": "m2"}]})
        self.fx.push_gmail(_hdr_msg("Hello one", "a@x.test"))
        self.fx.push_gmail(_hdr_msg("Hello two", "b@x.test"))
        msgs = self._client().list_messages("is:unread", 5)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["subject"], "Hello one")
        # token request hit the token endpoint with the refresh grant
        tok_calls = [c for c in self.fx.calls
                     if c["method"] == "POST" and c["path"] == "/token"]
        self.assertEqual(len(tok_calls), 1)
        body = tok_calls[0]["body"]
        self.assertIn(b"grant_type=refresh_token", body)
        self.assertIn(b"refresh_token=rtok", body)
        self.assertIn(b"client_secret=csec", body)
        # gmail calls carried the bearer token
        gmail_calls = [c for c in self.fx.calls
                       if c["path"].startswith("/gmail/")]
        self.assertTrue(gmail_calls)
        for c in gmail_calls:
            self.assertEqual(c["headers"].get("Authorization"),
                             "Bearer at1")

    def test_access_token_cached(self):
        self.fx.push_post({"access_token": "at1", "expires_in": 3600})
        self.fx.push_gmail({"messages": []})
        self.fx.push_gmail({"messages": []})
        c = self._client()
        c.list_messages("is:unread", 1)
        c.list_messages("is:unread", 1)
        tok_calls = [x for x in self.fx.calls if x["path"] == "/token"]
        self.assertEqual(len(tok_calls), 1)  # second call reused token

    def test_get_message_marks_untrusted(self):
        self.fx.push_post({"access_token": "at1", "expires_in": 3600})
        payload = {
            "id": "m9", "snippet": "snip",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [{"name": "Subject", "value": "Big"},
                            {"name": "From", "value": "x@y.test"}],
                "parts": [{
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(
                        b"body text here").decode()}}]}}
        self.fx.push_gmail(payload)
        m = self._client().get_message("m9")
        self.assertIn("UNTRUSTED EMAIL", m["body"])
        self.assertIn("body text here", m["body"])
        self.assertIn("END UNTRUSTED", m["body"])
        get = [c for c in self.fx.calls
               if c["path"].startswith("/gmail/v1/users/me/messages/m9")]
        self.assertTrue(get)
        self.assertIn("format=full", get[0]["path"])

    def test_empty_inbox(self):
        self.fx.push_post({"access_token": "at1", "expires_in": 3600})
        self.fx.push_gmail({})
        self.assertEqual(self._client().list_messages("x", 5), [])


class TestConfigGates(unittest.TestCase):
    def test_missing_credentials_needs_config(self):
        env = TmpEnv()
        for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
                  "GMAIL_REFRESH_TOKEN"):
            os.environ.pop(k, None)
        with self.assertRaises(gmod.GmailNotConfigured):
            gmod.GmailClient(env.settings)
        self.assertFalse(gmod.configured(os.environ))

    def test_token_file_permissions(self):
        env = TmpEnv()
        for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
                  "GMAIL_REFRESH_TOKEN"):
            os.environ.pop(k, None)
        d = tempfile.mkdtemp(prefix="pa-gmail-tok-")
        tok = os.path.join(d, "token.json")
        with open(tok, "w") as f:
            f.write('{"refresh_token": "r", "client_id": "c",'
                    ' "client_secret": "s"}')
        os.chmod(tok, 0o644)
        os.environ["GMAIL_TOKEN_FILE"] = tok
        try:
            with self.assertRaises(gmod.GmailNotConfigured):
                gmod.GmailClient(env.settings)
            os.chmod(tok, 0o600)
            # perms ok now — constructs fine (no network touched yet)
            gmod.GmailClient(env.settings)
        finally:
            os.environ.pop("GMAIL_TOKEN_FILE", None)


if __name__ == "__main__":
    unittest.main()

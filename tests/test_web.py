"""Acceptance #9: auth, CSRF, origin, bind safety, cookies, throttle."""

import json
import unittest

from pa.config import ConfigError, load_settings
from pa.webapp import App
from tests.helpers import TmpEnv, WSGIClient


def make_app(env=None, **over):
    env = env or TmpEnv(**over)
    env.add_owner()
    return App(env.settings), env


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.app, self.env = make_app()
        self.c = WSGIClient(self.app)

    def test_unauthenticated_get_redirects_to_login(self):
        status, headers, body = self.c.get("/")
        self.assertTrue(status.startswith("303"))
        self.assertEqual(headers["location"], "/login")

    def test_unauthenticated_post_gets_401(self):
        status, _, _ = self.c.post("/tasks", {"x": "y"})
        self.assertTrue(status.startswith("401"))

    def test_healthz_is_public_and_leaks_nothing(self):
        status, _, body = self.c.get("/healthz")
        self.assertTrue(status.startswith("200"))
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["checks"]["db"], "ok")
        self.assertNotIn(b"python", body.lower())
        self.assertNotIn(b"version", body.lower())
        self.assertNotIn(b"data_dir", body.lower())
        self.assertNotIn(b"token", body.lower())

    def test_security_headers_present(self):
        _, headers, _ = self.c.get("/healthz")
        self.assertIn("content-security-policy", headers)
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")

    def test_login_flow_and_session(self):
        self.c.get("/login")
        status, headers, _ = self.c.login("owner", "correct horse 99")
        self.assertTrue(status.startswith("303"))
        cookie = headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("pa_session_dev=", cookie)
        self.assertNotIn("Secure", cookie)  # dev mode: relaxed cookie name
        status, _, body = self.c.get("/")
        self.assertTrue(status.startswith("200"))
        self.assertIn(b"Assistants", body)

    def test_wrong_password_401(self):
        status, _, _ = self.c.login("owner", "wrong")
        self.assertTrue(status.startswith("401"))

    def test_session_rotation_on_login(self):
        # login twice -> different session ids (rotate, never reuse)
        self.c.login("owner", "correct horse 99")
        c1 = self.c.cookies.get("pa_session_dev")
        self.c.login("owner", "correct horse 99")
        c2 = self.c.cookies.get("pa_session_dev")
        self.assertNotEqual(c1, c2)

    def test_login_throttled(self):
        for _ in range(6):
            self.c.login("owner", "nope")
        status, _, body = self.c.login("owner", "correct horse 99")
        self.assertTrue(status.startswith("429"))
        self.assertIn(b"Too many attempts", body)

    def test_forged_client_ip_cannot_bypass_throttle(self):
        # global bucket: rotating Fly-Client-IP doesn't help
        for i in range(6):
            self.c.request("POST", "/login",
                           data={"username": "owner", "password": "x"},
                           headers={"Fly-Client-IP": f"9.9.9.{i}"})
        status, _, _ = self.c.request(
            "POST", "/login",
            data={"username": "owner", "password": "correct horse 99"},
            headers={"Fly-Client-IP": "8.8.8.8"})
        self.assertTrue(status.startswith("429"))


class TestCSRF(unittest.TestCase):
    def setUp(self):
        self.app, self.env = make_app()
        self.c = WSGIClient(self.app)
        self.c.login("owner", "correct horse 99")
        self.token = self.c.csrf(self.app)

    def test_post_without_csrf_403(self):
        status, _, _ = self.c.post(
            "/tasks", {"assistant_id": "1", "title": "x", "body": "y"},
            headers={"Origin": "http://testserver"})
        self.assertTrue(status.startswith("403"))

    def test_post_foreign_origin_403(self):
        status, _, _ = self.c.post(
            "/tasks", {"assistant_id": "1", "title": "x", "body": "y",
                       "csrf": self.token},
            headers={"Origin": "https://evil.example.com"})
        self.assertTrue(status.startswith("403"))

    def test_post_with_csrf_and_origin_ok(self):
        aid = self.env.add_assistant()
        status, headers, _ = self.c.post(
            "/tasks", {"assistant_id": str(aid), "title": "x",
                       "body": "y", "csrf": self.token},
            headers={"Origin": "http://testserver"})
        self.assertTrue(status.startswith("303"))

    def test_body_size_limit(self):
        status, _, _ = self.c.post(
            "/tasks", {"assistant_id": "1", "title": "x",
                       "body": "y" * 100_000, "csrf": self.token},
            headers={"Origin": "http://testserver"})
        self.assertTrue(status.startswith("413"))


class TestBindSafety(unittest.TestCase):
    def test_dev_refuses_non_loopback(self):
        s = load_settings({"PA_ENV": "development"})
        with self.assertRaises(ConfigError):
            s.check_bind("0.0.0.0", 8080)
        s.check_bind("127.0.0.1", 8080)  # ok

    def test_prod_refuses_non_loopback_without_topology(self):
        s = load_settings({"PA_ENV": "production"})
        with self.assertRaises(ConfigError):
            s.check_bind("0.0.0.0", 8080)
        s.check_bind("127.0.0.1", 8080)  # loopback always ok (behind Caddy)

    def test_prod_fly_topology_allows(self):
        s = load_settings({"PA_ENV": "production",
                           "FLY_APP_NAME": "myapp"})
        s.check_bind("0.0.0.0", 8080)

    def test_env_flag_alone_not_proof(self):
        # a random "I'm behind TLS" style flag means nothing
        s = load_settings({"PA_ENV": "production", "PA_BEHIND_TLS": "1"})
        with self.assertRaises(ConfigError):
            s.check_bind("0.0.0.0", 8080)


class TestCookieModes(unittest.TestCase):
    def test_production_cookie_is_secure_different_name(self):
        env = TmpEnv(PA_ENV="production")
        env.add_owner()
        app = App(env.settings)
        c = WSGIClient(app)
        status, headers, _ = c.login("owner", "correct horse 99")
        cookie = headers["set-cookie"]
        self.assertIn("pa_session=", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Domain=", cookie)  # host-only

    def test_prod_never_uses_dev_cookie(self):
        env = TmpEnv(PA_ENV="production")
        env.add_owner()
        app = App(env.settings)
        c = WSGIClient(app)
        # a hand-set dev cookie must NOT authenticate in production
        c.cookies["pa_session_dev"] = "anything"
        status, _, _ = c.get("/")
        self.assertTrue(status.startswith("303"))


if __name__ == "__main__":
    unittest.main()

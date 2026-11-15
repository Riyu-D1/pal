"""pal's computer: service lifecycle, URL policy, risk classification,
and the computer-agent wire protocol — all against a fake driver. No
playwright, no Chromium, no network beyond a loopback agent fixture.
"""

import http.server
import ipaddress
import json
import socket
import threading
import unittest

from pa import chattools, computer
from pa.config import ConfigError, load_settings
from pa.egress import FetchBlocked, FetchFailed
from tests.helpers import TmpEnv


# ---------------------------------------------------------------------------
# fake driver: same surface as PlaywrightDriver, canned state

class FakeDriver:
    def __init__(self):
        self.alive = False
        self.open_calls = 0
        self.open_kwargs = {}
        self.pages = [{"url": "about:blank", "title": ""}]
        self.idx = 0
        self.clicked = []
        self.typed = []
        self.pressed = []
        self.facts = {}            # target -> facts dict
        self.active_facts = {"found": False}
        self.mem = 0.0
        self.last_download = None
        self.tabs_capped = False
        self.closed = 0

    def open(self, **kw):
        self.open_calls += 1
        self.open_kwargs = kw
        self.alive = True
        return self

    def is_alive(self):
        return self.alive

    def close(self):
        self.alive = False
        self.closed += 1

    def memory_mb(self):
        return self.mem

    def tabs(self):
        return [{"index": i, "url": p["url"], "title": p["title"],
                 "current": i == self.idx}
                for i, p in enumerate(self.pages)]

    def switch(self, index):
        if not 0 <= index < len(self.pages):
            raise computer.ComputerError("no tab", "bad_arguments")
        self.idx = index
        return {"index": index, "url": self.pages[index]["url"]}

    def close_tab(self, index=None):
        i = self.idx if index is None else index
        self.pages.pop(i)
        self.idx = 0
        return {"closed": i, "open": len(self.pages)}

    def goto(self, url):
        if url == "about:blank":
            self.pages[self.idx]["url"] = url
            return {"url": url, "title": ""}
        self.pages[self.idx]["url"] = url
        return {"url": url, "title": "Page T"}

    def current_url(self):
        return self.pages[self.idx]["url"]

    def snapshot(self):
        return {"url": self.current_url(), "title": "Page T",
                "nodes": ['@e1 input type=text "q"',
                          '@e2 button "Search"'],
                "text": "hello world"}

    def inspect(self, target=None):
        if target is None:
            f = dict(self.active_facts)
        else:
            f = dict(self.facts.get(target) or {"found": False})
        f.setdefault("url", self.current_url())
        return f

    def click(self, target):
        if not target:
            raise computer.ComputerError("no target", "bad_arguments")
        self.clicked.append(target)
        return {"clicked": target, "url": self.current_url()}

    def type_text(self, target, text):
        self.typed.append((target, text))
        return {"typed": len(text), "into": target}

    def press(self, key):
        self.pressed.append(key)
        return {"pressed": key, "url": self.current_url()}

    def screenshot(self, max_bytes=computer.SCREENSHOT_MAX_BYTES):
        return (b"\xff\xd8\xff" + b"jpeg" * 100, "jpeg")

    def extract(self):
        return {"url": self.current_url(), "title": "Page T",
                "text": "page text",
                "links": [{"text": "l1", "href": "https://x.example"}],
                "tables": ["a | b"]}


def public_resolver(host, port):
    """Fake DNS: IP literals resolve to themselves; a few scripted
    names; everything else fails like real DNS would."""
    try:
        ipaddress.ip_address(host)
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 0, "", (host, port))]
    except ValueError:
        pass
    ips = {"good.example": "93.184.216.34",
           "meta.example": "169.254.169.254",
           "internal.example": "10.0.0.5",
           "v6.example": "2606:2800:220:1:248:1893:25c8:1946"}.get(host)
    if not ips:
        raise socket.gaierror("no such host")
    return [(socket.AF_INET6 if ":" in ips else socket.AF_INET,
             socket.SOCK_STREAM, 0, "", (ips, port))]


def make(driver=None, **env_over):
    """(env, service, FakeDriver) with PA_COMPUTER_ENABLED on."""
    env = TmpEnv(PA_COMPUTER_ENABLED="true", **env_over)
    drv = driver or FakeDriver()
    svc = computer.ComputerService(
        env.settings, env.env, driver_factory=lambda: drv,
        resolver=public_resolver)
    return env, svc, drv


class TestConfig(unittest.TestCase):
    def test_defaults_off(self):
        env = TmpEnv()
        s = env.settings
        self.assertFalse(s.computer_enabled)
        self.assertFalse(s.jina_reader)
        self.assertEqual(s.computer_max_tabs, 3)
        self.assertEqual(s.computer_idle_seconds, 300)
        self.assertTrue(s.computer_profile.endswith("browser-profile"))

    def test_jina_default_follows_computer_flag(self):
        self.assertTrue(TmpEnv(PA_COMPUTER_ENABLED="true")
                        .settings.jina_reader)
        self.assertFalse(TmpEnv(PA_COMPUTER_ENABLED="true",
                                PA_JINA_READER="false")
                         .settings.jina_reader)

    def test_desktop_minutes_hard_cap(self):
        s = TmpEnv(PA_DESKTOP_MINUTES="999").settings
        self.assertEqual(s.desktop_minutes, 120)

    def test_url_needs_token(self):
        with self.assertRaises(ConfigError):
            TmpEnv(PA_COMPUTER_URL="http://127.0.0.1:8788")

    def test_url_public_http_refused(self):
        with self.assertRaises(ConfigError):
            TmpEnv(PA_COMPUTER_URL="http://8.8.8.8:8788",
                   PA_COMPUTER_TOKEN="t")

    def test_url_tailnet_and_private_http_ok(self):
        for url in ("http://100.64.1.2:8788", "http://192.168.1.5:8788",
                    "http://127.0.0.1:8788", "https://host.ts.net"):
            s = TmpEnv(PA_COMPUTER_URL=url,
                       PA_COMPUTER_TOKEN="t").settings
            self.assertEqual(s.computer_url, url.rstrip("/"))

    def test_url_with_credentials_refused(self):
        with self.assertRaises(ConfigError):
            TmpEnv(PA_COMPUTER_URL="http://u:p@127.0.0.1:8788",
                   PA_COMPUTER_TOKEN="t")


class TestServiceLifecycle(unittest.TestCase):
    def test_disabled_returns_not_configured(self):
        env = TmpEnv()                       # enabled flag absent
        drv = FakeDriver()
        svc = computer.ComputerService(
            env.settings, env.env, driver_factory=lambda: drv,
            resolver=public_resolver)
        for action, args in (("goto", {"url": "https://x.example"}),
                             ("snapshot", {}), ("status", {})):
            out = svc.call(action, args)
            self.assertEqual(out["error"], "not_configured")
            self.assertIn("PA_COMPUTER_ENABLED", out["message"])
        self.assertEqual(drv.open_calls, 0)   # never launched

    def test_profile_dir_created_0700(self):
        import os
        import stat
        env, svc, drv = make()
        svc.call("goto", {"url": "https://good.example/"})
        mode = stat.S_IMODE(os.stat(env.settings.computer_profile).st_mode)
        self.assertEqual(mode, 0o700)
        self.assertEqual(drv.open_kwargs["profile_dir"],
                         env.settings.computer_profile)

    def test_goto_and_snapshot(self):
        env, svc, drv = make()
        out = svc.call("goto", {"url": "https://good.example/"})
        self.assertEqual(out["url"], "https://good.example/")
        snap = svc.call("snapshot")
        self.assertIn("@e2", snap["nodes"][1])
        self.assertEqual(drv.open_calls, 1)   # reused, not relaunched

    def test_goto_rejects_nonpublic(self):
        env, svc, drv = make()
        for url in ("http://127.0.0.1/", "http://10.0.0.5/x",
                    "http://169.254.169.254/latest/meta-data",
                    "http://meta.example/", "http://internal.example/",
                    "http://[::1]/", "ftp://good.example/",
                    "http://good.example:8080/",
                    "http://user:pw@good.example/"):
            out = svc.call("goto", {"url": url})
            self.assertIn(out["error"], ("fetch_blocked", "fetch_failed"),
                          url)
        self.assertFalse(drv.pages[0]["url"].startswith("http"))

    def test_goto_allows_public_v6(self):
        env, svc, drv = make()
        out = svc.call("goto", {"url": "https://v6.example/"})
        self.assertEqual(out["url"], "https://v6.example/")

    def test_redirect_to_private_walked_back(self):
        env, svc, drv = make()

        class Redirecting(FakeDriver):
            def goto(self, url):
                # simulates good.example -> internal.example redirect
                if url == "about:blank":
                    self.pages[self.idx]["url"] = url
                else:
                    self.pages[self.idx]["url"] = \
                        "http://internal.example/landing"
                return {"url": self.pages[self.idx]["url"], "title": "x"}

        drv2 = Redirecting()
        svc._drv = None
        svc._driver_factory = lambda: drv2
        out = svc.call("goto", {"url": "https://good.example/"})
        self.assertEqual(out["error"], "fetch_blocked")
        self.assertEqual(drv2.pages[0]["url"], "about:blank")

    def test_crash_recycles_browser(self):
        env, svc, drv = make()
        svc.call("goto", {"url": "https://good.example/"})
        drv.alive = False                       # simulate a crash
        svc.call("snapshot")
        self.assertEqual(drv.open_calls, 2)

    def test_memory_cap_recycles(self):
        env, svc, drv = make(PA_COMPUTER_MEMORY_MB="100")
        svc.call("goto", {"url": "https://good.example/"})
        self.assertTrue(drv.alive)
        drv.mem = 500.0                         # end of call recycles
        svc.call("snapshot")
        self.assertEqual(drv.closed, 1)
        self.assertFalse(drv.alive)
        svc.call("snapshot")                    # lazily relaunches
        self.assertEqual(drv.open_calls, 2)

    def test_idle_exit(self):
        env, svc, drv = make(PA_COMPUTER_IDLE_SECONDS="60")
        svc.call("goto", {"url": "https://good.example/"})
        self.assertFalse(svc.stop_if_idle(now=svc._now() + 30))
        self.assertTrue(svc.stop_if_idle(now=svc._now() + 61))
        self.assertFalse(drv.alive)
        svc.call("snapshot")                    # relaunches on demand
        self.assertEqual(drv.open_calls, 2)

    def test_busy_lock(self):
        env, svc, drv = make(PA_COMPUTER_LOCK_WAIT="0")
        svc._lock.acquire()
        try:
            out = svc.call("snapshot")
        finally:
            svc._lock.release()
        self.assertEqual(out["error"], "busy")

    def test_tab_cap_note(self):
        env, svc, drv = make(PA_COMPUTER_MAX_TABS="3")
        svc.call("goto", {"url": "https://good.example/"})
        drv.tabs_capped = True                  # driver closed a popup
        out = svc.call("tabs")
        self.assertIn("cap", out["note"])

    def test_screenshot_returns_b64(self):
        env, svc, drv = make()
        svc.call("goto", {"url": "https://good.example/"})
        out = svc.call("screenshot")
        self.assertEqual(out["ext"], "jpeg")
        self.assertGreater(out["bytes"], 100)
        import base64
        self.assertTrue(base64.b64decode(out["b64"])
                        .startswith(b"\xff\xd8"))

    def test_status_reports_state(self):
        env, svc, drv = make()
        out = svc.call("status")
        self.assertTrue(out["enabled"])
        self.assertFalse(out["running"])
        svc.call("goto", {"url": "https://good.example/"})
        self.assertTrue(svc.call("status")["running"])


class TestClassify(unittest.TestCase):
    """Risk classification is pure Python — no browser needed."""

    def test_password_field_is_secret(self):
        f = {"found": True, "tag": "input", "type": "password"}
        self.assertEqual(computer.classify(f)[0], "secret")

    def test_otp_and_card_fields_are_secret(self):
        for facts in (
            {"found": True, "tag": "input", "type": "text",
             "autocomplete": "one-time-code"},
            {"found": True, "tag": "input", "type": "text",
             "name": "cardnumber", "form_text": "Card number"},
            {"found": True, "tag": "input", "type": "text",
             "autocomplete": "cc-csc"},
            {"found": True, "tag": "input", "type": "text",
             "name": "otp", "form_text": "enter your 2FA code"},
        ):
            self.assertEqual(computer.classify(facts)[0], "secret",
                             facts)

    def test_named_submit_buttons(self):
        for name in ("Pay now", "Submit", "Confirm order", "Sign in",
                     "Place order", "Send"):
            f = {"found": True, "tag": "button", "text": name}
            self.assertEqual(computer.classify(f)[0], "submit", name)

    def test_post_form_submit_is_gated_get_is_not(self):
        post = {"found": True, "tag": "input", "type": "submit",
                "in_form": True, "form_method": "post", "text": "Go"}
        get = {"found": True, "tag": "input", "type": "submit",
               "in_form": True, "form_method": "get", "text": "Go"}
        self.assertEqual(computer.classify(post)[0], "submit")
        self.assertEqual(computer.classify(get)[0], "none")

    def test_sensitive_flow_context(self):
        f = {"found": True, "tag": "a", "text": "Continue",
             "url": "https://shop.example/checkout",
             "title": "Checkout — pay", "in_form": True}
        self.assertEqual(computer.classify(f)[0], "sensitive")
        f2 = {"found": True, "tag": "input", "type": "text",
              "in_form": True, "form_has_secret": True,
              "form_text": "Email Password", "name": "email"}
        self.assertEqual(computer.classify(f2)[0], "sensitive")

    def test_upload_and_download(self):
        up = {"found": True, "tag": "input", "type": "file"}
        dl = {"found": True, "tag": "a", "download": True,
              "text": "statement.pdf"}
        self.assertEqual(computer.classify(up)[0], "upload")
        self.assertEqual(computer.classify(dl)[0], "download")

    def test_plain_elements_free(self):
        for f in ({"found": True, "tag": "a", "text": "About",
                   "href": "https://x.example/about"},
                  {"found": True, "tag": "button", "text": "Search"},
                  {"found": True, "tag": "input", "type": "text",
                   "name": "q"},
                  {"found": False}):
            self.assertEqual(computer.classify(f)[0], "none", f)

    def test_press_rules(self):
        pw = {"found": True, "tag": "input", "type": "password"}
        self.assertEqual(computer.press_risk("a", pw)[0], "secret")
        self.assertEqual(computer.press_risk("Enter", pw)[0], "secret")
        self.assertEqual(computer.press_risk("Tab", pw)[0], "none")
        self.assertEqual(computer.press_risk("Escape", pw)[0], "none")
        post = {"found": True, "tag": "input", "type": "text",
                "in_form": True, "form_method": "post"}
        self.assertEqual(computer.press_risk("Enter", post)[0],
                         "submit")
        get = {"found": True, "tag": "input", "type": "text",
               "in_form": True, "form_method": "get"}
        self.assertEqual(computer.press_risk("Enter", get)[0], "none")


class TestInspectThroughService(unittest.TestCase):
    def test_inspect_reports_risk(self):
        env, svc, drv = make()
        drv.facts["@e9"] = {"found": True, "tag": "input",
                            "type": "password", "name": "pwd"}
        svc.call("goto", {"url": "https://good.example/"})
        out = svc.call("inspect", {"target": "@e9"})
        self.assertEqual(out["risk"], "secret")
        self.assertIn("password", out["reason"])


class TestAgentWire(unittest.TestCase):
    """make_agent_handler + RemoteComputer over a real loopback socket."""

    class StubService:
        def __init__(self):
            self.calls = []

        def enabled(self):
            return True

        def call(self, action, args):
            self.calls.append((action, args))
            if action == "boom":
                raise RuntimeError("kapow")
            return {"did": action, "got": args}

    def setUp(self):
        self.stub = self.StubService()
        handler = computer.make_agent_handler(self.stub, "tok-1")
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                   handler)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever,
                                  daemon=True)
        self.t.start()
        self.client = computer.RemoteComputer(
            f"http://127.0.0.1:{self.port}", "tok-1")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_call_roundtrip(self):
        out = self.client.call("goto", {"url": "https://x.example"})
        self.assertEqual(out["did"], "goto")
        self.assertEqual(self.stub.calls[0][1],
                         {"url": "https://x.example"})

    def test_bad_token_401(self):
        bad = computer.RemoteComputer(
            f"http://127.0.0.1:{self.port}", "wrong")
        out = bad.call("goto", {})
        self.assertEqual(out["error"], "agent_auth")

    def test_unknown_path_404(self):
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/nope", data=b"{}",
            headers={computer.TOKEN_HEADER: "tok-1",
                     "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_service_exception_is_500(self):
        out = self.client.call("boom", {})
        self.assertEqual(out["error"], "agent_error")

    def test_unreachable(self):
        dead = computer.RemoteComputer("http://127.0.0.1:1", "t",
                                       timeout=1)
        out = dead.call("status")
        self.assertEqual(out["error"], "agent_unreachable")

    def test_disabled_remote_shortcircuits(self):
        c = computer.RemoteComputer("http://127.0.0.1:1", "t",
                                    enabled=False)
        self.assertEqual(c.call("goto", {})["error"], "not_configured")


class TestThreadAffined(unittest.TestCase):
    def tearDown(self):
        aff = getattr(self, "aff", None)
        if aff is not None:
            aff.stop()

    def test_calls_from_many_threads_run_on_one_worker(self):
        # This is the production failure mode: the thread that first
        # talked to Playwright has already exited before the next
        # request (or the idle-exit watchdog) arrives.
        self.aff = computer.ThreadAffined(name="test-aff")
        self.aff.start()
        worker_ids = []
        caller_ids = []

        def rec():
            worker_ids.append(threading.get_ident())
            return threading.get_ident()

        def caller():
            caller_ids.append(threading.get_ident())
            self.aff.call(rec)

        for _ in range(3):
            t = threading.Thread(target=caller)
            t.start()
            t.join(5)
            self.assertFalse(t.is_alive())

        self.assertEqual(len(worker_ids), 3)
        self.assertEqual(len(set(worker_ids)), 1)
        self.assertTrue(all(cid != worker_ids[0] for cid in caller_ids))

    def test_exceptions_propagate(self):
        self.aff = computer.ThreadAffined(name="test-aff")
        self.aff.start()

        def boom():
            raise computer.ComputerError("x", "goto_failed")

        with self.assertRaises(computer.ComputerError) as cm:
            self.aff.call(boom)
        self.assertEqual(cm.exception.kind, "goto_failed")

    def test_reentrant_call_from_worker_does_not_deadlock(self):
        self.aff = computer.ThreadAffined(name="test-aff")
        self.aff.start()

        def inner():
            return 2

        def outer():
            return 1 + self.aff.call(inner)

        self.assertEqual(self.aff.call(outer), 3)


class TestServiceFor(unittest.TestCase):
    def test_picks_remote_with_url(self):
        env = TmpEnv(PA_COMPUTER_ENABLED="true",
                     PA_COMPUTER_URL="http://127.0.0.1:8788",
                     PA_COMPUTER_TOKEN="t")
        svc = computer.service_for(env.settings, env.env)
        self.assertIsInstance(svc, computer.RemoteComputer)

    def test_picks_local_and_memoizes(self):
        env = TmpEnv(PA_COMPUTER_ENABLED="true")
        a = computer.service_for(env.settings, env.env)
        b = computer.service_for(env.settings, env.env)
        self.assertIs(a, b)
        self.assertIsInstance(a, computer.ComputerService)

    def test_no_playwright_at_import(self):
        import sys
        importlib = __import__("importlib")
        importlib.reload(computer)
        self.assertNotIn("playwright.sync_api", sys.modules)


class TestBrowseTools(unittest.TestCase):
    """The chattools layer: prompt gating, the dynamic approval gate,
    and read_url. Service is the fake-driver ComputerService."""

    def ctx(self, svc, env=None, approve=None, unattended=False, **kw):
        env = env or TmpEnv(PA_COMPUTER_ENABLED="true")
        conn = env.conn()
        self.addCleanup(conn.close)
        ctx = chattools.ChatToolContext(
            conn, env.settings, {"name": "pal"}, "chan1", env=env.env,
            request_approval=approve, unattended=unattended,
            computer=svc, **kw)
        return env, conn, ctx

    def approval_rows(self, conn):
        return conn.execute(
            "SELECT kind, status, payload_json FROM approvals"
            ).fetchall()

    # -- prompt + disabled --------------------------------------------------

    def test_prompt_lists_browse_tools_only_when_enabled(self):
        off = TmpEnv().settings
        on = TmpEnv(PA_COMPUTER_ENABLED="true").settings
        self.assertNotIn("browse_goto", chattools.prompt_lines(off))
        self.assertNotIn("read_url", chattools.prompt_lines(off))
        self.assertIn("browse_goto", chattools.prompt_lines(on))
        self.assertIn("read_url", chattools.prompt_lines(on))
        self.assertIn("web_search", chattools.prompt_lines(off))

    def test_disabled_tools_answer_not_configured(self):
        env = TmpEnv()                       # flag off
        drv = FakeDriver()
        svc = computer.ComputerService(
            env.settings, env.env, driver_factory=lambda: drv,
            resolver=public_resolver)
        env2, conn, ctx = self.ctx(svc, env=env)
        for tool, args in (("browse_goto", {"url": "https://x.example"}),
                           ("browse_snapshot", {}),
                           ("read_url", {"url": "https://x.example"})):
            out = chattools.run(ctx, tool, args)
            self.assertEqual(out["error"], "not_configured", tool)

    # -- free actions --------------------------------------------------------

    def test_goto_and_snapshot_need_no_approval(self):
        env, svc, drv = make()
        calls = []
        env2, conn, ctx = self.ctx(
            svc, approve=lambda n, a, aid: calls.append(n) or "approved")
        out = chattools.run(ctx, "browse_goto",
                            {"url": "https://good.example/"})
        self.assertEqual(out["url"], "https://good.example/")
        snap = chattools.run(ctx, "browse_snapshot", {})
        self.assertIn("nodes", snap)
        chattools.run(ctx, "browse_tabs", {})
        chattools.run(ctx, "browse_extract",
                      {"instruction": "main text"})
        self.assertEqual(calls, [])
        self.assertEqual(self.approval_rows(conn), [])

    def test_goto_blocked_url_needs_no_approval(self):
        env, svc, drv = make()
        env2, conn, ctx = self.ctx(svc)
        out = chattools.run(ctx, "browse_goto",
                            {"url": "http://169.254.169.254/"})
        self.assertEqual(out["error"], "fetch_blocked")

    # -- the dynamic gate ----------------------------------------------------

    def test_click_free_element(self):
        env, svc, drv = make()
        drv.facts["@e2"] = {"found": True, "tag": "button",
                            "text": "Search"}
        env2, conn, ctx = self.ctx(svc)
        out = chattools.run(ctx, "browse_click", {"target": "@e2"})
        self.assertEqual(out["clicked"], "@e2")
        self.assertEqual(self.approval_rows(conn), [])

    def test_click_submit_waits_for_approval(self):
        env, svc, drv = make()
        drv.facts["@e9"] = {"found": True, "tag": "button",
                            "text": "Pay now"}
        decisions = []
        env2, conn, ctx = self.ctx(
            svc, approve=lambda n, a, aid: (
                decisions.append((n, a)) or "approved"))
        out = chattools.run(ctx, "browse_click", {"target": "@e9"})
        self.assertEqual(out["clicked"], "@e9")
        self.assertEqual(decisions[0][0], "browse_click")
        self.assertIn("Pay now", json.dumps(decisions[0][1]))
        rows = self.approval_rows(conn)
        self.assertEqual(rows[0][0], "chat_action")

    def test_click_submit_denied(self):
        env, svc, drv = make()
        drv.facts["@e9"] = {"found": True, "tag": "button",
                            "text": "Confirm order"}
        env2, conn, ctx = self.ctx(
            svc, approve=lambda n, a, aid: "denied")
        out = chattools.run(ctx, "browse_click", {"target": "@e9"})
        self.assertEqual(out["error"], "not_approved")
        self.assertEqual(drv.clicked, [])

    def test_secret_fields_never_get_approval_either(self):
        env, svc, drv = make()
        drv.facts["@e3"] = {"found": True, "tag": "input",
                            "type": "password", "name": "pwd"}
        drv.facts["@e4"] = {"found": True, "tag": "input",
                            "autocomplete": "one-time-code"}
        env2, conn, ctx = self.ctx(
            svc, approve=lambda n, a, aid: "approved")
        for target in ("@e3", "@e4"):
            out = chattools.run(ctx, "browse_type",
                                {"target": target, "text": "hunter2"})
            self.assertEqual(out["error"], "secret_field")
            self.assertIn("/desktop", out["message"])
        self.assertEqual(drv.typed, [])
        self.assertEqual(self.approval_rows(conn), [])

    def test_type_normal_field(self):
        env, svc, drv = make()
        drv.facts["@e1"] = {"found": True, "tag": "input",
                            "type": "text", "name": "q"}
        env2, conn, ctx = self.ctx(svc)
        out = chattools.run(ctx, "browse_type",
                            {"target": "@e1", "text": "hello"})
        self.assertEqual(out["typed"], 5)

    def test_press_rules_through_tool(self):
        env, svc, drv = make()
        drv.active_facts = {"found": True, "tag": "input",
                            "type": "password"}
        env2, conn, ctx = self.ctx(svc)
        out = chattools.run(ctx, "browse_press", {"key": "a"})
        self.assertEqual(out["error"], "secret_field")
        out = chattools.run(ctx, "browse_press", {"key": "Tab"})
        self.assertEqual(out["pressed"], "Tab")
        drv.active_facts = {"found": True, "tag": "input",
                            "type": "text", "in_form": True,
                            "form_method": "post"}
        approvals = []
        ctx.request_approval = lambda n, a, aid: (
            approvals.append(n) or "approved")
        out = chattools.run(ctx, "browse_press", {"key": "Enter"})
        self.assertEqual(out["pressed"], "Enter")
        self.assertEqual(approvals, ["browse_press"])

    def test_unattended_queues_write_actions(self):
        env, svc, drv = make()
        drv.facts["@e2"] = {"found": True, "tag": "button",
                            "text": "Search"}
        env2, conn, ctx = self.ctx(svc, unattended=True)
        out = chattools.run(ctx, "browse_click", {"target": "@e2"})
        self.assertTrue(out["queued"])
        self.assertEqual(drv.clicked, [])
        self.assertEqual(ctx.pending[0]["tool"], "browse_click")
        rows = self.approval_rows(conn)
        self.assertEqual(rows[0][0], "job_action")
        snap = chattools.run(ctx, "browse_snapshot", {})
        self.assertIn("nodes", snap)          # read-only still free

    def test_execute_approved_runs_queued_click(self):
        env, svc, drv = make()
        drv.facts["@e9"] = {"found": True, "tag": "button",
                            "text": "Place order"}
        env2, conn, ctx = self.ctx(svc, unattended=True)
        chattools.run(ctx, "browse_click", {"target": "@e9"})
        args = dict(ctx.pending[0]["args"])   # has risk/preview extras
        env3, conn3, ctx3 = self.ctx(svc)
        out = chattools.execute_approved(ctx3, "browse_click", args)
        self.assertEqual(out["clicked"], "@e9")

    # -- screenshots ---------------------------------------------------------

    def test_screenshot_posts_attachment(self):
        env, svc, drv = make()
        posted = []
        env2, conn, ctx = self.ctx(
            svc, post_file=lambda ch, fn, ct, data:
                posted.append((ch, fn, ct, data)) or True)
        svc.call("goto", {"url": "https://good.example/"})
        out = chattools.run(ctx, "browse_screenshot", {})
        self.assertTrue(out["attached"].endswith(".jpeg"))
        ch, fn, ct, data = posted[0]
        self.assertEqual(ch, "chan1")
        self.assertEqual(ct, "image/jpeg")
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_screenshot_sandbox_fallback(self):
        env, svc, drv = make()
        env2, conn, ctx = self.ctx(svc)       # no post_file
        svc.call("goto", {"url": "https://good.example/"})
        out = chattools.run(ctx, "browse_screenshot", {})
        import os
        self.assertTrue(os.path.exists(out["path"]))
        self.assertIn(env2.settings.chat_sandbox, out["path"])

    # -- read_url ------------------------------------------------------------

    def test_read_url_plain_fetch_path(self):
        env, svc, drv = make()
        seen = []

        def fake_fetch(url, settings):
            seen.append(url)
            return {"text": "readable text " * 40,
                    "content_type": "text/plain",
                    "final_url": url, "title": "T"}

        env2, conn, ctx = self.ctx(svc, fetch_impl=fake_fetch)
        out = chattools.run(ctx, "read_url",
                            {"url": "https://good.example/x"})
        self.assertEqual(out["via"], "fetch")
        self.assertEqual(drv.open_calls, 0)   # never touches Chromium

    def test_read_url_jina_fallback(self):
        env, svc, drv = make()
        jina_calls = []

        def blocked_fetch(url, settings):
            raise FetchBlocked("no")

        def fake_jina(url, settings, env):
            jina_calls.append(url)
            return "rendered " * 50

        env2, conn, ctx = self.ctx(
            svc, fetch_impl=blocked_fetch, jina_impl=fake_jina)
        out = chattools.run(ctx, "read_url",
                            {"url": "https://good.example/js"})
        self.assertEqual(out["via"], "jina_reader")
        self.assertEqual(jina_calls, ["https://good.example/js"])

    def test_read_url_jina_off_returns_fetch_error(self):
        env, svc, drv = make()
        env_off = TmpEnv(PA_COMPUTER_ENABLED="true",
                         PA_JINA_READER="false")

        def blocked_fetch(url, settings):
            raise FetchBlocked("private range")

        svc2 = computer.ComputerService(
            env_off.settings, env_off.env, driver_factory=lambda: drv,
            resolver=public_resolver)
        env2, conn, ctx = self.ctx(
            svc2, env=env_off, fetch_impl=blocked_fetch)
        out = chattools.run(ctx, "read_url",
                            {"url": "http://10.0.0.9/"})
        self.assertEqual(out["error"], "fetch_blocked")

    def test_read_url_size_cap(self):
        env, svc, drv = make()

        def fat_jina(url, settings, env):
            return "x" * 200_000

        env2, conn, ctx = self.ctx(
            svc, fetch_impl=lambda u, s: (_ for _ in ()).throw(
                FetchFailed("no html")),
            jina_impl=fat_jina)
        out = chattools.run(ctx, "read_url",
                            {"url": "https://good.example/big"})
        self.assertLessEqual(len(out["excerpt"]),
                            chattools.OBSERVATION_CAP + 20)

    def test_read_url_thin_pages(self):
        env, svc, drv = make()
        env2, conn, ctx = self.ctx(
            svc,
            fetch_impl=lambda u, s: {"text": "hi",
                                     "content_type": "text/plain",
                                     "final_url": u, "title": ""},
            jina_impl=lambda u, s, e: "tiny")
        out = chattools.run(ctx, "read_url",
                            {"url": "https://good.example/empty"})
        self.assertEqual(out["error"], "thin_content")


class TestServiceDesktopDispatch(unittest.TestCase):
    """ComputerService._desktop_action + idle-exit's desktop guard."""

    class FakeMgr:
        def __init__(self):
            self.started = []
            self._active = False
            self.stopped = 0

        def active(self):
            return self._active

        def cdp_url(self):
            return "http://127.0.0.1:9222" if self._active else ""

        def start(self, minutes=None):
            self.started.append(minutes)
            self._active = True
            return {"url": "http://x:6080/vnc.html?password=t",
                    "expires_in": 300}

        def stop(self):
            self._active = False
            self.stopped += 1
            return {"stopped": True}

        def status(self):
            return {"active": self._active}

    def test_service_routes_desktop_actions(self):
        env, svc, drv = make()
        svc._desktop = self.FakeMgr()
        out = svc.call("desktop_start", {"minutes": 7})
        self.assertIn("password=t", out["url"])
        self.assertEqual(svc._desktop.started, [7])
        self.assertTrue(svc.call("desktop_status")["active"])
        svc.call("desktop_stop")
        self.assertEqual(svc._desktop.stopped, 1)

    def test_idle_exit_respects_live_desktop(self):
        env, svc, drv = make(PA_COMPUTER_IDLE_SECONDS="60")
        svc._desktop = self.FakeMgr()
        svc.call("goto", {"url": "https://good.example/"})
        svc._desktop._active = True           # desktop owns the screen
        self.assertFalse(svc.stop_if_idle(now=svc._now() + 61))
        self.assertTrue(drv.alive)
        svc._desktop._active = False
        self.assertTrue(svc.stop_if_idle(now=svc._now() + 61))

    def test_service_attaches_via_cdp_while_desktop_live(self):
        env, svc, drv = make()
        svc._desktop = self.FakeMgr()
        svc._desktop._active = True
        svc.call("goto", {"url": "https://good.example/"})
        self.assertEqual(drv.open_kwargs["cdp_url"],
                         "http://127.0.0.1:9222")


class FakeProc:
    def __init__(self):
        self.dead = False
        self.terminated = False

    def poll(self):
        return 1 if self.dead else None

    def terminate(self):
        self.dead = True
        self.terminated = True

    def wait(self, timeout=3):
        self.dead = True
        return 1

    def kill(self):
        self.dead = True


class TestDesktop(unittest.TestCase):
    """pa/desktop.py — process plumbing and fail-closed rules, all
    faked. No Xvfb, no Chromium, no Tailscale binary."""

    def setUp(self):
        from unittest import mock
        from pa import desktop
        self.desktop = desktop
        self.mock_which = mock.patch(
            "pa.desktop.shutil.which", lambda b: f"/usr/bin/{b}")
        self.mock_sleep = mock.patch("pa.desktop.time.sleep",
                                     lambda s: None)
        self.mock_which.start()
        self.mock_sleep.start()
        self.addCleanup(self.mock_sleep.stop)
        self.addCleanup(self.mock_which.stop)

    def mgr(self, tailscale=None, now=None, env_over=None, **kw):
        env = TmpEnv(PA_COMPUTER_ENABLED="true",
                     PA_COMPUTER_BROWSER_BIN="/usr/bin/chromium",
                     **(env_over or {}))
        spawned = []
        clock = now if now is not None else [0.0]

        def runner(argv, env=None):
            spawned.append(argv)
            return FakeProc()

        mgr = self.desktop.DesktopManager(
            env.settings, env.env,
            before_start=kw.get("before_start"),
            on_stop=kw.get("on_stop"),
            runner=runner,
            tailscale=tailscale or (lambda: (
                True, "100.64.1.5", "pca-computer.tail-xyz.ts.net")),
            novnc=lambda: "/usr/share/novnc",
            now=lambda: clock[0])
        return env, mgr, spawned, clock

    def test_fails_closed_without_tailscale(self):
        env, mgr, spawned, _ = self.mgr(
            tailscale=lambda: (False, "", ""))
        out = mgr.start()
        self.assertEqual(out["error"], "no_tailscale")
        self.assertIn("tailscale up", out["message"])
        self.assertEqual(spawned, [])
        self.assertFalse(mgr.active())

    def test_fail_closed_on_non_tailnet_ip(self):
        env, mgr, spawned, _ = self.mgr(
            tailscale=lambda: (True, "8.8.8.8", ""))
        # a status probe that lies about its address still fails closed
        out = mgr.start()
        self.assertEqual(out["error"], "no_tailscale")
        self.assertEqual(spawned, [])

    def test_missing_tools(self):
        env, mgr, spawned, _ = self.mgr()
        with __import__("unittest.mock", fromlist=["mock"]).patch(
                "pa.desktop.shutil.which", lambda b: None):
            out = mgr.start()
        self.assertEqual(out["error"], "missing_tools")
        self.assertIn("Xvfb", out["message"])
        self.assertEqual(spawned, [])

    def test_start_binds_tailnet_only(self):
        env, mgr, spawned, _ = self.mgr()
        out = mgr.start(minutes=30)
        self.assertIn("password=", out["url"])
        self.assertIn("pca-computer.tail-xyz.ts.net:6080", out["url"])
        self.assertEqual(out["expires_in"], 30 * 60)
        self.assertEqual(out["bind"], "100.64.1.5")
        names = [a[0] for a in spawned]
        self.assertEqual(names, ["Xvfb", "openbox", "x11vnc",
                                 "websockify", "/usr/bin/chromium"])
        web = spawned[3]
        self.assertIn("100.64.1.5:6080", web)
        self.assertNotIn("0.0.0.0", " ".join(web))
        vnc = spawned[2]
        self.assertIn("-passwd", vnc)
        token = vnc[vnc.index("-passwd") + 1]
        self.assertIn(f"password={token}", out["url"])
        chrome = spawned[4]
        self.assertIn(f"--user-data-dir={env.settings.computer_profile}",
                      chrome)
        self.assertTrue(mgr.active())
        self.assertTrue(mgr.cdp_url().startswith("http://127.0.0.1:"))

    def test_before_start_and_reuse(self):
        calls = []
        env, mgr, spawned, _ = self.mgr(
            before_start=lambda: calls.append("stopped-headless"))
        first = mgr.start(minutes=10)
        second = mgr.start(minutes=10)
        self.assertEqual(first["url"], second["url"])
        self.assertEqual(len(spawned), 5)       # spawned once
        self.assertEqual(calls, ["stopped-headless"])

    def test_off_tears_down_everything(self):
        stopped = []
        env, mgr, spawned, _ = self.mgr(
            on_stop=lambda: stopped.append(1))
        mgr.start(minutes=10)
        out = mgr.stop()
        self.assertTrue(out["stopped"])
        self.assertFalse(mgr.active())
        self.assertEqual(mgr.cdp_url(), "")
        self.assertEqual(stopped, [1])
        st = mgr.status()
        self.assertFalse(st["active"])
        self.assertEqual(st["url"], "")

    def test_expiry_tears_down(self):
        stopped = []
        env, mgr, spawned, clock = self.mgr(
            on_stop=lambda: stopped.append(1))
        mgr.start(minutes=5)
        clock[0] += 5 * 60 + 1                  # past the deadline
        self.assertFalse(mgr.active())
        self.assertFalse(mgr.status()["active"])
        self.assertEqual(stopped, [1])

    def test_minutes_hard_cap(self):
        env, mgr, spawned, clock = self.mgr()
        out = mgr.start(minutes=999)
        self.assertLessEqual(out["expires_in"], 120 * 60)
        self.assertGreater(out["expires_in"], 119 * 60)

    def test_spawn_death_is_start_failed(self):
        env = TmpEnv(PA_COMPUTER_ENABLED="true",
                     PA_COMPUTER_BROWSER_BIN="/usr/bin/chromium")

        class DeadOnArrival(FakeProc):
            def __init__(self):
                super().__init__()
                self.dead = True

        mgr = self.desktop.DesktopManager(
            env.settings, env.env,
            runner=lambda argv, env=None: DeadOnArrival(),
            tailscale=lambda: (True, "100.64.1.5", ""),
            novnc=lambda: "/usr/share/novnc")
        out = mgr.start(minutes=5)
        self.assertEqual(out["error"], "start_failed")
        self.assertFalse(mgr.active())


class TestDesktopCommand(unittest.TestCase):
    """The /desktop Discord command on top of the service."""

    class FakeAPI:
        def __init__(self, dm="dm-1"):
            self._dm = dm
            self.sent = []

        def open_dm(self, uid):
            return self._dm

        def post_message(self, ch, text, components=None):
            self.sent.append((ch, text))
            return True

    class StubSvc:
        def __init__(self, enabled=True, result=None):
            self._enabled = enabled
            self.result = result or {"url": "http://pca-computer."
                                     "tail-xyz.ts.net:6080/vnc.html"
                                     "?autoconnect=true&password=t0k",
                                     "expires_in": 1800}
            self.calls = []

        def enabled(self):
            return self._enabled

        def call(self, action, args=None):
            self.calls.append((action, args))
            if action == "desktop_stop":
                return {"stopped": True}
            return dict(self.result)

    def bot(self, svc, api=None):
        from pa import discordbot
        env = TmpEnv(PA_COMPUTER_ENABLED="true",
                     DISCORD_OWNER_ID="owner-1")
        b = discordbot.DiscordBot(env.settings, db_path=env.db_path,
                                  env=env.env)
        b._computer = svc
        b.api = api if api is not None else self.FakeAPI()
        conn = env.conn()
        self.addCleanup(conn.close)
        return env, conn, b

    def test_desktop_off_command(self):
        svc = self.StubSvc()
        env, conn, b = self.bot(svc)
        out = b.execute(conn, ["desktop", "off"], "ch-1")
        self.assertIn("stopped", out.lower())
        self.assertEqual(svc.calls[0][0], "desktop_stop")

    def test_desktop_start_dms_link_not_channel(self):
        svc = self.StubSvc()
        api = self.FakeAPI()
        env, conn, b = self.bot(svc, api)
        out = b.execute(conn, ["desktop"], "guild-chan-9")
        self.assertNotIn("http", out)           # link never in-channel
        self.assertEqual(api.sent[0][0], "dm-1")
        self.assertIn("password=t0k", api.sent[0][1])
        self.assertIn("tailnet", api.sent[0][1])

    def test_desktop_start_dm_context(self):
        svc = self.StubSvc()
        api = self.FakeAPI()
        env, conn, b = self.bot(svc, api)
        out = b.execute(conn, ["desktop"], "dm-1")
        self.assertIn("expires", out)

    def test_desktop_start_failures_surface_message(self):
        svc = self.StubSvc(result={
            "error": "no_tailscale",
            "message": "Tailscale isn't running"})
        env, conn, b = self.bot(svc)
        out = b.execute(conn, ["desktop"], "ch-1")
        self.assertIn("Tailscale isn't running", out)

    def test_desktop_disabled_message(self):
        svc = self.StubSvc(enabled=False)
        env, conn, b = self.bot(svc)
        out = b.execute(conn, ["desktop"], "ch-1")
        self.assertIn("PA_COMPUTER_ENABLED", out)

    def test_desktop_no_dm_stops_session(self):
        svc = self.StubSvc()
        env, conn, b = self.bot(svc, api=self.FakeAPI(dm=None))
        out = b.execute(conn, ["desktop"], "ch-1")
        self.assertIn("couldn't open", out.lower())
        self.assertIn(("desktop_stop", None), svc.calls)


if __name__ == "__main__":
    unittest.main()

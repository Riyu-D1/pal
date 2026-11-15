"""the router 'escalate' tier."""

import unittest

from pa.router import RouterProvider
from tests.test_router import FakeApiClient, RouterBase, _backend, \
    ok_msg


class TestEscalate(RouterBase):
    def router(self, backends, clients, esc_order=None, flag=True):
        rp = RouterProvider(
            self.settings, env=self.env.env, db_path=self.env.db_path,
            client_factory=lambda be, model: clients[be.name])
        rp.backends = {b.name: b for b in backends}
        rp.orders = {"smart": [b.name for b in backends],
                     "fast": [b.name for b in backends],
                     "escalate": esc_order or
                     list(reversed([b.name for b in backends]))}
        rp.s.router_escalate = flag
        rp.s.router_escalate_rpd = 20
        return rp

    def test_flag_off_uses_smart(self):
        a, b = _backend("a"), _backend("b")
        c_a, c_b = FakeApiClient(), FakeApiClient()
        rp = self.router([a, b], {"a": c_a, "b": c_b}, flag=False)
        rp.chat([{"role": "user", "content": "hi"}],
                tier="escalate")
        # treated as smart: first in smart order, not escalate order
        self.assertEqual(len(c_a.calls), 1)
        self.assertEqual(len(c_b.calls), 0)

    def test_escalate_walks_its_order(self):
        a, b = _backend("a"), _backend("b")
        c_a = FakeApiClient(script=[Exception("boom")])
        c_b = FakeApiClient(script=[ok_msg("deep answer")])
        rp = self.router([a, b], {"a": c_a, "b": c_b},
                         esc_order=["b", "a"])
        msg, _ = rp.chat([{"role": "user", "content": "hi"}],
                         tier="escalate")
        self.assertEqual(msg["content"], "deep answer")
        self.assertEqual(len(c_b.calls), 1)
        self.assertEqual(rp.escalate_used()[0], 1)

    def test_rpd_cap_skips_escalate(self):
        a = _backend("a")
        c_a = FakeApiClient()
        rp = self.router([a], {"a": c_a}, esc_order=["a"])
        rp.s.router_escalate_rpd = 0
        with self.assertRaises(Exception) as cm:
            rp.chat([{"role": "user", "content": "hi"}],
                    tier="escalate")
        self.assertIn("escalate", str(cm.exception))
        self.assertEqual(len(c_a.calls), 0)

    def test_unknown_names_ignored_in_build(self):
        env_order = "groq,github-models,cerebras,mistral,nope"
        s = self.settings
        s.router_escalate = True
        s.router_escalate_order = env_order
        rp = RouterProvider(
            s, env={"PATH": ""}, db_path=self.env.db_path,
            client_factory=lambda be, model: FakeApiClient())
        for banned in ("github-models", "cerebras", "mistral",
                       "nope"):
            self.assertNotIn(banned, rp.orders.get("escalate", []))
        self.assertIn("groq", rp.orders["escalate"])


if __name__ == "__main__":
    unittest.main()

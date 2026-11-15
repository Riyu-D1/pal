"""Item 25: shareable Markdown pages."""

import unittest

from pa import chattools, pages
from tests.helpers import TmpEnv


class TestPages(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        aid = self.env.add_assistant(name="pal", tools=[])
        self.conn = self.env.conn()
        ast = self.conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        self.ctx = chattools.ChatToolContext(
            self.conn, self.env.settings, ast, "c1",
            env=self.env.env,
            request_approval=lambda *a: "approved")

    def test_publish_get_unpublish(self):
        out = chattools.run(self.ctx, "page_publish",
                            {"title": "Trip plan",
                             "markdown": "# Plan\n\n- one\n- two"})
        self.assertTrue(out["url"].endswith("/pages/" + out["slug"]))
        row = pages.get(self.conn, out["slug"])
        self.assertEqual(row["title"], "Trip plan")
        self.assertTrue(pages.unpublish(self.conn, out["slug"]))
        self.assertIsNone(pages.get(self.conn, out["slug"]))

    def test_custom_slug_and_republish(self):
        pages.publish(self.conn, "t", "body", slug="My Page!")
        row = pages.get(self.conn, "my-page")
        self.assertIsNotNone(row)
        pages.publish(self.conn, "t2", "body2", slug="my-page")
        self.assertEqual(pages.get(self.conn, "my-page")["title"], "t2")

    def test_empty_md(self):
        self.assertEqual(
            pages.publish(self.conn, "t", "  ")["error"],
            "bad_arguments")

    def test_render_escapes_html(self):
        html_out = pages.render("# Hi\n\n<script>alert(1)</script>"
                                "\n\n**bold** `code`", "T")
        self.assertIn("&lt;script&gt;", html_out)
        self.assertNotIn("<script>", html_out)
        self.assertIn("<strong>bold</strong>", html_out)
        self.assertIn("<code>code</code>", html_out)
        self.assertIn("<h1>T</h1>", html_out)
        self.assertIn("<h1>Hi</h1>", html_out)


if __name__ == "__main__":
    unittest.main()

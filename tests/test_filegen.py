"""Item 15+16: file generation incl. the built-in PDF writer."""

import unittest

from pa import chattools, filegen
from tests.helpers import TmpEnv


class TestRender(unittest.TestCase):
    def test_csv_md_txt(self):
        for name, ctype in (("a.csv", "text/csv"),
                            ("a.md", "text/markdown"),
                            ("a.txt", "text/plain"),
                            ("a.json", "application/json")):
            data, ct, n = filegen.render(name, "x,y\n1,2")
            self.assertEqual(ct, ctype, name)
            self.assertEqual(n, name)

    def test_bad_ext_refused(self):
        for bad in ("evil.sh", "x.exe", "y.html", "z", "../../a.csv"):
            try:
                data, ct, n = filegen.render(bad, "x")
            except ValueError:
                continue
            # safe_filename strips ../ to 'a.csv' — allowed then
            self.assertNotIn("/", n)

    def test_json_pretty(self):
        data, ct, _ = filegen.render("o.json", '{"b":1}')
        self.assertIn(b'"b": 1', data)

    def test_pdf_is_valid_structure(self):
        pdf = filegen.make_pdf("hello world", "T")
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertIn(b"/Type /Catalog", pdf)
        self.assertIn(b"/Pages", pdf)
        self.assertIn(b"(hello world) Tj", pdf)
        self.assertIn(b"xref", pdf)
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        # pypdf can read it back
        import io
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(pdf))
        self.assertGreaterEqual(len(r.pages), 1)
        self.assertIn("hello world", r.pages[0].extract_text())

    def test_pdf_escapes_parens(self):
        pdf = filegen.make_pdf("a ) b ( \\c")
        self.assertIn(rb"a \) b \( \\c", pdf)


class TestTool(unittest.TestCase):
    def test_file_write_attaches(self):
        env = TmpEnv()
        aid = env.add_assistant(name="pal", tools=[])
        conn = env.conn()
        ast = conn.execute(
            "SELECT * FROM assistants WHERE id=?", (aid,)).fetchone()
        ctx = chattools.ChatToolContext(
            conn, env.settings, ast, "c", env=env.env)
        got = []
        ctx.post_file = lambda ch, fn, ct, data: (
            got.append((fn, ct)), True)[1]
        out = chattools.run(ctx, "file_write",
                            {"filename": "report.csv",
                             "content": "a,b\n1,2"})
        self.assertEqual(out["attached"], "report.csv")
        self.assertEqual(got[0], ("report.csv", "text/csv"))
        out = chattools.run(ctx, "file_write",
                            {"filename": "notes.pdf",
                             "content": "hello"})
        self.assertEqual(out["content_type"], "application/pdf")
        out = chattools.run(ctx, "file_write",
                            {"filename": "evil.sh", "content": "x"})
        self.assertEqual(out["error"], "bad_arguments")


if __name__ == "__main__":
    unittest.main()

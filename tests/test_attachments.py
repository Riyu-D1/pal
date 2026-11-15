"""Inbound image/PDF attachments + the general attach-out path.

- images/PDFs in a DM are downloaded off the Discord CDN (mocked),
  saved under the chat sandbox and sent into the chat turn as a
  fenced untrusted block — never the importer.
- PDFs get pypdf text extraction (capped pages/chars); near-empty
  extraction is called out, not OCR'd.
- Oversize/wrong-type attachments are refused.
- ctx.attach posts small files to the channel, saves big ones.
"""

import io
import threading
import time
import unittest

from pa import attachments, chattools, discordbot
from pa import voice as voicemod
from tests.helpers import FakeGateway, Fixture, TmpEnv, wait_for

OWNER = "owner-1"
BOT = "999"

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
       + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
       + b"\x08\x06\x00\x00\x00" + b"rest")


def _pdf_fixture(text="Hello PDF world, this is a longer sentence"):
    from pypdf import PdfWriter
    from pypdf.generic import (DecodedStreamObject, DictionaryObject,
                               NameObject)
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")})
    ref = w._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject(
            {NameObject("/F1"): ref})})
    stream = DecodedStreamObject()
    stream.set_data(
        f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _event(t, data, seq):
    return {"op": 0, "s": seq, "t": t, "d": data}


def _dm_att(user, cid, mid, content, att):
    return {"id": mid, "channel_id": cid, "channel_type": 1,
            "author": {"id": user}, "content": content,
            "attachments": [att]}


class Engine:
    def __init__(self):
        self.calls = []

    def run_turn(self, channel_id, text, deadline_seconds=None,
                 progress=None, **kw):
        self.calls.append((channel_id, text))
        return {"text": "got it", "steps": 0, "trace": []}


class TestClassify(unittest.TestCase):
    def test_image_kinds(self):
        for ct, fn in [("image/png", "a"), ("image/jpeg", "b"),
                       ("image/webp", "c"), ("image/gif", "d"),
                       ("", "e.PNG"), ("", "f.jpeg")]:
            self.assertTrue(attachments.is_image(
                {"content_type": ct, "filename": fn}))
        self.assertFalse(attachments.is_image(
            {"content_type": "application/pdf", "filename": "x.pdf"}))

    def test_pdf_kinds(self):
        self.assertTrue(attachments.is_pdf(
            {"content_type": "application/pdf", "filename": "x"}))
        self.assertTrue(attachments.is_pdf(
            {"content_type": "", "filename": "x.pdf"}))
        self.assertFalse(attachments.is_pdf(
            {"content_type": "text/plain", "filename": "x.txt"}))

    def test_png_dimensions(self):
        self.assertEqual(attachments.image_dimensions(PNG), (640, 480))
        self.assertIsNone(attachments.image_dimensions(b"nope"))

    def test_safe_filename(self):
        self.assertEqual(attachments.safe_filename("../../etc/passwd"),
                         "passwd")
        self.assertEqual(attachments.safe_filename("a b.pdf"),
                         "a_b.pdf")


class TestPdfExtract(unittest.TestCase):
    def test_text_roundtrip(self):
        out = attachments.extract_pdf_text(_pdf_fixture())
        self.assertIn("Hello PDF world", out["text"])
        self.assertEqual(out["pages"], 1)
        self.assertFalse(out["truncated_pages"])

    def test_tiny_text_is_flagged_not_ocrd(self):
        block = attachments.pdf_turn_text(
            "scan.pdf", "/tmp/scan.pdf",
            {"text": "", "pages": 3,
             "truncated_pages": False, "truncated_chars": False})
        self.assertIn("UNTRUSTED PDF", block)
        self.assertIn("does not OCR", block)

    def test_caps(self):
        out = attachments.extract_pdf_text(
            _pdf_fixture("x" * 200), max_chars=50)
        self.assertTrue(len(out["text"]) <= 50)


class _Base(unittest.TestCase):
    def setUp(self):
        self.gw = FakeGateway()
        self.fx = Fixture()
        self.env = TmpEnv(
            DISCORD_BOT_TOKEN="fake-bot-token",
            DISCORD_OWNER_ID=OWNER,
            DISCORD_API_BASE=self.fx.web_base,
            DISCORD_GATEWAY_URL=self.gw.url)
        self.env.conn().close()
        self.engine = Engine()
        self.bot = discordbot.DiscordBot(
            self.env.settings, env=self.env.env, notifier=False,
            engine=self.engine)
        self.t = None
        self._dl = voicemod.download_attachment

    def tearDown(self):
        voicemod.download_attachment = self._dl
        self.bot.stop()
        self.gw.close()
        self.fx.close()
        if self.t:
            self.t.join(timeout=5)

    def _drive(self, script):
        self.gw.scripts.append(script)
        self.t = threading.Thread(target=self.bot.run_forever,
                                  daemon=True)
        self.t.start()

    def _hello_ready(self, g):
        g.send({"op": 10, "d": {"heartbeat_interval": 30000}})
        g.recv()
        g.send(_event("READY", {"session_id": "s1",
                                "resume_gateway_url": self.gw.url,
                                "application": {"id": "app1"},
                                "user": {"id": BOT}}, 1))

    def _channel_posts(self):
        return [c for c in self.fx.calls
                if c.get("method") == "POST" and
                str(c.get("path", "")).startswith("/channels/")]

    def _last_reply(self):
        posts = [c for c in self._channel_posts()
                 if str(c["path"]).endswith("/messages")]
        return posts[-1]["body"]["content"] if posts else ""


class TestInbound(_Base):
    def _send(self, att, content="", mid="m1"):
        def script(g):
            self._hello_ready(g)
            g.send(_event("MESSAGE_CREATE",
                          _dm_att(OWNER, "dm1", mid, content, att), 2))
            wait_for(lambda: self.engine.calls or
                         "couldn't" in self._last_reply(), timeout=10)
            time.sleep(0.4)
            g.drop()
        self._drive(script)
        wait_for(lambda: self.engine.calls or
                 "couldn't" in self._last_reply(), timeout=10)
        time.sleep(0.3)

    def test_image_routes_fenced_with_dims(self):
        voicemod.download_attachment = lambda s, url, cap=None: PNG
        self._send({"url": "https://cdn.discordapp.com/a/cat.png",
                    "filename": "cat.png",
                    "content_type": "image/png"},
                   content="what breed?")
        cid, text = self.engine.calls[0]
        self.assertEqual(cid, "dm1")
        self.assertIn("what breed?", text)
        self.assertIn("[Attached image: cat.png — 640x480", text)
        self.assertIn("UNTRUSTED IMAGE NOTE", text)
        self.assertNotIn("imported", self._last_reply())

    def test_pdf_extract_fenced_with_caption(self):
        voicemod.download_attachment = \
            lambda s, url, cap=None: _pdf_fixture()
        self._send({"url": "https://cdn.discordapp.com/a/spec.pdf",
                    "filename": "spec.pdf",
                    "content_type": "application/pdf"},
                   content="summarise this")
        cid, text = self.engine.calls[0]
        self.assertIn("summarise this", text)
        self.assertIn("<<<UNTRUSTED PDF spec.pdf>>>", text)
        self.assertIn("Hello PDF world, this is", text)
        self.assertIn("<<<END UNTRUSTED PDF>>>", text)

    def test_download_failure_replies(self):
        def boom(s, url, cap=None):
            raise voicemod.VoiceError("attachment too large")
        voicemod.download_attachment = boom
        self._send({"url": "https://cdn.discordapp.com/a/big.png",
                    "filename": "big.png",
                    "content_type": "image/png"})
        self.assertEqual(self.engine.calls, [])
        self.assertIn("couldn't read that image", self._last_reply())

    def test_unknown_type_ignored(self):
        self._send({"url": "https://cdn.discordapp.com/a/x.exe",
                    "filename": "x.exe",
                    "content_type": "application/octet-stream"})
        time.sleep(0.4)
        self.assertEqual(self.engine.calls, [])


class TestAttachOut(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()

    def _ctx(self, post_file=None):
        conn = self.env.conn()
        return chattools.ChatToolContext(
            conn, self.env.settings, None, "chan-1",
            env=self.env.env, post_file=post_file)

    def test_small_file_posts(self):
        seen = []
        ctx = self._ctx(
            post_file=lambda cid, fn, ct, data:
            seen.append((fn, ct, data)) or True)
        out = ctx.attach("data.csv", "text/csv", b"a,b\n1,2\n")
        self.assertEqual(out, {"attached": "data.csv"})
        self.assertEqual(seen[0][1], "text/csv")
        self.assertEqual(seen[0][2], b"a,b\n1,2\n")

    def test_oversize_or_no_channel_saves(self):
        ctx = self._ctx()                    # post_file=None
        out = ctx.attach("data.csv", "text/csv", b"x" * 40)
        self.assertIn("data.csv", out["path"])

        ctx2 = self._ctx(post_file=lambda *a: True)
        out2 = ctx2.attach("big.bin", "application/octet-stream",
                           b"x" * (9 * 1024 * 1024))
        self.assertIn("big.bin", out2["path"])


if __name__ == "__main__":
    unittest.main()

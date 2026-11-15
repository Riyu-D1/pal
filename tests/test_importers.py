"""Chat-history importers: ChatGPT zip -> per-conversation notes +
monthly digests under Imports/, generic .md/.json files, idempotent
re-runs, and the DM routing that feeds them."""

import io
import json
import os
import unittest
import zipfile

from pa import discordbot, importers
from pa.importers import chatgpt, generic
from tests.helpers import TmpEnv


def conv(cid, title, ts, pairs):
    mapping = {}
    prev = None
    for i, (role, text) in enumerate(pairs):
        nid = f"{cid}-{i}"
        node = {"message": {"author": {"role": role},
                            "content": {"parts": [text]}},
                "children": [], "parent": prev}
        if prev:
            mapping[prev]["children"] = [nid]
        mapping[nid] = node
        prev = nid
    mapping[f"{cid}-root"] = {"children": [f"{cid}-0"],
                              "parent": None, "message": None}
    # make root the actual parentless node
    mapping = {f"{cid}-root": {"children": [f"{cid}-0"],
                              "parent": None, "message": None},
               **{k: v for k, v in mapping.items()
                  if k != f"{cid}-root"}}
    return {"id": cid, "title": title, "create_time": ts,
            "mapping": mapping}


def export_zip(convs):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("conversations.json", json.dumps(convs))
    return buf.getvalue()


class TestChatGPT(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.vault = os.path.join(self.env.dir, "vault")
        os.makedirs(self.vault)
        self.env.settings.vault_path = self.vault
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def test_import_writes_notes_and_digest_idempotent(self):
        blob = export_zip([
            conv("c1", "Trip plan", 1735689600,       # 2025-01-01
                 [("user", "plan a trip"), ("assistant", "ok")]),
            conv("c2", "Code review", 1738368000,     # 2025-02-01
                 [("user", "review this"), ("assistant", "lgtm")]),
        ])
        out = importers.run(self.conn, self.env.settings, self.env.env,
                            blob, "export.zip")
        self.assertEqual(out["imported"], 2)
        self.assertEqual(out["digests"], 2)
        self.assertEqual(out["source"], "chatgpt")
        names = sorted(os.listdir(os.path.join(self.vault,
                                               "Imports", "ChatGPT")))
        self.assertTrue(any("trip-plan" in n for n in names))
        self.assertTrue(any("code-review" in n for n in names))
        self.assertTrue(any("_digest-2025-01" in n for n in names))
        self.assertTrue(any("_digest-2025-02" in n for n in names))
        with open(os.path.join(
                self.vault, "Imports", "ChatGPT",
                next(n for n in names if "trip-plan" in n))) as f:
            body = f.read()
        self.assertIn("**You:**", body)
        self.assertIn("plan a trip", body)
        self.assertIn("**ChatGPT:**", body)
        # re-run: everything skipped, nothing duplicated
        out = importers.run(self.conn, self.env.settings, self.env.env,
                            blob, "export.zip")
        self.assertEqual(out["imported"], 0)
        self.assertEqual(out["skipped"], 2)

    def test_bare_conversations_json(self):
        blob = json.dumps([
            conv("c9", "Solo", 1735689600,
                 [("user", "hi"), ("assistant", "hey")])]).encode()
        out = chatgpt.run(self.conn, self.env.settings, self.env.env,
                          blob, "conversations.json")
        self.assertEqual(out["imported"], 1)

    def test_detect(self):
        self.assertEqual(importers.detect(b"{}", "conversations.json"),
                         "chatgpt")
        self.assertEqual(importers.detect(b"# hi", "note.md"),
                         "generic")
        self.assertEqual(importers.detect(export_zip([]), "x.zip"),
                         "chatgpt")


class TestGeneric(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.vault = os.path.join(self.env.dir, "vault")
        os.makedirs(self.vault)
        self.env.settings.vault_path = self.vault
        self.conn = self.env.conn()

    def tearDown(self):
        self.conn.close()

    def test_md_and_json_list(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("notes/journal.md", "# journal\nstuff")
            z.writestr("chats.json", json.dumps([
                {"id": "a", "title": "One",
                 "messages": [{"role": "user", "text": "hi"}]},
                {"id": "b", "title": "Two",
                 "messages": [{"role": "user", "text": "yo"}]}]))
            z.writestr("bin.dat", "skipped")
        out = generic.run(self.conn, self.env.settings, self.env.env,
                          buf.getvalue(), "x.zip")
        self.assertEqual(out["imported"], 3)
        names = sorted(os.listdir(os.path.join(self.vault,
                                               "Imports", "Files")))
        self.assertIn("journal.md", names)
        self.assertIn("one.md", names)
        self.assertIn("two.md", names)
        out = generic.run(self.conn, self.env.settings, self.env.env,
                          buf.getvalue(), "x.zip")
        self.assertEqual(out["imported"], 0)
        self.assertEqual(out["skipped"], 3)


class TestRouting(unittest.TestCase):
    def test_dm_zip_attachment_routes_to_import(self):
        env = TmpEnv(DISCORD_OWNER_ID="o1")
        env.conn().close()
        bot = discordbot.DiscordBot(env.settings, env=env.env,
                                    notifier=False)
        d = {"channel_id": "dm1", "guild_id": None,
             "author": {"id": "o1"}, "content": "",
             "attachments": [{"filename": "export.zip",
                              "url": "http://x"}]}
        self.assertEqual(bot._message_route(d), "import")
        d["attachments"] = [{"filename": "v.ogg", "url": "http://x",
                             "content_type": "audio/ogg"}]
        self.assertEqual(bot._message_route(d), "voice")
        d["attachments"] = []
        d["content"] = "hello"
        self.assertEqual(bot._message_route(d), "chat")


if __name__ == "__main__":
    unittest.main()

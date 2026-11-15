"""Chat-history importers -> vault notes under Imports/<source>/.

Each importer exposes ``run(conn, s, env, blob, filename) -> dict`` and
skips anything already present in ``imported_items`` (PRIMARY KEY
(source, external_id)), so re-uploading the same export is a no-op.
The dispatcher picks chatgpt when the payload contains the ChatGPT
export shape (a zip with conversations.json, or the bare JSON), and
falls back to the generic importer for .md/.txt/.json files.
"""

import io
import zipfile


def detect(blob: bytes, filename: str = "") -> str:
    """'chatgpt' | 'generic' from a quick look at the payload."""
    name = (filename or "").lower()
    if name.endswith(".zip") or blob[:4] == b"PK\x03\x04":
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
            names = {n.split("/")[-1] for n in zf.namelist()}
            return "chatgpt" if "conversations.json" in names \
                else "generic"
        except zipfile.BadZipFile:
            return "generic"
    if name.endswith("conversations.json"):
        return "chatgpt"
    return "generic"


def run(conn, s, env, blob: bytes, filename: str = "") -> dict:
    kind = detect(blob, filename)
    if kind == "chatgpt":
        from . import chatgpt
        return chatgpt.run(conn, s, env, blob, filename)
    from . import generic
    return generic.run(conn, s, env, blob, filename)

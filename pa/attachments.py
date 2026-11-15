"""Inbound file attachments (images, PDFs) for chat turns.

Discord attachments arrive in the message payload; the bytes come off
the allowlisted Discord CDN via voicemod.download_attachment and land
under the chat sandbox. What reaches the model is always a fenced
untrusted block — never raw instructions:

- Images: saved locally; the turn sees "[Attached image: name — WxH]"
  plus a vision caption when PA_VISION is configured, else a
  plain "can't see it yet" note. Bytes never go near the importer.
- PDFs: text pulled with pypdf (pinned pure-Python dep), capped at
  PA_PDF_MAX_PAGES pages / PA_PDF_MAX_CHARS chars and fenced as
  <<<UNTRUSTED PDF>>>. Near-empty extraction is called out — pal does
  not pretend to OCR scans.

Attach-out: ctx.attach(filename, ctype, data) posts the file to the
channel when the Discord cap allows (~8 MB) and drops it in the
sandbox otherwise — same shape as browse_screenshot.
"""

import io
import logging
import os
import re
import struct

log = logging.getLogger(__name__)

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_PDF_TYPES = {"application/pdf"}

ATTACH_CAP = 8 * 1024 * 1024          # Discord free-tier upload cap


def is_image(att: dict) -> bool:
    ct = str(att.get("content_type") or "").lower()
    if ct in _IMAGE_TYPES:
        return True
    fn = str(att.get("filename") or "").lower()
    return ct.startswith("image/") or fn.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif"))


def is_pdf(att: dict) -> bool:
    ct = str(att.get("content_type") or "").lower()
    fn = str(att.get("filename") or "").lower()
    return ct in _PDF_TYPES or fn.endswith(".pdf")


def image_dimensions(data: bytes):
    """(w, h) from PNG/JPEG/GIF/WebP headers, or None. Stdlib only."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return int(w), int(h)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
            return int(w), int(h)
        if data[:2] == b"\xff\xd8":             # JPEG: scan for SOF
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return int(w), int(h)
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                b = data[21:25]
                v = int.from_bytes(b, "little")
                return (v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1
            if kind == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
    except (struct.error, IndexError):
        return None
    return None


def extract_pdf_text(data: bytes, max_pages: int = 20,
                     max_chars: int = 50_000) -> dict:
    """Pull text with pypdf. Returns {text, pages, truncated_pages,
    truncated_chars}; raises ValueError on unreadable files."""
    from pypdf import PdfReader        # pinned pure-Python dep
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data))
    except (PdfReadError, ValueError) as e:
        raise ValueError(f"not a readable PDF: {str(e)[:120]}")
    total = len(reader.pages)
    chunks = []
    used = 0
    for page in reader.pages[:max_pages]:
        try:
            t = page.extract_text() or ""
        except Exception:             # one bad page mustn't kill it
            continue
        chunks.append(t)
        used += len(t)
        if used >= max_chars:
            break
    text = "\n".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars]
    return {"text": text.strip(), "pages": total,
            "truncated_pages": total > max_pages,
            "truncated_chars": used > max_chars}


def safe_filename(name: str) -> str:
    """Attachment filename -> safe local name (no path parts)."""
    base = os.path.basename(name or "attachment")
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:80] or "attachment"


def save_to_sandbox(s, channel_id, filename, data) -> str:
    """Attachments live under chat_sandbox/<channel>/; fall back to
    the sandbox root if the per-channel dir can't be made."""
    fname = safe_filename(filename)
    for folder in (os.path.join(s.chat_sandbox, str(channel_id)[:48]),
                   s.chat_sandbox):
        try:
            os.makedirs(folder, mode=0o700, exist_ok=True)
            path = os.path.join(folder, fname)
            with open(path, "wb") as f:
                f.write(data)
            return path
        except OSError:
            continue
    return ""


def image_turn_text(filename: str, data: bytes, path: str,
                    caption: str = None) -> str:
    """The untrusted block the chat turn sees for an image. `caption`
    is the item-4 vision caption (already fenced by vision.py) or a
    stub when no backend is configured."""
    dims = image_dimensions(data)
    dim = f" — {dims[0]}x{dims[1]}" if dims else ""
    head = (f"[Attached image: {safe_filename(filename)}{dim}"
            f" — saved to {path}]")
    if caption:
        return f"{head}\n{caption}"
    return (f"{head}\n<<<UNTRUSTED IMAGE NOTE>>>\n"
            "an image was attached but vision isn't configured — "
            "describe it only if the owner told you what's in it\n"
            "<<<END UNTRUSTED IMAGE NOTE>>>")


def pdf_turn_text(filename: str, path: str, extracted: dict) -> str:
    """The fenced UNTRUSTED PDF block for the chat turn."""
    name = safe_filename(filename)
    text = extracted["text"]
    meta = []
    if extracted["truncated_pages"]:
        meta.append(f"only first {20} of {extracted['pages']} pages")
    if extracted["truncated_chars"]:
        meta.append("text capped")
    note = f" ({'; '.join(meta)})" if meta else ""
    head = (f"[Attached PDF: {name} — {extracted['pages']} "
            f"page(s){note} — saved to {path}]")
    if len(text) < 30:
        return (f"{head}\n<<<UNTRUSTED PDF {name}>>>\n"
                "(little or no extractable text — this may be a "
                "scanned document; pal does not OCR)\n"
                "<<<END UNTRUSTED PDF>>>")
    return (f"{head}\n<<<UNTRUSTED PDF {name}>>>\n{text}\n"
            "<<<END UNTRUSTED PDF>>>")

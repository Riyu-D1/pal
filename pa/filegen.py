"""File generation (docs/PARITY.md #16).

file_write(filename, content) — write CSV/Markdown/plain-text/JSON
straight to the sandbox and attach it to the channel (same path as
browse_screenshot). `.pdf` is rendered by a tiny built-in writer —
pypdf only reads, and no new dependency is wanted — producing a
plain one-column Helvetica document (title + wrapped lines).
"""

import json
import re
import textwrap

from .attachments import safe_filename

MAX_CONTENT = 200_000
ALLOWED_EXT = {".csv", ".md", ".txt", ".json", ".pdf"}


def _ext(name: str) -> str:
    m = re.search(r"(\.[A-Za-z0-9]{1,5})$", name or "")
    return m.group(1).lower() if m else ""


def _pdf_escape(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(") \
            .replace(")", r"\)").encode("latin-1", "replace") \
            .decode("latin-1")


def make_pdf(text: str, title: str = "") -> bytes:
    """Minimal valid single-font PDF: wrapped Helvetica lines, one
    page per ~46 lines. No deps, no images — plain text only."""
    lines = []
    if title:
        lines.append(title.strip())
        lines.append("")
    for para in textwrap.wrap(text or "", 92,
                              break_long_words=False) or [""]:
        lines.append(para)
    pages = [lines[i:i + 46] for i in range(0, len(lines), 46)] \
        or [[""]]

    # objects: 1 catalog, 2 pages, 3 font, then per page a content
    # stream (5,7,9...) and a page object (6,8,10...)
    page_ids = [6 + i * 2 for i in range(len(pages))]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids "
        f"[{' '.join(f'{p} 0 R' for p in page_ids)}] /Count "
        f"{len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< >>",                # object 4: spacer keeps ids stable
    ]
    for i, page_lines in enumerate(pages):
        content_id = 5 + i * 2
        ops = ["BT /F1 11 Tf 50 780 Td 16 TL"]
        ops += [f"({_pdf_escape(ln)}) Tj T*" for ln in page_lines]
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objects.append(b"<< /Length " + str(len(stream)).encode()
                       + b" >>\nstream\n" + stream + b"\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox"
                       f" [0 0 612 792] /Contents {content_id} 0 R"
                       f" /Resources << /Font << /F1 3 0 R >> >> >>"
                       .encode())

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1}"
            f" /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF").encode()
    return bytes(out)


def render(filename: str, content) -> tuple:
    """-> (bytes, content_type). Raises ValueError on a bad name."""
    name = safe_filename(filename)
    ext = _ext(name)
    if ext not in ALLOWED_EXT:
        raise ValueError(
            f"only {', '.join(sorted(ALLOWED_EXT))} files can be "
            "generated")
    text = str(content or "")[:MAX_CONTENT]
    if ext == ".json":
        try:
            text = json.dumps(json.loads(text), indent=2) \
                if isinstance(content, str) else json.dumps(
                    content, indent=2, default=str)
        except Exception:
            pass  # leave it as text — the owner sees what pal wrote
        return text.encode(), "application/json", name
    if ext == ".pdf":
        title = re.sub(r"\.pdf$", "", name, flags=re.I)
        return make_pdf(text, title), "application/pdf", name
    return text.encode(), ("text/csv" if ext == ".csv"
                           else "text/markdown" if ext == ".md"
                           else "text/plain"), name

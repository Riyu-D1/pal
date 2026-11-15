"""Logging: metadata only, with a scrubber for anything shaped like a key.

Logs must never contain request bodies or secrets. The scrub filter
redacts common key shapes defensively; tests plant a secret and verify it
shows up nowhere.
"""

import logging
import re
import sys

_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{8,}"),
    re.compile(r"tvly-[A-Za-z0-9-]{6,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(gh[pousr]|github_pat)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{8,}"),
    re.compile(r"ya29\.[A-Za-z0-9_-]{8,}"),          # Google OAuth token
    re.compile(r"[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{10,}"),  # Discord
    re.compile(r"(?i)(bearer|authorization:)\s+\S+"),
    re.compile(r"(?i)\bbot\s+[A-Za-z0-9._-]{20,}"),  # "Bot <token>" auth
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def scrub_text(text: str) -> str:
    """Replace credential-shaped substrings with [redacted] — the same
    patterns ScrubFilter applies to log lines, for reuse by anything
    that stores text (incidents, digests, eval fixtures)."""
    out = str(text or "")
    for p in _PATTERNS:
        out = p.sub("[redacted]", out)
    return out


class ScrubFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        scrubbed = scrub_text(msg)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True


def setup_logging(level=logging.INFO):
    root = logging.getLogger()
    if any(getattr(h, "_pa", False) for h in root.handlers):
        return
    h = logging.StreamHandler(sys.stderr)
    h._pa = True
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    h.addFilter(ScrubFilter())
    root.addHandler(h)
    root.setLevel(level)

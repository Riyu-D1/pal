#!/usr/bin/env python3
"""Fail if personal data or secret-looking material is in the repo.

Two layers:

1. A **denylist** of strings/patterns that must never appear supplied at run time via
   `PAL_DENYLIST`/`PAL_DENYLIST_FILE`, so the patterns themselves are
   never committed) plus generic shapes.
2. **Secret-shaped patterns** — private keys, cloud tokens, Discord
   bot tokens, long API-key-looking assignments — checked everywhere
   except explicit allowlisted test fixtures.

Usage:  python scripts/check_no_personal_data.py [root]
Exit:   0 clean, 1 hits, 2 usage error. Output lists file:line only —
never the matched secret itself.
"""

import os
import re
import sys

# --- personal identifiers (denylist) -------------------------------------
# Format: (label, regex, ignore_case). Keep entries specific — broad
# patterns (e.g. plain "gmail.com") catch legitimate references.
DENYLIST = [
    # Your own identifiers (name, IDs, IPs, home location) go in
    # PAL_DENYLIST / PAL_DENYLIST_FILE (or the CI secret PAL_DENYLIST),
    # never in this file: listing them here would publish them.
    # generic shapes that only ever mean a real person's data
    ("real discord user id", r"discord_owner_id\s*[=:]\s*[\"']?\d{15,20}", True),
    ("real ipv4 literal", r"\b(?:\d{1,3}\.){3}\d{1,3}\b", False),
    ("supabase ref", r"\bsupabase\b", True),
    ("oracle cloud id", r"\bocid1\.", True),
]

# lines excused for a given denylist label (e.g. the public repo URL
# legitimately contains the org name)
DENYLIST_LINE_ALLOW = {
}

# IPv4 literals that are documentation, not personal data: private,
# loopback, link-local, CGNAT (tailnet), multicast, TEST-NET examples
# (RFC 5737: 192.0.2/24, 198.51.100/24, 203.0.113/24), example.com's
# well-known address, public DNS anycast.
_IPV4_ALLOW = re.compile(
    r"^(?:127\.|0\.0\.0\.0|100\.6[4-9]\.|100\.[7-9]\d\.|100\.1[01]\d\.|"
    r"100\.12[0-7]\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|"
    r"169\.254\.|255\.|8\.8\.[48]|1\.1\.1\.1|9\.9\.9\.9|224\.|"
    r"192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|93\.184\.216\.34|"
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.0$|0\.0\.)")

# trees where secret-shaped literals are test fixtures by definition —
# the personal-data denylist still applies everywhere
SECRET_PATTERN_EXEMPT_DIRS = ("tests/", "evals/", "scripts/")

# files/dirs never scanned (venv, git, binary blobs, the scanner itself)
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
             "data", ".mypy_cache", ".pytest_cache"}
SKIP_NAMES = {"check_no_personal_data.py"}
SCAN_EXT = {".py", ".md", ".txt", ".sh", ".yml", ".yaml", ".toml",
            ".json", ".example", ".service", ".cfg", ".ini", ".html",
            ".css", ".js", ".env", ".log", ""}

# --- secret-shaped patterns ----------------------------------------------
SECRET_PATTERNS = [
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("discord bot token", r"\bM[TA-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27,}\b"),
    ("aws access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("github pat", r"\bghp_[A-Za-z0-9]{36,}\b"),
    ("github oauth", r"\bgho_[A-Za-z0-9]{36,}\b"),
    ("openai key", r"\bsk-[A-Za-z0-9]{20,}\b"),
    ("groq key", r"\bgsk_[A-Za-z0-9]{20,}\b"),
    ("tavily key", r"\btvly-[A-Za-z0-9]{17,}\b"),
    ("google api key", r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    ("slack token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ("generic secret assign",
     r"(?i)\b(?:api[_-]?key|secret|token|password|passphrase)\b"
     r"\s*[:=]\s*[\"']?[A-Za-z0-9/+=._\-]{32,}[\"']?"),
]

# paths where secret-shaped strings are expected (test fixtures use
# clearly-fake values; the denylist still applies everywhere)
SECRET_FIXTURE_RE = re.compile(
    r"(?i)(?:fake|dummy|test|example|sample|not.?a.?real|xxxx+|"
     r"abc123|placeholder|your[_-]|<[a-z_ -]+>|REDACT)")


def _load_extra_denylist(env):
    """Extra patterns from PAL_DENYLIST (os.sep-split) or a file."""
    out = []
    raw = env.get("PAL_DENYLIST", "")
    for item in raw.split(os.pathsep):
        item = item.strip()
        if item:
            out.append(("custom denylist", item, True))
    path = env.get("PAL_DENYLIST_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        out.append(("custom denylist", line, True))
        except OSError as e:
            print(f"cannot read PAL_DENYLIST_FILE {path}: {e}",
                  file=sys.stderr)
            sys.exit(2)
    return out


def _iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in SKIP_NAMES:
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in SCAN_EXT:
                continue
            yield os.path.join(dirpath, name)


def _line_is_fixtured(line):
    return bool(SECRET_FIXTURE_RE.search(line))


def scan(root):
    deny = list(DENYLIST) + _load_extra_denylist(os.environ)
    deny_res = [(label, re.compile(pat, re.I if ci else 0))
                for label, pat, ci in deny]
    sec_res = [(label, re.compile(pat))
               for label, pat in SECRET_PATTERNS]
    hits = []
    for path in sorted(_iter_files(root)):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        rel = os.path.relpath(path, root)
        rel_posix = rel.replace(os.sep, "/")
        secret_exempt = rel_posix.startswith(SECRET_PATTERN_EXEMPT_DIRS)
        for n, line in enumerate(lines, 1):
            for label, rx in deny_res:
                m = rx.search(line)
                if not m:
                    continue
                if label == "real ipv4 literal" and _IPV4_ALLOW.match(
                        m.group(0)):
                    continue
                allow = DENYLIST_LINE_ALLOW.get(label)
                if allow is not None and allow.search(line):
                    continue
                hits.append((rel, n, f"denylist:{label}",
                             line.strip()[:120]))
                break
            if secret_exempt:
                continue
            for label, rx in sec_res:
                if rx.search(line) and not _line_is_fixtured(line):
                    hits.append((rel, n, f"secret:{label}",
                                 line.strip()[:120]))
                    break
    return hits


def main(argv):
    root = argv[1] if len(argv) > 1 else os.getcwd()
    if not os.path.isdir(root):
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    hits = scan(root)
    if hits:
        print(f"FAIL — {len(hits)} personal-data/secret hit(s):")
        for rel, n, kind, text in hits:
            # never echo the secret itself: show file:line + kind only
            shown = text if not kind.startswith("secret:") else \
                text[:60] + "…"
            print(f"  {rel}:{n}  [{kind}]  {shown}")
        return 1
    print(f"OK — scanned {root}: no personal data, no secret-shaped "
          f"strings")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

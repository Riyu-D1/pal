"""Minimal `.env` loader — stdlib only, deliberately tiny.

Every `python -m pa...` entrypoint loads the env file before config
is read: `$PA_ENV_FILE` when set, else `.env` in the current working
directory. Variables already in the environment always win — an
exported var or a systemd `EnvironmentFile=` is never overridden by
the file, and a missing file is simply not an error.

Supported syntax: `KEY=VALUE`, optional `export ` prefix, single or
double quotes around the value, `#` comments (a ` #` sequence ends an
unquoted value), blank lines. No expansion, no multiline values.
"""

import os
import re

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_line(line: str):
    """One line -> (key, value) or None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    key, sep, val = line.partition("=")
    if not sep or not _KEY_RE.fullmatch(key.strip()):
        return None
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    elif val[:1] not in ("\"", "'"):
        cut = val.find(" #")
        if cut >= 0:
            val = val[:cut].rstrip()
    return key.strip(), val


def load(path: str = None, environ=None) -> int:
    """Set env vars from the file; return how many were applied.

    Existing entries in `environ` are never overridden. A missing or
    unreadable file applies nothing."""
    environ = os.environ if environ is None else environ
    path = path or environ.get("PA_ENV_FILE") or ".env"
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    applied = 0
    for line in lines:
        item = parse_line(line)
        if item is None or item[0] in environ:
            continue
        environ[item[0]] = item[1]
        applied += 1
    return applied

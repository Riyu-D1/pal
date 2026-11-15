"""Deploy identity + process start.

git_sha() is the short commit the service runs — PA_GIT_SHA when the
deploy stamps it, else a best-effort `git rev-parse` in the repo. /status
shows it, and the chat agent watches it to notice redeploys.
STARTED_AT is module import time — close enough to process start for
uptime.
"""

import functools
import os
import subprocess
import time

STARTED_AT = time.time()


@functools.lru_cache(maxsize=1)
def git_sha(cwd: str = "") -> str:
    v = (os.environ.get("PA_GIT_SHA") or os.environ.get("GIT_SHA")
         or "").strip()
    if v:
        return v[:12]
    try:
        root = cwd or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=3)
        if out.returncode == 0:
            return out.stdout.strip()[:12]
    except Exception:
        pass
    return ""

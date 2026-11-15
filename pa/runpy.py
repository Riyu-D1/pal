"""Restricted run_python (docs/PARITY.md #17).

Runs a short Python snippet INSIDE the chat sandbox:

- `python3 -I -c <code>` — isolated mode: no cwd on sys.path, no
  user site-packages, no PYTHON* env honoured.
- cwd is the chat sandbox; env is scrubbed to PATH/HOME/LANG — no
  API keys or tokens can leak into the process.
- POSIX rlimits: 512MB address space, 10s CPU, 8MB file size,
  64 processes (no-ops on platforms without resource).
- Timeout + bounded output like sandbox.run; code capped at 20k.

The tool itself is ALWAYS approval-gated — same rule as shell.
"""

import os
import subprocess
import sys

CODE_CAP = 20000


def _limits():                     # POSIX only — no-op elsewhere
    try:
        import resource
    except ImportError:
        return None

    def apply():
        # each limit is best-effort: RLIMIT_AS doesn't exist on
        # macOS, NPROC/FSIZE can be refused — never fail the spawn
        for name, val in (
                ("RLIMIT_AS", (512 * 1024 * 1024,) * 2),
                ("RLIMIT_CPU", (10, 10)),
                ("RLIMIT_FSIZE", (8 * 1024 * 1024,) * 2),
                ("RLIMIT_NPROC", (64, 64))):
            try:
                resource.setrlimit(getattr(resource, name), val)
            except (ValueError, OSError, AttributeError):
                pass
    return apply


def run(settings, code: str, timeout: float = None):
    """-> {ok, exit, stdout, stderr, cwd} or {error}."""
    code = str(code or "")
    if not code.strip():
        return {"error": "bad_arguments", "message": "empty code"}
    if len(code) > CODE_CAP:
        return {"error": "too_large",
                "message": f"code capped at {CODE_CAP} chars"}
    sandbox = settings.chat_sandbox
    os.makedirs(sandbox, exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": sandbox, "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    cap = settings.shell_output_cap
    kw = {}
    lim = _limits()
    if lim is not None and os.name == "posix":
        kw["preexec_fn"] = lim
    try:
        p = subprocess.run(
            [sys.executable, "-I", "-c", code],
            cwd=sandbox, env=env, capture_output=True, text=True,
            timeout=timeout or settings.shell_timeout, **kw)
    except subprocess.TimeoutExpired:
        return {"error": "timeout",
                "message": "killed after "
                           f"{timeout or settings.shell_timeout}s"}
    return {"ok": p.returncode == 0, "exit": p.returncode,
            "stdout": (p.stdout or "")[:cap],
            "stderr": (p.stderr or "")[:cap], "cwd": sandbox}

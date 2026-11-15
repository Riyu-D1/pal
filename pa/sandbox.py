"""Sandboxed shell tool for chat turns.

Runs one argv in a dedicated sandbox dir (PA_CHAT_SANDBOX, default
<data>/chat-sandbox) with a timeout and an output cap. Commands that
match a read-only allowlist run ungated; everything else needs owner
approval at the tool layer. There is deliberately no shell=True, so
metacharacters (|, ;, &&, >, `, $()) never compose into bigger effects —
a pipeline just fails argument-parsing of the first binary.
"""

import os
import shlex
import shutil
import subprocess

# binaries that cannot mutate state when invoked without dangerous flags
READ_ONLY = {
    "ls", "cat", "pwd", "date", "uname", "whoami", "id", "hostname",
    "echo", "head", "tail", "wc", "df", "free", "uptime", "grep", "rg",
    "find", "file", "stat", "du", "sort", "uniq", "cut", "tr", "which",
    "env", "printenv",
}

# args that turn an allowlisted binary into a writer
_DANGEROUS_ARGS = {
    "find": {"-exec", "-execdir", "-ok", "-delete", "-fprint",
             "-fprintf"},
}

# shell metachars: present -> never auto-allowed (approval can still run
# it, but as one argv, so they're inert anyway)
_METACHARS = ("|", ";", "&&", "||", ">", "<", "`", "$(", "\n")


def parse(command: str):
    """Return argv for a single command, or raise ValueError."""
    argv = shlex.split(command or "")
    if not argv:
        raise ValueError("empty command")
    return argv


def is_readonly(command: str) -> bool:
    """True when the command is a single allowlisted binary invoked
    without dangerous flags or shell composition."""
    try:
        argv = parse(command)
    except ValueError:
        return False
    if any(m in command for m in _METACHARS):
        return False
    exe = os.path.basename(argv[0])
    if exe not in READ_ONLY:
        return False
    bad = _DANGEROUS_ARGS.get(exe)
    if bad and any(a in bad or a.startswith(tuple(bad)) for a in argv[1:]):
        return False
    return True


def run(settings, command: str, timeout: float = None):
    """Run the command inside the sandbox dir. Returns
    {ok, exit, stdout, stderr, cwd} or an {error} dict."""
    try:
        argv = parse(command)
    except ValueError as e:
        return {"error": "bad_command", "message": str(e)}
    sandbox = settings.chat_sandbox
    os.makedirs(sandbox, exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": sandbox, "LANG": "C.UTF-8"}
    cap = settings.shell_output_cap
    try:
        p = subprocess.run(
            argv, cwd=sandbox, env=env, capture_output=True, text=True,
            timeout=timeout or settings.shell_timeout)
    except FileNotFoundError:
        return {"error": "not_found",
                "message": f"{argv[0]!r} is not installed"}
    except subprocess.TimeoutExpired:
        return {"error": "timeout",
                "message": f"killed after {timeout or settings.shell_timeout}s"}
    return {
        "ok": p.returncode == 0,
        "exit": p.returncode,
        "stdout": (p.stdout or "")[:cap],
        "stderr": (p.stderr or "")[:cap],
        "cwd": sandbox,
    }

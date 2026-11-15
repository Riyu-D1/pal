"""Owner's Mac over SSH (Tailscale). Always approval-gated upstream.

`mac` runs one remote command:
    ssh -i <MAC_SSH_KEY> -o BatchMode=yes -o ConnectTimeout=8 \
        <MAC_SSH_USER>@<MAC_SSH_HOST> <command>

Offline/unreachable is a first-class answer ({offline: true}) so the
agent can tell the owner "the Mac is off" instead of a raw SSH error.
"""

import os
import shlex
import subprocess

from .util import truncate_middle


class MacNotConfigured(Exception):
    kind = "mac_not_configured"


def configured(settings) -> bool:
    return bool(settings.mac_ssh_host and settings.mac_ssh_user)


_OFFLINE_HINTS = ("connection refused", "operation timed out",
                  "connection timed out", "no route to host",
                  "could not resolve hostname", "host is down",
                  "network is unreachable", "operation timedout",
                  "permission denied (publickey", "timeout")


def run(settings, command: str, timeout: float = None,
        runner=subprocess.run):
    """Returns {ok, offline, exit, stdout, stderr} or {error,...}.
    `runner` is injectable for tests (subprocess.run-compatible)."""
    cmd = (command or "").strip()
    if not cmd:
        return {"error": "bad_command", "message": "empty command"}
    if not configured(settings):
        return {"error": "mac_not_configured",
                "message": "MAC_SSH_HOST/MAC_SSH_USER are not set"}
    argv = ["ssh"]
    if settings.mac_ssh_key:
        argv += ["-i", settings.mac_ssh_key]
    argv += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "-o", "StrictHostKeyChecking=accept-new",
             f"{settings.mac_ssh_user}@{settings.mac_ssh_host}", cmd]
    cap = settings.shell_output_cap
    try:
        p = runner(argv, capture_output=True, text=True,
                   timeout=timeout or settings.mac_ssh_timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "offline": True,
                "message": "mac unreachable (ssh timed out) — likely off"}
    except FileNotFoundError:
        return {"error": "not_found", "message": "ssh is not installed"}
    out, err = p.stdout or "", p.stderr or ""
    offline = p.returncode == 255 and any(
        h in (out + err).lower() for h in _OFFLINE_HINTS)
    res = {"ok": p.returncode == 0, "offline": offline,
           "exit": p.returncode,
           "stdout": truncate_middle(out, cap),
           "stderr": truncate_middle(err, cap)}
    if offline:
        res["message"] = ("the Mac appears to be offline or unreachable "
                          "over the tailnet")
    return res


def transfer(settings, direction: str, local: str, remote: str,
             timeout: float = None, runner=subprocess.run) -> dict:
    """scp one file to/from the owner's Mac. direction 'get' pulls
    remote -> local; 'put' pushes local -> remote. Returns
    {ok, offline, message?}; never raises."""
    if not configured(settings):
        return {"error": "mac_not_configured",
                "message": "MAC_SSH_HOST/MAC_SSH_USER are not set"}
    remote = (remote or "").strip()
    local = (local or "").strip()
    if direction not in ("get", "put") or not remote or not local:
        return {"error": "bad_arguments",
                "message": "need direction + local + remote paths"}
    spec = f"{settings.mac_ssh_user}@{settings.mac_ssh_host}:" \
           f"{shlex.quote(remote)}"
    argv = ["scp", "-q"]
    if settings.mac_ssh_key:
        argv += ["-i", settings.mac_ssh_key]
    argv += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    argv += [spec, local] if direction == "get" else [local, spec]
    try:
        p = runner(argv, capture_output=True, text=True,
                   timeout=timeout or settings.mac_ssh_timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "offline": True,
                "message": "mac unreachable (scp timed out) — likely off"}
    except FileNotFoundError:
        return {"error": "not_found", "message": "scp is not installed"}
    out = (p.stdout or "") + (p.stderr or "")
    offline = p.returncode != 0 and any(
        h in out.lower() for h in _OFFLINE_HINTS)
    res = {"ok": p.returncode == 0, "offline": offline,
           "exit": p.returncode}
    if not res["ok"]:
        res["message"] = ("the Mac appears to be offline or "
                          "unreachable over the tailnet") if offline \
            else truncate_middle(out.strip() or "scp failed", 400)
    return res

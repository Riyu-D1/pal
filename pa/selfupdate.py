"""Self-update (docs/PARITY.md #28).

`plan()` compares HEAD against the upstream ref WITHOUT fetching
(git ls-remote) — the dry-run /update answer. `apply()` fetches and
fast-forward-merges; it only ever runs after an explicit owner
approval (the /update card or an approved self_update call).

Never raises — {ok, error, message} always.
"""

import os
import subprocess


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(root, argv, timeout=20, runner=subprocess.run):
    p = runner(["git", "-C", root] + argv,
               capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), \
        (p.stderr or "").strip()


def plan(root: str = None, runner=subprocess.run) -> dict:
    """{current_sha, branch, upstream, remote_sha, update_available}
    — read-only (ls-remote needs no fetch)."""
    root = root or repo_root()
    rc, head, err = _git(root, ["rev-parse", "HEAD"], runner=runner)
    if rc != 0:
        return {"error": "no_repo",
                "message": f"not a git checkout: {err[:200]}"}
    rc, upstream, err = _git(
        root, ["rev-parse", "--abbrev-ref",
               "--symbolic-full-name", "@{u}"], runner=runner)
    if rc != 0 or not upstream:
        return {"current_sha": head[:12], "update_available": None,
                "message": "no upstream branch — can't compare"}
    remote, _, branch = upstream.partition("/")
    rc, remote_ref, err = _git(
        root, ["ls-remote", remote, f"refs/heads/{branch}"],
        timeout=30, runner=runner)
    if rc != 0 or not remote_ref:
        return {"current_sha": head[:12], "branch": branch,
                "update_available": None,
                "message": f"couldn't reach {remote}: {err[:200]}"}
    remote_sha = remote_ref.split()[0][:12]
    rc, behind, _ = _git(
        root, ["rev-list", "--count", f"HEAD..{upstream}"],
        runner=runner)
    return {"current_sha": head[:12], "branch": branch,
            "upstream": upstream, "remote_sha": remote_sha,
            "behind": int(behind or 0) if behind.isdigit() else None,
            "update_available": remote_sha != head[:12]}


def apply(root: str = None, runner=subprocess.run, restart=None,
          health_probe=None, health_seconds=60, port=8080,
          settings=None) -> dict:
    """fetch + merge --ff-only @{u}, then the canary path: restart +
    health poll + auto-rollback (docs/EVOLVE.md #13). With no
    systemd the caller gets {needs_restart} — the old
    'please restart yourself' note."""
    root = root or repo_root()
    prev = _head(root, runner)
    rc, _, err = _git(root, ["fetch", "--all", "--prune"],
                      timeout=120, runner=runner)
    if rc != 0:
        return {"ok": False, "error": "fetch_failed",
                "message": err[:400]}
    rc, out, err = _git(
        root, ["merge", "--ff-only", "@{u}"], timeout=60, runner=runner)
    if rc != 0:
        return {"ok": False, "error": "merge_failed",
                "message": (err or out)[:400] or "not fast-forwardable"}
    rc, head, _ = _git(root, ["rev-parse", "--short", "HEAD"],
                       runner=runner)
    if prev and head != prev[:len(head)]:
        res = _canary(root, prev, runner, _restart(restart, runner,
                      settings), health_probe, health_seconds, port)
        res["sha"] = head
        res["output"] = out
        return res
    return {"ok": True, "sha": head,
            "message": out or "already up to date"}


def describe(plan_out: dict) -> str:
    if "error" in plan_out:
        return f"update check failed: {plan_out.get('message')}"
    if plan_out.get("update_available") is None:
        return (f"on {plan_out.get('current_sha')} — "
                + (plan_out.get("message") or "no remote to compare"))
    if plan_out["update_available"]:
        return (f"update available: {plan_out['current_sha']} -> "
                f"{plan_out['remote_sha']} ({plan_out['upstream']})")
    return f"up to date at {plan_out['current_sha']}"


# -- canary deploy (docs/EVOLVE.md #13) ------------------------------------

import shutil
import time
import urllib.request


def _scope_unit(settings):
    """(scope, unit) for restarts — PA_SYSTEMD_SCOPE/PA_SYSTEMD_UNIT
    on Settings, 'user'/'pa' while unset (the user-unit default)."""
    return (getattr(settings, "systemd_scope", "user"),
            getattr(settings, "systemd_unit", "pa"))


def _restart(restart, runner, settings):
    if restart is not None:
        return restart
    scope, unit = _scope_unit(settings)
    return lambda: _default_restart(runner, scope, unit)


def _default_restart(runner=subprocess.run, scope="user",
                     unit="pa") -> bool:
    """`systemctl [--user] restart <unit>`. False when systemd isn't
    there — callers surface {needs_restart} rather than pretend."""
    if shutil.which("systemctl") is None:
        return False
    argv = ["systemctl"]
    if scope == "user":
        argv.append("--user")
    argv += ["restart", unit]
    p = runner(argv, capture_output=True, text=True, timeout=60)
    return p.returncode == 0


def _default_health(port: int):
    def probe() -> bool:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz",
                    timeout=5) as r:
                import json
                return json.loads(r.read()).get("ok") is True
        except Exception:
            return False
    return probe


def _wait_health(probe, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if probe():
            return True
        time.sleep(2)
    return False


def _head(root, runner) -> str:
    rc, head, _ = _git(root, ["rev-parse", "HEAD"], runner=runner)
    return head if rc == 0 else ""


def _diff_paths(root, base, sha, runner) -> list:
    rc, out, _ = _git(
        root, ["diff", "--name-only", f"{base}..{sha}"],
        runner=runner)
    return [l.strip() for l in out.splitlines() if l.strip()] \
        if rc == 0 else []


def _canary(root, prev, runner, restart, health_probe,
            health_seconds, port) -> dict:
    """Shared post-checkout path: restart -> health poll -> rollback."""
    if not restart():
        return {"ok": True, "needs_restart": True,
                "previous_sha": prev,
                "message": "applied; no systemd — restart pal "
                           "yourself to run it"}
    probe = health_probe or _default_health(port)
    if _wait_health(probe, health_seconds):
        return {"ok": True, "previous_sha": prev,
                "message": "applied, restarted, healthy"}
    rb = rollback(prev, root=root, runner=runner, restart=restart,
                  health_probe=probe, health_seconds=health_seconds)
    return {"ok": False, "error": "health_failed",
            "rolled_back": rb.get("ok", False),
            "previous_sha": prev,
            "message": "health check failed — rolled back to "
                       f"{prev[:12]}"}


def apply_sha(sha: str, root: str = None, runner=subprocess.run,
              restart=None, health_probe=None, health_seconds=60,
              port=8080, policy=None, settings=None) -> dict:
    """Canary checkout of a known sha: ff-only to it, restart,
    poll /healthz, auto-rollback on failure. Protected-path diffs
    are refused outright (owner reviews those by hand)."""
    root = root or repo_root()
    policy = policy if policy is not None else __import__(
        "pa.evolve.policy", fromlist=["policy"])
    prev = _head(root, runner)
    if not prev:
        return {"ok": False, "error": "no_repo"}
    rc, _, err = _git(root, ["fetch", "--all", "--prune"],
                      timeout=120, runner=runner)
    if rc != 0:
        return {"ok": False, "error": "fetch_failed",
                "message": err[:400]}
    hits = policy.touches_protected(
        _diff_paths(root, prev, sha, runner))
    if hits:
        return {"ok": False, "error": "protected_paths",
                "protected": hits,
                "message": "diff touches protected paths — owner "
                           "reviews those by hand"}
    rc, out, err = _git(
        root, ["merge", "--ff-only", sha], timeout=60, runner=runner)
    if rc != 0:
        return {"ok": False, "error": "not_ff",
                "message": (err or out)[:400] or
                           "not fast-forwardable"}
    res = _canary(root, prev, runner, _restart(restart, runner,
                  settings), health_probe, health_seconds, port)
    res["sha"] = sha[:12]
    return res


def rollback(previous_sha: str, root: str = None,
             runner=subprocess.run, restart=None,
             health_probe=None, health_seconds=30, port=8080,
             settings=None) -> dict:
    """Explicit reset to a recorded previous SHA (never force-push,
    never an unknown ref) + restart + health re-check."""
    root = root or repo_root()
    rc, out, err = _git(
        root, ["reset", "--hard", previous_sha], timeout=60,
        runner=runner)
    if rc != 0:
        return {"ok": False, "error": "reset_failed",
                "message": (err or out)[:400]}
    restart = _restart(restart, runner, settings)
    restarted = restart()
    probe = health_probe or _default_health(port)
    healthy = _wait_health(probe, health_seconds) if restarted \
        else False
    return {"ok": restarted and healthy, "sha": previous_sha[:12],
            "restarted": restarted, "healthy": healthy}

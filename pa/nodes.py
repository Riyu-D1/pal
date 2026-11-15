"""node registry + dispatch (docs/NODES.md).

pal's "hands": small remote agents (the Moto E5 phone over an SSH
tunnel, the computer VM) that run pal-made or built-in capabilities on
pal's behalf. Config lives in PA_NODES (`name=url` CSV, same URL rules
as PA_COMPUTER_URL) + PA_NODE_<NAME>_TOKEN.

Wire protocol is pa/node_proto.py — the ONLY module the stdlib-only
node agent shares with the rest of pa.

Every dispatch goes through policy.decide first: the cap maps to an
action class and an account owner (phone SMS/camera/location act on the
OWNER's device -> hard approval; pal-owned reads are free within
budgets). Each job reserves node_sec:<name> in the budget ledger and
settles the real cost when it finishes. Three consecutive failures open
a per-node circuit breaker; jobs with no healthy node stay 'queued' and
the scheduler retries them on later calls.
"""

import http.client
import json
import math
import time
import urllib.parse

from . import budget, node_proto, policy
from .util import json_dumps, json_loads_bounded, now_iso, parse_iso, \
    utcnow

BREAKER_FAILURES = 3
BREAKER_BASE_S = 30.0
BREAKER_MAX_S = 900.0
JOB_TIMEOUT_CAP = 600.0
POLL_INTERVAL_S = 0.5

# cap -> (action_class, account_owner). Phone caps that touch the
# owner's SIM/camera/location act on the owner account — they always
# hard-gate. Reads and pal-local storage stay on the pal account.
_CAP_POLICY = {
    "fetch":        ("read", "pal"),
    "rss":          ("read", "pal"),
    "ping_http":    ("read", "pal"),
    "file_get":     ("read", "pal"),
    "file_put":     ("write", "pal"),
    "run_tool":     ("write", "pal"),   # registration was the gate
    "browse":       ("read", "pal"),
    "screenshot":   ("read", "pal"),
    "read_url_js":  ("read", "pal"),
    "battery":      ("read", "owner"),
    "sensor":       ("read", "owner"),
    "sms_list":     ("read", "owner"),
    "sms_send":     ("send", "owner"),
    # sensitive reads from the owner's device: gated, but a standing
    # allow rule may cover them (owner's choice — see _APPROVE_ALWAYS
    # vs 'sensitive' in pa/policy.py)
    "camera_photo": ("sensitive", "owner"),
    "location":     ("sensitive", "owner"),
    "vibrate":      ("sensitive", "owner"),
}
# pal -> owner channel: reaching the owner's own device with a message
# for him is the same channel as call_owner/email_owner — always free.
_OWNER_CHANNEL_CAPS = frozenset({"notify", "tts_speak"})


class NodeError(Exception):
    def __init__(self, kind: str, detail: str = "", status: int = 0):
        super().__init__(f"{kind}: {detail}"[:200])
        self.kind = kind
        self.status = status


def cap_policy(cap: str):
    """-> (action_class, account_owner); unknown caps fail closed."""
    return _CAP_POLICY.get(cap, ("write", "owner"))


def cap_decision(conn, cap, args=None, *, unattended=False,
                 tainted=False, taint_source="", tool_name=None):
    """policy.decide for one node capability. tool_name keeps the
    chat-facing tool in the audit (phone_sms_send, not node.sms_send)."""
    if cap in _OWNER_CHANNEL_CAPS:
        return policy.Decision("allow", "pal -> owner channel",
                               "send", "pal")
    cls, owner = cap_policy(cap)
    return policy.decide(
        conn, tool_name or f"node.{cap}", args or {},
        unattended=unattended, tainted=tainted,
        source=taint_source,
        account_owner=owner, action_class=cls)


# ---------------------------------------------------------------------------
# client

class NodeClient:
    """HTTP client for one node agent. http only ever reaches loopback/
    tailnet/private endpoints — config validation enforces it."""

    def __init__(self, name: str, url: str, token: str,
                 timeout: float = 10.0):
        self.name = name
        parts = urllib.parse.urlsplit(url)
        self.scheme = parts.scheme
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or (443 if self.scheme == "https" else 80)
        self.base = parts.path.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, obj=None):
        cls = http.client.HTTPSConnection if self.scheme == "https" \
            else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=self.timeout)
        body = json_dumps(obj).encode("utf-8") if obj is not None else None
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        try:
            conn.request(method, self.base + path, body=body,
                         headers=headers)
            r = conn.getresponse()
            data = r.read(node_proto.MAX_BODY + 1)
        except (OSError, http.client.HTTPException) as e:
            raise NodeError("unreachable", str(e)[:120])
        finally:
            conn.close()
        if len(data) > node_proto.MAX_BODY:
            raise NodeError("bad_response", "body too large")
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            raise NodeError("bad_response", f"status {r.status}")
        if r.status == 401 or r.status == 403:
            raise NodeError("auth", "bad node token", status=r.status)
        if r.status >= 400:
            raise NodeError(payload.get("error") or f"http_{r.status}",
                            str(payload.get("detail") or "")[:120],
                            status=r.status)
        return payload

    def health(self) -> dict:
        return self._req("GET", "/health")

    def caps(self) -> list:
        return list(self.health().get("caps") or [])

    def submit(self, cap: str, args: dict, timeout: float) -> dict:
        return self._req("POST", "/jobs",
                         {"cap": cap, "args": args or {},
                          "timeout": timeout})

    def poll(self, remote_id: str) -> dict:
        return self._req("GET", f"/jobs/{remote_id}")

    def cancel(self, remote_id: str) -> dict:
        return self._req("POST", f"/jobs/{remote_id}/cancel")


def configured(s) -> list:
    """Node names that are set up: PA_NODES + 'computer' when the
    computer agent has its own URL."""
    names = sorted((s.nodes or {}).keys())
    if getattr(s, "computer_url", "") and "computer" not in s.nodes:
        names.append("computer")
    return names


COMPUTER_CAPS = ("browse", "screenshot", "read_url_js")


class ComputerNodeClient(NodeClient):
    """Adapter: the existing computer-agent speaks /call with
    x-pa-computer-token, not the node protocol. Jobs complete
    synchronously inside call(); results are cached client-side so
    poll() finds the same record the dispatcher expects."""

    CAP_ACTION = {
        "browse": "goto",
        "screenshot": "screenshot",
    }

    def __init__(self, name, url, token, timeout):
        super().__init__(name, url, token, timeout)
        from . import computer as computermod
        self._rc = computermod.RemoteComputer(
            url, token, timeout=max(timeout, 60.0))
        self._done = {}

    def health(self):
        import http.client as _hc
        import urllib.parse as _up
        try:
            cls = _hc.HTTPSConnection if self.scheme == "https" \
                else _hc.HTTPConnection
            conn = cls(self.host, self.port, timeout=self.timeout)
            conn.request("GET", "/healthz")
            r = conn.getresponse()
            body = json.loads(r.read().decode("utf-8") or "{}")
            conn.close()
            ok = bool(body.get("ok", r.status == 200))
        except Exception as e:
            raise NodeError("unreachable", str(e)[:120])
        return {"ok": ok, "caps": list(COMPUTER_CAPS),
                "paused": False}

    def submit(self, cap, args, timeout):
        args = dict(args or {})
        out = self._dispatch_action(cap, args)
        rid = node_proto.new_job_id()
        if out.get("error"):
            rec = node_proto.job_record(
                rid, cap, node_proto.JOB_FAILED, error=out["error"],
                started_at=now_iso(), finished_at=now_iso(),
                cost_s=0.0)
        else:
            rec = node_proto.job_record(
                rid, cap, node_proto.JOB_DONE, result=out,
                started_at=now_iso(), finished_at=now_iso(),
                cost_s=0.5)
        self._done[rid] = rec
        return {"id": rid, "status": rec["status"]}

    def _dispatch_action(self, cap, args):
        if cap == "read_url_js":
            url = str(args.get("url") or "")
            out = self._rc.call("goto", {"url": url})
            if out.get("error"):
                return out
            ext = self._rc.call(
                "extract", {"instruction":
                            str(args.get("instruction") or "")[:300]})
            ext = dict(ext or {})
            ext["url"] = out.get("url")
            ext["title"] = out.get("title")
            return ext
        action = self.CAP_ACTION.get(cap)
        if action is None:
            return {"error": "bad_cap"}
        return self._rc.call(action, args)

    def poll(self, remote_id):
        rec = self._done.get(remote_id)
        if rec is None:
            raise NodeError("no_job", remote_id, status=404)
        return rec

    def cancel(self, remote_id):
        rec = self._done.get(remote_id)
        if rec is None:
            raise NodeError("no_job", remote_id, status=404)
        if rec["status"] not in node_proto.JOB_TERMINAL:
            rec["status"] = node_proto.JOB_CANCELLED
        return rec


_COMPUTER_CLIENTS = {}


def client_for(s, name: str) -> NodeClient:
    if name == "computer" and name not in (s.nodes or {}) \
            and getattr(s, "computer_url", ""):
        # memoized: computer jobs finish inside submit() and their
        # records live in client._done — a fresh client would lose them
        key = (s.computer_url, s.computer_token)
        cli = _COMPUTER_CLIENTS.get(key)
        if cli is None:
            cli = ComputerNodeClient(name, s.computer_url,
                                     s.computer_token,
                                     s.node_connect_seconds)
            _COMPUTER_CLIENTS[key] = cli
        return cli
    url = (s.nodes or {}).get(name)
    if not url:
        raise NodeError("unknown_node", name)
    return NodeClient(name, url, (s.node_tokens or {}).get(name, ""),
                      s.node_connect_seconds)


# ---------------------------------------------------------------------------
# registry (nodes table)

def _row(conn, name: str):
    r = conn.execute("SELECT * FROM nodes WHERE name=?",
                     (name,)).fetchone()
    return dict(r) if r else None


def ensure_rows(conn, s) -> None:
    for name in configured(s):
        url = (s.nodes or {}).get(
            name, getattr(s, "computer_url", "") or "")
        conn.execute(
            "INSERT INTO nodes(name, url, created_at, updated_at)"
            " VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET"
            " url=excluded.url, updated_at=excluded.updated_at",
            (name, url, now_iso(), now_iso()))


def _breaker_open(conn, name: str, row) -> bool:
    until = (row or {}).get("breaker_until")
    if not until:
        return False
    try:
        if parse_iso(until) > utcnow():
            conn.execute(
                "UPDATE nodes SET state='breaker', updated_at=?"
                " WHERE name=?", (now_iso(), name))
            return True
    except Exception:
        pass
    return False


def _note_failure(conn, name: str, err) -> None:
    row = _row(conn, name)
    fails = int((row or {}).get("fail_count") or 0) + 1
    state = "down"
    breaker_until = None
    if fails >= BREAKER_FAILURES:
        cool = min(BREAKER_MAX_S,
                   BREAKER_BASE_S * (2 ** (fails - BREAKER_FAILURES)))
        breaker_until = _iso_at(utcnow().timestamp() + cool)
        state = "breaker"
    conn.execute(
        "UPDATE nodes SET state=?, fail_count=?, breaker_until=?,"
        " last_error=?, updated_at=? WHERE name=?",
        (state, fails, breaker_until, str(err)[:200], now_iso(), name))


def _iso_at(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc).isoformat(timespec="seconds")


def _note_success(conn, name: str, health: dict) -> None:
    caps = health.get("caps") or []
    state = "paused" if health.get("paused") else "ok"
    conn.execute(
        "UPDATE nodes SET state=?, fail_count=0, breaker_until=NULL,"
        " caps_json=?, battery_pct=?, temp_c=?, last_health_json=?,"
        " last_health_at=?, last_error='', updated_at=? WHERE name=?",
        (state, json_dumps(caps), health.get("battery_pct"),
         health.get("temp_c"), json_dumps(health)[:8000], now_iso(),
         now_iso(), name))


def refresh_health(conn, s, name: str, force: bool = False):
    """-> health dict or None. Caches for node_health_seconds unless
    forced; a failure bumps the breaker instead of raising."""
    ensure_rows(conn, s)
    row = _row(conn, name)
    if row is None:
        return None
    if _breaker_open(conn, name, row):
        return dict(row.get("last_health_json") and
                    json_loads_bounded(row["last_health_json"]) or {})
    if not force and row.get("last_health_at"):
        try:
            age = (utcnow() - parse_iso(row["last_health_at"])
                   ).total_seconds()
            if age < s.node_health_seconds and row.get("state") in (
                    "ok", "paused"):
                return json_loads_bounded(row["last_health_json"]) or {}
        except Exception:
            pass
    try:
        h = client_for(s, name).health()
    except NodeError as e:
        _note_failure(conn, name, e)
        return None
    _note_success(conn, name, h)
    return h


def node_state(conn, s, name: str) -> str:
    row = _row(conn, name)
    return (row or {}).get("state") or "unknown"


# ---------------------------------------------------------------------------
# scheduling

def candidates(conn, s, cap: str) -> list:
    """Node names that advertise `cap` and are currently healthy."""
    out = []
    for name in configured(s):
        h = refresh_health(conn, s, name)
        if not h or h.get("paused"):
            continue
        if cap in (h.get("caps") or []):
            out.append(name)
    return out


def pick_node(conn, s, cap: str):
    """Best node for a cap: healthy, advertises it, then most battery /
    coolest first (phone-friendliness), then name for stability."""
    rows = []
    for name in candidates(conn, s, cap):
        row = _row(conn, name) or {}
        rows.append((-(row.get("battery_pct") or -1),
                     row.get("temp_c") or 99.0, name))
    if not rows:
        return None
    rows.sort()
    return rows[0][2]


def submit_job(conn, s, cap: str, args=None, *, node=None,
               scope="owner-chat", timeout=30.0, unattended=False,
               tainted=False, taint_source="", tool_name=None,
               approved=False, env=None) -> dict:
    """Gate + schedule one capability call.

    Returns {job_id, node, status} — status 'queued' means no healthy
    node (try_dispatch_queued will retry), 'submitted' means the agent
    accepted it. Policy verdicts map to {'error': ...} so the caller
    can put an approval card in between and resubmit with
    approved=True."""
    timeout = max(1.0, min(float(timeout or 30.0), JOB_TIMEOUT_CAP))
    d = cap_decision(conn, cap, args, unattended=unattended,
                     tainted=tainted, taint_source=taint_source,
                     tool_name=tool_name)
    if d.verdict == "deny":
        _record(conn, node or "", cap, args, scope, status="denied",
                error=d.reason)
        return {"error": "policy_denied", "reason": d.reason}
    if d.verdict in ("approve", "pending") and not approved:
        return {"error": "approval_required", "reason": d.reason,
                "verdict": d.verdict}
    ensure_rows(conn, s)
    name = node or pick_node(conn, s, cap)
    if not name:
        job_id = _record(conn, "", cap, args, scope, status="queued")
        return {"job_id": job_id, "status": "queued",
                "reason": "no healthy node advertises " + cap}
    if name not in configured(s):
        return {"error": "unknown_node", "node": name}
    # the computer node eats browser minutes, not just node seconds —
    # a session is a session however it was launched
    if name == "computer" and not budget.charge(
            conn, scope, "browser_min", 1, s=s, env=env):
        job_id = _record(conn, name, cap, args, scope,
                         status="queued", error="browser_min budget")
        return {"job_id": job_id, "status": "queued",
                "reason": "browser_min budget exhausted"}
    res = f"node_sec:{name}"
    hold = int(math.ceil(timeout))
    if not budget.reserve(conn, scope, res, hold, s=s, env=env):
        job_id = _record(conn, name, cap, args, scope,
                         status="queued", error="budget")
        return {"job_id": job_id, "status": "queued",
                "reason": f"node_sec:{name} budget exhausted"}
    client = client_for(s, name)
    try:
        out = client.submit(cap, args or {}, timeout)
    except NodeError as e:
        budget.release(conn, scope, res, hold)
        _note_failure(conn, name, e)
        if e.kind == "paused":
            job_id = _record(conn, name, cap, args, scope,
                             status="queued", error="node paused")
            return {"job_id": job_id, "status": "queued",
                    "reason": "node is self-throttled"}
        job_id = _record(conn, name, cap, args, scope,
                         status="failed", error=e.kind)
        return {"error": e.kind, "job_id": job_id, "node": name}
    job_id = _record(conn, name, cap, args, scope,
                     status="submitted", remote_id=out.get("id", ""),
                     reserved=hold)
    return {"job_id": job_id, "node": name,
            "remote_id": out.get("id", ""), "status": "submitted"}


def _record(conn, node, cap, args, scope, *, status, remote_id="",
            error=None, result=None, cost=0.0, reserved=0) -> int:
    cur = conn.execute(
        "INSERT INTO node_jobs(node, cap, args_json, remote_id, status,"
        " result_json, error, scope, cost_s, reserved_s, created_at,"
        " updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (node, cap, json_dumps(args or {}), remote_id, status,
         json_dumps(result) if result is not None else None, error,
         scope, cost, int(reserved), now_iso(), now_iso()))
    return cur.lastrowid


def _job_row(conn, job_id: int):
    r = conn.execute("SELECT * FROM node_jobs WHERE id=?",
                     (int(job_id),)).fetchone()
    return dict(r) if r else None


def poll_job(conn, s, job_id: int) -> dict:
    """Sync one job's state from its node; settles the budget hold on
    terminal states. Returns the row as a dict."""
    row = _job_row(conn, job_id)
    if row is None:
        return {"error": "no_such_job"}
    if row["status"] in node_proto.JOB_TERMINAL \
            or row["status"] in ("denied", "queued"):
        return row
    try:
        remote = client_for(s, row["node"]).poll(row["remote_id"])
    except NodeError as e:
        _note_failure(conn, row["node"], e)
        return {**row, "poll_error": e.kind}
    # a good response proves the node is alive — clear the breaker
    # without overwriting the cached /health detail
    conn.execute(
        "UPDATE nodes SET fail_count=0, breaker_until=NULL,"
        " state=CASE WHEN state='paused' THEN 'paused' ELSE 'ok' END,"
        " updated_at=? WHERE name=?",
        (now_iso(), row["node"]))
    status = remote.get("status") or row["status"]
    upd = {"status": status, "updated_at": now_iso()}
    if status in node_proto.JOB_TERMINAL:
        cost = float(remote.get("cost_s") or 0.0)
        upd["cost_s"] = cost
        upd["error"] = remote.get("error")
        upd["result_json"] = json_dumps(remote.get("result"))
        res = f"node_sec:{row['node']}"
        billed = int(math.ceil(max(cost, 0.1)))
        budget.settle(conn, row["scope"], res, billed)
        budget.release(conn, row["scope"], res,
                       max(0, int(row["reserved_s"]) - billed))
        conn.execute(
            "UPDATE node_jobs SET status=?, cost_s=?, error=?,"
            " result_json=?, updated_at=? WHERE id=?",
            (status, cost, remote.get("error"),
             upd["result_json"], upd["updated_at"], row["id"]))
        return _job_row(conn, job_id)
    conn.execute("UPDATE node_jobs SET status=?, updated_at=?"
                 " WHERE id=?", (status, upd["updated_at"], row["id"]))
    return _job_row(conn, job_id)


def _caps_of(conn, name):
    row = _row(conn, name)
    try:
        return json_loads_bounded((row or {}).get("caps_json") or "[]")
    except Exception:
        return []


def cancel_job(conn, s, job_id: int) -> dict:
    row = _job_row(conn, job_id)
    if row is None:
        return {"error": "no_such_job"}
    if row["status"] == "queued":
        conn.execute("UPDATE node_jobs SET status='cancelled',"
                     " updated_at=? WHERE id=?", (now_iso(), job_id))
        return _job_row(conn, job_id)
    if row["status"] in node_proto.JOB_TERMINAL:
        return row
    try:
        remote = client_for(s, row["node"]).cancel(row["remote_id"])
    except NodeError as e:
        _note_failure(conn, row["node"], e)
        return {**row, "cancel_error": e.kind}
    conn.execute("UPDATE node_jobs SET status=?, updated_at=?"
                 " WHERE id=?",
                 (remote.get("status") or "cancelled", now_iso(),
                  job_id))
    res = f"node_sec:{row['node']}"
    budget.release(conn, row["scope"], res,
                   int(row["reserved_s"] or 0))
    return _job_row(conn, job_id)


def submit_and_wait(conn, s, cap, args=None, *, wait=None,
                    poll_s=POLL_INTERVAL_S, sleep=time.sleep, **kw):
    """submit_job then poll until terminal or `wait` seconds. Returns
    the job row; a still-running job at the deadline is cancelled."""
    out = submit_job(conn, s, cap, args, **kw)
    if out.get("error") or out.get("status") != "submitted":
        return out
    wait = wait if wait is not None else \
        min(float(kw.get("timeout") or 30.0) + 10.0, JOB_TIMEOUT_CAP)
    deadline = time.monotonic() + wait
    row = poll_job(conn, s, out["job_id"])
    while row.get("status") not in node_proto.JOB_TERMINAL:
        if time.monotonic() >= deadline:
            cancel_job(conn, s, out["job_id"])
            row = _job_row(conn, out["job_id"]) or row
            row["error"] = row.get("error") or "timeout"
            return row
        sleep(poll_s)
        row = poll_job(conn, s, out["job_id"])
    return row


def try_dispatch_queued(conn, s, limit=3, env=None) -> int:
    """Scheduler: re-drive queued jobs whose cap now has a healthy
    node. Returns how many were submitted this pass."""
    n = 0
    rows = conn.execute(
        "SELECT * FROM node_jobs WHERE status='queued'"
        " ORDER BY id LIMIT ?", (int(limit),)).fetchall()
    for row in rows:
        name = pick_node(conn, s, row["cap"])
        if not name:
            continue
        if name not in configured(s):
            continue
        res = f"node_sec:{name}"
        if not budget.reserve(conn, row["scope"], res, 60,
                              s=s, env=env):
            continue
        try:
            out = client_for(s, name).submit(
                row["cap"], json_loads_bounded(row["args_json"]) or {},
                60.0)
        except NodeError as e:
            budget.release(conn, row["scope"], res, 60)
            _note_failure(conn, name, e)
            continue
        conn.execute(
            "UPDATE node_jobs SET node=?, remote_id=?, status='submitted',"
            " reserved_s=60, updated_at=? WHERE id=?",
            (name, out.get("id", ""), now_iso(), row["id"]))
        n += 1
    return n


# ---------------------------------------------------------------------------
# /nodes + /status

def status_lines(conn, s, refresh: bool = True) -> list:
    names = configured(s)
    if not names:
        return []
    ensure_rows(conn, s)
    lines = []
    for name in names:
        if refresh:
            refresh_health(conn, s, name)
        row = _row(conn, name) or {}
        state = row.get("state") or "unknown"
        bits = [state]
        caps = _caps_of(conn, name)
        if caps:
            bits.append("caps: " + ",".join(caps[:8]))
        if row.get("battery_pct") is not None:
            bits.append(f"bat {row['battery_pct']}%")
        if row.get("temp_c") is not None:
            bits.append(f"{row['temp_c']:.0f}\u00b0C")
        if row.get("last_health_at"):
            bits.append("seen " + str(row["last_health_at"])[11:19])
        if row.get("last_error"):
            bits.append("err " + str(row["last_error"])[:40])
        lines.append(f"\u2022 {name}: " + " \u00b7 ".join(bits))
    q = conn.execute(
        "SELECT COUNT(*) n FROM node_jobs WHERE status='queued'"
    ).fetchone()["n"]
    if q:
        lines.append(f"\u2022 {q} job(s) queued waiting for a node")
    return lines

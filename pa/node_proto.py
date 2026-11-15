"""pa-node wire protocol — tiny shared module.

The node agent (pa/node_agent.py) runs on the phone or any small Linux
box with NOTHING but Python's stdlib. This module is the only piece of
pa it may import, so everything in here must stay stdlib-only too. It
is also safe to copy flat next to node_agent.py on a device.

Wire shape (all JSON, UTF-8):

    GET  /health                    -> {ok, caps, battery_pct, temp_c,
                                       load, mem_free_mb, uptime_s,
                                       paused, pause_reason,
                                       jobs_running, jobs_queued}
    POST /jobs      {cap,args,timeout} -> {id, status}   (202-ish 200)
    GET  /jobs/<id>                  -> {id, cap, status, result, error,
                                        started_at, finished_at, cost_s}
    POST /jobs/<id>/cancel           -> {id, status}

Auth: "Authorization: Bearer <token>" checked with hmac.compare_digest.
"""

import hmac
import ipaddress
import json
import time

PROTO_VERSION = 1
AUTH_HEADER = "Authorization"
MAX_BODY = 64 * 1024
MAX_RESULT = 256 * 1024
DEFAULT_PORT = 8790

# job states
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_TERMINAL = frozenset({JOB_DONE, JOB_FAILED, JOB_CANCELLED})

# ---------------------------------------------------------------------------
# auth + json helpers (shared by client and agent)


def bearer_ok(headers, token: str) -> bool:
    """Constant-time check of the Bearer header against `token`."""
    if not token:
        return False
    want = "Bearer " + token
    got = headers.get(AUTH_HEADER) or headers.get(AUTH_HEADER.lower()) or ""
    return hmac.compare_digest(got.strip(), want)


def read_json(handler, max_body=MAX_BODY):
    """Read a bounded JSON body from a BaseHTTPRequestHandler.
    Returns (obj, error); exactly one is truthy."""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return None, ("bad_length", 411)
    if length <= 0:
        return {}, None
    if length > max_body:
        handler.rfile.read(length)          # drain so conn stays sane
        return None, ("too_large", 413)
    raw = handler.rfile.read(length)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, ("bad_json", 400)
    if not isinstance(obj, dict):
        return None, ("bad_json", 400)
    return obj, None


def write_json(handler, status: int, obj):
    """Send one JSON response and close."""
    try:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except Exception:
        status, body = 500, b'{"error":"encode"}'
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def job_record(job_id, cap, status=JOB_QUEUED, result=None, error=None,
               started_at=None, finished_at=None, cost_s=0.0):
    return {"id": job_id, "cap": cap, "status": status, "result": result,
            "error": error, "started_at": started_at,
            "finished_at": finished_at, "cost_s": round(cost_s, 3)}


def new_job_id() -> str:
    import secrets
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# egress rules — port of pa/egress.py:is_allowed_ip + vet_url
# (kept here so the stdlib-only agent shares the same public-IP policy)

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443, 8080})

BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96",
    "100::/64", "2001::/23", "2001:2::/48", "2001:db8::/32",
    "2001:10::/28", "fc00::/7", "fe80::/10", "ff00::/8",
)]


def is_allowed_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return False
    for net in BLOCKED_NETS:
        if ip in net:
            return False
    return True


def vet_url(url: str):
    """Minimal URL vet for node-side fetch: scheme, port, no creds.
    Returns (error, host, port). DNS vetting happens in the caller
    (it owns the resolver)."""
    import urllib.parse
    try:
        u = urllib.parse.urlsplit(url or "")
    except Exception:
        return "bad_url", "", 0
    if u.scheme.lower() not in ALLOWED_SCHEMES:
        return "bad_scheme", "", 0
    if u.username or u.password:
        return "credentials", "", 0
    host = u.hostname or ""
    if not host or len(host) > 253:
        return "no_host", "", 0
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return "bad_port", "", 0
    if port not in ALLOWED_PORTS:
        return "bad_port", "", 0
    return None, host, port


def resolve_public(host: str, getaddrinfo=None):
    """Resolve a host and return True when EVERY answer is a public IP.
    getaddrinfo is injectable for tests; default is socket's."""
    if getaddrinfo is None:
        import socket
        getaddrinfo = socket.getaddrinfo
    try:
        ipaddress.ip_address(host)
        answers = [host]
    except ValueError:
        try:
            infos = getaddrinfo(host, None)
        except Exception:
            return False
        answers = {i[4][0] for i in infos}
    if not answers:
        return False
    return all(is_allowed_ip(a) for a in answers)


def monotonic() -> float:
    return time.monotonic()

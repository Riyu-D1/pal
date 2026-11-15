#!/usr/bin/env python3
"""pa-node agent — pal's hands on a phone or any small Linux box.

STDLIB ONLY. Runs on Termux (pkg install python) with ~40 MB RSS; the
only piece of pal it shares is pa/node_proto.py, imported flat when
this file is deployed on its own:

    python node_agent.py --port 8790
    # (ship node_proto.py next to it, or the whole pa/ package)

Modelled on pa/computer.py:make_agent_handler — a tiny http.server on
loopback, one bearer token, a fixed allowlist of Python functions as
capabilities. NO arbitrary shell: the only subprocess it ever spawns is
the sandboxed `run_tool` runner (manifest-gated, rlimits applied).

Security: token file ~/.pa-node/token mode 600; every request is Bearer-
checked with hmac.compare_digest; file ops are jailed to the data dir;
fetch hits public IPs only (node_proto vet); jobs run one at a time and
the agent self-throttles on a hot/low battery.
"""

import argparse
import base64
import http.server
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

try:
    from pa import node_proto          # running inside the repo
except ImportError:                    # deployed flat on a device
    import node_proto

QUEUE_MAX = 5
FETCH_MAX_BYTES = 1024 * 1024
FETCH_TIMEOUT = 10.0
FILE_MAX_BYTES = 4 * 1024 * 1024
TOOL_DIR = "tools"
START = time.monotonic()

BATTERY_LOW = 25
TEMP_HIGH = 42.0


class Job:
    def __init__(self, job_id, cap, args, timeout):
        self.id = job_id
        self.cap = cap
        self.args = args
        self.timeout = min(float(timeout or 30.0), 600.0)
        self.status = node_proto.JOB_QUEUED
        self.result = None
        self.error = None
        self.started_at = None
        self.finished_at = None
        self.cost_s = 0.0
        self.cancel = threading.Event()

    def record(self):
        return node_proto.job_record(
            self.id, self.cap, self.status, self.result, self.error,
            self.started_at, self.finished_at, self.cost_s)


class NodeState:
    """Everything the handler needs; readers are injectable for tests."""

    def __init__(self, token, data_dir, battery_reader=None,
                 temp_reader=None, max_queue=QUEUE_MAX):
        self.token = token
        self.data_dir = data_dir
        self.jobs = {}
        self._lock = threading.Lock()
        self._q = queue.Queue(maxsize=max_queue)
        self.battery_reader = battery_reader or _read_battery
        self.temp_reader = temp_reader or _read_temp
        self.started = False

    def throttle_reason(self):
        """-> reason str or ''. Phone-friendliness: stop taking work
        on a low battery or a hot SoC."""
        try:
            bat = self.battery_reader()
            if bat is not None and bat < BATTERY_LOW:
                return f"battery {bat}%"
        except Exception:
            pass
        try:
            temp = self.temp_reader()
            if temp is not None and temp > TEMP_HIGH:
                return f"temp {temp:.0f}C"
        except Exception:
            pass
        return ""

    def health(self):
        bat = temp = None
        try:
            bat = self.battery_reader()
        except Exception:
            pass
        try:
            temp = self.temp_reader()
        except Exception:
            pass
        paused = self.throttle_reason()
        with self._lock:
            running = sum(1 for j in self.jobs.values()
                          if j.status == node_proto.JOB_RUNNING)
            queued = sum(1 for j in self.jobs.values()
                         if j.status == node_proto.JOB_QUEUED)
        return {"ok": True, "proto": node_proto.PROTO_VERSION,
                "caps": sorted(CAPS), "battery_pct": bat,
                "temp_c": temp, "load": _load(),
                "mem_free_mb": _mem_free_mb(),
                "disk_free_mb": _disk_free_mb(self.data_dir),
                "uptime_s": round(time.monotonic() - START, 1),
                "paused": bool(paused), "pause_reason": paused,
                "jobs_running": running, "jobs_queued": queued}

    def enqueue(self, cap, args, timeout):
        if cap not in CAPS:
            raise NodeCapError(f"unknown capability {cap!r}")
        if self.throttle_reason():
            raise NodePaused(self.throttle_reason())
        job = Job(node_proto.new_job_id(), cap, args, timeout)
        with self._lock:
            self.jobs[job.id] = job
        try:
            self._q.put_nowait(job)
        except queue.Full:
            with self._lock:
                del self.jobs[job.id]
            raise NodeBusy("job queue full")
        self._start_worker()
        return job

    def _start_worker(self):
        with self._lock:
            if self.started:
                return
            self.started = True
        t = threading.Thread(target=self._work, name="pa-node-worker",
                             daemon=True)
        t.start()

    def _work(self):
        while True:
            try:
                job = self._q.get(timeout=5.0)
            except queue.Empty:
                with self._lock:
                    self.started = False
                    if self._q.empty():
                        return
                    self.started = True
                continue
            if job.cancel.is_set():
                job.status = node_proto.JOB_CANCELLED
                job.finished_at = _iso()
                continue
            job.status = node_proto.JOB_RUNNING
            job.started_at = _iso()
            t0 = time.monotonic()
            try:
                job.result = CAPS[job.cap](job.args, job, self)
                job.status = node_proto.JOB_DONE
            except JobTimeout as e:
                job.status = node_proto.JOB_FAILED
                job.error = f"timeout: {e}"
            except Exception as e:
                job.status = node_proto.JOB_FAILED
                job.error = str(e)[:500]
            job.cost_s = time.monotonic() - t0
            job.finished_at = _iso()

    def get(self, job_id):
        with self._lock:
            return self.jobs.get(job_id)

    def cancel_job(self, job_id):
        job = self.get(job_id)
        if job is None:
            return None
        job.cancel.set()
        if job.status == node_proto.JOB_QUEUED:
            job.status = node_proto.JOB_CANCELLED
            job.finished_at = _iso()
        return job


class NodeCapError(Exception):
    pass


class NodePaused(Exception):
    pass


class NodeBusy(Exception):
    pass


class JobTimeout(Exception):
    pass


def _iso():
    import datetime
    return datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="seconds")


def _load():
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def _mem_free_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def _disk_free_mb(path):
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return None


def _run_json_cmd(argv, timeout=10.0):
    """Run an allowlisted helper binary (termux-*) — never a shell.
    Returns parsed JSON or None on any failure."""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout,
                           check=False)
        out = p.stdout.decode("utf-8", "replace").strip()
        if p.returncode != 0 or not out:
            return None
        return json.loads(out)
    except Exception:
        return None


def _read_battery():
    """termux-battery-status -> %, else /sys power_supply capacity."""
    if shutil.which("termux-battery-status"):
        data = _run_json_cmd(["termux-battery-status"])
        if data and "percentage" in data:
            return data["percentage"]
    for base in ("/sys/class/power_supply/battery",
                 "/sys/class/power_supply/BAT0",
                 "/sys/class/power_supply/BAT1"):
        try:
            with open(os.path.join(base, "capacity")) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            continue
    return None


def _read_temp():
    """termux-battery-status temperature, else /sys thermal zone."""
    if shutil.which("termux-battery-status"):
        data = _run_json_cmd(["termux-battery-status"])
        if data and data.get("temperature") is not None:
            try:
                return float(data["temperature"])
            except (TypeError, ValueError):
                pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# capabilities — fixed allowlist; each takes (args, job, state) -> result

def _safe_path(state, rel, sub=""):
    """Join a relative path inside the data dir; reject traversal and
    absolute paths outright."""
    rel = str(rel or "").replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/") \
            or "\x00" in rel:
        raise NodeCapError("bad path")
    base = os.path.join(state.data_dir, sub) if sub else state.data_dir
    root = os.path.realpath(base)
    path = os.path.realpath(os.path.join(root, rel))
    if path != root and not path.startswith(root + os.sep):
        raise NodeCapError("bad path")
    return path


def cap_file_put(args, job, state):
    rel = _safe_path(state, args.get("path"), "")
    data = base64.b64decode(args.get("data_b64") or "", validate=True)
    if len(data) > FILE_MAX_BYTES:
        raise NodeCapError("too large")
    os.makedirs(os.path.dirname(rel), exist_ok=True)
    tmp = rel + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, rel)
    return {"path": os.path.relpath(rel, state.data_dir),
            "bytes": len(data)}


def cap_file_get(args, job, state):
    rel = _safe_path(state, args.get("path"), "")
    if not os.path.isfile(rel):
        raise NodeCapError("no such file")
    if os.path.getsize(rel) > FILE_MAX_BYTES:
        raise NodeCapError("too large")
    with open(rel, "rb") as f:
        data = f.read()
    return {"path": os.path.relpath(rel, state.data_dir),
            "data_b64": base64.b64encode(data).decode()}


def cap_fetch(args, job, state):
    """GET a public URL, size/time capped. No redirects on the node —
    pal's own egress handles those; we answer with the redirect
    status + Location instead."""
    url = str(args.get("url") or "")
    err, host, port = node_proto.vet_url(url)
    if err:
        raise NodeCapError(err)
    if not node_proto.resolve_public(host):
        raise NodeCapError("private_address")
    req = urllib.request.Request(url, headers={
        "User-Agent": "pa-node/1.0",
        "Accept": "text/plain,text/html,application/json,*/*"})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=FETCH_TIMEOUT) as r:
            data = r.read(FETCH_MAX_BYTES + 1)
            return {"status": r.status,
                    "final_url": r.geturl(),
                    "bytes": len(data[:FETCH_MAX_BYTES]),
                    "truncated": len(data) > FETCH_MAX_BYTES,
                    "body": data[:FETCH_MAX_BYTES]
                    .decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location") or ""
            err2, _, _ = node_proto.vet_url(urllib.parse.urljoin(url, loc)) \
                if loc else (None, "", 0)
            if loc and err2:
                loc = ""
            return {"status": e.code, "location": loc}
        return {"status": e.code,
                "error": e.reason or f"http {e.code}"}
    except Exception as e:
        raise NodeCapError(f"fetch_failed:{str(e)[:120]}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def cap_ping_http(args, job, state):
    """HEAD-ish timing probe of a public URL."""
    url = str(args.get("url") or "")
    err, host, _ = node_proto.vet_url(url)
    if err:
        raise NodeCapError(err)
    if not node_proto.resolve_public(host):
        raise NodeCapError("private_address")
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "pa-node/1.0"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        raise NodeCapError(f"fetch_failed:{str(e)[:120]}")
    return {"status": status,
            "ms": round((time.monotonic() - t0) * 1000)}


def cap_rss(args, job, state):
    return {"load": _load(), "mem_free_mb": _mem_free_mb(),
            "disk_free_mb": _disk_free_mb(state.data_dir),
            "battery_pct": state.battery_reader(),
            "uptime_s": round(time.monotonic() - START, 1)}


def cap_run_tool(args, job, state):
    """Run a pal-made tool (K1) in a subprocess with resource limits.
    The tool file + manifest must already live under <data>/tools —
    file_put is the only way they get there, and its manifest must say
    node_ok."""
    name = str(args.get("name") or "")
    if not name.replace("-", "").replace("_", "").isalnum() \
            or len(name) > 40:
        raise NodeCapError("bad tool name")
    tdir = os.path.join(state.data_dir, TOOL_DIR)
    tfile = os.path.realpath(os.path.join(tdir, name + ".py"))
    mfile = os.path.realpath(os.path.join(tdir, name + ".json"))
    if not tfile.startswith(os.path.realpath(tdir) + os.sep) \
            or not os.path.isfile(tfile):
        raise NodeCapError("no such tool")
    try:
        with open(mfile) as f:
            manifest = json.load(f)
    except Exception:
        raise NodeCapError("no manifest")
    if not manifest.get("node_ok"):
        raise NodeCapError("tool is not node_ok")
    timeout = min(float(args.get("timeout") or job.timeout), 300.0)
    wrapper = (
        "import json,sys,importlib.util,resource\n"
        "def _lim(res,n):\n"
        "    try:\n"
        "        soft,hard=resource.getrlimit(res)\n"
        "        cap=hard if hard>0 and hard<n else n\n"
        "        resource.setrlimit(res,(min(n,soft if soft>0 else n),cap))\n"
        "    except Exception:\n"
        "        pass\n"
        "_lim(resource.RLIMIT_CPU,%d)\n"
        "_lim(resource.RLIMIT_AS,268435456)\n"
        "_lim(resource.RLIMIT_NOFILE,64)\n"
        "if hasattr(resource,'RLIMIT_NPROC'): _lim(resource.RLIMIT_NPROC,32)\n"
        "spec=importlib.util.spec_from_file_location('made',sys.argv[1])\n"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
        "args=json.loads(sys.argv[2])\n"
        "out=m.run(args)\n"
        "print(json.dumps({'ok':True,'result':out}))\n"
        % (int(timeout) + 5))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": state.data_dir,
           "PYTHONDONTWRITEBYTECODE": "1",
           "TMPDIR": state.data_dir}
    try:
        p = subprocess.run([sys.executable, "-I", "-c", wrapper,
                            tfile, json.dumps(args.get("args") or {})],
                           capture_output=True, timeout=timeout,
                           env=env, cwd=state.data_dir)
    except subprocess.TimeoutExpired:
        raise JobTimeout(f"{timeout:.0f}s")
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace")[-300:]
        raise NodeCapError(f"tool failed: {err}")
    try:
        parsed = json.loads(out.splitlines()[-1])
    except Exception:
        raise NodeCapError("tool produced no result")
    return parsed.get("result")


# --- Termux:API caps (docs/PHONE.md) — each shells out to ONE fixed
# termux-* binary (no shell, no arbitrary args). Missing binary -> a
# clear capability error, so the same agent runs on plain Linux too.

def _have(binary):
    return shutil.which(binary) is not None


def _termux_run(binary, argv, timeout=15.0):
    if not _have(binary):
        raise NodeCapError(f"{binary} not installed")
    try:
        p = subprocess.run([binary] + list(argv),
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise JobTimeout(f"{binary} {timeout:.0f}s")
    return p


def cap_notify(args, job, state):
    p = _termux_run("termux-notification", [
        "--title", str(args.get("title") or "pal")[:80],
        "--content", str(args.get("text") or "")[:400]])
    if p.returncode != 0:
        raise NodeCapError(
            (p.stderr or b"").decode("utf-8", "replace")[:120])
    return {"sent": True}


def cap_tts_speak(args, job, state):
    p = _termux_run("termux-tts-speak",
                    [str(args.get("text") or "")[:600]],
                    timeout=60.0)
    if p.returncode != 0:
        raise NodeCapError(
            (p.stderr or b"").decode("utf-8", "replace")[:120])
    return {"spoken": True}


def cap_sms_list(args, job, state):
    n = max(1, min(int(args.get("max") or 10), 50))
    p = _termux_run("termux-sms-list", ["-l", str(n)], timeout=20.0)
    if p.returncode != 0:
        raise NodeCapError(
            (p.stderr or b"").decode("utf-8", "replace")[:120])
    try:
        msgs = json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception:
        raise NodeCapError("bad sms output")
    out = []
    for m in (msgs if isinstance(msgs, list) else [])[:n]:
        out.append({"from": m.get("address"),
                    "when": m.get("date"),
                    "body": str(m.get("body") or "")[:500]})
    return {"messages": out}


def cap_sms_send(args, job, state):
    to = str(args.get("to") or "").strip()
    body = str(args.get("body") or "")[:1600]
    if not to or not body:
        raise NodeCapError("to+body required")
    p = _termux_run("termux-sms-send", ["-n", to, body], timeout=30.0)
    if p.returncode != 0:
        raise NodeCapError(
            (p.stderr or b"").decode("utf-8", "replace")[:120])
    return {"sent": True, "to": to}


def cap_camera_photo(args, job, state):
    cam = re.sub(r"[^0-9a-z-]", "", str(args.get("camera") or "0"))[:8] \
        or "0"
    out = _safe_path(state, f"photos/cam-{cam}-{int(time.time())}.jpg")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    p = _termux_run("termux-camera-photo", ["-c", cam, out],
                    timeout=30.0)
    if p.returncode != 0 or not os.path.isfile(out):
        raise NodeCapError(
            (p.stderr or b"").decode("utf-8", "replace")[:120]
            or "no photo")
    data = open(out, "rb").read()
    if len(data) > FILE_MAX_BYTES:
        raise NodeCapError("photo too large")
    return {"bytes": len(data),
            "data_b64": base64.b64encode(data).decode(),
            "ext": "jpeg"}


def cap_location(args, job, state):
    p = _termux_run("termux-location", [], timeout=30.0)
    if p.returncode != 0:
        raise NodeCapError(
            (p.stderr or b"").decode("utf-8", "replace")[:120])
    try:
        loc = json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception:
        raise NodeCapError("bad location output")
    return {"lat": loc.get("latitude"), "lon": loc.get("longitude"),
            "accuracy_m": loc.get("accuracy"),
            "provider": loc.get("provider")}


def cap_battery(args, job, state):
    if _have("termux-battery-status"):
        data = _run_json_cmd(["termux-battery-status"], timeout=15.0)
        if data:
            return data
    return {"percentage": state.battery_reader(),
            "note": "no termux-api; host reading"}


def cap_sensor(args, job, state):
    name = str(args.get("name") or "")
    argv = ["-n", "1", "-d", "1000"]
    if name:
        argv += ["-s", name[:40]]
    p = _termux_run("termux-sensor", argv, timeout=20.0)
    if p.returncode != 0:
        raise NodeCapError(
            (p.stderr or b"").decode("utf-8", "replace")[:120])
    try:
        return json.loads(p.stdout.decode("utf-8", "replace"))
    except Exception:
        return {"raw": p.stdout.decode("utf-8", "replace")[:2000]}


def cap_vibrate(args, job, state):
    ms = max(0, min(int(args.get("ms") or 300), 5000))
    p = _termux_run("termux-vibrate", ["-d", str(ms)], timeout=10.0)
    if p.returncode != 0:
        raise NodeCapError(
            (p.stderr or b"").decode("utf-8", "replace")[:120])
    return {"vibrated_ms": ms}


CAPS = {
    "fetch": cap_fetch,
    "ping_http": cap_ping_http,
    "rss": cap_rss,
    "file_get": cap_file_get,
    "file_put": cap_file_put,
    "run_tool": cap_run_tool,
    # Termux:API (absent binaries fail cleanly on non-Android hosts)
    "notify": cap_notify,
    "tts_speak": cap_tts_speak,
    "sms_list": cap_sms_list,
    "sms_send": cap_sms_send,
    "camera_photo": cap_camera_photo,
    "location": cap_location,
    "battery": cap_battery,
    "sensor": cap_sensor,
    "vibrate": cap_vibrate,
}


# ---------------------------------------------------------------------------
# HTTP layer

def make_handler(state: NodeState):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "pa-node/1.0"

        def log_message(self, *a):
            pass

        def _auth(self):
            return node_proto.bearer_ok(self.headers, state.token)

        def do_GET(self):
            if not self._auth():
                return node_proto.write_json(
                    self, 401, {"error": "unauthorized"})
            path = self.path.split("?", 1)[0].rstrip("/")
            if path in ("", "/health"):
                return node_proto.write_json(self, 200, state.health())
            if path.startswith("/jobs/"):
                job = state.get(path.rsplit("/", 1)[-1])
                if job is None:
                    return node_proto.write_json(
                        self, 404, {"error": "no_job"})
                return node_proto.write_json(self, 200, job.record())
            return node_proto.write_json(self, 404, {"error": "no_path"})

        def do_POST(self):
            if not self._auth():
                return node_proto.write_json(
                    self, 401, {"error": "unauthorized"})
            path = self.path.split("?", 1)[0].rstrip("/")
            body, err = node_proto.read_json(self)
            if err:
                return node_proto.write_json(
                    self, err[1], {"error": err[0]})
            if path == "/jobs":
                try:
                    job = state.enqueue(
                        str(body.get("cap") or ""),
                        body.get("args") or {},
                        body.get("timeout") or 30.0)
                except NodePaused as e:
                    return node_proto.write_json(
                        self, 503, {"error": "paused",
                                    "detail": str(e)})
                except NodeCapError as e:
                    return node_proto.write_json(
                        self, 400, {"error": "bad_cap",
                                    "detail": str(e)})
                except NodeBusy as e:
                    return node_proto.write_json(
                        self, 429, {"error": "busy", "detail": str(e)})
                return node_proto.write_json(
                    self, 200, {"id": job.id, "status": job.status})
            if path.startswith("/jobs/") and path.endswith("/cancel"):
                jid = path.split("/")[2]
                job = state.cancel_job(jid)
                if job is None:
                    return node_proto.write_json(
                        self, 404, {"error": "no_job"})
                return node_proto.write_json(self, 200, job.record())
            return node_proto.write_json(self, 404, {"error": "no_path"})

    return Handler


def serve(state: NodeState, host="127.0.0.1", port=8790):
    srv = http.server.ThreadingHTTPServer(
        (host, port), make_handler(state))
    srv.daemon_threads = True
    return srv


# ---------------------------------------------------------------------------
# provisioning + entry

def load_token(token_file: str) -> str:
    """Read the shared token; create the file (0600) on first run."""
    try:
        with open(token_file) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    import secrets
    tok = secrets.token_urlsafe(32)
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


def main(argv=None):
    ap = argparse.ArgumentParser(description="pa-node agent")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=node_proto.DEFAULT_PORT)
    ap.add_argument("--data-dir",
                    default=os.path.expanduser("~/pa-node-data"))
    ap.add_argument("--token-file",
                    default=os.path.expanduser("~/.pa-node/token"))
    args = ap.parse_args(argv)
    os.makedirs(args.data_dir, mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(args.data_dir, TOOL_DIR), exist_ok=True)
    token = os.environ.get("PA_NODE_TOKEN") or load_token(args.token_file)
    state = NodeState(token, args.data_dir)
    srv = serve(state, args.host, args.port)
    print(f"pa-node on {args.host}:{args.port} — "
          f"{len(CAPS)} capabilities, data {args.data_dir}",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()

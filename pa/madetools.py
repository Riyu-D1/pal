"""K1: tool making (LATM arxiv:2305.17126 / CREATOR arxiv:2305.14318).

pal writes small pure-Python tools itself: tool_make asks the model
for one file defining ``run(args)`` plus 3-5 ``test_*`` functions,
runs the tests inside the runpy subprocess sandbox, and only then
registers the tool.

Files live OUTSIDE pa/, under ``<PA_DATA_DIR>/tools/``:
- ``<name>-v<N>.py``  one file per registered version
- ``<name>.json``     manifest: {name, version, purpose, inputs,
                      network, node_ok, tests}
- ``<name>.py``       copy of the active version (what nodes run)

Gates, in order:
1. static_check — pure-Python only: a whitelist of stdlib imports
   (json/math/re/datetime/...), no socket/subprocess/os/sys/urllib-
   request, no open/eval/exec/__import__/dunder tricks. Applied to the
   tool AND its tests.
2. sandboxed tests — runpy subprocess (rlimits, scrubbed env); the
   ONLY egress is the injected fetch() which routes through
   pa.egress.guarded_get (DNS-vetted, size/time-capped).
3. owner approval — the FIRST version of a name needs an Approve
   card that shows code + tests (chat_action 'tool_register');
   later versions whose manifest is unchanged and whose tests pass
   auto-register with a diff line in the audit/activity log.

tool_run dispatches to a node via the run_tool cap when the manifest
says node_ok and a node advertises it — else the pal-VM sandbox.
"""

import difflib
import json
import os
import re
import shutil

from . import db as dbmod, runpy
from .util import json_dumps, json_loads_bounded, now_iso

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
MAX_CODE = 20_000
MAX_TESTS = 15_000
TEST_TIMEOUT = 30.0
RUN_TIMEOUT = 30.0

# stdlib whitelist — pure computation only. Anything that can touch
# the network, the FS, the process table or the interpreter itself is
# absent BY CONSTRUCTION (requests/urllib.request/http/socket/
# subprocess/os/sys/shutil/sqlite3/ctypes/signal/... all missing).
ALLOWED_IMPORTS = frozenset({
    "json", "math", "re", "datetime", "time", "hashlib", "base64",
    "binascii", "itertools", "functools", "collections", "statistics",
    "textwrap", "string", "random", "html", "csv", "io", "urllib.parse",
    "zoneinfo", "decimal", "fractions", "calendar", "difflib",
    "unicodedata", "pprint", "typing", "enum", "dataclasses",
    "contextlib", "copy", "heapq", "bisect", "queue",
})

_IMPORT_RE = re.compile(
    r"^[ \t]*(?:import[ \t]+([A-Za-z0-9_., \t]+)|from[ \t]+"
    r"([A-Za-z0-9_.]+)[ \t]+import[ \t])", re.M)
_BANNED_NAME_RE = re.compile(
    r"\b(eval|exec|compile|__import__|open|input|breakpoint|"
    r"globals|locals|vars|setattr|delattr|getattr|exit|quit)\s*\(")
_BANNED_DUNDER_RE = re.compile(
    r"__(builtins|import|class|subclasses|mro|globals|dict|code|"
    r"closure|getattribute|setattr|delattr|loader|spec|file|path)__")
_RUN_DEF_RE = re.compile(r"^def\s+run\s*\(\s*args\b", re.M)


def tools_dir(s) -> str:
    return os.path.join(s.data_dir, "tools")


def static_check(src: str) -> str:
    """'' when the source is allowed; a short reason otherwise.
    Whitelist imports + banned builtins + the run(args) contract."""
    for m in _IMPORT_RE.finditer(src or ""):
        mods = m.group(1) or m.group(2) or ""
        for mod in mods.split(","):
            mod = mod.strip().split(" as ")[0].strip()
            if not mod:
                continue
            if not any(mod == ok or mod.startswith(ok + ".")
                       for ok in ALLOWED_IMPORTS):
                return f"banned import: {mod}"
    m = _BANNED_NAME_RE.search(src or "")
    if m:
        return f"banned builtin: {m.group(1)}()"
    m = _BANNED_DUNDER_RE.search(src or "")
    if m:
        return f"banned attribute: __{m.group(1)}__"
    return ""


def _need_run(src: str) -> str:
    return "" if _RUN_DEF_RE.search(src or "") else \
        "code must define run(args)"


def _parse_manifest(obj: dict, name: str) -> dict:
    """Normalise the model-declared manifest; pal owns the truth on
    name/version — the model can only declare inputs/network/node_ok."""
    inputs = obj.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {"type": "object",
                  "properties": {},
                  "additionalProperties": True}
    return {
        "name": name,
        "purpose": str(obj.get("purpose") or "")[:300],
        "inputs": inputs,
        "network": bool(obj.get("network")),
        "node_ok": bool(obj.get("node_ok")),
        "tests": [],
    }


# ---------------------------------------------------------------------------
# sandboxed test run — one runpy subprocess; fetch() is the only egress

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_HARNESS_PRELUDE = """\
import sys, json, traceback
sys.path.insert(0, __REPO__)
from pa.egress import guarded_get

def fetch(url, **kw):
    r = guarded_get(url, timeout=min(float(kw.get('timeout', 10)), 10.0))
    return {'status': r.get('status'), 'headers': dict(r.get('headers') or {}),
            'text': (r.get('text') or '')[:200000]}

_ns = {'fetch': fetch}
"""

_HARNESS_RUNNER = """
try:
    exec(__CODE__, _ns)
except Exception as _e:
    print('__RESULTS__' + json.dumps({'error': 'tool code failed to load: ' + str(_e)[:300]}))
    raise SystemExit(0)
try:
    exec(__TESTS__, _ns)
except Exception as _e:
    print('__RESULTS__' + json.dumps({'error': 'test code failed to load: ' + str(_e)[:300]}))
    raise SystemExit(0)
_out = []
_names = sorted(k for k in _ns if k.startswith('test_'))
if not _names:
    print('__RESULTS__' + json.dumps({'error': 'no test_* functions'}))
    raise SystemExit(0)
for _name in _names:
    try:
        _ns[_name]()
        _out.append([_name, 'pass'])
    except Exception as _e:
        _out.append([_name, 'fail: ' + str(_e)[:200]])
print('__RESULTS__' + json.dumps({'tests': _out}))
"""


def run_tests(s, code: str, tests: str,
              timeout: float = TEST_TIMEOUT) -> dict:
    """Execute test_* fns against code in the runpy sandbox.
    -> {passed: bool, tests: [[name, status]..]} or {error}."""
    harness = (_HARNESS_PRELUDE.replace("__REPO__", repr(_REPO))
               + _HARNESS_RUNNER.replace("__CODE__", repr(code))
               .replace("__TESTS__", repr(tests)))
    out = runpy.run(s, harness, timeout=timeout)
    if out.get("error"):
        return {"error": out["error"],
                "message": out.get("message") or "sandbox failed"}
    marker = "__RESULTS__"
    text = out.get("stdout") or ""
    i = text.rfind(marker)
    if i == -1:
        return {"error": "no_results",
                "message": (out.get("stderr") or "")[-300:] or
                           "tests produced no result"}
    try:
        res = json.loads(text[i + len(marker):].strip().splitlines()[0])
    except Exception:
        return {"error": "bad_results"}
    if "error" in res:
        return {"error": "tests_error", "message": res["error"]}
    results = res.get("tests") or []
    passed = bool(results) and all(s_ == "pass" for _, s_ in results)
    return {"passed": passed, "tests": results}


def _run_local(s, path: str, args: dict,
               timeout: float = RUN_TIMEOUT) -> dict:
    """run(args) in the same sandbox shape the tests saw."""
    wrapper = (_HARNESS_PRELUDE.replace("__REPO__", repr(_REPO)) + """
import importlib.util
_spec = importlib.util.spec_from_file_location('made', __PATH__)
_m = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_m)
except Exception as _e:
    print('__RESULTS__' + json.dumps({'error': 'load failed: ' + str(_e)[:300]}))
    raise SystemExit(0)
setattr(_m, 'fetch', fetch)
try:
    _r = _m.run(__ARGS__)
    print('__RESULTS__' + json.dumps({'ok': True, 'result': _r}))
except Exception as _e:
    print('__RESULTS__' + json.dumps({'error': str(_e)[:400]}))
""".replace("__PATH__", repr(path))
               .replace("__ARGS__", repr(args)))
    out = runpy.run(s, wrapper, timeout=timeout)
    if out.get("error"):
        return {"error": out["error"],
                "message": out.get("message") or "sandbox failed"}
    text = out.get("stdout") or ""
    i = text.rfind("__RESULTS__")
    if i == -1:
        return {"error": "no_result",
                "message": (out.get("stderr") or "")[-300:]}
    try:
        res = json.loads(text[i + len("__RESULTS__"):].strip()
                         .splitlines()[0])
    except Exception:
        return {"error": "bad_result"}
    return res


# ---------------------------------------------------------------------------
# registry

def _active(conn, name: str):
    return conn.execute(
        "SELECT * FROM made_tools WHERE name=? AND status='active'"
        " ORDER BY version DESC LIMIT 1", (name,)).fetchone()


def _next_version(conn, name: str) -> int:
    row = conn.execute(
        "SELECT MAX(version) v FROM made_tools WHERE name=?",
        (name,)).fetchone()
    return (row["v"] or 0) + 1


def list_tools(conn, status="active") -> list:
    rows = conn.execute(
        "SELECT name, version, purpose, manifest_json, status,"
        " created_at FROM made_tools WHERE status=?"
        " ORDER BY name, version DESC", (status,)).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["name"] in seen:        # newest active version per name
            continue
        seen.add(r["name"])
        d = dict(r)
        try:
            d["manifest"] = json_loads_bounded(r["manifest_json"]) \
                or {}
        except Exception:
            d["manifest"] = {}
        d.pop("manifest_json", None)
        out.append(d)
    return out


def _manifest_surface(m: dict) -> dict:
    """What the caller sees — version and bookkeeping stripped."""
    return {k: m.get(k) for k in
            ("name", "purpose", "inputs", "network", "node_ok")}


def _surface_diff(old: dict, new: dict) -> str:
    lines = list(difflib.unified_diff(
        json.dumps(_manifest_surface(old), indent=1,
                   sort_keys=True).splitlines(),
        json.dumps(_manifest_surface(new), indent=1,
                   sort_keys=True).splitlines(),
        lineterm="", n=1))
    return "\n".join(lines)[:400]


def _write_files(s, name: str, version: int, code: str,
                 manifest: dict) -> dict:
    tdir = tools_dir(s)
    os.makedirs(tdir, exist_ok=True)
    vpath = os.path.join(tdir, f"{name}-v{version}.py")
    with open(vpath, "w") as f:
        f.write(code)
    live = os.path.join(tdir, f"{name}.py")
    shutil.copyfile(vpath, live)
    mpath = os.path.join(tdir, f"{name}.json")
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=1)
    return {"dir": tdir, "file": vpath, "live": live,
            "manifest": mpath}


def register(conn, s, name: str, code: str, tests: str,
             manifest: dict, source="owner_approved") -> dict:
    """Persist a passed tool: files + active row (older versions of
    the name keep their rows for history/rollback)."""
    version = _next_version(conn, name)
    manifest = dict(manifest)
    manifest["name"] = name
    manifest["version"] = version
    manifest["tests"] = [
        n for n in sorted(set(
            re.findall(r"def\s+(test_\w+)\s*\(", tests or "")))]
    files = _write_files(s, name, version, code, manifest)
    conn.execute(
        "INSERT INTO made_tools(name, version, purpose, manifest_json,"
        " status, path, tests_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (name, version, manifest.get("purpose") or "",
         json_dumps(manifest), "active", files["file"],
         json_dumps(manifest["tests"]), now_iso()))
    dbmod.audit(conn, "assistant", "tool_register",
                f"{name} v{version} ({source})")
    from .evolve import archive
    archive.note(conn, s, "made_tool", name,
                 {"code": code, "tests": tests, "manifest": manifest},
                 created_by=source, status="live")
    return {"registered": True, "name": name, "version": version,
            "node_ok": bool(manifest.get("node_ok")),
            "network": bool(manifest.get("network")),
            "path": files["file"]}


# ---------------------------------------------------------------------------
# generation + the make/run/list chat fns

_GEN_PROMPT = """\
Write a small pure-Python tool and its tests.

Tool contract:
- ONE python file defining run(args: dict) -> JSON-serialisable result.
- Pure computation only. Whitelisted imports: {imports}. NOTHING else
  may be imported — no os/sys/subprocess/socket/urllib.request, no
  open(), eval(), exec(), getattr(), globals(), or dunder attributes.
- If the tool needs the web, call the provided fetch(url, timeout=10)
  function (it is injected; do NOT import it) — it returns
  {{status, headers, text}} and is the ONLY network path.
- Tests: 3-5 top-level functions named test_1() ... test_5(), each
  calling run(...) with plain asserts. Tests may not touch the
  network, files, or the clock-dependent edge cases.

Name: {name}
Purpose: {purpose}
Examples: {examples}

Reply with ONE fenced json block and nothing else:
```json
{{"code": "<the file>", "tests": "<the test functions>",
  "inputs": {{"type":"object","properties":{{...}}}},
  "network": true|false, "node_ok": true|false}}
```
network = the tool calls fetch(). node_ok = the tool is safe to run
on a small helper device (no secrets, no heavy deps).
"""


def _brain_complete(ctx, prompt: str, remaining=60.0) -> str:
    brain = getattr(ctx, "brain", None)
    if brain is None:
        raise RuntimeError("no model brain wired into this turn")
    try:
        return brain.complete(
            [{"role": "user", "content": prompt}], remaining,
            tier="smart", scope="owner-chat") or ""
    except TypeError:
        return brain.complete(
            [{"role": "user", "content": prompt}], remaining) or ""


def _extract_json(raw: str):
    """First fenced json object in the model reply."""
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", raw or "", re.S):
        try:
            obj = json_loads_bounded(m.group(1).strip(),
                                     max_len=200_000)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    try:
        obj = json_loads_bounded((raw or "").strip(),
                                 max_len=200_000)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def make(ctx, name: str, purpose: str, examples: str) -> dict:
    """tool_make: generate -> static check -> sandboxed tests ->
    approval (first version) -> register."""
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        return {"error": "bad_arguments",
                "message": "tool names are [a-z][a-z0-9_]{0,30}"}
    purpose = (purpose or "").strip()
    if not purpose:
        return {"error": "bad_arguments", "message": "empty purpose"}
    prompt = _GEN_PROMPT.format(
        name=name, purpose=purpose[:500],
        examples=str(examples or "")[:800],
        imports=", ".join(sorted(ALLOWED_IMPORTS)))
    try:
        raw = _brain_complete(ctx, prompt)
    except Exception as e:
        return {"error": "model_error", "message": str(e)[:200]}
    obj = _extract_json(raw)
    if obj is None:
        return {"error": "bad_model_output",
                "message": "the model did not return the tool as one "
                           "fenced json block"}
    code = str(obj.get("code") or "")
    tests = str(obj.get("tests") or "")
    if not code.strip() or not tests.strip():
        return {"error": "bad_model_output",
                "message": "model returned empty code or tests"}
    if len(code) > MAX_CODE or len(tests) > MAX_TESTS:
        return {"error": "too_large",
                "message": "generated code/tests over the cap"}
    for src, label in ((code, "code"), (tests, "tests")):
        bad = static_check(src)
        if bad:
            return {"error": "static_check",
                    "message": f"{label}: {bad}"}
    bad = _need_run(code)
    if bad:
        return {"error": "static_check", "message": bad}
    res = run_tests(ctx.s, code, tests)
    if res.get("error"):
        return {"error": "test_error",
            "message": res.get("message") or res["error"]}
    if not res["passed"]:
        return {"error": "tests_failed", "tests": res["tests"],
                "message": "generated tests did not all pass — the "
                           "tool was NOT registered"}
    manifest = _parse_manifest({**obj, "purpose": purpose}, name)
    prior = _active(ctx.conn, name)
    version = _next_version(ctx.conn, name)
    if prior is not None:
        old_manifest = json_loads_bounded(prior["manifest_json"]) or {}
        if _manifest_surface(old_manifest) == _manifest_surface(
                {**manifest, "version": prior["version"]}):
            out = register(ctx.conn, ctx.s, name, code, tests,
                           manifest, source="auto")
            # the new version's own surface equals the old — the diff
            # is in the body, which the audit line links
            dbmod.audit(ctx.conn, "assistant", "tool_auto_register",
                        f"{name} v{prior['version']} -> v{out['version']}")
            out["auto_registered"] = True
            out["diff"] = _surface_diff(old_manifest, manifest)
            return out
        return {"error": "manifest_changed",
                "message": f"{name} already exists and this version "
                           "changes its inputs/network/node_ok — the "
                           "owner must retire it first",
                "diff": _surface_diff(old_manifest, manifest)}
    # first registration: owner approval showing code + tests
    if ctx.approved_replay:
        return register(ctx.conn, ctx.s, name, code, tests,
                        manifest, source="replayed_approval")
    if ctx.unattended:
        aid = _queue_register(ctx, name, version, code, tests,
                              manifest)
        return {"queued": True, "approval_id": aid,
                "message": f"{name} passes its tests — registration "
                           "is held for the owner"}
    decision = ctx.approve(
        "tool_register",
        {"name": name, "version": version,
         "manifest": manifest, "code": code, "tests": tests})
    if decision != "approved":
        return {"error": "not_approved", "decision": decision,
                "message": "owner did not approve — the tool was "
                           "not registered",
                "tests": res["tests"]}
    return register(ctx.conn, ctx.s, name, code, tests, manifest)


def _queue_register(ctx, name, version, code, tests, manifest) -> str:
    """Unattended turns: a job_action carrying everything register()
    needs; on Approve, execute_approved -> register_approved."""
    return _queue_register_like(
        ctx, "tool_register",
        {"name": name, "version": version, "code": code,
         "tests": tests, "manifest": manifest})


def _queue_register_like(ctx, tool: str, args: dict) -> str:
    """Generic held action: a job_action the owner's Approve replays
    through execute_approved (K2 uses it for integration_register)."""
    from . import approvals
    return approvals.create_approval(
        ctx.conn, "job_action",
        {"tool": tool, "args": args,
         "channel_id": str(ctx.channel_id)})


def register_approved(ctx, args: dict) -> dict:
    """execute_approved path: an owner click on a held
    tool_register job_action."""
    name = str(args.get("name") or "")
    code = str(args.get("code") or "")
    tests = str(args.get("tests") or "")
    manifest = args.get("manifest") or {}
    prior = _active(ctx.conn, name)
    if prior is not None:
        return {"error": "already_registered",
                "message": f"{name} is already active at v"
                           f"{prior['version']}"}
    return register(ctx.conn, ctx.s, name, code, tests,
                    manifest, source="approved")


def run(ctx, name: str, args: dict) -> dict:
    """tool_run: node when manifest says node_ok, else the local
    sandbox. Node dispatch is capability+health routed."""
    name = (name or "").strip().lower()
    row = _active(ctx.conn, name)
    if row is None:
        pend = ctx.conn.execute(
            "SELECT 1 FROM made_tools WHERE name=? AND"
            " status='pending' LIMIT 1", (name,)).fetchone()
        return {"error": "not_found",
                "message": (f"{name} is still awaiting registration "
                            "approval" if pend else
                            f"no made tool named {name!r} "
                            "(tool_list shows them)")}
    manifest = json_loads_bounded(row["manifest_json"]) or {}
    if manifest.get("integration"):
        from . import integrations as intmod
        return intmod.run_integration(ctx, row, manifest, args)
    if manifest.get("node_ok"):
        try:
            from . import nodes as nodesmod
            out = _run_on_node(ctx, nodesmod, row, manifest, args)
            if out is not None:
                return out
        except Exception:
            pass                      # fall through to the local run
    res = _run_local(ctx.s, row["path"],
                     args if isinstance(args, dict) else {})
    if "result" in res and res.get("ok"):
        return {"name": name, "version": int(row["version"]),
                "ran_on": "local", "result": res["result"]}
    return {"error": res.get("error", "run_failed"),
            "message": res.get("message") or "",
            "name": name}


def _run_on_node(ctx, nodesmod, row, manifest, args):
    """Ship <name>.py + <name>.json via file_put, then run_tool.
    None = no usable node (caller falls back to local)."""
    node = nodesmod.pick_node(ctx.conn, ctx.s, "run_tool")
    if not node:
        return None
    s = ctx.s
    try:
        with open(row["path"], "rb") as f:
            code = f.read()
        import base64
        manifest_bytes = json.dumps(manifest, indent=1).encode()
        for rel, data in ((f"tools/{row['name']}.py", code),
                          (f"tools/{row['name']}.json",
                           manifest_bytes)):
            ship = nodesmod.submit_and_wait(
                ctx.conn, s, "file_put",
                {"path": rel, "data_b64":
                 base64.b64encode(data).decode()},
                node=node, timeout=20.0,
                scope="owner-chat")
            if ship.get("status") != "done":
                return None
        job = nodesmod.submit_and_wait(
            ctx.conn, s, "run_tool",
            {"name": row["name"], "args": args or {}},
            node=node, timeout=min(RUN_TIMEOUT, 60.0),
            scope="owner-chat")
    except Exception:
        return None
    if job.get("status") != "done":
        return None
    try:
        result = json_loads_bounded(job["result_json"])
    except Exception:
        result = job.get("result_json")
    return {"name": row["name"], "version": int(row["version"]),
            "ran_on": node, "result": result}

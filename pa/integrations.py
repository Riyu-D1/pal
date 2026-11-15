"""Integration builder (Alita-style) — pal writes its own small
API clients instead of needing pre-built connectors.

integration_build(service, goal) -> the model produces one integration
manifest + a set of made-tool-style tools ({name, purpose, code,
tests}) that
call the service's API through fetch(). At BUILD time the tools are
tested against model-provided fixtures — no live calls. On pass, the
whole bundle goes to the owner for approval (first registration shows
manifest + code); on Approve it registers as:

* a `connections` row kind='made', provider 'made:<name>' (or
  'made:<name>@pal' — the @label IS the account-owner tag, so
  owner_kind/policies/`/connect` listing all reuse the existing
  machinery), meta_json carrying base_url + auth scheme + tool names;
* `made_tools` rows — one per generated tool — whose manifest carries
  `integration` + `read_only`, so tool_run policy classifies owner-
  tagged write tools as exec (per-run approval) and pal-tagged read
  tools as read (autonomous).

API keys never touch generated code or the sandbox env: the owner
deposits them with `integration_key` (sealed into the connection's
token_blob); at run time the harness writes the key to a 0600 file
and the wrapper — not the tool — injects it into fetch(), which is
also pinned to the integration's own base host so nothing can leak
the key elsewhere.
"""

import json
import logging
import os
import re
import tempfile

from . import connectflows, db as dbmod, madetools, secretsbox
from .util import json_dumps, json_loads_bounded, now_iso

log = logging.getLogger("pa.integrations")

MAX_TOOLS = 6
SCHEMES = ("none", "bearer", "header", "query")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_HOST_RE = re.compile(r"^https://[A-Za-z0-9_.-]+(?::\d+)?/?$")


def provider_for(name: str, account: str = "") -> str:
    base = f"made:{name}"
    return f"{base}@{account}" if account else base


# ---------------------------------------------------------------------------
# generation

_GEN = """\
Write a tiny API client integration for pal.

Service: {service}
Goal: {goal}
Docs: {docs}

Return ONE fenced json block:
{{"name": "<snake_case integration name>",
  "base_url": "https://<api host>",
  "auth": {{"scheme": "none|bearer|header|query",
           "param": "<header or query param name>"}},
  "tools": [{{"name": "<snake_case tool name>",
             "purpose": "<one line>",
             "read_only": true|false,
             "code": "<python module>",
             "tests": "<python test fns>"}}],
  "fixtures": {{"<url substring>": "<response body>"}}}}

Tool contract (same as tool_make):
- each module defines run(args: dict) -> JSON-serialisable result;
- requests go through fetch(url) -> {{status, headers, text}} — NEVER
  import os/sys/subprocess/socket/urllib.request, never open(), eval(),
  exec(), getattr() or dunder attributes;
- only whitelisted imports: {imports};
- build URLs from base_url + args; the API key is injected by the
  runtime — code must NEVER reference a key, token or secret itself;
- 3 test_* fns per tool exercising run() against fetch() — the test
  harness answers fetch() from `fixtures` (no real network).
"""


def _extract_block(raw: str):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw or "", re.S)
    if not m:
        m = re.search(r"(\{.*\})", raw or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _validate(obj: dict):
    """Manifest sanity -> (manifest, tools, fixtures) or error str."""
    name = str(obj.get("name") or "").strip().lower()
    if not _NAME_RE.match(name):
        return None, None, None, "bad name"
    base = str(obj.get("base_url") or "").strip().rstrip("/")
    if not _HOST_RE.match(base + "/"):
        return None, None, None, "base_url must be plain https host"
    auth = obj.get("auth") or {}
    scheme = str(auth.get("scheme") or "none").lower()
    if scheme not in SCHEMES:
        return None, None, None, f"auth scheme must be {SCHEMES}"
    tools = obj.get("tools") or []
    if not tools or len(tools) > MAX_TOOLS:
        return None, None, None, f"need 1..{MAX_TOOLS} tools"
    out_tools = []
    for t in tools:
        tname = f"{name}_{str(t.get('name') or '').strip().lower()}"
        tname = re.sub(r"[^a-z0-9_]+", "_", tname)
        if not _NAME_RE.match(tname):
            return None, None, None, f"bad tool name {tname!r}"
        code, tests = str(t.get("code") or ""), str(t.get("tests") or "")
        if not code.strip() or not tests.strip():
            return None, None, None, f"{tname}: empty code/tests"
        if re.search(r"(?i)(api[_-]?key|token|secret|password)\s*=\s*"
                     r"['\"]\w", code):
            return None, None, None, \
                f"{tname}: hardcoded credential in code"
        for src, label in ((code, "code"), (tests, "tests")):
            bad = madetools.static_check(src)
            if bad:
                return None, None, None, f"{tname} {label}: {bad}"
        bad = madetools._need_run(code)
        if bad:
            return None, None, None, f"{tname}: {bad}"
        out_tools.append({"name": tname,
                          "purpose": str(t.get("purpose") or "")[:200],
                          "read_only": bool(t.get("read_only")),
                          "code": code, "tests": tests})
    fixtures = obj.get("fixtures") or {}
    manifest = {"name": name, "base_url": base,
                "auth": {"scheme": scheme,
                         "param": str(auth.get("param") or "")[:40]},
                "docs": str(obj.get("docs") or "")[:200]}
    return manifest, out_tools, fixtures, ""


# ---------------------------------------------------------------------------
# fixture-backed test harness — build-time tests never hit the network

_FX_PRELUDE = """\
import sys, json
_fixtures = __FIXTURES__
def fetch(url, **kw):
    for pat, body in _fixtures.items():
        if pat in url:
            return {'status': 200, 'headers': {}, 'text': body[:200000]}
    return {'status': 404, 'headers': {}, 'text': 'no fixture'}
_ns = {'fetch': fetch}
"""


def run_fixture_tests(s, tool, fixtures, timeout=30.0) -> dict:
    harness = (_FX_PRELUDE.replace("__FIXTURES__", repr(fixtures))
               + madetools._HARNESS_RUNNER
               .replace("__CODE__", repr(tool["code"]))
               .replace("__TESTS__", repr(tool["tests"])))
    from . import runpy
    out = runpy.run(s, harness, timeout=timeout)
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
    res["passed"] = bool(res.get("tests")) and all(
        st == "pass" for _, st in res["tests"])
    return res


# ---------------------------------------------------------------------------
# chat fns

def build(ctx, service: str, goal: str, docs_url: str = "",
          account: str = "") -> dict:
    s = ctx.s
    if not getattr(s, "integrations", False):
        return {"error": "not_configured",
                "message": "integration building is off "
                           "(PA_INTEGRATIONS)"}
    if not getattr(s, "madetools", False):
        return {"error": "not_configured",
                "message": "needs PA_MADETOOLS=1 — integrations are "
                           "made tools"}
    service = str(service or "").strip()[:120]
    goal = str(goal or "").strip()[:400]
    if not service or not goal:
        return {"error": "bad_arguments",
                "message": "service and goal required"}
    account = "pal" if str(account or "").lower() == "pal" else ""

    raw = madetools._brain_complete(
        ctx, _GEN.format(service=service, goal=goal,
                         docs=docs_url[:300] or "(not provided)",
                         imports=", ".join(
                             sorted(madetools.ALLOWED_IMPORTS))))
    obj = _extract_block(raw)
    if obj is None:
        return {"error": "bad_model_output"}
    manifest, tools, fixtures, err = _validate(obj)
    if err:
        return {"error": "invalid_integration", "message": err}
    name = manifest["name"]
    provider = provider_for(name, account)

    if connectflows.get(ctx.conn, provider) is not None:
        return {"error": "exists",
                "message": f"integration {name!r} already registered"}
    for t in tools:
        res = run_fixture_tests(s, t, fixtures)
        if res.get("error") or not res.get("passed"):
            return {"error": "tests_failed", "tool": t["name"],
                    "tests": res.get("tests"),
                    "message": res.get("message") or
                    f"{t['name']} tests failed — not registered"}

    payload = {"provider": provider, "name": name, "account": account,
               "manifest": manifest, "service": service, "goal": goal,
               "tools": [{"name": t["name"], "purpose": t["purpose"],
                          "read_only": t["read_only"],
                          "code": t["code"], "tests": t["tests"]}
                         for t in tools]}
    if ctx.approved_replay:
        return register_approved(ctx, payload)
    if ctx.unattended:
        aid = madetools._queue_register_like(
            ctx, "integration_register", payload)
        return {"queued": True, "approval_id": aid,
                "message": f"integration {name} passes its fixture "
                           "tests — registration held for the owner"}
    decision = ctx.approve("integration_register",
                           {**payload,
                            "tools": [t["name"] for t in tools]})
    if decision != "approved":
        return {"error": "not_approved", "decision": decision}
    return register(ctx, payload)


def register(ctx, payload: dict) -> dict:
    """Write the connections row + made_tools rows."""
    manifest = payload["manifest"]
    provider = payload["provider"]
    names = []
    for t in payload["tools"]:
        m = {"name": t["name"], "purpose": t["purpose"],
             "inputs": {"type": "object", "properties": {}},
             "network": True, "node_ok": False,
             "read_only": bool(t.get("read_only")),
             "integration": manifest["name"],
             "integration_provider": provider}
        out = madetools.register(ctx.conn, ctx.s, t["name"], t["code"],
                                 t["tests"], m, source="integration")
        names.append(out["name"])
    meta = {"integration": True, "base_url": manifest["base_url"],
            "auth": manifest["auth"], "tools": names,
            "service": payload.get("service") or "",
            "goal": payload.get("goal") or "",
            "docs": manifest.get("docs") or ""}
    connectflows.upsert(
        ctx.conn, provider, "made", "",
        "integration " + ",".join(names), "active",
        manifest["base_url"], "", None, meta)
    dbmod.audit(ctx.conn, "assistant", "integration_register",
                f"{provider}: {len(names)} tools")
    return {"registered": True, "name": manifest["name"],
            "provider": provider, "tools": names,
            "account": "pal" if provider.endswith("@pal") else "owner",
            "message": f"{manifest['name']} registered with "
                       f"{len(names)} tools — deposit its API key with "
                       "integration_key if the service needs one"}


def register_approved(ctx, payload: dict) -> dict:
    if connectflows.get(ctx.conn, payload["provider"]) is not None:
        return {"error": "already_registered"}
    return register(ctx, payload)


def deposit_key(ctx, name: str, key: str, account: str = "") -> dict:
    """integration_key: seal the API key into the connection's
    token_blob. The value is never echoed or audited."""
    name = re.sub(r"^made:", "", str(name or "").strip().lower())
    provider = provider_for(name, account)
    row = connectflows.get(ctx.conn, provider)
    if row is None:
        return {"error": "not_found",
                "message": f"no integration {provider!r}"}
    key = str(key or "").strip()
    if not key or len(key) > 400:
        return {"error": "bad_arguments",
                "message": "key required (<=400 chars)"}
    blob = secretsbox.seal(ctx.env, connectflows._aad(provider),
                           {"api_key": key})
    ctx.conn.execute(
        "UPDATE connections SET token_blob=?, updated_at=? WHERE"
        " provider=?", (blob, now_iso(), provider))
    ctx.conn.commit()
    dbmod.audit(ctx.conn, "owner", "integration_key", provider)
    return {"saved": True, "provider": provider}


def list_integrations(conn) -> list:
    rows = conn.execute(
        "SELECT provider, status, meta_json, owner_kind, updated_at,"
        " token_blob FROM connections WHERE kind='made'"
        " ORDER BY provider").fetchall()
    out = []
    for r in rows:
        meta = json_loads_bounded(r["meta_json"]) or {}
        out.append({"provider": r["provider"],
                    "owner": r["owner_kind"],
                    "status": r["status"],
                    "base_url": meta.get("base_url", ""),
                    "tools": meta.get("tools") or [],
                    "key_set": bool(r["token_blob"]),
                    "updated_at": r["updated_at"]})
    return out


# ---------------------------------------------------------------------------
# auth-injecting run — called from madetools.run for integration tools

def _keyfile(ctx, provider: str):
    """Unseal the API key into a 0600 file; the sandbox wrapper reads
    it (tool code can't: open()/os are banned). Path only in argv."""
    row = connectflows.get(ctx.conn, provider)
    key = ""
    if row and row["token_blob"]:
        try:
            blob = secretsbox.open_blob(
                ctx.env, connectflows._aad(provider),
                row["token_blob"])
            key = blob.get("api_key") or ""
        except Exception:
            key = ""
    fd, path = tempfile.mkstemp(prefix="pakey-", dir=ctx.s.data_dir)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return path


_RUN_WRAP = madetools._HARNESS_PRELUDE.replace(
    "from pa.egress import guarded_get",
    "from pa.egress import guarded_get") + """
import importlib.util, json as _json
_base = __BASE__
_scheme = __SCHEME__
_param = __PARAM__
_keyfile = __KEYFILE__
_key = ''
try:
    with __builtins__.open(_keyfile) as _f:
        _key = _f.read().strip()
except Exception:
    _key = ''

def fetch(url, **kw):
    if not str(url).startswith(_base):
        return {'status': 403, 'headers': {},
                'text': 'fetch pinned to integration base_url'}
    if _key:
        if _scheme == 'bearer':
            kw.setdefault('headers', {})['Authorization'] = \
                'Bearer ' + _key
        elif _scheme == 'header' and _param:
            kw.setdefault('headers', {})[_param] = _key
        elif _scheme == 'query' and _param:
            sep = '&' if '?' in url else '?'
            url = url + sep + _param + '=' + _key
    r = guarded_get(url, timeout=min(float(kw.get('timeout', 10)), 10.0))
    return {'status': r.get('status'),
            'headers': dict(r.get('headers') or {}),
            'text': (r.get('text') or '')[:200000]}

_ns['fetch'] = fetch
_spec = importlib.util.spec_from_file_location('made', __PATH__)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
setattr(_m, 'fetch', fetch)
try:
    _r = _m.run(__ARGS__)
    print('__RESULTS__' + _json.dumps({'ok': True, 'result': _r}))
except Exception as _e:
    print('__RESULTS__' + _json.dumps({'error': str(_e)[:400]}))
"""


def _wrapper(base, scheme, param, keyfile, path, args) -> str:
    return (_RUN_WRAP
            .replace("__REPO__", repr(madetools._REPO))
            .replace("__BASE__", repr(base))
            .replace("__SCHEME__", repr(scheme or "none"))
            .replace("__PARAM__", repr(param or ""))
            .replace("__KEYFILE__", repr(keyfile))
            .replace("__PATH__", repr(path))
            .replace("__ARGS__", repr(args)))


def run_integration(ctx, row, manifest, args: dict) -> dict:
    """tool_run for an integration tool: fetch() injects the sealed
    key and is pinned to the integration's base_url."""
    provider = manifest.get("integration_provider") or ""
    conn_row = connectflows.get(ctx.conn, provider)
    if conn_row is None:
        return {"error": "not_found",
                "message": f"integration {provider!r} is gone"}
    meta = json_loads_bounded(conn_row["meta_json"]) or {}
    base = (meta.get("base_url") or manifest.get("base_url") or "")
    auth = meta.get("auth") or {}
    keyfile = _keyfile(ctx, provider)
    try:
        wrapper = _wrapper(base, auth.get("scheme"),
                           auth.get("param"), keyfile, row["path"],
                           args if isinstance(args, dict) else {})
        from . import runpy
        out = runpy.run(ctx.s, wrapper, timeout=madetools.RUN_TIMEOUT)
    finally:
        try:
            os.unlink(keyfile)
        except OSError:
            pass
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
    if res.get("ok"):
        return {"name": row["name"], "version": int(row["version"]),
                "ran_on": "local", "integration": provider,
                "result": res["result"]}
    return {"error": res.get("error", "run_failed"),
            "name": row["name"]}

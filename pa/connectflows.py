"""Connections library: orchestration for /connect.

The catalog below is DATA — one line per verified official remote MCP
server (endpoint checked against the vendor's current docs on
2026-11-10). `url:<https://...>` connects any remote MCP server; `google`
is the owner's own OAuth client for Gmail+Calendar; anything else falls
back to Composio's managed connect link when COMPOSIO_API_KEY is set.

Status lifecycle: a /connect writes an oauth_states row (10 min,
single-use) and returns a sign-in URL; the provider redirects to
/oauth/callback; the callback exchanges the code, seals the token blob
(PA_SECRETS_KEY) and marks the connection active. Refreshing happens
inside access_token() — an invalid_grant marks the row 'expired' and
chat tools report "reconnect with /connect".
"""

import datetime
import json
import urllib.parse

from . import composio as composiomod
from . import mcp as mcpmod
from . import oauth, secretsbox
from .config import GMAIL_AUTH_SCOPES, is_private_host
from .util import json_dumps, json_loads_bounded, new_id, now_iso, \
    parse_iso, utcnow

STATE_TTL_SECONDS = 600          # 10 minutes, single-use
CLIENT_NAME = "pa — personal assistant"


class ConnectError(Exception):
    """User-facing connect failure (safe to show in Discord)."""


# ---------------------------------------------------------------------------
# catalog (data — verified against vendor docs; add one line per server)

MCP_CATALOG = {
    "notion": {
        "label": "Notion",
        "url": "https://mcp.notion.com/mcp",
        "scopes": "",
        "docs": "developers.notion.com/guides/mcp"},
    "linear": {
        "label": "Linear",
        "url": "https://mcp.linear.app/mcp",
        "scopes": "read write",
        "docs": "linear.app/docs/mcp"},
    "atlassian": {
        "label": "Atlassian (Jira/Confluence)",
        "url": "https://mcp.atlassian.com/v2/mcp",
        "scopes": "offline_access read:me "
                  "read:jira:agent-interface write:jira:agent-interface "
                  "search:jira:agent-interface "
                  "read:confluence:agent-interface "
                  "write:confluence:agent-interface "
                  "search:confluence:agent-interface",
        "docs": "developer.atlassian.com/cloud/rovo-mcp"},
    "github": {
        "label": "GitHub",
        "url": "https://api.githubcopilot.com/mcp/",
        "scopes": "repo read:org read:user project notifications",
        "client_env": ("PA_GITHUB_CLIENT_ID", "PA_GITHUB_CLIENT_SECRET"),
        "composio_toolkit": "github",
        "docs": "github.com/github/github-mcp-server"},
    # verified official servers that need no sign-in at all
    "deepwiki": {
        "label": "DeepWiki (public repos)",
        "url": "https://mcp.deepwiki.com/mcp",
        "auth": "none",
        "docs": "docs.devin.ai/work-with-devin/deepwiki-mcp"},
    "cloudflare-docs": {
        "label": "Cloudflare Docs",
        "url": "https://docs.mcp.cloudflare.com/mcp",
        "auth": "none",
        "docs": "developers.cloudflare.com/agents"},
    "context7": {
        "label": "Context7 (library docs)",
        "url": "https://mcp.context7.com/mcp",
        "auth": "none",
        "docs": "context7.com/docs"},
}


# a few popular Composio toolkits — autocomplete fallback when the
# live catalog search can't be reached (the real catalog is ~1000 apps
# and is queried live, cached briefly in pa.composio)
COMMON_COMPOSIO = ("gmail", "googlecalendar", "slack", "github",
                   "notion", "trello", "googledrive", "googlesheets")


def _aad(provider: str) -> str:
    return f"pa-connection:{provider}"


def split_account(provider: str):
    """'gmail@pal' -> ('gmail', 'pal'); 'gmail' -> ('gmail', 'owner').
    A labelled connection stores its account in the provider string
    itself, so tool dispatch (`gmail@pal.<tool>`) is just the existing
    longest-prefix match. `url:` providers never carry a label (the
    account syntax is refused for them — '@' can be URL userinfo)."""
    if "@" in provider and not provider.startswith("url:"):
        app, _, acct = provider.rpartition("@")
        return app, acct or "owner"
    return provider, "owner"


def _clean_account(account) -> str:
    """Normalize an account label -> 'pal' | ''. Only [a-z0-9_-] so it
    can never collide with provider/tool punctuation. 'owner' and ''
    mean the default account."""
    acct = (account or "").strip().lower()
    if acct in ("", "owner", "default"):
        return ""
    if not acct.replace("-", "").replace("_", "").isalnum():
        raise ConnectError(
            f"account label {account!r} must be letters/digits/-/_ "
            "(e.g. account:pal)")
    return acct


def provider_for(app: str, account: str = "") -> str:
    """The connections.provider key for an app+account pair."""
    return f"{app}@{account}" if account else app


def owner_kind_for(provider: str) -> str:
    """which side of the approval boundary a connection is on.
    'pal'-labelled accounts are pal's own; everything else is the
    owner's — fail closed."""
    return "pal" if split_account(provider)[1] == "pal" else "owner"


def set_owner_kind(conn, provider: str, kind: str):
    """The /connect 'whose account?' button writes this. kind is
    'owner' or 'pal' — anything else is refused (fail closed)."""
    if kind not in ("owner", "pal"):
        raise ConnectError(f"owner_kind must be owner|pal, got {kind!r}")
    conn.execute(
        "UPDATE connections SET owner_kind=?, updated_at=? "
        "WHERE provider=?", (kind, now_iso(), provider))


def domain_owner(s, env, conn, domain: str) -> str:
    """Browser domain -> 'owner'|'pal'. PA_BROWSER_OWNER_DOMAINS
    (csv domain=kind) plus kinds learned when the owner signs in via
    /desktop; unknown signed-in domains are the owner's (fail closed)."""
    domain = (domain or "").strip().lower()
    if not domain:
        return "owner"
    learned = {}
    try:
        import json as _j
        learned = _j.loads(
            get_setting_safe(conn, "browser_owner_map", "{}"))
    except Exception:
        learned = {}
    raw = (env.get("PA_BROWSER_OWNER_DOMAINS") or
           getattr(s, "browser_owner_domains", "") or "")
    out = dict(learned)
    for part in raw.split(","):
        d, _, k = part.partition("=")
        if d.strip() and k.strip() in ("owner", "pal"):
            out[d.strip().lower()] = k.strip()
    return out.get(domain, "owner")


def learn_domain_owner(conn, domain: str, kind: str):
    """A /desktop sign-in records domain -> owner_kind for the
    browser map."""
    if kind not in ("owner", "pal"):
        return
    import json as _j
    try:
        m = _j.loads(get_setting_safe(conn, "browser_owner_map", "{}"))
    except Exception:
        m = {}
    m[(domain or "").strip().lower()] = kind
    from . import db as dbmod
    dbmod.set_setting(conn, "browser_owner_map", _j.dumps(m))


def get_setting_safe(conn, key, default=""):
    try:
        from . import db as dbmod
        return dbmod.get_setting(conn, key, default)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# row helpers

def get(conn, provider: str):
    row = conn.execute(
        "SELECT * FROM connections WHERE provider=?",
        (provider,)).fetchone()
    return dict(row) if row else None


def list_all(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM connections ORDER BY provider").fetchall()]


def _bump_caps(conn):
    """A connection came or changed -> chat gets the 'tools changed'
    note on its next turn (kills stale 'I can't' answers)."""
    try:
        from . import db as dbmod
        dbmod.set_setting(conn, "caps_updated_at", now_iso())
    except Exception:
        pass


def upsert(conn, provider, kind, account_label, scopes, status,
           server_url, token_blob, expires_at, meta):
    now = now_iso()
    account = split_account(provider)[1]
    conn.execute(
        "INSERT INTO connections(provider, kind, account, account_label,"
        " scopes, status, server_url, token_blob, expires_at, meta_json,"
        " owner_kind, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(provider) DO UPDATE SET kind=excluded.kind,"
        " account=excluded.account,"
        " account_label=excluded.account_label, scopes=excluded.scopes,"
        " status=excluded.status, server_url=excluded.server_url,"
        " token_blob=excluded.token_blob,"
        " expires_at=excluded.expires_at, meta_json=excluded.meta_json,"
        " updated_at=excluded.updated_at",
        (provider, kind, account, account_label, scopes, status,
         server_url, token_blob, expires_at, json_dumps(meta),
         owner_kind_for(provider), now, now))
    _bump_caps(conn)


def disconnect(conn, env, provider: str):
    row = get(conn, provider)
    if row is None:
        return None
    # best-effort revocation when the server advertised an endpoint —
    # the row is deleted regardless
    meta = _meta(row)
    blob = {}
    try:
        blob = secretsbox.open_blob(env, _aad(provider),
                                    row["token_blob"])
    except Exception:
        pass
    revoke_url = meta.get("revocation_endpoint")
    tok = blob.get("access_token") or blob.get("refresh_token")
    if revoke_url and tok:
        try:
            oauth_revoke(revoke_url, tok, meta.get("client_id") or "",
                         meta.get("client_secret") or "")
        except Exception:
            pass
    conn.execute("DELETE FROM connections WHERE provider=?",
                 (provider,))
    _bump_caps(conn)
    return row


def oauth_revoke(endpoint, token, client_id, client_secret):
    from .netclient import APIClient
    parts = urllib.parse.urlsplit(endpoint)
    fields = {"token": token, "client_id": client_id}
    if client_secret:
        fields["client_secret"] = client_secret
    APIClient(f"{parts.scheme}://{parts.netloc}", timeout=10) \
        .post_form(parts.path, fields)


def _meta(row) -> dict:
    try:
        m = json_loads_bounded(row["meta_json"])
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


# -- state store --------------------------------------------------------------

def _state_aad(state: str) -> str:
    return f"pa-oauth-state:{state}"


def _new_state(conn, env, provider, kind, verifier, redirect_uri, meta):
    state = new_id(16)
    now = utcnow()
    exp = now + datetime.timedelta(seconds=STATE_TTL_SECONDS)
    conn.execute(
        "INSERT INTO oauth_states(state, provider, kind, pkce_verifier,"
        " redirect_uri, meta_json, created_at, expires_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (state, provider, kind, verifier, redirect_uri,
         secretsbox.seal(env, _state_aad(state), meta), now_iso(),
         exp.isoformat(timespec="milliseconds")))
    return state


def _state_meta(env, row) -> dict:
    """Decrypt a consumed oauth_states row's meta (it can carry a
    client_secret for non-DCR providers)."""
    blob = row["meta_json"] or ""
    if blob.startswith(secretsbox.MAGIC):
        try:
            return secretsbox.open_blob(env, _state_aad(row["state"]),
                                        blob)
        except Exception:
            return {}
    try:
        return json_loads_bounded(blob)
    except Exception:
        return {}


def _consume_state(conn, state: str):
    """Single-use: returns the row and deletes it, or None. Expired
    rows are deleted too."""
    row = conn.execute(
        "SELECT * FROM oauth_states WHERE state=?", (state,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    if parse_iso(row["expires_at"]) <= utcnow():
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# start_connect — produce the sign-in link

def _require_secrets_key(env):
    secretsbox.load_key(env)         # raises SecretsKeyMissing/BoxError


def _require_public_url(s):
    uri = s.oauth_redirect_uri()
    if not uri:
        raise ConnectError(
            "PA_PUBLIC_URL is not set — expose the web server "
            "(cloudflared, see docs/CONNECTIONS.md) and set it, then "
            "try again")
    return uri


def start_connect(conn, s, env, app: str, account: str = ""):
    """-> {provider, kind, label, url, note}. The URL is what the owner
    opens to sign in (or '' for an instantly-connected no-auth app).
    `account` labels a second connection to the same app — provider key
    'gmail@pal', tools addressed 'gmail@pal.<tool>'."""
    app = (app or "").strip()
    if not app:
        raise ConnectError("usage: /connect <app>")
    _require_secrets_key(env)
    acct = _clean_account(account)
    low = app.lower()
    if low == "google":
        return _start_google(conn, s, env, acct)
    if low.startswith("url:"):
        if acct:
            raise ConnectError(
                "account labels aren't supported for url: connections")
        target = app[4:].strip()
        host = urllib.parse.urlsplit(target).netloc or target
        return _start_mcp(conn, s, env, "url:" + host,
                          {"url": target, "label": host, "scopes": ""})
    provider = provider_for(low, acct)
    if low in MCP_CATALOG:
        entry = MCP_CATALOG[low]
        if _client_env_missing(env, entry) and s.composio_key(env):
            return _start_composio(
                conn, s, env, provider,
                toolkit=entry.get("composio_toolkit") or low)
        return _start_mcp(conn, s, env, provider, entry)
    if s.composio_key(env):
        return _start_composio(conn, s, env, provider, toolkit=low)
    raise ConnectError(
        f"unknown app {app!r}. Catalog: {catalog_names_text()}. "
        "Or connect any MCP server with /connect url:<https://...>. "
        "Set COMPOSIO_API_KEY to reach more apps (docs/CONNECTIONS.md).")


def catalog_names_text() -> str:
    return ", ".join(sorted(MCP_CATALOG) + ["url:<https://...>"])


def _client_env_missing(env, entry) -> bool:
    """True when the entry declares `client_env` vars and they aren't
    all set — i.e. the self-hosted OAuth path can't run."""
    ce = entry.get("client_env")
    return bool(ce) and not all(
        (env.get(v) or "").strip() for v in ce)


def _start_google(conn, s, env, account: str = ""):
    provider = provider_for("google", account)
    cid, csec = s.google_client(env)
    if not (cid and csec):
        if s.composio_key(env):
            # Google on Composio is split into per-product toolkits
            # (gmail, googlecalendar, ...); gmail is the one chat
            # needs most — the note points at the calendar one.
            res = _start_composio(conn, s, env,
                                  provider_for("gmail", account),
                                  toolkit="gmail")
            res["label"] = "Gmail (via Composio)"
            res["note"] += (" — Calendar is a separate toolkit: also "
                            "run /connect googlecalendar for Calendar")
            return res
        raise ConnectError(
            "google needs your own OAuth client: set PA_GOOGLE_CLIENT_ID"
            " and PA_GOOGLE_CLIENT_SECRET (click-by-click setup in "
            "docs/CONNECTIONS.md), then /connect google again")
    redirect_uri = _require_public_url(s)
    verifier, challenge = oauth.pkce_pair()
    state = _new_state(conn, env, provider, "google", verifier,
                       redirect_uri,
                       {"client_id": cid, "client_secret": csec,
                        "auth_endpoint": s.google_auth_url,
                        "token_endpoint": s.gmail_token_url,
                        "label": "Google (Gmail + Calendar)"})
    url = oauth.authorize_url(
        s.google_auth_url, cid, redirect_uri, state, challenge,
        scopes=" ".join(GMAIL_AUTH_SCOPES),
        extra={"access_type": "offline", "prompt": "consent"})
    return {"provider": provider, "kind": "google",
            "label": "Google (Gmail + Calendar)", "url": url,
            "note": "scopes: gmail read/draft/send + calendar"}


def _start_mcp(conn, s, env, provider, entry):
    url = entry["url"].strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConnectError("MCP endpoint must be an http(s) URL")
    if parts.scheme == "http" and not is_private_host(
            parts.hostname.lower()):
        raise ConnectError(
            "only https:// MCP endpoints can be connected over the "
            "public internet (http is fine for loopback/private hosts)")
    if entry.get("auth") == "none":
        # verify the server answers initialize, then connect instantly
        try:
            client = mcpmod.McpClient(url, timeout=s.mcp_timeout)
            tools = client.tools_list()
        except mcpmod.McpError as e:
            raise ConnectError(f"couldn't reach {url}: {e}")
        upsert(conn, provider, "mcp", entry.get("label") or provider,
               "", "active", url, "", None,
               {"server": client.server_info.get("name") or "",
                "tools_hint": len(tools)})
        return {"provider": provider, "kind": "mcp",
                "label": entry.get("label") or provider, "url": "",
                "note": f"connected (no sign-in needed; "
                        f"{len(tools)} tools)"}
    redirect_uri = _require_public_url(s)
    client_creds = None
    if entry.get("client_env"):
        cid = env.get(entry["client_env"][0] or "", "").strip()
        csec = env.get(entry["client_env"][1] or "", "").strip()
        if not (cid and csec):
            raise ConnectError(
                f"{provider} needs an OAuth app you register yourself "
                f"(GitHub has no dynamic registration): set "
                f"{entry['client_env'][0]}/"
                f"{entry['client_env'][1]} — see docs/CONNECTIONS.md — "
                "then /connect github again")
        client_creds = {"client_id": cid, "client_secret": csec}
    try:
        disc = oauth.discover(url)
    except oauth.OAuthError as e:
        raise ConnectError(f"OAuth discovery failed for {url}: {e}")
    as_meta = disc["metadata"]
    if client_creds is None:
        try:
            client_creds = oauth.ensure_client(
                conn, disc["issuer"], as_meta, redirect_uri,
                entry.get("scopes") or "")
        except oauth.ClientRegistrationError as e:
            raise ConnectError(
                f"couldn't register an OAuth client: {e}")
    verifier, challenge = oauth.pkce_pair()
    state = _new_state(conn, env, provider, "mcp", verifier,
                       redirect_uri, {
        "issuer": disc["issuer"],
        "token_endpoint": as_meta["token_endpoint"],
        "revocation_endpoint": as_meta.get("revocation_endpoint") or "",
        "client_id": client_creds["client_id"],
        "client_secret": client_creds.get("client_secret") or "",
        "resource": disc.get("resource") or url,
        "scopes": entry.get("scopes") or "",
        "server_url": url, "label": entry.get("label") or provider})
    url_out = oauth.authorize_url(
        as_meta["authorization_endpoint"], client_creds["client_id"],
        redirect_uri, state, challenge,
        scopes=entry.get("scopes") or "",
        resource=disc.get("resource") or url)
    return {"provider": provider, "kind": "mcp",
            "label": entry.get("label") or provider, "url": url_out,
            "note": "sign in at the link to finish"}


def _start_composio(conn, s, env, provider, toolkit=None):
    toolkit = toolkit or provider
    if not toolkit.replace("-", "").replace("_", "").isalnum():
        raise ConnectError(f"app name {toolkit!r} can't be a Composio "
                           "toolkit slug")
    redirect_uri = s.oauth_redirect_uri()      # optional for composio
    state = _new_state(conn, env, provider, "composio", "", redirect_uri,
                       {"toolkit": toolkit})
    callback = (redirect_uri + "?state=" + state + "&app="
                + urllib.parse.quote(provider)) if redirect_uri else ""
    cli = composiomod.ComposioClient(s, s.composio_key(env),
                                   timeout=s.mcp_timeout)
    try:
        link = cli.connect_link(toolkit, callback)
    except composiomod.ComposioError as e:
        raise ConnectError(f"composio connect failed: {e}")
    conn.execute(
        "UPDATE oauth_states SET meta_json=? WHERE state=?",
        (secretsbox.seal(env, _state_aad(state), {
            "toolkit": toolkit,
            "session_id": link["session_id"],
            "connected_account_id": link["connected_account_id"]}),
         state))
    return {"provider": provider, "kind": "composio", "label": provider,
            "url": link["redirect_url"], "state": state,
            "meta": {"toolkit": toolkit,
                     "session_id": link["session_id"],
                     "connected_account_id":
                         link["connected_account_id"]},
            "note": "Composio sign-in — I'll confirm when it completes"}


# ---------------------------------------------------------------------------
# completion (the /oauth/callback handler drives this)

def complete_callback(conn, s, env, query: dict):
    """-> (ok, title, message, provider). `query` is the parsed query
    string dict {k: [v]}."""
    state = (query.get("state") or [""])[0]
    err = (query.get("error") or [""])[0]
    if err:
        return False, "Sign-in failed", \
            f"The provider said: {err}. You can close this tab.", ""
    row = _consume_state(conn, state) if state else None
    if row is None:
        # The Discord poller or an earlier visit may have already
        # finished this connection — when the named app is active the
        # link did its job, so say "connected", not "expired". (The
        # composio callback carries `app`; other providers rely on the
        # state row alone.) provider stays "" so the web layer doesn't
        # re-notify Discord.
        app = (query.get("app") or [""])[0]
        done = get(conn, app) if app else None
        if done is not None and done["status"] == "active":
            return True, "Connected", \
                f"{app} is connected — you can close this tab.", ""
        return False, "Link expired", \
            "This sign-in link has expired or was already used. Run " \
            "/connect again in Discord.", ""
    if row["kind"] == "composio":
        return _finish_composio(conn, s, env, row, query)
    code = (query.get("code") or [""])[0]
    if not code:
        return False, "Sign-in failed", \
            "The provider returned no code. You can close this tab.", ""
    meta = _state_meta(env, row)
    if not meta.get("token_endpoint") or not meta.get("client_id"):
        return False, "Sign-in failed", \
            "This sign-in session is corrupt — run /connect again.", \
            row["provider"]
    try:
        data = oauth.exchange_code(
            meta["token_endpoint"], code, row["redirect_uri"],
            meta["client_id"], meta.get("client_secret") or "",
            verifier=row["pkce_verifier"],
            resource=meta.get("resource") or "")
    except oauth.ExchangeError as e:
        return False, "Sign-in failed", \
            f"Token exchange failed ({str(e)[:200]}). Try /connect " \
            "again.", row["provider"]
    blob = {"access_token": data["access_token"]}
    if data.get("refresh_token"):
        blob["refresh_token"] = data["refresh_token"]
    if data.get("id_token"):
        blob["id_token"] = data["id_token"]
    if meta.get("client_secret"):
        blob["client_secret"] = meta["client_secret"]
    expires_at = None
    if data.get("expires_in"):
        expires_at = (utcnow() + datetime.timedelta(
            seconds=int(data["expires_in"]) - 60)).isoformat(
            timespec="milliseconds")
    kind = row["kind"] if row["kind"] in ("mcp", "google") else "mcp"
    label = meta.get("label") or row["provider"]
    if kind == "google":
        label = "Google (Gmail + Calendar)"
    elif row["provider"] in MCP_CATALOG:
        label = MCP_CATALOG[row["provider"]]["label"]
    meta_keep = {k: v for k, v in meta.items()
                 if k != "client_secret"}
    meta_keep["token_endpoint"] = meta["token_endpoint"]
    meta_keep["client_id"] = meta["client_id"]
    meta_keep["issuer"] = meta.get("issuer") or ""
    meta_keep["revocation_endpoint"] = \
        meta.get("revocation_endpoint") or ""
    meta_keep["resource"] = meta.get("resource") or ""
    upsert(conn, row["provider"], kind, label,
           meta.get("scopes") or "", "active",
           _server_url_for(row, meta),
           secretsbox.seal(env, _aad(row["provider"]), blob),
           expires_at, meta_keep)
    return True, "Connected", \
        f"{label} is connected — you can close this tab.", \
        row["provider"]


def _server_url_for(row, meta):
    if row["kind"] == "mcp":
        if meta.get("server_url"):
            return meta["server_url"]
        if row["provider"] in MCP_CATALOG:
            return MCP_CATALOG[row["provider"]]["url"]
    return ""


def _finish_composio(conn, s, env, strow, query):
    meta = _state_meta(env, strow)
    acc_id = meta.get("connected_account_id")
    if not acc_id:
        return False, "Almost there", \
            "Composio sign-in started. I'll confirm in Discord once it " \
            "completes.", strow["provider"]
    try:
        acc = composiomod.ComposioClient(
            s, s.composio_key(env), timeout=s.mcp_timeout
        ).account(acc_id)
    except composiomod.ComposioError as e:
        return False, "Composio error", str(e)[:200], strow["provider"]
    if acc["status"] == "ACTIVE":
        activate_composio(conn, strow["provider"], meta)
        return True, "Connected", \
            f"{strow['provider']} is connected — you can close this " \
            "tab.", strow["provider"]
    return False, "Still finishing", \
        f"Composio reports the connection as {acc['status'] or 'pending'}"\
        " — pal will confirm in Discord when it completes.", \
        strow["provider"]


def activate_composio(conn, provider, meta):
    upsert(conn, provider, "composio", provider,
           "", "active", "", "", None,
           {"connected_account_id": meta.get("connected_account_id"),
            "toolkit": meta.get("toolkit") or provider,
            "session_id": meta.get("session_id") or ""})


# ---------------------------------------------------------------------------
# token access for tool calls

def access_token(conn, s, env, row) -> str:
    """A valid access token for `row`, refreshing + persisting as
    needed. Raises ConnectError('reconnect') when reauth is required."""
    if row["kind"] == "composio":
        raise ConnectError("composio connections have no local token")
    if not row["token_blob"]:
        raise ConnectError(f"{row['provider']}: reconnect with /connect")
    try:
        blob = secretsbox.open_blob(env, _aad(row["provider"]),
                                    row["token_blob"])
    except Exception:
        raise ConnectError(f"{row['provider']}: token unreadable — "
                           "reconnect with /connect")
    exp = row["expires_at"]
    fresh = blob.get("access_token") and (
        not exp or parse_iso(exp) > utcnow() + datetime.timedelta(
            seconds=60))
    if fresh:
        return blob["access_token"]
    rt = blob.get("refresh_token")
    if not rt:
        raise ConnectError(
            f"{row['provider']}: reconnect with /connect (no refresh "
            "token)")
    meta = _meta(row)
    try:
        data = oauth.refresh_token(
            meta["token_endpoint"], rt, meta.get("client_id") or "",
            blob.get("client_secret") or meta.get("client_secret") or "",
            resource=meta.get("resource") or "",
            scopes=row["scopes"])
    except oauth.ExchangeError as e:
        conn.execute(
            "UPDATE connections SET status='expired', updated_at=? "
            "WHERE provider=?", (now_iso(), row["provider"]))
        _bump_caps(conn)
        raise ConnectError(
            f"{row['provider']}: token refresh failed — reconnect with "
            f"/connect ({str(e)[:120]})")
    blob["access_token"] = data["access_token"]
    if data.get("refresh_token"):
        blob["refresh_token"] = data["refresh_token"]
    expires_at = (utcnow() + datetime.timedelta(
        seconds=int(data.get("expires_in") or 3600) - 60)).isoformat(
        timespec="milliseconds")
    conn.execute(
        "UPDATE connections SET token_blob=?, expires_at=?,"
        " status='active', updated_at=? WHERE provider=?",
        (secretsbox.seal(env, _aad(row["provider"]), blob),
         expires_at, now_iso(), row["provider"]))
    return data["access_token"]


def mcp_client(conn, s, env, row) -> mcpmod.McpClient:
    tok = access_token(conn, s, env, row) if row["token_blob"] else ""
    return mcpmod.McpClient(row["server_url"], token=tok,
                            timeout=s.mcp_timeout)


# -- tools surface for the chat agent ----------------------------------------

_TOOLS_CACHE = {}
_TOOLS_CACHE_TTL = 600


def list_tools(conn, s, env, row):
    """-> [{name, description, schema, read_only}] for one connection,
    cached per row-updated_at for 10 min."""
    key = (row["provider"], row["updated_at"])
    ent = _TOOLS_CACHE.get(key)
    if ent and ent[0] > utcnow():
        return ent[1]
    if row["kind"] == "mcp":
        client = mcp_client(conn, s, env, row)
        raw = client.tools_list()
        tools = [{"name": t.get("name") or "",
                  "description": (t.get("description") or "")[:300],
                  "schema": t.get("inputSchema") or {},
                  "read_only": bool(
                      (t.get("annotations") or {}).get("readOnlyHint"))}
                 for t in raw if t.get("name")]
    elif row["kind"] == "composio":
        meta = _meta(row)
        tools = [{"name": t["slug"], "description": t["description"],
                  "schema": t["schema"],
                  "read_only": bool(t.get("read_only"))}
                 for t in composiomod.ComposioClient(
                     s, s.composio_key(env), timeout=s.mcp_timeout
                 ).tools(meta.get("toolkit") or row["provider"])]
    else:  # google — its tools are the built-in email_*/calendar_*
        tools = []
    _TOOLS_CACHE[key] = (
        utcnow() + datetime.timedelta(seconds=_TOOLS_CACHE_TTL), tools)
    return tools


def call_tool(conn, s, env, row, tool_name: str, arguments: dict):
    """Run one connected-app tool. -> dict suitable as a chattools
    observation."""
    if row["kind"] == "mcp":
        client = mcp_client(conn, s, env, row)
        res = client.call_tool(tool_name, arguments)
        texts = []
        for c in res.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                texts.append(str(c.get("text") or ""))
        out = {"tool": f"{row['provider']}.{tool_name}"}
        if texts:
            out["text"] = "\n".join(texts)[:8000]
        if res.get("structuredContent") is not None:
            out["structured"] = res["structuredContent"]
        if res.get("isError"):
            out["error"] = "tool_error"
        return out
    if row["kind"] == "composio":
        meta = _meta(row)
        count = composiomod.bump_usage(conn)
        res = composiomod.ComposioClient(
            s, s.composio_key(env), timeout=s.mcp_timeout).execute(
            tool_name, arguments,
            meta.get("connected_account_id") or "")
        out = {"tool": f"{row['provider']}.{tool_name}",
               "composio_calls_this_month": count}
        data = res.get("data") if isinstance(res, dict) else None
        out["result"] = data if data is not None else res
        if composiomod.over_warn_threshold(s, count):
            out["usage_warning"] = (
                f"Composio usage is at {count}/"
                f"{s.composio_monthly_limit} managed calls this month "
                "(80% of the free cap)")
        return out
    raise ConnectError(
        f"{row['provider']}: not a tool-bearing connection")


def find_connection_for_tool(conn, qualified: str):
    """'notion.search' -> (row, 'search') or (None, None). Provider
    names may themselves contain dots ('url:mcp.foo.com'), so match the
    longest provider prefix."""
    rows = conn.execute(
        "SELECT provider FROM connections WHERE kind IN ('mcp',"
        " 'composio') ORDER BY length(provider) DESC").fetchall()
    for r in rows:
        prefix = r["provider"] + "."
        if qualified.startswith(prefix):
            return get(conn, r["provider"]), qualified[len(prefix):]
    return None, None

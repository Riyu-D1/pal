"""OAuth 2.1 client machinery for the connections library.

Implements the MCP authorization spec against any compliant server:

- RFC 9728 protected-resource discovery (path-suffixed well-known, bare
  well-known, and the WWW-Authenticate resource_metadata hint a 401 on
  the MCP endpoint may carry);
- RFC 8414 authorization-server discovery (same well-known placement
  rules; OpenID configuration as a fallback);
- RFC 7591 dynamic client registration, cached in `oauth_clients` per
  (issuer, redirect_uri) so /connect doesn't re-register;
- authorization-code + PKCE(S256) + single-use state (10 min) + the
  RFC 8707 `resource` parameter;
- token exchange and refresh against the discovered token endpoint.

Everything is transport-agnostic: callers pass a base URL and this
module returns plain dicts. Tokens leave here only inside the encrypted
blob (pa.secretsbox) — nothing in this module ever logs or prints them.
"""

import base64
import hashlib
import secrets
import urllib.parse

from .netclient import APIClient, HTTPError, TransportError

HTTP_TIMEOUT = 20


class OAuthError(Exception):
    pass


class ClientRegistrationError(OAuthError):
    pass


class ExchangeError(OAuthError):
    pass


def _join(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _origin(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _well_knowns(name: str, resource_url: str):
    """Candidate metadata URLs, spec order: /.well-known/<name><path>
    first (RFC 9728/8414 insert the well-known segment before the path),
    then the bare /.well-known/<name> on the origin."""
    p = urllib.parse.urlsplit(resource_url)
    out = [f"{_origin(resource_url)}/.well-known/{name}{p.path}"]
    bare = f"{_origin(resource_url)}/.well-known/{name}"
    if out[0] != bare:
        out.append(bare)
    return out


def _get_json(url: str, timeout=HTTP_TIMEOUT):
    parts = urllib.parse.urlsplit(url)
    client = APIClient(_origin(url), timeout=timeout)
    return client.get_json(parts.path + ("?" + parts.query
                                         if parts.query else ""))


def parse_resource_metadata_header(header: str):
    """WWW-Authenticate: Bearer ... resource_metadata="URL" -> URL."""
    if not header:
        return ""
    for part in str(header).split(","):
        part = part.strip()
        if part.startswith("resource_metadata="):
            return part.split("=", 1)[1].strip().strip('"')
    return ""


def protected_resource_metadata(server_url: str, auth_hint: str = ""):
    """{resource, authorization_servers, ...} or {}. auth_hint is the
    resource_metadata URL taken from a 401 WWW-Authenticate header."""
    urls = []
    if auth_hint:
        urls.append(auth_hint)
    urls += _well_knowns("oauth-protected-resource", server_url)
    for url in urls:
        try:
            _h, data = _get_json(url)
            if isinstance(data, dict) and data.get(
                    "authorization_servers"):
                return data
        except (HTTPError, TransportError, ValueError):
            continue
    return {}


def authorization_server_metadata(issuer_or_url: str):
    """AS metadata for an issuer (RFC 8414) — tries the well-known
    placements, then /.well-known/openid-configuration."""
    for name in ("oauth-authorization-server", "openid-configuration"):
        for url in _well_knowns(name, issuer_or_url):
            try:
                _h, data = _get_json(url)
                if isinstance(data, dict) and data.get(
                        "authorization_endpoint"):
                    return data
            except (HTTPError, TransportError, ValueError):
                continue
    raise OAuthError(
        f"no OAuth server metadata found for {issuer_or_url!r}")


def discover(server_url: str, auth_hint: str = ""):
    """-> {issuer, metadata, resource}. Raises OAuthError when no AS
    metadata can be found anywhere."""
    prm = protected_resource_metadata(server_url, auth_hint)
    issuers = prm.get("authorization_servers") or \
        [_origin(server_url)]
    last_err = None
    for issuer in issuers:
        try:
            meta = authorization_server_metadata(issuer)
            return {"issuer": issuer, "metadata": meta,
                    "resource": prm.get("resource") or server_url,
                    "scopes_supported": prm.get("scopes_supported") or []}
        except OAuthError as e:
            last_err = e
    raise last_err or OAuthError("discovery failed")


# -- dynamic client registration (RFC 7591) ---------------------------------

def register_client(registration_endpoint: str, redirect_uri: str,
                    scopes: str = ""):
    body = {
        "client_name": "pa — personal assistant",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scopes:
        body["scope"] = scopes
    parts = urllib.parse.urlsplit(registration_endpoint)
    client = APIClient(_origin(registration_endpoint),
                       timeout=HTTP_TIMEOUT)
    try:
        _h, data = client.post_json(
            parts.path + ("?" + parts.query if parts.query else ""), body)
    except HTTPError as e:
        raise ClientRegistrationError(
            f"client registration HTTP {e.status}")
    except TransportError as e:
        raise ClientRegistrationError(f"client registration failed: {e}")
    cid = (data or {}).get("client_id")
    if not cid:
        raise ClientRegistrationError(
            "registration returned no client_id")
    return {"client_id": cid,
            "client_secret": data.get("client_secret") or ""}


def ensure_client(conn, issuer: str, as_meta: dict, redirect_uri: str,
                  scopes: str = ""):
    """Cached DCR client for (issuer, redirect_uri); static env-supplied
    clients are handled by the caller (kind-specific overrides)."""
    row = conn.execute(
        "SELECT client_id, client_secret FROM oauth_clients WHERE"
        " issuer=? AND redirect_uri=?",
        (issuer, redirect_uri)).fetchone()
    if row:
        return {"client_id": row["client_id"],
                "client_secret": row["client_secret"]}
    reg = as_meta.get("registration_endpoint")
    if not reg:
        raise ClientRegistrationError(
            "authorization server has no dynamic registration endpoint; "
            "register an OAuth app manually and set its credentials")
    creds = register_client(reg, redirect_uri, scopes)
    conn.execute(
        "INSERT OR REPLACE INTO oauth_clients(issuer, redirect_uri,"
        " client_id, client_secret, created_at)"
        " VALUES(?,?,?,?,datetime('now'))",
        (issuer, redirect_uri, creds["client_id"],
         creds["client_secret"]))
    return creds


# -- PKCE / authorize ---------------------------------------------------------

def pkce_pair():
    verifier = secrets.token_urlsafe(48)[:64]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(authorization_endpoint: str, client_id: str,
                  redirect_uri: str, state: str, challenge: str,
                  scopes: str = "", resource: str = "",
                  extra: dict = None):
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        q["scope"] = scopes
    if resource:
        q["resource"] = resource
    for k, v in (extra or {}).items():
        q[k] = v
    return authorization_endpoint + \
        ("&" if "?" in authorization_endpoint else "?") + \
        urllib.parse.urlencode(q)


def exchange_code(token_endpoint: str, code: str, redirect_uri: str,
                  client_id: str, client_secret: str = "",
                  verifier: str = "", resource: str = ""):
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }
    if client_secret:
        fields["client_secret"] = client_secret
    if verifier:
        fields["code_verifier"] = verifier
    if resource:
        fields["resource"] = resource
    parts = urllib.parse.urlsplit(token_endpoint)
    client = APIClient(_origin(token_endpoint), timeout=HTTP_TIMEOUT)
    try:
        _h, data = client.post_form(
            parts.path + ("?" + parts.query if parts.query else ""),
            fields)
    except HTTPError as e:
        raise ExchangeError(
            f"token exchange HTTP {e.status}: {e.body[:200].decode('utf-8', 'replace')}")
    except TransportError as e:
        raise ExchangeError(f"token endpoint unreachable: {e}")
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ExchangeError("token endpoint returned no access_token")
    return data


def refresh_token(token_endpoint: str, refresh_tok: str, client_id: str,
                  client_secret: str = "", resource: str = "",
                  scopes: str = ""):
    fields = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": client_id,
    }
    if client_secret:
        fields["client_secret"] = client_secret
    if resource:
        fields["resource"] = resource
    if scopes:
        fields["scope"] = scopes
    parts = urllib.parse.urlsplit(token_endpoint)
    client = APIClient(_origin(token_endpoint), timeout=HTTP_TIMEOUT)
    try:
        _h, data = client.post_form(
            parts.path + ("?" + parts.query if parts.query else ""),
            fields)
    except HTTPError as e:
        raise ExchangeError(f"refresh HTTP {e.status}")
    except TransportError as e:
        raise ExchangeError(f"token endpoint unreachable: {e}")
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ExchangeError("refresh returned no access_token")
    return data

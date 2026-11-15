"""Guarded egress for fetch_url.

- http/https on ports 80/443 only.
- DNS resolved once; every returned address must be public. Any
  loopback/private/link-local/CGNAT/multicast/reserved/IPv4-mapped/NAT64/
  ULA/metadata address fails the whole request (not just that address).
- We connect to the vetted IP while keeping the hostname for TLS SNI,
  certificate verification and Host - so a second DNS answer can never
  smuggle in a different target (DNS-rebinding safe).
- <=3 redirects, each re-vetted end to end.
- 5MB cap (raw AND decompressed), 10s total, text/html or text/plain only.
- No cookies or credentials, ever.

`resolver` and `dialer` are injectable purely so tests can simulate DNS
answers and observe the exact IP we dial - production uses the real ones.
"""

import gzip
import ipaddress
import re
import socket
import ssl
import time
import urllib.parse
import zlib

BLOCKED_V4 = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.31.196.0/24", "192.52.193.0/24", "192.88.99.0/24",
    "192.168.0.0/16", "192.175.48.0/24", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "255.255.255.255/32",
]
BLOCKED_V6 = [
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96",
    "64:ff9b:1::/48", "100::/64", "2001::/32", "2001:2::/48",
    "2001:3::/32", "2001:4:112::/48", "2001:10::/28", "2001:20::/28",
    "2001:30::/28", "2001:db8::/32", "2002::/16", "fc00::/7",
    "fe80::/10", "ff00::/8",
]
BLOCKED_NETS = [ipaddress.ip_network(n) for n in BLOCKED_V4 + BLOCKED_V6]

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = (80, 443)
ALLOWED_CONTENT = ("text/html", "text/plain")
MAX_HEADER_BYTES = 16 * 1024
USER_AGENT = "pa-fetch/1.0 (+self-owned assistant)"


class FetchBlocked(Exception):
    """Destination or response violates the egress policy."""
    kind = "fetch_blocked"


class FetchFailed(Exception):
    """Network-level failure (dns, connect, http error)."""
    kind = "fetch_failed"


def is_allowed_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return False
    for net in BLOCKED_NETS:
        if ip.version == net.version and ip in net:
            return False
    return not (ip.is_loopback or ip.is_multicast or ip.is_unspecified)


def _vet_url(url: str):
    """Parse and structurally validate a URL. Returns (scheme, host, port, path)."""
    if len(url) > 2048:
        raise FetchBlocked("url too long")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise FetchBlocked("unparseable url")
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise FetchBlocked(f"scheme {parts.scheme!r} not allowed")
    if parts.username or parts.password:
        raise FetchBlocked("credentials in URL are not allowed")
    host = parts.hostname
    if not host:
        raise FetchBlocked("missing host")
    host = host.rstrip(".").lower()
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise FetchBlocked("bad port")
    if port not in ALLOWED_PORTS:
        raise FetchBlocked(f"port {port} not allowed")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, path


def _resolve(host: str, port: int, resolver):
    """Resolve once; every answer must be allowed. Returns ordered IPs."""
    try:
        infos = resolver(host, port)
    except OSError as e:
        raise FetchFailed(f"dns resolution failed for {host!r}")
    ips = []
    for fam, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise FetchFailed(f"dns returned no addresses for {host!r}")
    bad = [ip for ip in ips if not is_allowed_ip(ip)]
    if bad:
        raise FetchBlocked(
            f"{host!r} resolves to non-public address(es): {', '.join(bad)}")
    return ips


def _default_resolver(host, port):
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _default_dialer(ip, port, timeout):
    return socket.create_connection((ip, port), timeout=timeout)


def _read_headers(sock_file, deadline):
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HEADER_BYTES:
            raise FetchFailed("response headers too large")
        if time.monotonic() > deadline:
            raise FetchFailed("fetch timed out (headers)")
        try:
            chunk = sock_file.read(1)
        except socket.timeout:
            raise FetchFailed("fetch timed out (headers)")
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines or not lines[0].startswith(b"HTTP/"):
        raise FetchFailed("malformed http response")
    try:
        status = int(lines[0].split(b" ", 2)[1])
    except (IndexError, ValueError):
        raise FetchFailed("malformed http status line")
    headers = {}
    for ln in lines[1:]:
        if b":" not in ln:
            continue
        k, v = ln.split(b":", 1)
        headers[k.strip().lower().decode("latin-1")] = \
            v.strip().decode("latin-1")
    return status, headers, rest


def _recv_chunk(sock_file, deadline):
    if time.monotonic() > deadline:
        raise FetchFailed("fetch timed out")
    try:
        return sock_file.read(65536)
    except socket.timeout:
        raise FetchFailed("fetch timed out")


def _fetch_once(scheme, host, port, path, ip, ctx, dialer, deadline):
    """Single HTTP round-trip against a vetted IP. Returns
    (status, headers, body_stream_file, raw_socket_cleanup)."""
    sock = dialer(ip, port, max(1.0, deadline - time.monotonic()))
    sock.settimeout(max(1.0, deadline - time.monotonic()))
    if scheme == "https":
        sock = ctx.wrap_socket(sock, server_hostname=host)
    host_hdr = host if port in (80, 443) else f"{host}:{port}"
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_hdr}\r\n"
        f"User-Agent: {USER_AGENT}\r\n"
        f"Accept: text/html,text/plain;q=0.9,*/*;q=0.1\r\n"
        f"Accept-Encoding: gzip, deflate\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("latin-1")
    sock.sendall(req)
    f = sock.makefile("rb")
    status, headers, rest = _read_headers(f, deadline)
    return status, headers, f, rest, sock


_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)


def _dechunked(first: bytes, f, max_bytes: int, deadline) -> bytes:
    """Decode transfer-encoding: chunked. `first` is the body bytes that
    already arrived inside the header read; the rest comes from `f`."""
    buf = bytearray(first)

    def need(n):
        while len(buf) < n:
            chunk = _recv_chunk(f, deadline)
            if not chunk:
                raise FetchFailed("truncated chunked body")
            buf.extend(chunk)

    def readline():
        while True:
            i = buf.find(b"\r\n")
            if i >= 0:
                line = bytes(buf[:i])
                del buf[:i + 2]
                return line
            need(1)

    out = bytearray()
    while True:
        line = readline().split(b";", 1)[0].strip()
        try:
            size = int(line, 16)
        except ValueError:
            raise FetchFailed("malformed chunk size")
        if size == 0:
            return bytes(out)
        need(size + 2)
        out += buf[:size]
        del buf[:size + 2]
        if len(out) > max_bytes:
            raise FetchFailed(f"body exceeds {max_bytes} byte limit")


def _read_body(f, first: bytes, headers: dict, max_bytes: int,
               deadline, enc=None):
    enc = (enc or headers.get("content-encoding") or "").lower()
    cl = headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > max_bytes:
                raise FetchFailed(f"body exceeds {max_bytes} byte limit")
        except ValueError:
            pass
    if "chunked" in (headers.get("transfer-encoding") or "").lower():
        raw = _dechunked(first, f, max_bytes, deadline)
    else:
        raw = bytearray(first)
        while True:
            if len(raw) > max_bytes:
                raise FetchFailed(f"body exceeds {max_bytes} byte limit")
            chunk = _recv_chunk(f, deadline)
            if not chunk:
                break
            raw.extend(chunk)
        raw = bytes(raw[:max_bytes + 1])
    if len(raw) > max_bytes:
        raise FetchFailed(f"body exceeds {max_bytes} byte limit")
    # bounded decompression
    if enc in ("gzip", "x-gzip", "deflate"):
        try:
            d = zlib.decompressobj(
                16 + zlib.MAX_WBITS if "gzip" in enc else zlib.MAX_WBITS)
            out = d.decompress(raw, max_bytes + 1)
            if len(out) > max_bytes or d.unconsumed_tail:
                raise FetchFailed(
                    f"decompressed body exceeds {max_bytes} byte limit")
            out += d.flush()
        except zlib.error:
            raise FetchFailed("corrupt compressed body")
        if len(out) > max_bytes:
            raise FetchFailed(f"decompressed body exceeds {max_bytes} byte limit")
        return bytes(out)
    return raw


def _extract_title(body: bytes) -> str:
    m = _TITLE_RE.search(body[:200_000])
    if not m:
        return ""
    t = re.sub(rb"<[^>]+>", b"", m.group(1))
    return t.decode("utf-8", "replace").strip()[:200]


def guarded_fetch(url: str, *, max_bytes: int = 5 * 1024 * 1024,
                  timeout: float = 10.0, max_redirects: int = 3,
                  allowed: tuple = ALLOWED_CONTENT,
                  resolver=None, dialer=None,
                  tls_context: ssl.SSLContext = None,
                  seen=None) -> dict:
    """Fetch a URL under the egress policy. Returns a dict with url,
    final_url, status, content_type, title, text, body, fetched_at.
    `allowed` widens the content-type allowlist (vision's image/*)."""
    resolver = resolver or _default_resolver
    dialer = dialer or _default_dialer
    ctx = tls_context or ssl.create_default_context()
    deadline = time.monotonic() + timeout

    current = url
    redirects = 0
    while True:
        scheme, host, port, path = _vet_url(current)
        ips = _resolve(host, port, resolver)
        last_err = None
        status = headers = f = rest = sock = None
        for ip in ips:
            try:
                status, headers, f, rest, sock = _fetch_once(
                    scheme, host, port, path, ip, ctx, dialer, deadline)
                last_err = None
                break
            except (OSError, FetchFailed) as e:
                last_err = e
        if last_err:
            raise FetchFailed(str(last_err))
        try:
            if status in (301, 302, 303, 307, 308):
                loc = headers.get("location")
                if not loc:
                    raise FetchFailed(f"redirect {status} without location")
                redirects += 1
                if redirects > max_redirects:
                    raise FetchBlocked("too many redirects")
                current = urllib.parse.urljoin(current, loc)
                continue
            if status != 200:
                raise FetchFailed(f"http {status}")
            ctype = (headers.get("content-type") or "").split(";")[0] \
                .strip().lower()
            if ctype not in allowed:
                raise FetchBlocked(
                    f"content type {ctype or 'unknown'} not allowed")
            body = _read_body(f, rest, headers, max_bytes, deadline)
            return {
                "url": url,
                "final_url": current,
                "status": status,
                "content_type": ctype,
                "title": _extract_title(body),
                "text": body.decode("utf-8", "replace"),
                "body": body,
                "bytes": len(body),
            }
        finally:
            try:
                f.close()
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass


def guarded_get(url: str, settings=None, **kw) -> dict:
    if settings is not None:
        kw.setdefault("max_bytes", settings.fetch_max_bytes)
        kw.setdefault("timeout", settings.fetch_timeout_seconds)
        kw.setdefault("max_redirects", settings.fetch_max_redirects)
    return guarded_fetch(url, **kw)

"""Minimal WebSocket (RFC 6455) client for the Discord gateway.

Deliberately tiny: text frames, ping/pong, close, fragmentation
reassembly. Client sends are masked per spec; server sends arrive
unmasked. `ws://` (plain) exists for deterministic tests against a fake
gateway; production uses `wss://` with default TLS verification.

The frame helpers are shared so tests can run a fake gateway server with
the same codec (server side: mask=False on send, expect masked recv).
"""

import base64
import os
import socket
import ssl
import struct
import urllib.parse

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_FRAME = 4 * 1024 * 1024
MAX_MESSAGE = 8 * 1024 * 1024
_GUIDE = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(Exception):
    pass


class WSClosed(WSError):
    pass


def encode_frame(opcode: int, payload: bytes, mask: bool) -> bytes:
    b0 = 0x80 | (opcode & 0x0F)
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", b0, n | (0x80 if mask else 0))
    elif n <= 0xFFFF:
        header = struct.pack("!BBH", b0, 126 | (0x80 if mask else 0), n)
    else:
        header = struct.pack("!BBQ", b0, 127 | (0x80 if mask else 0), n)
    if not mask:
        return header + payload
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return header + key + masked


def _read_exact(f, n: int) -> bytes:
    data = f.read(n)
    if data is None or len(data) < n:
        raise WSClosed("connection closed mid-frame")
    return data


def read_frame(f, expect_masked: bool):
    """One frame -> (fin, opcode, payload). Raises WSClosed on close or
    socket EOF."""
    try:
        head = _read_exact(f, 2)
    except (OSError, WSClosed) as e:
        raise WSClosed(str(e))
    b0, b1 = head[0], head[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    if expect_masked and not masked:
        raise WSError("unmasked client frame")
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", _read_exact(f, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _read_exact(f, 8))[0]
    if n > MAX_FRAME:
        raise WSError("frame too large")
    key = _read_exact(f, 4) if masked else b""
    payload = _read_exact(f, n) if n else b""
    if masked:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


class WSClient:
    """A connected client websocket. recv() returns complete text
    messages (fragmentation reassembled, ping auto-ponged)."""

    def __init__(self, sock):
        self.sock = sock
        self.f = sock.makefile("rb")
        self._frag = None

    @classmethod
    def connect(cls, url: str, timeout: float = 15.0,
                tls_context=None, sock_factory=None):
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("ws", "wss"):
            raise WSError(f"ws scheme {scheme!r} not supported")
        host = parts.hostname
        port = parts.port or (443 if scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        if sock_factory:
            sock = sock_factory(host, port, timeout)
        else:
            sock = socket.create_connection((host, port), timeout=timeout)
            if scheme == "wss":
                ctx = tls_context or ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        host_hdr = host if port in (80, 443) else f"{host}:{port}"
        req = (f"GET {path} HTTP/1.1\r\nHost: {host_hdr}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(req.encode("latin-1"))
        f = sock.makefile("rb")
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = f.read(1)
            if not chunk:
                sock.close()
                raise WSError("gateway closed during handshake")
            head += chunk
            if len(head) > 16384:
                sock.close()
                raise WSError("handshake too large")
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101" not in status_line:
            sock.close()
            raise WSError(f"gateway handshake failed: "
                          f"{status_line.decode('latin-1', 'replace')}")
        import hashlib
        accept = base64.b64encode(hashlib.sha1(key.encode() + _GUIDE)
                                  .digest()).decode()
        if accept.encode() not in head.split(b"\r\n\r\n")[0]:
            sock.close()
            raise WSError("bad Sec-WebSocket-Accept")
        ws = cls(sock)
        ws.f = f
        return ws

    def send_text(self, text: str):
        self.sock.sendall(encode_frame(OP_TEXT, text.encode("utf-8"),
                                       mask=True))

    def send_pong(self, payload: bytes = b""):
        self.sock.sendall(encode_frame(OP_PONG, payload, mask=True))

    def close(self):
        try:
            self.sock.sendall(encode_frame(OP_CLOSE, b"", mask=True))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def recv(self) -> str:
        """Next complete text message. Handles ping->pong, close, and
        continuation frames."""
        while True:
            fin, op, payload = read_frame(self.f, expect_masked=False)
            if op == OP_PING:
                self.send_pong(payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                raise WSClosed("gateway sent close")
            if op == OP_CONT:
                if self._frag is None:
                    continue
                self._frag += payload
                if fin:
                    data, self._frag = self._frag, None
                    if len(data) > MAX_MESSAGE:
                        raise WSError("message too large")
                    return data.decode("utf-8", "replace")
                if len(self._frag) > MAX_MESSAGE:
                    raise WSError("message too large")
                continue
            if op in (OP_TEXT, OP_BINARY):
                if fin:
                    return payload.decode("utf-8", "replace")
                self._frag = payload
                continue
            # unknown opcodes ignored per spec

    def settimeout(self, t: float):
        self.sock.settimeout(t)

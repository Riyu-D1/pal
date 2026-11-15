"""At-rest encryption for connection token blobs.

AES-256-GCM, keyed by PA_SECRETS_KEY (64 hex chars / 32 bytes, or a
base64 encoding of 32 bytes). Generate one with:

    python3 -c "import secrets; print(secrets.token_hex(32))"

The plaintext is always a JSON object; the associated data binds the
ciphertext to its purpose+row so a blob copied between connections (or
tables) will not decrypt. PA_SECRETS_KEY is never logged, printed or
stored — losing it means re-connecting every app.
"""

import base64
import json
import os
import secrets

from .util import json_dumps, json_loads_bounded

MAGIC = "pasec1"


class SecretsKeyMissing(Exception):
    pass


class SecretsBoxError(Exception):
    pass


def generate_key() -> str:
    return secrets.token_hex(32)


def load_key(env) -> bytes:
    """PA_SECRETS_KEY -> 32 raw bytes. Accepts 64-hex or base64(32B)."""
    raw = (env.get("PA_SECRETS_KEY") or "").strip()
    if not raw:
        raise SecretsKeyMissing(
            "PA_SECRETS_KEY is not set — generate one with `python3 -c "
            "\"import secrets; print(secrets.token_hex(32))\"` and set "
            "it before connecting apps")
    try:
        key = bytes.fromhex(raw)
        if len(key) == 32:
            return key
    except ValueError:
        pass
    try:
        key = base64.b64decode(raw, validate=True)
        if len(key) == 32:
            return key
    except ValueError:
        pass
    raise SecretsBoxError(
        "PA_SECRETS_KEY must be 32 bytes as 64 hex chars (preferred) or "
        "base64")


def seal(env, aad: str, payload: dict) -> str:
    """JSON -> 'pasec1.<nonce_b64>.<ct_b64>' bound to `aad`."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = load_key(env)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(
        nonce, json_dumps(payload).encode("utf-8"), aad.encode("utf-8"))
    return f"{MAGIC}.{base64.b64encode(nonce).decode()}." \
           f"{base64.b64encode(ct).decode()}"


def open_blob(env, aad: str, blob: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = load_key(env)
    parts = (blob or "").split(".")
    if len(parts) != 3 or parts[0] != MAGIC:
        raise SecretsBoxError("not a pa secrets blob")
    try:
        nonce = base64.b64decode(parts[1])
        ct = base64.b64decode(parts[2])
        plain = AESGCM(key).decrypt(nonce, ct, aad.encode("utf-8"))
        obj = json_loads_bounded(plain.decode("utf-8"))
    except SecretsBoxError:
        raise
    except Exception:
        raise SecretsBoxError(
            "decryption failed (wrong PA_SECRETS_KEY or corrupt blob)")
    if not isinstance(obj, dict):
        raise SecretsBoxError("secrets blob is not a JSON object")
    return obj

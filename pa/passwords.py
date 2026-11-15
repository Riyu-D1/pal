"""Password hashing with hashlib.scrypt (stdlib, no extra dependency).

Format: scrypt$N$r$p$salt_b64$hash_b64.  N=2**15, r=8, p=1 uses ~32MB of
memory for ~0.1s on modest hardware - reasonable on the 512MB target for a
single-owner app that also rate-limits logins.
"""

import base64
import hashlib
import hmac
import os

N = 1 << 15
R = 8
P = 1
DKLEN = 32
SALTLEN = 16
_MAXMEM = 256 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = os.urandom(SALTLEN)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=N, r=R, p=P,
                        dklen=DKLEN, maxmem=_MAXMEM)
    return "scrypt${}${}${}${}${}".format(
        N, R, P,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$", 5)
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=int(n), r=int(r), p=int(p), dklen=len(expected),
                            maxmem=_MAXMEM)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False

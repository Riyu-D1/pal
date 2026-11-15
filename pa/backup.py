"""Backup, restore and encrypted export.

- `backup`: SQLite online backup API to a file under DATA_DIR/backups
  (a same-volume copy - NOT disaster recovery).
- `export-backup`: online backup into a temp db, then AES-256-GCM
  encrypt it to a path the owner chooses (PA_BACKUP_DEST + passphrase).
  The encrypted artifact can be copied off-box by the owner - we never
  create paid storage ourselves.
- `restore`: online-API copy from a backup file into the live db.
- `import-backup`: decrypt an export then restore.
"""

import base64
import hashlib
import os
import sqlite3
import tempfile

from .util import now_iso

MAGIC = b"PABK1\n"


class BackupError(Exception):
    pass


def backup(db_path: str, dest: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def restore(db_path: str, src_path: str) -> None:
    if not os.path.exists(src_path):
        raise BackupError(f"backup not found: {src_path}")
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(db_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt,
                          n=1 << 15, r=8, p=1, dklen=32,
                          maxmem=256 * 1024 * 1024)


def export_backup(db_path: str, dest_path: str, passphrase: str) -> str:
    """Encrypted export. Format: MAGIC | salt | nonce | ciphertext|tag."""
    if not passphrase:
        raise BackupError("a backup passphrase is required")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise BackupError("cryptography package is required for "
                          "export-backup (pip install -r requirements.txt)")
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".",
                exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "snapshot.db")
        backup(db_path, tmp)
        with open(tmp, "rb") as f:
            raw = f.read()
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(passphrase, salt)
    blob = AESGCM(key).encrypt(nonce, raw, MAGIC)
    payload = MAGIC + salt + nonce + blob
    with open(dest_path, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    return dest_path


def import_backup(db_path: str, src_path: str, passphrase: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    with open(src_path, "rb") as f:
        raw = f.read()
    if not raw.startswith(MAGIC):
        raise BackupError("not a pa export file")
    body = raw[len(MAGIC):]
    salt, nonce, blob = body[:16], body[16:28], body[28:]
    key = _derive_key(passphrase, salt)
    try:
        plain = AESGCM(key).decrypt(nonce, blob, MAGIC)
    except Exception:
        raise BackupError("decryption failed (wrong passphrase or corrupt "
                          "file)")
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "snapshot.db")
        with open(tmp, "wb") as f:
            f.write(plain)
        restore(db_path, tmp)


def default_backup_dest(data_dir: str) -> str:
    stamp = now_iso().replace(":", "-").replace("+", "Z")
    return os.path.join(data_dir, "backups", f"pa-{stamp}.db")

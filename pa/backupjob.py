"""scheduled-backup system job (docs/PARITY.md #27).

Nightly AES-256-GCM encrypted export via pa/backup.py. OFF until the
owner enables the job (/jobs scheduled-backup on) AND sets
PA_BACKUP_PASSPHRASE — without the passphrase the tick skips quietly
(never fails the job row for a config choice).

Destination: PA_BACKUP_DEST or DATA_DIR/backups/pa-<stamp>.db.enc.
Keeps the newest PA_BACKUP_KEEP (default 7) exports, prunes older.
"""

import logging
import os

from . import backup as backupmod

log = logging.getLogger("pa.backupjob")


def _keep(env) -> int:
    try:
        return max(1, int(env.get("PA_BACKUP_KEEP") or 7))
    except ValueError:
        return 7


def _prune(folder: str, keep: int) -> int:
    try:
        files = sorted(
            f for f in os.listdir(folder)
            if f.startswith("pa-") and f.endswith(".enc"))
    except OSError:
        return 0
    doomed = files[:-keep] if len(files) > keep else []
    gone = 0
    for f in doomed:
        try:
            os.unlink(os.path.join(folder, f))
            gone += 1
        except OSError:
            pass
    return gone


def run(bot, payload: dict) -> str:
    from . import sysjobs
    s, env = bot.s, bot.env
    passphrase = env.get("PA_BACKUP_PASSPHRASE") or ""
    if not passphrase:
        return "skipped — PA_BACKUP_PASSPHRASE is not set"
    dest = (env.get("PA_BACKUP_DEST") or "").strip()
    if not dest:
        dest = backupmod.default_backup_dest(s.data_dir) + ".enc"
    try:
        path = backupmod.export_backup(s.db_path, dest, passphrase)
    except Exception as e:
        log.exception("scheduled backup failed")
        return f"backup failed: {str(e)[:150]}"
    pruned = _prune(os.path.dirname(path) or ".", _keep(env))
    size = os.path.getsize(path) if os.path.exists(path) else 0
    off = ""
    if env.get("PA_BACKUP_OFFSITE"):
        from . import offsite
        conn = bot._db()
        try:
            res = offsite.copy(path, conn, s, env)
            # A7: persist the last outcome for dr-check's status row
            import json as _json
            from . import db as dbmod
            from .util import now_iso
            dbmod.set_setting(conn, "offsite_last", _json.dumps({
                "ok": bool(res.get("ok")), "via": res.get("via", ""),
                "detail": str(res.get("detail", ""))[:120],
                "at": now_iso()}))
        finally:
            conn.close()
        off = ", offsite " + ("✅" if res.get("ok") else "❌ "
                             + res.get("detail", ""))
    dm = sysjobs.owner_dm(bot)
    if dm and bot.api is not None:
        bot.api.post_message(
            dm, f"💾 encrypted backup written: `{path}` "
                f"({size // 1024} KB"
                + (f", pruned {pruned} old" if pruned else "")
                + off + ")")
    return f"exported {os.path.basename(path)} ({size}B)" \
           + (f", pruned {pruned}" if pruned else "")

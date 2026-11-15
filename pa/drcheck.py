"""Disaster-recovery drill: `python -m pa dr-check`.

Read-only. Verifies the backup story end-to-end on the live box and
prints a green/red table. It never prints secret VALUES — only which
env names are set/missing. Weekly `dr-check` sysjob (seeded disabled)
DMs only on red.
"""

import glob
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time

from . import backup as backupmod, db as dbmod

log = logging.getLogger("pa.drcheck")

MAX_BACKUP_AGE_H = 48
MIN_FREE_MB = 200
# names only — dr-check asserts presence, never echoes values
REQUIRED_ENV = ("DISCORD_BOT_TOKEN", "DISCORD_OWNER_ID",
                "PA_BACKUP_PASSPHRASE")
# a restored DB must at least reach this schema floor — catches
# truncated/old backups without tying the drill to today's version
MIN_RESTORE_VERSION = 30
PROBE_TABLES = ("schedules", "pa_settings", "chat_inbox")


def _row(name, ok, detail=""):
    return {"check": name, "ok": bool(ok), "detail": str(detail)[:120]}


def _skip(name, detail=""):
    """Not-applicable row (e.g. offsite unset) — never counts as red."""
    return {"check": name, "ok": None, "detail": str(detail)[:120]}


def _verify_restored(path: str):
    """Open the decrypted DB: integrity_check, schema floor, expected
    tables. Returns (ok, detail). Caller deletes the file."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            return False, f"integrity_check: {str(ic)[:60]}"
        v = dbmod.current_version(conn)
        if v < MIN_RESTORE_VERSION:
            return False, f"schema {v} below floor " \
                          f"{MIN_RESTORE_VERSION}"
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in PROBE_TABLES if t not in names]
        if missing:
            return False, "missing tables: " + ",".join(missing)
        return True, f"integrity ok, schema {v}"
    except sqlite3.Error as e:
        return False, f"restore unreadable: {str(e)[:60]}"
    finally:
        conn.close()


def latest_backup(s, env=None) -> str:
    env = os.environ if env is None else env
    dest = (env.get("PA_BACKUP_DEST") or "").strip()
    dirs = [os.path.join(s.data_dir, "backups")]
    if dest:
        dirs.insert(0, os.path.dirname(dest) or ".")
    newest, newest_t = "", 0.0
    for d in dirs:
        for p in glob.glob(os.path.join(d, "*.enc")) + \
                glob.glob(os.path.join(d, "pa-*.db")):
            try:
                t = os.path.getmtime(p)
            except OSError:
                continue
            if t > newest_t:
                newest, newest_t = p, t
    return newest


def run(s, env=None, conn=None) -> dict:
    """Every check -> {check, ok, detail}; {ok, rows} overall."""
    env = os.environ if env is None else env
    rows = []
    close = False
    if conn is None:
        conn = dbmod.connect(s.db_path)
        close = True
    try:
        path = latest_backup(s, env)
        if not path:
            rows.append(_row("backup present", False,
                             "no *.enc under PA_BACKUP_DEST/"
                             "data_dir backups"))
            age_h = None
        else:
            age_h = (time.time() - os.path.getmtime(path)) / 3600
            rows.append(_row("backup age", age_h <= MAX_BACKUP_AGE_H,
                             f"{os.path.basename(path)} {age_h:.0f}h old"))
        ok_dec = detail = False, "no backup"
        if path:
            pw = env.get("PA_BACKUP_PASSPHRASE") or ""
            if not pw:
                detail = False, "PA_BACKUP_PASSPHRASE not set"
            else:
                try:
                    with tempfile.TemporaryDirectory() as td:
                        target = os.path.join(td, "r.db")
                        backupmod.import_backup(target, path, pw)
                        detail = _verify_restored(target)
                except Exception as e:
                    detail = False, f"decrypt failed: {str(e)[:60]}"
        rows.append(_row("backup restores", detail[0], detail[1]))
        try:
            live = dbmod.current_version(conn)
            rows.append(_row("schema version",
                             live >= dbmod.SCHEMA_VERSION,
                             f"at {live}, expected "
                             f"{dbmod.SCHEMA_VERSION}"))
        except Exception as e:
            rows.append(_row("schema version", False, str(e)[:80]))
        missing = [n for n in REQUIRED_ENV if not env.get(n)]
        rows.append(_row("env names present", not missing,
                         "missing: " + ",".join(missing)
                         if missing else "all present"))
        try:
            units = subprocess.run(
                ["systemctl", "--user", "list-unit-files"],
                capture_output=True, text=True, timeout=5).stdout + \
                subprocess.run(["systemctl", "list-unit-files"],
                               capture_output=True, text=True,
                               timeout=5).stdout
            have = [u for u in ("pa.service", "pa-discord.service",
                                "pa.service") if u in units]
            deploy_units = [f for f in os.listdir("deploy")
                            if f.endswith(".service")] \
                if os.path.isdir("deploy") else []
            rows.append(_row(
                "systemd units", bool(have) or bool(deploy_units),
                ("live: " + ",".join(have)) if have else
                f"{len(deploy_units)} unit file(s) in deploy/"
                if deploy_units else "none found"))
        except Exception as e:
            rows.append(_row("systemd units", False, str(e)[:80]))
        free_mb = shutil.disk_usage(s.data_dir).free / 1e6
        rows.append(_row("disk free", free_mb >= MIN_FREE_MB,
                         f"{free_mb:.0f} MB"))
        # last offsite copy result, persisted by backupjob — a
        # configured-but-never-copied (or failed) target is red; an
        # unconfigured one is a skip row, not a failure.
        target = (env.get("PA_BACKUP_OFFSITE") or "").strip()
        if not target:
            rows.append(_skip("offsite copy", "PA_BACKUP_OFFSITE unset"))
        else:
            import json as _json
            try:
                st = _json.loads(dbmod.get_setting(
                    conn, "offsite_last", "{}") or "{}")
            except (ValueError, sqlite3.Error):
                st = {}
            if not st:
                rows.append(_row(
                    "offsite copy", False,
                    f"{target} — no copy recorded yet"))
            elif st.get("ok"):
                rows.append(_row(
                    "offsite copy", True,
                    f"{st.get('via', target)} @ "
                    f"{str(st.get('at', ''))[:16]}"))
            else:
                rows.append(_row(
                    "offsite copy", False,
                    f"{st.get('via', target)} failed @ "
                    f"{str(st.get('at', ''))[:16]}: "
                    f"{str(st.get('detail', ''))[:50]}"))
    finally:
        if close:
            conn.close()
    return {"ok": all(r["ok"] for r in rows if r["ok"] is not None),
            "rows": rows}


def table(out: dict) -> str:
    lines = ["dr-check:", "  check                status   detail"]
    for r in out["rows"]:
        mark = "skip " if r["ok"] is None else \
            ("green" if r["ok"] else "RED  ")
        lines.append(f"  {r['check'][:20]:20} {mark}   {r['detail']}")
    lines.append(f"overall: {'green' if out['ok'] else 'RED'}")
    return "\n".join(lines)


# -- weekly sysjob (seeded disabled) — DMs only on red ------------------

def job(bot, payload) -> str:
    out = run(bot.s, env=bot.env)
    if out["ok"]:
        return "dr-check green"
    from . import sysjobs
    sysjobs.dm_and_voice(bot, "🛟 dr-check is RED:\n" + table(out),
                         voice=False)
    return "dr-check red — owner DM sent"

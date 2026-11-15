"""Optional off-box backup copy.

`PA_BACKUP_OFFSITE=gdrive@pal` uploads each encrypted `.enc` export to
**pal's own** Google Drive right after `scheduled-backup` writes it —
pal's account, so no owner approval is needed (the owner rule covers
owner accounts only). The passphrase never leaves the env file; only
the already-encrypted blob is copied.

Best-effort: failures log and report into the backup DM, never fail
the backup itself.
"""

import base64
import logging
import os

log = logging.getLogger("pa.offsite")


def offsite_target(env) -> str:
    return (env.get("PA_BACKUP_OFFSITE") or "").strip().lower()


def copy(path: str, conn, s, env, upload_impl=None) -> dict:
    """Copy `path` to the configured offsite target -> {"ok", "via",
    "detail"}. upload_impl(file_name, b64, mime)->dict is injectable
    for tests; production goes through Composio's googledrive
    connection labelled @pal."""
    target = offsite_target(env)
    if not target:
        return {"ok": False, "skipped": True, "via": ""}
    if target != "gdrive@pal":
        return {"ok": False, "via": target,
                "detail": "unsupported target (want gdrive@pal)"}
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        name = os.path.basename(path)
        if upload_impl is None:
            upload_impl = _gdrive_upload
        out = upload_impl(conn, s, env, name, b64)
        return {"ok": True, "via": target,
                "detail": str(out.get("file_id") or name)}
    except Exception as e:
        log.warning("offsite copy failed: %s", str(e)[:120])
        return {"ok": False, "via": target,
                "detail": str(e)[:120]}


def _gdrive_upload(conn, s, env, name: str, b64: str) -> dict:
    """GOOGLEDRIVE_UPLOAD_FILE via the @pal connection."""
    from . import composio as composiomod
    cli = composiomod.ComposioClient(s, s.composio_key(env))
    return cli.run_action(
        "GOOGLEDRIVE_UPLOAD_FILE",
        {"file_to_upload": b64, "file_to_upload_name": name,
         "file_name": name},
        account="pal", conn=conn) if hasattr(cli, "run_action") else \
        cli.execute("googledrive@pal", "GOOGLEDRIVE_UPLOAD_FILE",
                    {"file_to_upload": b64,
                     "file_to_upload_name": name,
                     "file_name": name})

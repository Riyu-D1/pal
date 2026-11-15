# Backup & restore

The SQLite file under `PA_DATA_DIR` is the state of record: assistants,
tasks, steps, memories, owner facts, approvals, sessions, usage.

## Commands

```sh
python -m pa backup        DEST.db          # consistent online backup (same volume OK for snapshots)
python -m pa restore       SRC.db           # replace the live db from a backup file
python -m pa export-backup DEST.bin         # AES-256-GCM encrypted export — passphrase prompted
python -m pa import-backup SRC.bin          # decrypt + restore
```

- `backup` uses SQLite's online backup API — safe while the app runs.
- `export-backup` encrypts the snapshot with AES-256-GCM under a key derived
  from an owner passphrase (scrypt). The passphrase is prompted, never a
  flag, never stored. The output file can go to any owner-chosen
  destination; **this repo creates no paid storage**.
- `import-backup` verifies the `PABK1` magic and fails cleanly on a wrong
  passphrase.

## Policy

- Same-volume copies are **snapshots, not disaster recovery**. Schedule
  `export-backup` to a destination the owner configures (off-box).
- Suggested cadence: daily encrypted export, retain a week or two.
- **Restore drill before live-ready** (tested in CI as a round-trip):
  1. `export-backup` the live db.
  2. On a scratch `PA_DATA_DIR`, `import-backup`.
  3. `check-config`, boot, verify an owner fact + a memory row survive.
- WAL files matter: always back up through these commands, never copy the
  raw `.db` while the app is running.

## What is *not* backed up

Nothing. Everything durable lives in the one SQLite file; `static/` and
code ship with the repo. Sessions are in the db but are short-lived —
restoring an old backup simply logs everyone out.

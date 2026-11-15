# W1 — File workspace

A durable per-project file store for artefacts pal makes — reports,
generated files, research output. Root:
`<PA_DATA_DIR>/workspace/<project>/<path>`, versioned on every write,
indexed in the `files` table (m36).

Off by default: `PA_WORKSPACE=1`. `PA_WORKSPACE_MB=500` caps total
bytes; `PA_WORKSPACE_PROJECT=main` sets the default project.

## Chat tools

| tool | notes |
|------|-------|
| `file_list` | list files (+versions) in a project |
| `file_read` | read a file (latest or `version=N`) |
| `file_version` | list one file's version history |
| `file_delete` | tombstone — history stays recoverable |
| `file_write` | unchanged attachment behaviour; when the workspace is on the file also persists to the default project |

The workspace tools are pal-local reads/writes (no owner-account
touch). Attaching a file **to** an owner account still goes through
the normal policy path — nothing changes there.

## Guarantees

- Path safety: `_safe_relpath` rejects `..`, absolute paths,
  backslashes, control chars, >200-char segments; resolved paths are
  confined to the project root (realpath check).
- Versions: each write archives the outgoing file under
  `.versions/`; new content lands via temp + `os.replace` so archived
  hardlinks keep their bytes.
- Quota: `PA_WORKSPACE_MB` counts live bytes across all projects;
  oversize writes are refused with `cap_exceeded`.
- File titles feed the recall index when `pa/recall.py` is present
  (off unless `PA_RECALL=true`).
- `/files` in Discord lists the default project.

## Storage layout

```
<data>/workspace/<project>/<path>             # latest copy
<data>/workspace/<project>/.versions/<path>.v<N>   # history
files table: project, path, version, size, sha256, deleted, created_at
```

Deleting `<data>/workspace/` removes the bytes; DB rows stay for
audit. `file_delete` only tombstones.

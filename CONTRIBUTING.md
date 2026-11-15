# Contributing

Issues and PRs welcome. A few house rules keep the codebase coherent:

## Style

- **stdlib-first** — Python, no framework, no ORM. New dependencies need
  a strong reason and a pin.
- Follow the existing module layout (`pa/<thing>.py` + `pa/<thing>_test`
  coverage in `tests/`). Small, boring, single-purpose functions.
- Env config goes in `pa/config.py` only; document it in `.env.example`
  + `docs/CONFIG.md`.

## Invariants — don't weaken these

1. **Approval floor**: every write/send/delete/spend on the owner's
   accounts requires an approval click. No code path may skip it.
2. **Untrusted fencing**: email/web/SMS/document content is data, never
   instructions. New ingest paths must fence identically.
3. **Single owner**: `DISCORD_OWNER_ID` is the only account; fail closed.
4. **Secrets**: env only — never persisted, logged, or printed.
   `python scripts/check_no_personal_data.py` must stay clean.

## Verify before PR

```sh
python -m unittest discover -s tests   # must end OK (~1500 tests)
python -m pa eval                       # offline eval fixtures
python scripts/check_no_personal_data.py
```

CI runs the same three plus the sidecar-deploy and Docker smoke jobs.
If you add a feature flag, default it **off** and write the tests first.

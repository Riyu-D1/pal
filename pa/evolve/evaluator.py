"""Protected evaluator (final-4 E2).

The scoring core lives here, inside PROTECTED_FILES territory —
pal's own change paths can never modify it at runtime. Scores are
only meaningful against a *pinned* evaluator set: at startup the
first scorer stores a SHA-256 manifest of evaluator + fixtures in
pa_settings; a later deploy (pa selfupdate) re-pins it. If the set
on disk no longer matches the pin, every score request REFUSES —
a candidate can never make itself look better by editing fixtures.

Held-out fixtures (evals/heldout/*.json) drive promotion decisions
only. load_fixtures() never reads the subdirectory, so planner /
reflect / heal prompts can never see or quote them.
"""

import hashlib
import json
import logging
import os

from .. import db as dbmod
from . import eval as evalmod

log = logging.getLogger("pa.evolve.evaluator")

PIN_KEY = "eval_manifest_sha256"
HELDOUT_DIR = os.path.join(evalmod.FIXTURE_DIR, "heldout")


def _manifest_files(eval_dir=None):
    """Sorted (relpath, bytes) the pin covers: evaluator source +
    every fixture + every held-out fixture."""
    d = eval_dir or evalmod.FIXTURE_DIR
    paths = [os.path.abspath(__file__)]
    for sub in ("", "heldout"):
        sd = os.path.join(d, sub)
        try:
            names = sorted(os.listdir(sd))
        except OSError:
            continue
        for fn in names:
            p = os.path.join(sd, fn)
            if fn.endswith(".json") and os.path.isfile(p):
                paths.append(p)
    return paths


def manifest_hash(eval_dir=None) -> str:
    h = hashlib.sha256()
    root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    for p in _manifest_files(eval_dir):
        rel = os.path.relpath(p, root)
        h.update(rel.encode())
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            return ""
    return h.hexdigest()


def pin(conn, eval_dir=None) -> str:
    """Record the current manifest as the approved pin. Called at
    owner-approved deploy time (pa selfupdate / first boot)."""
    h = manifest_hash(eval_dir)
    dbmod.set_setting(conn, PIN_KEY, h)
    log.info("evaluator manifest pinned %s", h[:16])
    return h


def check(conn, eval_dir=None) -> dict:
    """Compare on-disk manifest to the pinned one. Bootstraps the
    pin on first use. {"ok": bool, "pinned", "current"}."""
    current = manifest_hash(eval_dir)
    pinned = dbmod.get_setting(conn, PIN_KEY)
    if not pinned:
        dbmod.set_setting(conn, PIN_KEY, current)
        return {"ok": True, "pinned": current, "current": current,
                "bootstrapped": True}
    return {"ok": pinned == current, "pinned": pinned,
            "current": current}


def run(conn, settings, eval_dir=None, ids=None, live=False) -> dict:
    """Score on the main set + held-out set, or REFUSE when the
    evaluator set drifted from the owner-approved pin."""
    st = check(conn, eval_dir)
    if not st["ok"]:
        log.warning("evaluator refused: manifest drift "
                    "(pinned %s, current %s)",
                    (st["pinned"] or "")[:12],
                    (st["current"] or "")[:12])
        return {"refused": "evaluator manifest drift — redeploy or "
                "re-pin via pa selfupdate", "score": 0.0,
                "results": [], "regressions": [],
                "eval_set_hash": ""}
    out = evalmod.run(settings, eval_dir=eval_dir, ids=ids, live=live)
    heldout = evalmod.run(settings, eval_dir=HELDOUT_DIR, live=live) \
        if eval_dir is None else {"score": None}
    out["heldout_score"] = heldout.get("score")
    out["heldout_results"] = heldout.get("results", [])
    return out

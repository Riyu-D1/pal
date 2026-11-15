"""Protected-path policy (docs/EVOLVE.md #11).

Paths that are NEVER Track A and that make a Track B approval card
high-risk — the card shows the hit and offers no "Always allow".
A proposed diff touching any of these can only ever go to the
owner, who reviews it as a normal code change.
"""

import posixpath

# exact files (safety-critical modules + this policy itself)
PROTECTED_FILES = frozenset({
    "pa/approvals.py", "pa/allowrules.py", "pa/review.py",
    "pa/policy.py",                       # the action floor
    "pa/vault.py", "pa/credvault.py", "pa/secretsbox.py",
    "pa/passwords.py", "pa/egress.py", "pa/untrusted.py",
    "pa/discordbot.py", "pa/webapp.py",   # owner gating + auth/CSRF
    "pa/evolve/policy.py",
    "pa/evolve/evaluator.py",             # the scorer itself
})

# whole trees — CI config, deploy units, secret material
PROTECTED_PREFIXES = (
    ".github/workflows/",
    "deploy/",              # units that bind/expose ports
    ".env",
    "evals/",               # scoring fixtures are frozen
)


def _norm(path: str) -> str:
    p = posixpath.normpath(str(path or "").replace("\\", "/")
                           ).lstrip("/")
    return p


def is_protected(path: str) -> bool:
    p = _norm(path)
    if p in PROTECTED_FILES:
        return True
    return any(p.startswith(pre) for pre in PROTECTED_PREFIXES)


def touches_protected(diff_paths) -> list:
    """The protected subset of a list of diff paths ([] = clean)."""
    return [p for p in (diff_paths or []) if is_protected(p)]

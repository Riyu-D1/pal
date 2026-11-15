"""Anti-gaming checks (final-4 E3).

The Darwin-Gödel-Machine failure mode — score that looks better
because the tests were weakened, not the system — gets rejected
here before a candidate can promote. Each rule returns a reason
string; the caller keeps the variant as `rejected` and flags the
owner, and the weekly digest lists them.
"""

import re

from . import policy

JUMP = 1.30          # >30% single-step score jump is suspicious

_ASSERT_RE = re.compile(
    r"\b(assert(?:Equal|In|True|False|NotIn|Raises|IsNone|AlmostEqual)?"
    r"\s*\(|expect_tools|forbidden_tools|must_contain|must_not_contain"
    r"|obs_must_contain)\b")
_LOG_METRICS_RE = re.compile(
    r"(logsetup|logging|metrics|tracing|telemetry)", re.I)
_TEST_RE = re.compile(r"(tests?/|/tests?_|_test\.|conftest|evals?/)", re.I)


def _assertion_count(text) -> int:
    return len(_ASSERT_RE.findall(text or ""))


def check_candidate(*, score_before, score_after,
                    eval_hash_before="", eval_hash_after="",
                    paths=None, new_content=None, old_content=None):
    """Pure rejection checks -> list of reason strings (empty=ok)."""
    reasons = []
    paths = list(paths or [])
    score_before = float(score_before or 0.0)
    score_after = float(score_after or 0.0)

    if score_before > 0 and score_after > score_before * JUMP \
            and eval_hash_before == eval_hash_after:
        reasons.append(
            f"score_jump:{score_before:.2f}->{score_after:.2f} "
            f"without a fixture change")

    for p in paths:
        if policy.is_protected(p):
            reasons.append(f"protected_path:{p}")

    if old_content is not None and new_content is not None:
        old_n, new_n = _assertion_count(old_content), \
            _assertion_count(new_content)
        if new_n < old_n:
            reasons.append(
                f"assertion_weakened:{old_n}->{new_n}")

    touched_log = [p for p in paths if _LOG_METRICS_RE.search(p)]
    touched_test = [p for p in paths if _TEST_RE.search(p)]
    touched_code = [p for p in paths
                    if p not in touched_log and p not in touched_test]
    if touched_log and (touched_code or touched_test):
        reasons.append(
            "log_metrics_coupled:" + ",".join(touched_log[:3]))
    return reasons

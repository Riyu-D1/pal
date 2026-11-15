"""pal evolve — the self-improvement loop (docs/EVOLVE.md).

Incidents are always written when the table exists (cheap, useful);
everything that *acts* on them is gated behind PA_EVOLVE plus the
seeded-disabled system jobs, so a mid-migration deploy never starts
behaving differently by accident.
"""

from .incidents import fingerprint, record

__all__ = ["fingerprint", "record"]

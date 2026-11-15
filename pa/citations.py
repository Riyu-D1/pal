"""Citation validation.

An answer is {claims:[{text, source_ids}], sources:[{id,url,title,
fetched_at,excerpt}]}. The `finish` tool already rejects source ids not
fetched in this task; this module re-validates a stored answer (used by
the UI renderer and by tests that feed fabricated ids).
"""


def validate_answer(answer: dict, fetched_ids: set) -> dict:
    """Return {claims:[{text, source_ids, status}], sources:[...]} with
    each claim labelled 'supported' or 'unsupported'."""
    claims = []
    for c in (answer.get("claims") or [])[:50]:
        sids = [s for s in (c.get("source_ids") or [])
                if isinstance(s, int) and s in fetched_ids]
        claims.append({
            "text": c.get("text") or "",
            "source_ids": sids,
            "status": "supported" if sids else "unsupported",
        })
    sources = [s for s in (answer.get("sources") or [])
               if isinstance(s.get("id"), int) and s["id"] in fetched_ids]
    return {"claims": claims, "sources": sources,
            "summary": answer.get("summary") or ""}

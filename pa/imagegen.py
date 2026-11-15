"""Image generation (docs/PARITY.md #15).

generate_image(prompt) — two free paths:

1. Cloudflare Workers AI when CLOUDFLARE_ACCOUNT_ID +
   CLOUDFLARE_API_TOKEN are set: @cf/black-forest-labs/flux-1-schnell
   (free-tier), JSON reply {result:{image: b64}}.
2. Pollinations — no key: GET image.pollinations.ai/prompt/<prompt>
   through the bounded egress guard, image/* only.

Result bytes are capped (PA_IMAGE_MAX_BYTES) and handed to
ctx.attach() — posted to the channel when small enough, otherwise
saved under the chat sandbox.
"""

import base64
import urllib.parse

from .netclient import APIClient, HTTPError, TransportError

MAX_BYTES = 8 * 1024 * 1024
_CF_MODEL = "@cf/black-forest-labs/flux-1-schnell"


def _cloudflare(s, env, prompt: str) -> bytes:
    acct = (env.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    key = (env.get("CLOUDFLARE_API_TOKEN") or "").strip()
    if not (acct and key):
        return b""
    base = (getattr(s, "cloudflare_ai_base", "") or
            "https://api.cloudflare.com/client/v4")
    try:
        _h, raw = APIClient(base, timeout=60).post_json(
            f"/accounts/{acct}/ai/run/{_CF_MODEL}",
            {"prompt": prompt[:1000]},
            headers={"Authorization": f"Bearer {key}"})
    except (HTTPError, TransportError):
        return b""
    b64 = ((raw or {}).get("result") or {}).get("image") or ""
    try:
        return base64.b64decode(b64)
    except Exception:
        return b""


def _pollinations(s, env, prompt: str) -> bytes:
    from . import egress
    url = ("https://image.pollinations.ai/prompt/"
           + urllib.parse.quote(prompt[:500])
           + "?width=768&height=768&nologo=true")
    try:
        res = egress.guarded_fetch(
            url, max_bytes=MAX_BYTES + 1, timeout=30,
            allowed=("image/png", "image/jpeg", "image/webp"))
    except Exception:
        return b""
    body = res.get("body") or b""
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    if len(body) > MAX_BYTES or not body[:8].startswith(
            (b"\x89PNG", b"\xff\xd8\xff", b"RIFF")):
        return b""
    return body


def generate(s, env, prompt: str) -> dict:
    """-> {data, backend} or {error}. Prompt is bounded; the result is
    byte-capped and magic-checked — it is only ever an image."""
    prompt = (prompt or "").strip()[:500]
    if not prompt:
        return {"error": "bad_arguments", "message": "empty prompt"}
    data = _cloudflare(s, env, prompt)
    backend = "cloudflare"
    if not data:
        data = _pollinations(s, env, prompt)
        backend = "pollinations"
    if not data:
        return {"error": "generate_failed",
                "message": "both image backends failed"}
    if len(data) > MAX_BYTES:
        return {"error": "too_large"}
    return {"data": data, "backend": backend}

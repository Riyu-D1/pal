"""Vision captions on the free tier.

Chain order: PA_VISION_ORDER (default groq,gemini,cloudflare). Each
backend tries PA_VISION_MODEL_<BACKEND> first, then a short built-in
list — Groq's vision lineup moves, so a 400/404 just walks to the next
candidate instead of hard-failing. Rate-limits/breaks record a short
cooldown in router_cooldowns under 'vision-<backend>' so the next call
skips a hot backend.

- Off when no backend is configured (or PA_VISION=false); the image
  turn then keeps the item-3 stub.
- Images go as data URIs, capped at PA_VISION_MAX_BYTES (default
  4 MB) — no re-encode (stdlib has no JPEG writer); oversized files
  just don't get a caption.
- Captions re-enter the model fenced: <<<UNTRUSTED VISION CAPTION>>>.
- look(path_or_url) is the chat tool: sandbox file or public http(s)
  image via the same egress guard as fetch_url.
"""

import base64
import datetime
import logging
import os
import urllib.parse

from . import db as dbmod, egress
from .netclient import APIClient, HTTPError, TransportError
from .util import now_iso, parse_iso, utcnow

log = logging.getLogger(__name__)

_OPENAI_IMAGE = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}

_BUILTIN_MODELS = {
    "groq": ["meta-llama/llama-4-scout-17b-16e-instruct",
             "meta-llama/llama-4-maverick-17b-128e-instruct"],
    "gemini": ["gemini-3.5-flash-lite", "gemini-2.0-flash"],
    "cloudflare": ["@cf/meta/llama-3.2-11b-vision-instruct",
                   "@cf/llava-hf/llava-1.5-7b-hf"],
}

_PROMPT = ("Describe this image in 2-4 plain sentences: what it shows, "
           "any legible text, and the obvious context.")

_COOLDOWN_S = 300
_resolved = {}          # backend -> model that answered last


class VisionError(Exception):
    pass


def _key(env, name):
    return (env.get(name) or "").strip()


def _configured(s, env, backend):
    if backend == "cloudflare":
        return bool(_key(env, "CLOUDFLARE_ACCOUNT_ID")
                    and _key(env, "CLOUDFLARE_API_TOKEN"))
    return bool(_key(env, f"{backend.upper()}_API_KEY"))


def _models(s, env, backend):
    out = []
    over = _key(env, f"PA_VISION_MODEL_{backend.upper()}")
    if over:
        out.append(over)
    last = _resolved.get(backend)
    if last and last not in out:
        out.append(last)
    for m in _BUILTIN_MODELS.get(backend, []):
        if m not in out:
            out.append(m)
    return out


def _cooldown(db_path, backend):
    conn = dbmod.connect(db_path)
    try:
        row = conn.execute(
            "SELECT until FROM router_cooldowns WHERE backend=?",
            (f"vision-{backend}",)).fetchone()
        if row and row["until"]:
            try:
                return parse_iso(row["until"]) > utcnow()
            except (ValueError, TypeError):
                return False
        return False
    finally:
        conn.close()


def _mark_cooldown(db_path, backend):
    conn = dbmod.connect(db_path)
    try:
        until = utcnow() + datetime.timedelta(seconds=_COOLDOWN_S)
        conn.execute(
            "INSERT INTO router_cooldowns(backend, until, reason,"
            " strikes, updated_at) VALUES(?,?,?,0,?)"
            " ON CONFLICT(backend) DO UPDATE SET"
            " until=excluded.until, reason=excluded.reason,"
            " updated_at=excluded.updated_at",
            (f"vision-{backend}",
             until.isoformat(timespec="seconds"), "rate_limited",
             now_iso()))
    finally:
        conn.close()


def _mime(path, data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    ext = os.path.splitext(path)[1].lower()
    return {".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".gif": "image/gif"}.get(ext)


def _openai_call(base, key, model, mime, data, prompt):
    """OpenAI-style chat/completions with an image_url data URI —
    works for Groq, Gemini's compat endpoint and Cloudflare /ai/v1."""
    uri = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": uri}}]}],
        "max_tokens": 300,
    }
    _h, raw = APIClient(base, timeout=30).post_json(
        "/chat/completions", body,
        headers={"Authorization": f"Bearer {key}"})
    try:
        return raw["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise VisionError("malformed vision reply")


def _call(s, env, backend, model, mime, data):
    if backend in _OPENAI_IMAGE:
        base = _OPENAI_IMAGE[backend]
        key = _key(env, f"{backend.upper()}_API_KEY")
    elif backend == "cloudflare":
        acct = _key(env, "CLOUDFLARE_ACCOUNT_ID")
        key = _key(env, "CLOUDFLARE_API_TOKEN")
        base = (s.cloudflare_ai_base or
                "https://api.cloudflare.com/client/v4") + \
            f"/accounts/{acct}/ai/v1"
    else:
        raise VisionError(f"unknown vision backend {backend!r}")
    return _openai_call(base, key, model, mime, data, _PROMPT)


def describe_data(s, env, data: bytes, name: str = "image") -> str:
    """Caption image bytes via the first working backend. Returns the
    fenced caption string, or '' when vision is off/unavailable."""
    if not getattr(s, "vision", True):
        return ""
    if not data or len(data) > s.vision_max_bytes:
        return ""
    mime = _mime(name, data)
    if not mime:
        return ""
    db_path = getattr(s, "db_path", None)
    order = [b.strip() for b in
             getattr(s, "vision_order", "groq,gemini,cloudflare")
             .split(",") if b.strip()]
    for backend in order:
        if not _configured(s, env, backend):
            continue
        if db_path and _cooldown(db_path, backend):
            continue
        moved_on = False
        for model in _models(s, env, backend):
            try:
                text = _call(s, env, backend, model, mime, data)
            except HTTPError as e:
                if e.status in (400, 404, 422):
                    log.info("vision %s %s rejected (%s) — next model",
                             backend, model, e.status)
                    continue          # try the next model name
                if e.status == 429 and db_path:
                    _mark_cooldown(db_path, backend)
                moved_on = True
                break                 # backend trouble -> next backend
            except (TransportError, VisionError) as e:
                log.info("vision %s failed: %s", backend, e)
                moved_on = True
                break
            if text:
                _resolved[backend] = model
                return ("<<<UNTRUSTED VISION CAPTION>>>\n"
                        f"{text[:1500]}\n<<<END UNTRUSTED "
                        "VISION CAPTION>>>")
            break
        if moved_on:
            continue
    return ""


def caption_image(s, env, path: str) -> str:
    """Fenced caption for a local image file, '' when unavailable."""
    try:
        with open(path, "rb") as f:
            data = f.read(s.vision_max_bytes + 1)
    except OSError:
        return ""
    return describe_data(s, env, data, os.path.basename(path))


def look(s, env, ref: str) -> dict:
    """The `look` chat tool: sandbox path or public https image URL."""
    ref = (ref or "").strip()
    if not getattr(s, "vision", True):
        return {"error": "not_configured",
                "message": "vision is off (PA_VISION)"}
    if ref.lower().startswith(("http://", "https://")):
        try:
            res = egress.guarded_get(
                ref, s, max_bytes=s.vision_max_bytes,
                allowed=("image/png", "image/jpeg", "image/webp",
                         "image/gif"))
        except Exception as e:
            return {"error": "fetch_failed", "message": str(e)[:200]}
        data = res["body"]
        name = urllib.parse.urlsplit(ref).path or "image"
    else:
        root = os.path.realpath(s.chat_sandbox)
        path = os.path.realpath(ref)
        if not path.startswith(root + os.sep) and path != root:
            return {"error": "bad_arguments",
                    "message": "look only reads inside the chat "
                               "sandbox"}
        try:
            with open(path, "rb") as f:
                data = f.read(s.vision_max_bytes + 1)
        except OSError as e:
            return {"error": "not_found", "message": str(e)[:150]}
        name = os.path.basename(path)
    if len(data) > s.vision_max_bytes:
        return {"error": "too_large",
                "message": "image exceeds PA_VISION_MAX_BYTES"}
    caption = describe_data(s, env, data, name)
    if not caption:
        return {"error": "unavailable",
                "message": "no vision backend configured or all "
                           "backends cooling down"}
    return {"caption": caption}

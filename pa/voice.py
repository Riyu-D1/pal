"""Voice: TTS with a free-provider fallback chain + Whisper transcription.

synthesize() walks PA_TTS_ORDER (default gemini -> fish -> azure ->
cloudflare), skipping providers whose env keys are missing and cooling
down ones that error or rate-limit (same shape as pa.router cooldowns,
kept in-process). Whatever the provider returns (mp3, wav, raw PCM) is
normalised through ffmpeg to OGG/Opus 48 kHz mono — the exact shape a
Discord native voice message wants — while the decoded PCM doubles as
the source for duration_secs and the waveform preview.

transcribe() sends owner audio (voice-note attachments, call audio) to
Groq's whisper-large-v3-turbo, falling back to Cloudflare Workers AI.

Keys come only from env vars; nothing here logs or prints them.
"""

import base64
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import wave

from .netclient import APIClient, HTTPError, TransportError

log = logging.getLogger("pa.voice")

SAMPLE_RATE = 48000            # Discord voice-message format
WAVEFORM_MAX = 256             # datapoints Discord renders
_MAX_TTS_CHARS = 4000          # bound a single synthesis request
_REQ_TIMEOUT = 45.0

# Discord message flag for a native voice message
FLAG_IS_VOICE_MESSAGE = 1 << 13

# hosts Discord serves message attachments from
_ATTACHMENT_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")

_CRYPT = (401, 403)
_RATE = (402, 429)


class VoiceError(Exception):
    """A voice provider/feature failed; carries the error kind."""
    def __init__(self, message, kind="unavailable", retry_after=None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


def encode_multipart(fields, files):
    """RFC 2388 body. fields: [(name, str)]; files: [(name, filename,
    content_type, bytes)]. Returns (body_bytes, content_type)."""
    boundary = "pa-" + secrets.token_hex(12)
    out = io.BytesIO()
    for name, value in fields:
        out.write(f"--{boundary}\r\n".encode())
        out.write(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            .encode())
        out.write(str(value).encode("utf-8"))
        out.write(b"\r\n")
    for name, filename, ctype, data in files:
        out.write(f"--{boundary}\r\n".encode())
        out.write(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'.encode())
        out.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        out.write(data)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# ffmpeg: any-provider audio -> OGG/Opus 48k mono + PCM for duration/waveform

def _ffmpeg(bin_name):
    path = shutil.which(bin_name or "ffmpeg")
    if not path:
        raise VoiceError("ffmpeg not installed — run "
                         "`sudo apt install ffmpeg libopus0`",
                         kind="not_configured")
    return path


def to_pcm48k(audio: bytes, in_args=(), ffmpeg="ffmpeg") -> bytes:
    """Decode provider audio to raw s16le 48 kHz mono via ffmpeg.
    `in_args` are extra INPUT flags (e.g. ('-f','s16le','-ar','24000',
    '-ac','1') for headerless PCM)."""
    ff = _ffmpeg(ffmpeg)
    cmd = [ff, "-v", "error"] + list(in_args) + [
        "-i", "pipe:0", "-f", "s16le", "-ar", str(SAMPLE_RATE),
        "-ac", "1", "pipe:1"]
    try:
        proc = subprocess.run(cmd, input=audio, capture_output=True,
                              timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise VoiceError(f"ffmpeg decode failed: {e}")
    if proc.returncode != 0:
        raise VoiceError("ffmpeg decode failed: "
                         + proc.stderr.decode("utf-8", "replace")[:200])
    return proc.stdout


def pcm_to_ogg(pcm: bytes, ffmpeg="ffmpeg") -> bytes:
    """s16le 48 kHz mono -> OGG/Opus 32kbps (Discord voice-message shape)."""
    ff = _ffmpeg(ffmpeg)
    cmd = [ff, "-v", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE),
           "-ac", "1", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k",
           "-f", "ogg", "pipe:1"]
    try:
        proc = subprocess.run(cmd, input=pcm, capture_output=True,
                              timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise VoiceError(f"ffmpeg encode failed: {e}")
    if proc.returncode != 0:
        raise VoiceError("ffmpeg encode failed: "
                         + proc.stderr.decode("utf-8", "replace")[:200])
    return proc.stdout


def duration_secs(pcm: bytes) -> float:
    """Seconds of s16le 48 kHz mono PCM."""
    return round(len(pcm) / (2 * SAMPLE_RATE), 2)


def _audioop():
    """audioop — still in Python 3.12 (removed 3.13); imported lazily
    with the deprecation warning silenced. Used for in-process PCM
    resample/stereo so the call path needs no ffmpeg at play time."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import audioop
    return audioop


def pcm_to_stereo48k(pcm: bytes, in_rate: int, in_channels: int = 1,
                     state=None):
    """s16le PCM @in_rate -> s16le stereo @48 kHz, fully in-process
    (audioop). Returns (pcm48k_stereo, state) — pass the state back on
    streamed chunks to keep the resampler continuous."""
    a = _audioop()
    out, st = a.ratecv(pcm, 2, in_channels, in_rate, SAMPLE_RATE, state)
    return a.tostereo(out, 2, 1, 1), st


def to_pcm48k_stereo(audio: bytes, in_args=(), ffmpeg="ffmpeg") -> bytes:
    """Provider audio -> s16le stereo 48 kHz (V2 playback format).
    Raw s16le input (in_args '-f s16le -ar R -ac 1') resamples
    in-process — no ffmpeg; containers take one ffmpeg decode."""
    ia = list(in_args or ())
    try:
        if ia[ia.index("-f") + 1] == "s16le" and "-ar" in ia:
            rate = int(ia[ia.index("-ar") + 1])
            return pcm_to_stereo48k(audio, rate)[0]
    except (ValueError, IndexError):
        pass
    mono = to_pcm48k(audio, in_args, ffmpeg)
    return _audioop().tostereo(mono, 2, 1, 1)


def waveform_b64(pcm: bytes) -> str:
    """Base64 amplitude preview (<=256 bytes), as Discord expects: one
    byte per datapoint, downsampled to WAVEFORM_MAX."""
    n = len(pcm) // 2
    if n == 0:
        return base64.b64encode(bytes([0])).decode()
    buckets = min(WAVEFORM_MAX, n)
    step = max(1, n // buckets)
    pts = []
    for i in range(0, n, step):
        peak = 0
        for j in range(i, min(i + step, n)):
            v = int.from_bytes(pcm[j * 2:j * 2 + 2], "little", signed=True)
            peak = max(peak, abs(v))
        pts.append(min(255, peak >> 7))
        if len(pts) >= WAVEFORM_MAX:
            break
    return base64.b64encode(bytes(pts)).decode()


def pcm_to_wav(pcm: bytes, channels=1, rate=SAMPLE_RATE) -> bytes:
    """Wrap raw s16le PCM in a WAV container (Whisper input)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def to_voice_ogg(audio: bytes, in_args=(), ffmpeg="ffmpeg"):
    """Provider audio -> (ogg_opus_bytes, duration_secs, waveform_b64)."""
    pcm = to_pcm48k(audio, in_args, ffmpeg)
    return pcm_to_ogg(pcm, ffmpeg), duration_secs(pcm), waveform_b64(pcm)


# ---------------------------------------------------------------------------
# TTS providers — each returns (audio_bytes, ffmpeg_in_args)

def _client(base, timeout=_REQ_TIMEOUT):
    # API keys ride in headers — never send them cleartext.
    if urllib.parse.urlsplit(base or "").scheme != "https":
        raise VoiceError(f"voice provider base must be https: {base!r}",
                         kind="not_configured")
    return APIClient(base, timeout=timeout)


def _raise_http(name, e):
    retry = None
    ra = (e.headers or {}).get("retry-after")
    if ra:
        try:
            retry = min(3600.0, float(ra))
        except ValueError:
            retry = None
    if e.status in _CRYPT:
        raise VoiceError(f"{name}: auth rejected (HTTP {e.status})",
                         kind="auth", retry_after=retry)
    if e.status in _RATE or e.status >= 500:
        raise VoiceError(f"{name}: HTTP {e.status}", kind="rate_limited",
                         retry_after=retry)
    raise VoiceError(f"{name}: HTTP {e.status}", kind="bad_response",
                     retry_after=retry)


def _tts_gemini(s, env, text, style):
    """Gemini TTS via generateContent. Returns base64 audio in
    candidates[0].content.parts[].inlineData (usually raw 24 kHz PCM)."""
    prompt = f"Speak in a {style} manner:\n{text}" if style else text
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                "voiceName": s.tts_gemini_voice}}}},
    }
    try:
        _h, data = _client(s.tts_gemini_base).post_json(
            f"/v1beta/models/{s.tts_gemini_model}:generateContent",
            body, headers={"x-goog-api-key": env["GEMINI_API_KEY"]})
    except HTTPError as e:
        _raise_http("gemini", e)
    except TransportError as e:
        raise VoiceError(f"gemini: {e}")
    mime = ""
    audio_b64 = None
    for cand in (data or {}).get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if inline.get("data"):
                audio_b64 = inline["data"]
                mime = (inline.get("mimeType")
                        or inline.get("mime_type") or "")
                break
        if audio_b64:
            break
    if not audio_b64:
        raise VoiceError("gemini: no audio in response",
                         kind="bad_response")
    audio = base64.b64decode(audio_b64)
    mime = mime.lower()
    # e.g. 'audio/l16;codec=pcm;rate=24000' — headerless PCM needs
    # explicit format args; container formats (wav/mp3/ogg) self-probe.
    if "l16" in mime or "pcm" in mime:
        rate = 24000
        for tok in mime.replace(";", " ").split():
            if tok.startswith("rate="):
                try:
                    rate = int(tok.split("=", 1)[1])
                except ValueError:
                    pass
        return audio, ("-f", "s16le", "-ar", str(rate), "-ac", "1")
    return audio, ()


def _tts_fish(s, env, text, style):
    body = {"text": (f"({style}) {text}" if style else text),
            "format": "mp3"}
    if s.tts_fish_voice:
        body["reference_id"] = s.tts_fish_voice
    try:
        _h, audio = _client(s.tts_fish_base).request(
            "POST", "/v1/tts", body=body,
            headers={"Authorization": f"Bearer {env['FISH_API_KEY']}",
                     "model": s.tts_fish_model})
    except HTTPError as e:
        _raise_http("fish", e)
    except TransportError as e:
        raise VoiceError(f"fish: {e}")
    if not audio:
        raise VoiceError("fish: empty audio", kind="bad_response")
    return audio, ()


def _tts_azure(s, env, text, style):
    region = env["AZURE_SPEECH_REGION"].strip()
    esc = (text.replace("&", "&amp;").replace("<", "&lt;")
           .replace(">", "&gt;").replace('"', "&quot;"))
    inner = f"<prosody>{esc}</prosody>"
    if style:
        inner = (f'<mstts:express-as style="{style}"><prosody>{esc}'
                 "</prosody></mstts:express-as>")
    ssml = ("<speak version='1.0' xml:lang='en-GB' "
            "xmlns:mstts='https://www.w3.org/2001/mstts'>"
            f"<voice name='{s.tts_azure_voice}'>{inner}</voice></speak>")
    try:
        _h, audio = _client(
            f"https://{region}.tts.speech.microsoft.com").request(
            "POST", "/cognitiveservices/v1", body=ssml.encode("utf-8"),
            headers={
                "Ocp-Apim-Subscription-Key": env["AZURE_SPEECH_KEY"],
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": s.tts_azure_format})
    except HTTPError as e:
        _raise_http("azure", e)
    except TransportError as e:
        raise VoiceError(f"azure: {e}")
    if not audio:
        raise VoiceError("azure: empty audio", kind="bad_response")
    return audio, ()


def _tts_cloudflare(s, env, text, style):
    acct = env["CLOUDFLARE_ACCOUNT_ID"].strip()
    body = {"text": (f"({style}) {text}" if style else text),
            "speaker": s.tts_cf_speaker, "encoding": "mp3"}
    try:
        _h, audio = _client(s.cloudflare_ai_base).request(
            "POST", f"/accounts/{acct}/ai/run/@cf/deepgram/aura-2-en",
            body=body,
            headers={"Authorization":
                     f"Bearer {env['CLOUDFLARE_API_TOKEN']}"})
    except HTTPError as e:
        _raise_http("cloudflare", e)
    except TransportError as e:
        raise VoiceError(f"cloudflare: {e}")
    if not audio:
        raise VoiceError("cloudflare: empty audio", kind="bad_response")
    return audio, ()


_TTS = {
    "gemini": (("GEMINI_API_KEY",), _tts_gemini),
    "fish": (("FISH_API_KEY",), _tts_fish),
    "azure": (("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"), _tts_azure),
    "cloudflare": (("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"),
                   _tts_cloudflare),
}

_COOLDOWNS = {}
_COOL_LOCK = threading.Lock()
_BACKOFF = {"rate_limited": 120, "auth": 12 * 3600, "bad_response": 60,
            "unavailable": 60, "not_configured": 300}


def _cool_until(name):
    with _COOL_LOCK:
        return _COOLDOWNS.get(name, 0.0)


def _cool(name, err):
    until = time.monotonic() + min(
        err.retry_after if err.retry_after else
        _BACKOFF.get(err.kind, 60), 12 * 3600)
    with _COOL_LOCK:
        _COOLDOWNS[name] = until
    log.info("tts: %s cooling %ss (%s)", name,
             int(until - time.monotonic()), err.kind)


def tts_order(s):
    raw = (s.tts_order or "").split(",")
    names = [n.strip() for n in raw if n.strip() in _TTS]
    return names or list(_TTS)


def _record_usage(s, name: str, n: int = 1, scope: str = "owner-chat"):
    """Best-effort daily counter in voice_usage for /status; B2 also
    charges the budget ledger (tts_chars:<p>, stt_sec) under scope."""
    try:
        from . import db as dbmod
        from .util import utcnow
        conn = dbmod.connect(s.db_path)
        try:
            dbmod.migrate(conn)
            conn.execute(
                "INSERT INTO voice_usage(name, day, n) VALUES(?,?,?)"
                " ON CONFLICT(name, day) DO UPDATE SET n=n+? ",
                (name, utcnow().strftime("%Y-%m-%d"), n, n))
            try:
                from . import budget as _budget
                if name.startswith("tts_chars:"):
                    _budget.charge(
                        conn, scope, name, n, s=s)
                elif name.startswith("stt:"):
                    _budget.charge(conn, scope, "stt_sec", n, s=s)
            except Exception:
                pass
        finally:
            conn.close()
    except Exception:
        pass


# V7: per-provider daily synthesised-char budgets. Cloudflare's free
# ~10k neurons/day is roughly 3.6k spoken chars; Gemini TTS 429s under
# call load, so calls reserve it for voice notes unless configured.
_TTS_CHAR_DEFAULTS = {"cloudflare": 3000, "gemini": 1500,
                      "azure": 20000, "fish": 20000}


def tts_char_budget(s, env, name):
    """Daily synthesised-char budget for a provider. 0 = unlimited.
    PA_TTS_CHARS_<NAME>_DAY overrides the defaults above;
    settings.tts_chars_day overrides them all."""
    v = (env.get(f"PA_TTS_CHARS_{name.upper()}_DAY") or "").strip()
    if v:
        try:
            return int(v)
        except ValueError:
            pass
    g = getattr(s, "tts_chars_day", 0) or 0
    return g if g > 0 else _TTS_CHAR_DEFAULTS.get(name, 0)


def tts_chars_used(s, name):
    """Chars synthesised by `name` today (best-effort read)."""
    try:
        from . import db as dbmod
        from .util import utcnow
        conn = dbmod.connect(s.db_path)
        try:
            row = conn.execute(
                "SELECT n FROM voice_usage WHERE name=? AND day=?",
                (f"tts_chars:{name}",
                 utcnow().strftime("%Y-%m-%d"),)).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def tts_chars_remaining(s, env, name):
    """Chars left for `name` today; None = unlimited."""
    b = tts_char_budget(s, env, name)
    if not b or b <= 0:
        return None
    return max(0, b - tts_chars_used(s, name))


def provider_states(s, env):
    """TTS health for /status + failure alerts:
    {name: {configured, missing, cooling_s}} in PA_TTS_ORDER order."""
    out = {}
    for name in tts_order(s):
        required, _fn = _TTS[name]
        missing = [k for k in required
                   if not (env.get(k) or "").strip()]
        out[name] = {
            "configured": not missing,
            "missing": missing,
            "cooling_s": max(0, int(_cool_until(name)
                                    - time.monotonic()))}
    return out


def stt_states(s, env):
    """STT health for /status: {name: {configured, missing}}."""
    out = {}
    for name, (required, _fn) in _STT.items():
        missing = [k for k in required
                   if not (env.get(k) or "").strip()]
        out[name] = {"configured": not missing, "missing": missing}
    return out


def call_tts_order(s):
    """PA_CALL_TTS_ORDER — the in-call provider preference. Calls get
    their own order (V7 default azure,cloudflare,gemini: Gemini TTS
    429s under call load so it's last — it's the voice-note provider)
    because ONE provider is locked per call — mixing voices mid-call
    sounds glitchy."""
    raw = (getattr(s, "call_tts_order", "") or "").split(",")
    names = [n.strip() for n in raw if n.strip() in _TTS]
    return names or ["azure", "cloudflare", "gemini"]


def _synth_convert(s, env, text, style, order, convert,
                   scope="owner-chat"):
    """The provider walk shared by synthesize()/synthesize_pcm():
    convert(audio, in_args) turns provider bytes into the final form;
    returns (converted, provider). Raises VoiceError when every
    provider is unusable. `scope` is the budget-ledger scope the
    synth charges (call turns pass 'call')."""
    text = (text or "").strip()[:_MAX_TTS_CHARS]
    if not text:
        raise VoiceError("empty text", kind="bad_response")
    reasons = []
    for name in (order or tts_order(s)):
        if name not in _TTS:
            continue
        required, fn = _TTS[name]
        missing = [k for k in required if not (env.get(k) or "").strip()]
        if missing:
            reasons.append(f"{name}:needs config({','.join(missing)})")
            continue
        left = _cool_until(name) - time.monotonic()
        if left > 0:
            reasons.append(f"{name}:cooling({int(left)}s)")
            continue
        try:
            audio, in_args = fn(s, env, text, style)
            out = convert(audio, in_args)
            _record_usage(s, f"tts:{name}")
            _record_usage(s, f"tts_chars:{name}", n=len(text),
                          scope=scope)
            return out, name
        except VoiceError as e:
            _cool(name, e)
            reasons.append(f"{name}:{e.kind}")
            log.info("tts: %s failed (%s), trying next", name, e)
            continue
        except Exception as e:
            _cool(name, VoiceError(str(e), kind="bad_response"))
            reasons.append(f"{name}:bad_response")
            log.info("tts: %s failed (%r), trying next", name, e)
            continue
    raise VoiceError("no usable TTS provider (" + "; ".join(reasons)
                     + ")", kind="unavailable")


def synthesize(s, env, text, style=None, convert=None, order=None,
               scope="owner-chat"):
    """-> (ogg_opus_bytes, duration_secs, waveform_b64, provider).

    Walks the provider chain; unconfigured/cooling providers are
    skipped, errors cool a provider down and the NEXT provider is
    tried. Raises VoiceError only when every provider is unusable.
    `convert` is the to_voice_ogg seam for tests; `order` overrides
    PA_TTS_ORDER (CallTTS pins a single provider this way)."""
    if convert is None:
        convert = lambda a, ia: to_voice_ogg(  # noqa: E731
            a, ia, s.ffmpeg_bin)
    (ogg, dur, wave), name = _synth_convert(
        s, env, text, style, order, convert, scope=scope)
    log.info("tts: %s synthesised %.1fs", name, dur)
    return ogg, dur, wave, name


def synthesize_pcm(s, env, text, order=None, convert=None,
                   scope="owner-chat"):
    """V2 in-proc call playback: -> (s16le stereo 48 kHz PCM, provider).

    Raw-PCM providers (Gemini l16, Live) resample via audioop — no
    ffmpeg at all; mp3/wav providers take ONE ffmpeg decode here at
    synthesis time, never at play time."""
    if convert is None:
        convert = lambda a, ia: to_pcm48k_stereo(  # noqa: E731
            a, ia, s.ffmpeg_bin)
    return _synth_convert(s, env, text, None, order, convert,
                          scope=scope)


# ---------------------------------------------------------------------------
# CallTTS: one provider locked per call + persistent clip cache
#
# The per-turn chain (synthesize() above) is wrong for calls in two
# ways: each clip could land on a different provider (a call that
# opens in Gemini's voice and continues in Cloudflare's sounds
# glitchy), and re-synthesising ~10 static filler clips every call
# burns a free-tier daily quota in seconds. CallTTS fixes both: the
# first usable entry of call_tts_order() is locked for the whole
# call — a hard failure permanently advances the lock — and
# say_cached() serves static clips (fillers, the greeting, fixed
# lines) from <cache_dir>/<provider>-<voice>-<sha(text)>.ogg so they
# are synthesised once ever, then reused across calls.

def _voice_id(s, name):
    """The voice selection of a provider — part of the cache key so a
    voice change re-synthesises instead of serving a stale clip."""
    return {
        "gemini": getattr(s, "tts_gemini_voice", ""),
        "fish": getattr(s, "tts_fish_voice", "")
                or getattr(s, "tts_fish_model", ""),
        "azure": getattr(s, "tts_azure_voice", ""),
        "cloudflare": getattr(s, "tts_cf_speaker", ""),
    }.get(name, "")


def _slug(v):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(v or ""))[:48] or "x"


def clip_cache_path(cache_dir, provider, voice, text, ext=".ogg"):
    sha = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]
    return os.path.join(
        cache_dir, f"{_slug(provider)}-{_slug(voice)}-{sha}{ext}")


class CallTTS:
    """Per-call TTS: a single locked provider + a disk clip cache.

    - say(text)         — dynamic text (replies): synthesised fresh
      with the locked provider, never cached.
    - say_cached(text)  — static text (fillers, fixed lines): served
      from the disk cache; misses synthesise once, then persist.
    Both return ogg bytes or None (failures are logged, never raised)
    and both always speak in the SAME provider voice within a call.
    """

    def __init__(self, s, env, cache_dir=None, order=None, convert=None,
                 fmt="ogg"):
        self.s = s
        self.env = env or {}
        self._order = list(order or call_tts_order(s))
        self._convert = convert
        # 'ogg' (voice-note shape, ffmpeg playback) | 'pcm' (s16le
        # stereo 48 kHz — the V2 in-proc CallAudioSource path)
        self._fmt = fmt
        self._ext = ".pcm" if fmt == "pcm" else ".ogg"
        self._cache_dir = cache_dir if cache_dir is not None else (
            getattr(s, "voice_cache_dir", "") or
            os.path.join(getattr(s, "data_dir", "data"), "voice-cache"))
        self._provider = None          # locked provider name
        self._lock = threading.Lock()
        self._path_locks = {}          # cache path -> write lock
        self.budget_scope = "call"     # B2: calls charge the call pot

    # -- provider lock ------------------------------------------------
    @property
    def provider(self):
        with self._lock:
            return self._provider

    def _usable(self, name):
        required, _fn = _TTS[name]
        if not all((self.env.get(k) or "").strip() for k in required):
            return False
        if _cool_until(name) > time.monotonic():
            return False
        # V7: a provider whose daily char budget is spent is skipped
        # before the lock, not after a failed synth
        left = tts_chars_remaining(self.s, self.env, name)
        if left is not None and left <= 0:
            return False
        return True

    def _next_usable(self, after=None):
        start = self._order.index(after) + 1 \
            if after in self._order else 0
        for name in self._order[start:]:
            if self._usable(name):
                return name
        return None

    def _pick(self):
        """The locked provider; on first call, the first usable one in
        the call order (keys present, not cooling)."""
        with self._lock:
            if self._provider is None:
                self._provider = self._next_usable()
                if self._provider:
                    log.info("call tts: provider locked to %s (order %s)",
                             self._provider, ",".join(self._order))
            return self._provider

    def _failover(self, name):
        """`name` hard-failed — permanently move the lock down the call
        order. Another thread may already have switched; then keep
        theirs."""
        with self._lock:
            if self._provider != name:
                return self._provider
            self._provider = self._next_usable(after=name)
            if self._provider:
                log.info("call tts: %s failed — voice locked to %s "
                         "for the rest of the call",
                         name, self._provider)
            else:
                log.warning("call tts: %s failed — no usable provider "
                            "left (order %s)", name, ",".join(self._order))
            return self._provider

    # -- synthesis ----------------------------------------------------
    def _synth(self, text):
        """-> (ogg|pcm bytes|None, provider_used|None)."""
        name = self._pick()
        reasons = []
        while name:
            try:
                if self._fmt == "pcm":
                    return synthesize_pcm(
                        self.s, self.env, text, order=[name],
                        convert=self._convert,
                        scope=self.budget_scope)
                ogg, _d, _w, prov = synthesize(
                    self.s, self.env, text, order=[name],
                    convert=self._convert, scope=self.budget_scope)
                return ogg, prov
            except VoiceError as e:
                reasons.append(f"{name}:{e.kind}")
                log.info("call tts: %s failed (%s), trying next", name, e)
                name = self._failover(name)
            except Exception as e:
                reasons.append(f"{name}:{e!r}")
                log.info("call tts: %s failed (%r), trying next", name, e)
                name = self._failover(name)
        log.warning("call tts failed: no usable provider (%s)",
                    "; ".join(reasons) or "none in order")
        return None, None

    def say(self, text):
        """Dynamic line: synthesised fresh on the locked provider."""
        text = (text or "").strip()[:_MAX_TTS_CHARS]
        if not text:
            return None
        return self._synth(text)[0]

    def say_cached(self, text):
        """Static line: disk cache first; a miss synthesises once and
        persists, so fillers stop burning TTS quota every call. The
        cache is keyed by the provider the clip was ACTUALLY made with
        — after a failover the lookup moves to the new voice, keeping
        the whole call single-voiced."""
        text = (text or "").strip()[:_MAX_TTS_CHARS]
        if not text:
            return None
        name = self._pick()
        if name:
            path = clip_cache_path(
                self._cache_dir, name, _voice_id(self.s, name), text,
                ext=self._ext)
            try:
                with open(path, "rb") as f:
                    ogg = f.read()
                log.info("tts: cache hit %s", os.path.basename(path))
                return ogg
            except OSError:
                pass
            lock = self._path_locks.setdefault(path, threading.Lock())
        else:
            path = None
            lock = threading.Lock()
        # double-checked under a per-path lock: two callers warming the
        # same missing clip synthesise it once, not twice
        with lock:
            if path:
                try:
                    with open(path, "rb") as f:
                        return f.read()
                except OSError:
                    pass
            ogg, used = self._synth(text)
            if ogg and used:
                try:
                    os.makedirs(self._cache_dir, exist_ok=True)
                    dst = clip_cache_path(
                        self._cache_dir, used,
                        _voice_id(self.s, used), text, ext=self._ext)
                    tmp = dst + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(ogg)
                    os.replace(tmp, dst)
                except OSError as e:
                    log.warning("tts: cache write failed: %s", e)
            return ogg


# ---------------------------------------------------------------------------
# STT: owner audio -> text

def _stt_groq(s, env, audio, filename, content_type, verbose=False):
    """verbose=True asks for verbose_json and returns (text, meta):
    meta carries max segments[].no_speech_prob + min avg_logprob so the
    call path can drop Whisper hallucinations-on-noise."""
    body, ctype = encode_multipart(
        [("model", s.whisper_model),
         ("response_format", "verbose_json" if verbose else "json")],
        [("file", filename, content_type or "audio/ogg", audio)])
    try:
        _h, data = _client(s.groq_whisper_base).request(
            "POST", "/audio/transcriptions", body=body,
            headers={"Authorization": f"Bearer {env['GROQ_API_KEY']}",
                     "Content-Type": ctype})
    except HTTPError as e:
        _raise_http("groq-whisper", e)
    except TransportError as e:
        raise VoiceError(f"groq-whisper: {e}")
    # request() returns the raw body; Groq answers JSON {"text": ...}
    try:
        obj = json.loads(data or b"{}")
        text = obj.get("text")
    except ValueError:
        raise VoiceError("groq-whisper: non-JSON response",
                         kind="bad_response")
    text = (text or "").strip()
    if not verbose:
        return text
    meta = {}
    segs = obj.get("segments") or []
    nsp = [g.get("no_speech_prob") for g in segs
           if g.get("no_speech_prob") is not None]
    alp = [g.get("avg_logprob") for g in segs
           if g.get("avg_logprob") is not None]
    if nsp:
        meta["no_speech_prob"] = max(nsp)
    if alp:
        meta["avg_logprob"] = min(alp)
    return text, meta


def _stt_cloudflare(s, env, audio, filename, content_type):
    acct = env["CLOUDFLARE_ACCOUNT_ID"].strip()
    path = f"/accounts/{acct}/ai/run/{s.cf_whisper_model}"
    headers = {"Authorization": f"Bearer {env['CLOUDFLARE_API_TOKEN']}"}
    # whisper-large-v3-turbo takes base64 audio over REST; other CF
    # whisper models take a byte array. Try b64 first, then int array.
    for payload in ({"audio": base64.b64encode(audio).decode()},
                    {"audio": list(audio)}):
        try:
            _h, data = _client(s.cloudflare_ai_base).post_json(
                path, payload, headers=headers)
        except HTTPError as e:
            last = e
            continue
        except TransportError as e:
            raise VoiceError(f"cf-whisper: {e}")
        result = (data or {}).get("result") or data or {}
        text = (result.get("text") or "").strip()
        if text:
            return text
        last = None
    if last:
        _raise_http("cf-whisper", last)
    raise VoiceError("cf-whisper: empty transcript", kind="bad_response")


_STT = {
    "groq": (("GROQ_API_KEY",), _stt_groq),
    "cloudflare": (("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"),
                   _stt_cloudflare),
}


def transcribe(s, env, audio, filename="voice.ogg", content_type=""):
    """Owner audio -> (text, provider). Groq first, then Cloudflare."""
    if not audio:
        raise VoiceError("empty audio", kind="bad_response")
    reasons = []
    for name, (required, fn) in _STT.items():
        missing = [k for k in required if not (env.get(k) or "").strip()]
        if missing:
            reasons.append(f"{name}:needs config")
            continue
        try:
            text = fn(s, env, audio, filename, content_type)
            if text:
                _record_usage(s, f"stt:{name}")
                return text, name
            reasons.append(f"{name}:empty")
        except VoiceError as e:
            reasons.append(f"{name}:{e.kind}")
            log.info("stt: %s failed (%s), trying next", name, e)
            continue
    raise VoiceError("no usable STT provider (" + "; ".join(reasons)
                     + ")", kind="unavailable")


def _stt_cf_meta(s, env, audio, filename, content_type, verbose=False):
    return _stt_cloudflare(s, env, audio, filename, content_type), {}


# same provider order as _STT but each fn returns (text, meta)
_STT_META = {
    "groq": (("GROQ_API_KEY",), _stt_groq),
    "cloudflare": (("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"),
                   _stt_cf_meta),
}


def transcribe_meta(s, env, audio, filename="voice.ogg",
                    content_type=""):
    """transcribe() + per-provider metadata -> (text, provider, meta).
    Used by the call path for hallucination filtering; meta is {} for
    providers without segment stats."""
    if not audio:
        raise VoiceError("empty audio", kind="bad_response")
    reasons = []
    for name, (required, fn) in _STT_META.items():
        missing = [k for k in required if not (env.get(k) or "").strip()]
        if missing:
            reasons.append(f"{name}:needs config")
            continue
        try:
            out = fn(s, env, audio, filename, content_type, verbose=True)
            text, meta = out if isinstance(out, tuple) else (out, {})
            if text:
                _record_usage(s, f"stt:{name}")
                return (text or "").strip(), name, meta or {}
            reasons.append(f"{name}:empty")
        except VoiceError as e:
            reasons.append(f"{name}:{e.kind}")
            log.info("stt: %s failed (%s), trying next", name, e)
            continue
    raise VoiceError("no usable STT provider (" + "; ".join(reasons)
                     + ")", kind="unavailable")


# ---------------------------------------------------------------------------
# Discord plumbing shared by discordbot + call.py

def attachment_is_audio(att: dict) -> bool:
    """True when a Discord attachment is audio: a voice-message ogg or
    any audio/* file (owner voice notes, shared clips)."""
    ctype = (att.get("content_type") or "").lower()
    name = (att.get("filename") or "").lower()
    return ctype.startswith("audio/") or name.endswith(
        (".ogg", ".mp3", ".wav", ".m4a", ".opus", ".flac"))


def download_attachment(s, url: str, cap=8 * 1024 * 1024) -> bytes:
    """GET a message attachment from the Discord CDN (allowlisted
    hosts; the URL itself comes from the gateway payload)."""
    parts = urllib.parse.urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in _ATTACHMENT_HOSTS:
        raise VoiceError(f"attachment host {host!r} not allowed",
                         kind="bad_response")
    try:
        _h, data = APIClient(f"https://{host}", timeout=20).request(
            "GET", (parts.path or "/")
            + (f"?{parts.query}" if parts.query else ""))
    except HTTPError as e:
        raise VoiceError(f"attachment download: HTTP {e.status}")
    except TransportError as e:
        raise VoiceError(f"attachment download: {e}")
    if len(data) > cap:
        raise VoiceError("attachment too large")
    return data


class VoiceServices:
    """What chat tools can reach: voice notes out + owner calls.

    `api` posts to Discord; `call_manager` (may be a fake in tests)
    runs the guarded call flow; `pref` reads the owner toggle
    (pa_settings 'voice_notes')."""

    def __init__(self, settings, env, api, call_manager=None,
                 db_path=None, voice_pref=None):
        self.s = settings
        self.env = env
        self.api = api
        self.call_manager = call_manager
        self.db_path = db_path
        self._voice_pref = voice_pref

    def voice_notes_on(self) -> bool:
        if self._voice_pref is not None:
            return bool(self._voice_pref())
        return bool(self.s.voice_notes_default)

    def send_voice_note(self, channel_id, text, style=None) -> dict:
        """Synthesize + post a native Discord voice message."""
        if self.api is None:
            return {"error": "not_configured",
                    "message": "Discord API is not configured"}
        try:
            ogg, dur, wave, provider = synthesize(self.s, self.env, text,
                                                  style=style)
        except VoiceError as e:
            return {"error": "tts_failed", "message": str(e)[:300],
                    "kind": e.kind}
        ok = self.api.post_voice_message(channel_id, ogg, dur, wave)
        if not ok:
            return {"error": "send_failed",
                    "message": "Discord rejected the voice message"}
        return {"sent": True, "provider": provider,
                "duration_secs": dur}

    def call_owner(self, message, urgent=False, source="chat") -> dict:
        if self.call_manager is None:
            return {"error": "not_configured",
                    "message": "calls need PA_CALL_GUILD_ID + the "
                               "voice extras (see docs/VOICE.md)"}
        return self.call_manager.call(message, urgent=urgent,
                                      source=source)

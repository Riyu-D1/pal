"""Lightweight video understanding (docs/PARITY.md #19).

video_look(path) — OFF by default (PA_VIDEO=true). Samples up to 4
frames out of a sandbox video with ffmpeg (only if it is already
installed — nothing new to install), captions each with the
configured vision chain, and returns one fenced summary.

Caps: file ≤ PA_VIDEO_MAX_MB (default 50), path must resolve inside
the chat sandbox, ffmpeg runs with a hard timeout, every caption is
untrusted data.
"""

import os
import subprocess
import tempfile

from . import vision as visionmod
from .untrusted import untrusted

FRAMES = 4


def _resolve(s, ref: str):
    root = os.path.realpath(s.chat_sandbox)
    path = os.path.realpath(ref or "")
    if not path.startswith(root + os.sep):
        return None
    return path


def _have_ffmpeg() -> bool:
    import shutil
    return shutil.which("ffmpeg") is not None


def _duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries",
             "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15)
        return max(1.0, float(out.stdout.strip() or 10))
    except Exception:
        return 10.0


def look(s, env, ref: str) -> dict:
    if not getattr(s, "video_enabled", False):
        return {"error": "disabled",
                "message": "video understanding is off (PA_VIDEO)"}
    path = _resolve(s, ref)
    if path is None:
        return {"error": "bad_arguments",
                "message": "video_look only reads inside the chat "
                           "sandbox"}
    max_b = int(getattr(s, "video_max_mb", 50)) * 1024 * 1024
    try:
        if os.path.getsize(path) > max_b:
            return {"error": "too_large"}
        if not os.path.isfile(path):
            return {"error": "not_found"}
    except OSError as e:
        return {"error": "not_found", "message": str(e)[:150]}
    if not _have_ffmpeg():
        return {"error": "no_ffmpeg",
                "message": "ffmpeg isn't installed — video sampling "
                           "needs it"}
    dur = _duration(path)
    frames = []
    with tempfile.TemporaryDirectory(dir=s.chat_sandbox) as td:
        for i in range(FRAMES):
            t = round(dur * (i + 0.5) / FRAMES, 1)
            out = os.path.join(td, f"f{i}.jpg")
            try:
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-ss", str(t), "-i",
                     path, "-frames:v", "1", "-q:v", "5", "-y", out],
                    capture_output=True, timeout=20)
            except Exception:
                continue
            if os.path.isfile(out) and os.path.getsize(out):
                with open(out, "rb") as f:
                    frames.append((t, f.read()))
    if not frames:
        return {"error": "no_frames",
                "message": "couldn't sample any frames"}
    caps = []
    for t, data in frames:
        cap = visionmod.describe_data(s, env, data, "frame.jpg")
        caps.append(f"[{t:>5.1f}s] " + (cap or "(no caption)"))
    name = os.path.basename(path)
    return {"file": name, "duration_s": round(dur, 1),
            "frames": len(frames),
            "summary": untrusted(
                f"VIDEO FRAMES {name}",
                "\n".join(caps) + "\n(frame captions only — sound "
                "and motion between them are unseen)")}

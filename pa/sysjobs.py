"""System jobs — seeded agent_job schedules handled by code, not a
generic agent turn.

The scheduler fires them into chat_inbox like any agent_job (so /jobs
lists and toggles them identically). The Discord bot's job drain looks
the job name up here; a hit runs the Python handler instead of a ReAct
turn. Handler signature: handler(bot, payload_dict) -> str — a one-line
status recorded on the inbox row; the handler posts its own Discord
output. An exception marks the row 'failed' (failure alerts key off
that) and DMs the owner.

is_system() also guards chat tools so a prompt can't clobber or
impersonate a managed job's schedule.
"""

import logging

log = logging.getLogger("pa.sysjobs")

_NAMES = ("inbox-watch", "morning-briefing", "daily-journal",
          "monitor-tick", "scheduled-backup", "evolve-detect",
          "evolve-digest", "evolve-reflect",
          # (docs/FEATURES.md)
          "proactive-tick", "recall-index", "kg-build",
          "cal-triage",
          # (docs/DR.md)
          "dr-check", "evolve-practice",
          # (docs/FEATURES.md)
          "mail-inbound", "coursework-watch", "notion-index")


def handler_for(name: str):
    if name == "inbox-watch":
        from . import inboxwatch
        return inboxwatch.run
    if name == "morning-briefing":
        from . import briefing
        return briefing.run
    if name == "daily-journal":
        from . import journal
        return journal.run
    if name == "monitor-tick":
        from . import monitors
        return monitors.run
    if name == "scheduled-backup":
        from . import backupjob
        return backupjob.run
    if name == "evolve-detect":
        from .evolve import detect
        return detect.run
    if name == "evolve-digest":
        from .evolve import digest
        return digest.run
    if name == "evolve-reflect":
        from .evolve import reflect
        return reflect.run
    if name == "proactive-tick":
        from . import proactive
        return proactive.job
    if name == "recall-index":
        from . import recall
        return recall.job
    if name == "kg-build":
        from . import kg
        return kg.job
    if name == "cal-triage":
        from . import caltriage
        return caltriage.run
    if name == "dr-check":
        from . import drcheck
        return drcheck.job
    if name == "evolve-practice":
        from .evolve import practice
        return practice.run
    if name == "mail-inbound":
        from . import mailinbound
        return mailinbound.run
    if name == "coursework-watch":
        from . import coursework
        return coursework.run
    if name == "notion-index":
        from . import notionindex
        return notionindex.run
    return None


def is_system(name: str) -> bool:
    return name in _NAMES


def model_text(bot, conn, prompt: str, budget: float = 25.0,
               tier: str = "fast") -> str:
    """One cheap model call -> reply text, '' when no brain is wired or
    the call fails. System jobs never let a model failure kill them."""
    try:
        eng = bot._engine()
        eng._resolve_brains(conn)
        out = eng._brain.complete(
            [{"role": "user", "content": prompt}], budget, tier)
        return (out or "").strip()
    except Exception as e:
        log.info("sysjob model call failed: %s", e)
        return ""


def owner_dm(bot) -> str:
    """The owner's DM channel id ('' until Discord has created one)."""
    try:
        return str(bot._owner_dm_id() or "")
    except Exception:
        return ""


def dm_and_voice(bot, text: str, voice: bool = True,
                 target: str = "") -> str:
    """Post text + (best-effort) a voice note of it to the owner's DM —
    the standard 'reach the owner' path for system jobs."""
    if getattr(bot.s, "notify_v2", False) and not target:
        # all owner DM traffic routes through the
        # notifier (quiet-hour batching, dedupe, per-hour cap).
        from . import notify
        return notify.notify(bot, None, "dm", "normal", text)
    dm = target or owner_dm(bot)
    if not dm:
        return ""
    bot.api.post_message(dm, text[:1900])
    if voice and bot._voice is not None:
        try:
            # proactive voice obeys the same rule as replies
            # voice_notes 'on' always, 'off' never, 'auto' when busy
            from . import busy as busymod
            if busymod.voice_should_send(bot):
                bot._voice.send_voice_note(dm, text)
        except Exception:
            pass
    return dm

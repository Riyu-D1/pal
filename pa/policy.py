"""one action classifier + one decision floor for every
path that can act. classify() maps a tool call to an action_class;
decide() maps (who owns the account, class, attended?, tainted?) to a
verdict. The owner's rule is non-negotiable: writes/sends/deletes/
spends/settings/publishes on the OWNER's accounts always need an
explicit Discord approval — allow rules and unattended execution can
never cover them. Unknown tools fail closed to 'write'.

Flag: PA_POLICY_ENGINE=on (default on — it only tightens);
PA_POLICY_VAULT_UNATTENDED keeps the old vault-write exception.
"""
from dataclasses import dataclass, field

CLASSES = ("read", "draft_local", "write", "send", "delete",
           "spend", "settings", "publish", "exec", "sensitive")

# Built-in CHAT_TOOLS -> action_class. Anything missing here falls
# through to _verb_class() on the name, then 'write' — fail closed.
_BUILTIN = {
    # pure reads
    "read": {"email_list", "email_read", "calendar_list", "memory_search",
             "vault_read", "vault_search", "todo_list", "job_list",
             "watch_list", "skill_list", "web_search", "read_url",
             "fetch_url", "weather", "places", "look", "video_look",
             "app_tools", "browse_goto", "browse_snapshot",
             "browse_extract", "browse_screenshot", "browse_tabs",
             "browse_switch", "browse_close", "ask_owner",
             "need_capability", "tools_more", "goal_list",
             "project_status", "recall", "kg_query",
             "phone_sms_read", "phone_status",
             "file_list", "file_read", "file_version",
             "tool_list", "integration_list"},
    # local artifacts, nothing leaves the machine/account
    "draft_local": {"draft_email", "feedback", "automation_propose"},
    # writes that stay inside pal's own stores or a page session
    "write": {"file_write", "memory_save", "memory_correct",
              "memory_forget", "owner_fact", "todo_add", "todo_done",
              "remind_me", "vault_write", "vault_append",
              "browse_click", "browse_press", "browse_type",
              "browse_fill_secret", "generate_image", "watch_add",
              "watch_set", "delegate", "goal_report",
              "memory_block_set", "file_delete", "tool_make",
              "research", "integration_build", "integration_key"},
    # messages that leave pal toward someone
    "send": {"send_email", "email_owner", "send_voice_note",
             "call_owner", "phone_sms_send"},
    # sensitive reads from the owner's own device (phone camera,
    # location) — approval every time UNLESS a standing allow rule
    # covers them (non-hard gate by design)
    "sensitive": {"phone_photo", "phone_locate"},
    "delete": set(),
    # shell / code execution — keeps its existing approval behaviour
    "exec": {"shell", "run_python", "mac", "mac_get", "mac_put",
             "code_task"},
    # durable configuration of what pal does unsupervised
    "settings": {"self_update", "job_add", "job_set", "skill_save",
                 "goal_add", "goal_update", "goal_done",
                 "project_start", "project_stop", "automation_apply"},
    "publish": {"page_publish"},
    "spend": set(),
}
_BUILTIN_CLASS = {t: c for c, names in _BUILTIN.items() for t in names}

# verb -> class for Composio slugs / MCP tool names. Order matters:
# the first matching group wins.
_SEND_VERBS = frozenset({
    "SEND", "REPLY", "FORWARD", "MESSAGE", "POST_MESSAGE", "NOTIFY",
    "INVITE", "SHARE", "TRANSFER", "DM"})
_DELETE_VERBS = frozenset({
    "DELETE", "REMOVE", "TRASH", "DESTROY", "REVOKE", "UNSUBSCRIBE",
    "CANCEL", "DROP", "PURGE", "UNINSTALL"})
_SPEND_VERBS = frozenset({
    "PAY", "CHECKOUT", "PURCHASE", "ORDER", "SUBSCRIBE",
    "SUBSCRIPTION", "BOOK", "BUY", "CHARGE", "TOPUP", "DONATE",
    "TIP"})
_PUBLISH_VERBS = frozenset({
    "PUBLISH", "TWEET", "POST", "BROADCAST", "DEPLOY", "RELEASE"})
_WRITE_VERBS = frozenset({
    "CREATE", "MODIFY", "UPDATE", "INSERT", "ADD", "SET", "MOVE",
    "ARCHIVE", "LABEL", "MARK", "EDIT", "WRITE", "UPLOAD", "SAVE",
    "RENAME", "COPY", "STAR", "PIN", "MUTE", "BLOCK", "MERGE",
    "APPLY", "ASSIGN", "ATTEND", "RSVP", "RESPOND"})
_EXEC_VERBS = frozenset({
    "EXECUTE", "RUN", "EXEC", "SHELL", "COMMAND", "SSH", "TERMINAL"})
_READ_VERBS = frozenset({
    "FETCH", "GET", "LIST", "SEARCH", "FIND", "READ", "RETRIEVE",
    "QUERY", "CHECK", "VIEW", "LOOKUP", "DOWNLOAD", "EXPORT", "COUNT",
    "COMPARE", "VALIDATE", "CALCULATE", "CONVERT", "DESCRIBE",
    "INSPECT", "STATUS", "SHOW", "ENUMERATE", "PREVIEW"})


def _verbs(name: str) -> list:
    """UPPER_SNAKE tokens, toolkit prefix already acceptable — we scan
    every token, so GMAIL_SEND_EMAIL yields SEND...EMAIL."""
    return [t for t in name.upper().replace("-", "_").split("_") if t]


def _verb_class(tokens: list):
    """Class from the first decisive verb. Destructive/outbound verbs
    beat later read-ish tokens (FETCH_AND_DELETE is a delete)."""
    for verbs, cls in ((_DELETE_VERBS, "delete"), (_SPEND_VERBS, "spend"),
                       (_SEND_VERBS, "send"), (_EXEC_VERBS, "exec"),
                       (_PUBLISH_VERBS, "publish")):
        if any(t in verbs for t in tokens):
            return cls
    if any(t in _WRITE_VERBS for t in tokens):
        return "write"
    # a read only when a read verb is present and no mutating verb was
    # found above (FETCH_AND_DELETE never reaches here)
    if any(t in _READ_VERBS for t in tokens):
        return "read"
    return None


def classify(tool: str, args: dict = None, spec: dict = None) -> str:
    """tool -> action_class. Built-ins from the table; '<app>.<slug>'
    and Composio slugs from verb parsing (annotation alone never makes
    it a read — a lying readOnlyHint on 'sendMail' is still a send).
    Unknown -> 'write'."""
    name = (tool or "").split(".")[-1] if "." in (tool or "") else tool
    if tool in _BUILTIN_CLASS:
        return _BUILTIN_CLASS[tool]
    spec = spec or {}
    ann = spec.get("annotations") or {}
    tokens = _verbs(name)
    cls = _verb_class(tokens)
    if cls is None:
        return "write"
    if cls == "read":
        # MCP rule: read only when the annotation AND the verb agree.
        if "." in (tool or "") or spec:
            hint = ann.get("readOnlyHint") or ann.get("read_only_hint") \
                or spec.get("read_only")
            return "read" if hint else "write"
        return "read"
    return cls


@dataclass
class Decision:
    verdict: str            # allow | approve | deny
    reason: str
    action_class: str = "write"
    account_owner: str = "owner"     # owner | pal
    risk: str = ""
    queued: bool = False


# built-ins that act on pal's OWN stores (memory, vault, todos,
# watches, files on its disk) — free within budgets, not owner-gated
_PAL_OWNED = frozenset({
    "file_write", "file_delete", "tool_make", "tool_run",
    "memory_save", "memory_correct", "memory_forget",
    "owner_fact", "todo_add", "todo_done", "vault_write",
    "vault_append", "remind_me", "watch_add", "watch_set",
    "skill_save", "feedback", "generate_image", "job_add", "job_set",
    "self_update", "need_capability", "delegate", "research",
    "integration_build", "integration_key", "integration_list",
    "browse_click",
    "browse_press", "browse_type", "browse_fill_secret",
    "goal_add", "goal_list", "goal_update", "goal_done",
    "goal_report",
    "project_start", "project_status", "project_stop",
    "automation_propose", "automation_apply",
    "memory_block_set"})

# pal -> owner channels (voice note, call, email TO the owner): the
# recipient is the owner, not a third party — always free
_OWNER_CHANNEL = frozenset({
    "send_voice_note", "call_owner", "email_owner", "ask_owner",
    "phone_notify", "phone_say"})


def _made_tool_class(conn, args) -> str:
    """tool_run's manifest-aware class: 'write' for pure tools,
    'exec' for ones that may fetch — except integration tools:
    read_only endpoints are reads (owner-tagged included), and a
    pal-tagged integration's writes are pal-owned writes. None =
    unknown tool, keep the verb-derived class."""
    try:
        from .util import json_loads_bounded
        row = conn.execute(
            "SELECT manifest_json FROM made_tools WHERE name=? AND"
            " status='active' ORDER BY version DESC LIMIT 1",
            (str((args or {}).get("name") or ""),)).fetchone()
        if row is None:
            return ""
        m = json_loads_bounded(row["manifest_json"]) or {}
        if m.get("read_only"):
            return "read"
        if m.get("integration"):
            prov = str(m.get("integration_provider") or "")
            return "write" if prov.endswith("@pal") else "exec"
        return "exec" if m.get("network") else "write"
    except Exception:
        return ""


def _account_owner(conn, tool: str) -> str:
    """Which side of the boundary the target account is on. App tools
    resolve their provider row; pal-labelled providers and pal's own
    built-in stores are 'pal'; everything else is the owner's."""
    if tool in _PAL_OWNED or tool in _OWNER_CHANNEL:
        return "pal"
    try:
        from . import connectflows
        provider = tool.split(".", 1)[0] if "." in tool else ""
        if provider:
            row = connectflows.get(conn, provider)
            if row is not None and row.get("owner_kind") == "pal":
                return "pal"
            if provider.endswith("@pal"):
                return "pal"
            return "owner"
    except Exception:
        return "owner"
    return "owner"


_APPROVE_ALWAYS = frozenset(
    {"write", "send", "delete", "spend", "settings", "publish"})


def decide(conn, tool: str, args: dict = None, *,
           unattended: bool = False, tainted: bool = False,
           allow_rule: bool = False, approved_replay: bool = False,
           account_owner: str = "", spec: dict = None,
           action_class: str = "", vault_unattended: bool = True,
           source: str = "") -> Decision:
    """The hard floor. Called by every action path when the policy
    engine is on; callers keep their UX (approval card, queue) keyed
    off verdict.

    1. owner account x non-read -> approve, always; allow rules and
       unattended execution cannot cover it.
    2. spend anywhere -> approve.
    3. pal account -> allow within budget; a send to a *new* third
       party -> approve once (allow-rule-able thereafter).
    4. exec -> approve (existing behaviour).
    5. taint upgrades any allow -> approve for non-read actions."""
    # action_class: trusted callers that carry their own classification
    # (pa/nodes cap map) can pass it; it is never read from a spec, so
    # an untrusted tool can never relabel itself.
    cls = action_class or classify(tool, args, spec)
    if tool == "tool_run":
        # a registered pure tool is pal-owned write (the owner's
        # approval already covered its code at registration); only a
        # manifest.network tool keeps exec's per-run approval.
        cls = _made_tool_class(conn, args) or cls
    if approved_replay:
        return Decision("allow", "already approved by the owner",
                        cls, risk="replay")
    owner = account_owner or _account_owner(conn, tool)
    # UNATTENDED_FREE: vault writes keep working unattended (flagged).
    if unattended and tool in ("vault_write", "vault_append"):
        if vault_unattended:
            return Decision(
                "allow", "vault writes are unattended-free",
                cls, owner)
        return _pend(unattended, cls, owner,
                     "vault writes need the owner "
                     "(PA_POLICY_VAULT_UNATTENDED=off)",
                     tainted, source)
    if cls == "read" or cls == "draft_local":
        return Decision("allow", f"{cls} is free", cls, owner)
    if tool == "shell":
        # readonly shell stays free (existing behaviour); anything
        # else is exec -> approve
        try:
            from . import sandbox
            if sandbox.is_readonly(
                    str((args or {}).get("command") or "")):
                return Decision("allow", "read-only shell command",
                                cls, owner)
        except Exception:
            pass
    if tool in _OWNER_CHANNEL:
        return Decision("allow", "pal -> owner channel", cls, "pal")
    if cls == "spend":
        return _pend(unattended, cls, owner,
                     "spending money always needs the owner",
                     tainted, source)
    if cls == "sensitive":
        # reads from the owner's device (camera/location): approval
        # every time, but NOT a hard gate — a standing allow rule may
        # cover them because the data only ever flows to the owner.
        return _pend(unattended, cls, owner,
                     "reads sensitive data from your device",
                     tainted, source)
    if owner == "owner" and cls in _APPROVE_ALWAYS:
        why = {"send": "sends from your account",
               "delete": "deletes from your account",
               "settings": "changes what pal does unsupervised",
               "publish": "publishes under your name"}.get(
                   cls, f"writes to your account")
        return _pend(unattended, cls, owner,
                     f"{why} — owner approval every time"
                     + (" (requested after reading %s)" % source
                        if tainted and source else ""),
                     tainted, source)
    if cls == "exec":
        return _pend(unattended, cls, owner,
                     "executes code/commands", tainted, source)
    # pal account
    if cls == "send":
        # first contact with a third party asks; replies/known
        # recipients may be allow-ruled later.
        if allow_rule:
            return Decision("allow", "allow rule for this send",
                            cls, owner)
        return _pend(unattended, cls, owner,
                     "first send to this recipient from pal's account",
                     tainted, source)
    if owner == "pal" and cls in ("write", "delete", "settings"):
        if tainted:
            return _pend(unattended, cls, owner,
                         "requested after reading untrusted "
                         + (source or "content"), tainted, source)
        return Decision("allow",
                        "pal's own account may act within budgets",
                        cls, owner)
    if cls == "settings":
        return _pend(unattended, cls, owner,
                     "changes pal's unattended behaviour",
                     tainted, source)
    # publish on pal account
    return _pend(unattended, cls, owner,
                 f"{cls} from pal's account", tainted, source)


def _pend(unattended, cls, owner, reason, tainted, source):
    if tainted and source and "reading" not in reason:
        reason += f" (after reading {source})"
    return Decision("pending" if unattended else "approve",
                    reason, cls, owner,
                    risk="tainted" if tainted else "",
                    queued=unattended)

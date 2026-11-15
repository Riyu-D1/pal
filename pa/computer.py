"""pal's own computer: one persistent-profile Chromium, OFF unless
PA_COMPUTER_ENABLED=true. See docs/COMPUTER.md.

- ComputerService owns at most one browser: persistent user-data-dir
  (chmod 0700, never logged), lazy launch on first call, idle-exit after
  PA_COMPUTER_IDLE_SECONDS with no live desktop session, recycle on
  crash or when the self-check RSS exceeds PA_COMPUTER_MEMORY_MB
  (informational only — the cgroup is the real cap). One lock: two chat
  turns can never drive the browser at the same time.
- PlaywrightDriver is the real browser boundary. playwright is imported
  lazily so nothing here (or CI) needs it installed; tests inject a
  fake driver via driver_factory. Playwright's sync API is thread-
  affine, so the driver hops every call onto one long-lived worker
  thread (ThreadAffined): the agent serves each HTTP request on a new
  thread, and the idle-exit watchdog is another.
- With PA_COMPUTER_URL set, pal is a client: RemoteComputer exposes the
  same call(action, args) surface to `python -m pa computer-agent`,
  which serves a ComputerService on a loopback/tailnet bind behind a
  shared bearer token (PA_COMPUTER_TOKEN) — same pattern as the
  freebuff sidecar.
- Navigation targets are vetted with the same egress policy as
  fetch_url (public http/https on 80/443 only). Element risk
  (secret/submit/upload/sensitive-flow) is classified HERE so the
  remote agent computes it too; the approval decision itself lives in
  pa/chattools.py.
"""

import base64
import glob
import http.server
import json
import logging
import os
import queue
import re
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.parse

from .egress import FetchBlocked, FetchFailed, _default_resolver, \
    _resolve, _vet_url
from .netclient import APIClient, HTTPError, TransportError
from .util import json_loads_bounded, truncate_middle

log = logging.getLogger("pa.computer")

DOCS_HINT = ("pal's computer is off — set PA_COMPUTER_ENABLED=true on "
             "the assistant host (see docs/COMPUTER.md)")

# keys that can never put text into a field — allowed even when a
# password/OTP/payment input is focused (they can't submit or type)
SAFE_NAV_KEYS = {"Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft",
                 "ArrowRight", "Home", "End", "PageUp", "PageDown"}

SNAPSHOT_MAX_NODES = 50
SNAPSHOT_TEXT_CAP = 1200
EXTRACT_TEXT_CAP = 6000
EXTRACT_MAX_LINKS = 40
SCREENSHOT_MAX_BYTES = 512 * 1024
SECRET_REFUSAL = (
    "I can't enter secrets — open /desktop and type them yourself; "
    "I'll reuse the session after.")


class ComputerError(Exception):
    """Driver/service failure; `kind` becomes the tool error kind."""
    def __init__(self, message, kind="computer_error"):
        super().__init__(message)
        self.kind = kind


def not_configured() -> dict:
    return {"error": "not_configured", "message": DOCS_HINT}


# ---------------------------------------------------------------------------
# risk classification — pure functions over element "facts" the driver
# collects with its own fixed JS (never model-supplied code). The
# chattools layer turns the risk into approval gates / refusals.

_SECRET_TYPES = {"password"}
_SECRET_AUTOCOMPLETE = ("cc-", "one-time-code", "current-password",
                        "new-password")
_SECRET_RE = re.compile(
    r"(?i)(one.?time.?code|\botp\b|\b2fa\b|\bmfa\b|verification.?code|"
    r"\bcvv\b|\bcvc\b|card.?num|ccnum|security.?code|passcode|\bssn\b|"
    r"sort.?code|account.?num|card.?exp|expiry.?date)")
_SUBMIT_RE = re.compile(
    r"(?i)\b(submit|pay|purchase|buy|order|place order|confirm|send|"
    r"transfer|checkout|sign ?in|log ?in|add card|verify|subscribe|"
    r"register|book|donate)\b")
_SENSITIVE_PAGE_RE = re.compile(
    r"(?i)(checkout|payment|billing|card.?details|banking|"
    r"verify your identity|oauth|\bsso\b|two.?factor|"
    r"one.?time passcode|identity provider|sign.?in|log.?in|"
    r"secure.?account)")

# risk kinds
RISK_NONE = "none"
RISK_SECRET = "secret"          # never typed/clicked by pal — refusal
RISK_SUBMIT = "submit"          # state-changing submit/confirm control
RISK_SENSITIVE = "sensitive"    # inside a login/checkout/IdP flow
RISK_UPLOAD = "upload"          # file-upload field
RISK_DOWNLOAD = "download"      # download link


def page_flags(url: str, title: str, text: str) -> list:
    """Page-level sensitivity flags from its URL/title/text (bounded)."""
    blob = f"{url or ''} {title or ''} {(text or '')[:1500]}"
    flags = []
    if _SENSITIVE_PAGE_RE.search(blob):
        flags.append("sensitive_flow")
    return flags


def classify(facts: dict):
    """(risk, reason) for one element's facts dict. 'none' means the
    action looks safe; everything else is gated or refused upstream."""
    if not facts.get("found"):
        return RISK_NONE, ""
    tag = facts.get("tag") or ""
    if tag == "input" and (facts.get("type") or "") == "file":
        return RISK_UPLOAD, "file-upload field"
    ac = (facts.get("autocomplete") or "").lower()
    blob = " ".join(str(facts.get(k) or "")
                    for k in ("name", "text", "autocomplete",
                              "form_text", "title"))
    if (facts.get("type") in _SECRET_TYPES or
            any(ac.startswith(p) for p in _SECRET_AUTOCOMPLETE) or
            _SECRET_RE.search(blob)):
        return RISK_SECRET, "password/OTP/payment field"
    ctx = (f"{facts.get('url') or ''} {facts.get('title') or ''} "
           f"{facts.get('form_text') or ''}")
    if (facts.get("form_has_secret") or
            _SENSITIVE_PAGE_RE.search(ctx)) and (
            facts.get("in_form") or tag in
            ("a", "button", "input", "select", "textarea")):
        return RISK_SENSITIVE, \
            "inside a login/checkout/identity flow"
    if facts.get("download"):
        return RISK_DOWNLOAD, "download link"
    method = (facts.get("form_method") or "get").lower()
    if ((facts.get("type") == "submit" and method == "post") or
            _SUBMIT_RE.search(blob)):
        return RISK_SUBMIT, "submits a form or confirms an action"
    return RISK_NONE, ""


def press_risk(key: str, facts: dict):
    """classify() for a keypress on the focused element. Navigation
    keys are safe even on secret fields; typing keys are not."""
    risk, reason = classify(facts)
    if risk == RISK_SECRET and key in SAFE_NAV_KEYS:
        return RISK_NONE, ""
    if risk != RISK_NONE:
        return risk, reason
    if key == "Enter" and facts.get("in_form") and \
            (facts.get("form_method") or "get").lower() == "post":
        return RISK_SUBMIT, "Enter submits a POST form"
    return RISK_NONE, ""


# ---------------------------------------------------------------------------
# driver boundary

# Fixed JS snippets the driver evaluates. These are OUR strings — the
# tool layer never passes model-written code to page.evaluate.

_FACT_JS = """el => {
  const tag = (el.tagName || '').toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  const form = el.closest('form');
  const r = el.getBoundingClientRect();
  const f = {
    tag: tag, type: type,
    role: el.getAttribute('role') || '',
    name: ((el.getAttribute('aria-label') ||
            el.getAttribute('placeholder') || el.getAttribute('name') ||
            el.getAttribute('id') || '') + '').slice(0, 120),
    text: ((el.innerText || el.value || '') + '').slice(0, 120),
    href: el.getAttribute('href') || '',
    download: el.hasAttribute('download'),
    accept: el.getAttribute('accept') || '',
    autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
    in_form: !!form,
    form_method: form ? (form.getAttribute('method') || 'get')
                         .toLowerCase() : '',
    form_has_secret: !!(form && form.querySelector(
        'input[type="password"],input[autocomplete*="cc-"],' +
        'input[autocomplete="one-time-code"]')),
    form_text: form ? ((form.innerText || '') + '').slice(0, 400) : '',
    y: Math.round(r.top)
  };
  return f;
}"""

_TAG_JS = """() => {
  const F = %s;
  document.querySelectorAll('[data-pa-ref]').forEach(
      e => e.removeAttribute('data-pa-ref'));
  const els = document.querySelectorAll(
      'a[href],button,input,select,textarea,[role="button"],' +
      '[role="link"],[role="checkbox"],[role="tab"],summary,' +
      '[contenteditable="true"]');
  const out = [];
  let i = 0;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    i++;
    const ref = 'e' + i;
    el.setAttribute('data-pa-ref', ref);
    const f = F(el);
    f.ref = ref;
    out.push(f);
    if (out.length >= %d) break;
  }
  return out;
}""" % (_FACT_JS, SNAPSHOT_MAX_NODES)

_INSPECT_JS = """(sel) => {
  const F = %s;
  let el = null;
  try {
    el = sel ? document.querySelector(sel) : document.activeElement;
  } catch (e) {
    return {found: false};
  }
  if (!el || el === document.documentElement) return {found: false};
  const f = F(el);
  f.found = true;
  f.title = document.title || '';
  return f;
}""" % _FACT_JS

_TABLES_JS = """() => {
  const tabs = [];
  for (const t of document.querySelectorAll('table')) {
    const rows = [];
    for (const tr of t.querySelectorAll('tr')) {
      const cells = [...tr.querySelectorAll('th,td')].map(
          c => (c.innerText || '').trim());
      if (cells.length) rows.push(cells.join(' | '));
    }
    if (rows.length) tabs.push(rows.slice(0, 20).join('\\n'));
    if (tabs.length >= 5) break;
  }
  return tabs;
}"""

_LINKS_JS = """() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const text = (a.innerText || '').trim();
    if (!text) continue;
    out.push({text: text.slice(0, 80), href: a.href});
    if (out.length >= %d) break;
  }
  return out;
}""" % EXTRACT_MAX_LINKS

_REF_RE = re.compile(r"^@?e(\d+)$")


def find_browser_bin(env, settings=None) -> str:
    """Chromium executable for the headed /desktop session: explicit
    override first, then a system chromium, then the Playwright cache."""
    override = (env.get("PA_COMPUTER_BROWSER_BIN")
                or (settings.computer_browser_bin if settings else "")
                or "").strip()
    if override:
        return override
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    home = os.path.expanduser("~")
    for pat in (".cache/ms-playwright/chromium-*/chrome-linux/chrome",
                    ".cache/ms-playwright/chromium-*/chrome-mac/*"):
        hits = sorted(glob.glob(os.path.join(home, pat)))
        if hits:
            return hits[-1]
    return ""


def _proc_pss_kb(pid: str):
    """Proportional set size in kB, so shared Chrome mappings are
    not counted once per process. None if unreadable."""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        total = 0.0
        found = False
        with open(f"/proc/{pid}/smaps") as f:
            for line in f:
                if line.startswith("Pss:"):
                    total += float(line.split()[1])
                    found = True
        if found:
            return total
    except (OSError, ValueError, IndexError):
        pass
    return None


def _proc_rss_kb(pid: str):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _tree_memory_mb(profile_dir: str):
    """Best-effort memory (MB) of every process whose cmdline carries
    the profile path. Uses PSS: summing VmRSS double-counts shared
    Chrome mappings (often 2-3x unique RAM) and would recycle the
    browser after a single page against the 600 MB default.
    Informational only; the cgroup is the real cap."""
    if not profile_dir:
        return None
    tag = profile_dir.encode()
    total_kb = 0.0
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    if tag not in f.read():
                        continue
            except (OSError, ValueError):
                continue
            kb = _proc_pss_kb(pid)
            if kb is None:
                kb = _proc_rss_kb(pid)
            if kb:
                total_kb += kb
        return (total_kb / 1024.0) or None
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ps", "-axo", "rss=,args="], capture_output=True,
            text=True, timeout=5).stdout or ""
        for line in out.splitlines():
            if profile_dir in line and "rss=,args=" not in line:
                try:
                    total_kb += float(line.strip().split(None, 1)[0])
                except (ValueError, IndexError):
                    continue
        return (total_kb / 1024.0) or None
    except Exception:
        return None



class ThreadAffined:
    """Run callables on one long-lived worker thread.

    Playwright's sync API requires every call to happen on the thread
    that started the driver. The computer-agent uses a ThreadingMixIn
    HTTP server (a new thread per request) and the idle-exit watchdog
    runs on a daemon thread, so without this the second call fails
    with "cannot switch to a different thread (which happens to have
    exited)" and Chromium is never closed.
    """

    def __init__(self, name="pa-thread"):
        self._name = name
        self._q = queue.Queue()
        self._thread = None
        self._start_lock = threading.Lock()

    def alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self):
        with self._start_lock:
            if self.alive():
                return
            ready = threading.Event()
            self._thread = threading.Thread(
                target=self._loop, name=self._name, daemon=True,
                args=(ready,))
            self._thread.start()
            if not ready.wait(15):
                raise ComputerError(
                    "browser worker thread failed to start",
                    "launch_failed")

    def _loop(self, ready):
        ready.set()
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, args, kwargs, ev, box = item
            try:
                box["ok"] = fn(*args, **kwargs)
            except BaseException as e:
                box["err"] = e
            finally:
                ev.set()

    def call(self, fn, *args, timeout=120.0, **kwargs):
        if self._thread is not None and                 threading.current_thread() is self._thread:
            return fn(*args, **kwargs)
        if not self.alive():
            raise ComputerError(
                "browser worker thread is not running",
                "computer_error")
        ev = threading.Event()
        box = {}
        self._q.put((fn, args, kwargs, ev, box))
        if not ev.wait(timeout):
            raise ComputerError(
                "browser worker thread timed out", "computer_error")
        if "err" in box:
            raise box["err"]
        return box.get("ok")

    def stop(self, timeout=15.0):
        t = self._thread
        if t is None:
            return
        self._q.put(None)
        t.join(timeout)
        self._thread = None


def _pw_method(fn):
    """Hop a PlaywrightDriver method onto the worker thread."""
    def wrapped(self, *args, **kwargs):
        return self._aff.call(fn, self, *args, **kwargs)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


class PlaywrightDriver:
    """The real browser boundary. Playwright is imported lazily — none
    of this exists at import time, so CI never needs it installed.

    open() launches a persistent context on the profile dir; when
    `cdp_url` is set it instead attaches to the already-running headed
    Chromium the /desktop session launched on the SAME profile (the
    owner watches pal act; pal never owns that browser's lifecycle).
    """

    def __init__(self, env=None):
        self.env = env or {}
        self._aff = ThreadAffined(name="pa-playwright")
        self._pw = None
        self._ctx = None
        self._browser = None
        self._attached = False
        self._profile = ""
        self._download_dir = ""
        self._download_max = 20 * 1024 * 1024
        self._idx = 0
        self.tabs_capped = False
        self.last_download = None

    # -- lifecycle ----------------------------------------------------
    def open(self, profile_dir, download_dir, headless=True,
             executable="", cdp_url="", download_max=0, max_tabs=3,
             nav_timeout=30.0):
        self._aff.start()
        return self._aff.call(
            self._open_impl, profile_dir, download_dir, headless,
            executable, cdp_url, download_max, max_tabs, nav_timeout)

    def _open_impl(self, profile_dir, download_dir, headless=True,
                   executable="", cdp_url="", download_max=0, max_tabs=3,
                   nav_timeout=30.0):
        from playwright.sync_api import sync_playwright
        self._profile = profile_dir
        self._download_dir = download_dir
        if download_max:
            self._download_max = download_max
        self._max_tabs = max(1, int(max_tabs or 3))
        self._nav_timeout = max(5.0, nav_timeout) * 1000.0
        self._pw = sync_playwright().start()
        try:
            if cdp_url:
                self._browser = self._pw.chromium.connect_over_cdp(
                    cdp_url)
                ctxs = self._browser.contexts
                self._ctx = ctxs[0] if ctxs else \
                    self._browser.new_context()
                self._attached = True
            else:
                kw = {"user_data_dir": profile_dir, "headless": headless,
                      "viewport": {"width": 1280, "height": 800},
                      "accept_downloads": True,
                      "args": ["--disable-dev-shm-usage",
                               "--no-first-run",
                               "--no-default-browser-check"]}
                if executable:
                    kw["executable_path"] = executable
                self._ctx = self._pw.chromium \
                    .launch_persistent_context(**kw)
                self._attached = False
            self._ctx.set_default_timeout(15000)
            self._ctx.set_default_navigation_timeout(self._nav_timeout)
            self._ctx.on("page", self._on_page)
            for p in self._ctx.pages:
                self._hook_page(p)
        except Exception:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = self._ctx = self._browser = None
            raise
        return self

    def _on_page(self, page):
        self._hook_page(page)
        if len(self._ctx.pages) > self._max_tabs:
            self.tabs_capped = True
            try:
                page.close()
            except Exception:
                pass

    def _hook_page(self, page):
        try:
            page.on("download", self._on_download)
        except Exception:
            pass

    def _on_download(self, download):
        """Save into the chat-sandbox dir only, capped at
        download_max; bigger artifacts are deleted and reported."""
        try:
            name = os.path.basename(
                download.suggested_filename or "download.bin")[:120]
            os.makedirs(self._download_dir, exist_ok=True)
            dest = os.path.join(self._download_dir, name)
            download.save_as(dest)
            size = os.path.getsize(dest)
            if size > self._download_max:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                self.last_download = {
                    "file": name, "bytes": size, "too_large": True}
            else:
                self.last_download = {
                    "file": dest, "bytes": size, "too_large": False}
        except Exception as e:
            self.last_download = {"error": str(e)[:200]}

    @_pw_method
    def is_alive(self) -> bool:
        try:
            if self._attached:
                return bool(self._browser and
                            self._browser.is_connected())
            return self._ctx is not None and \
                self._ctx.pages is not None
        except Exception:
            return False

    def close(self):
        try:
            if self._aff.alive():
                self._aff.call(self._close_impl, timeout=30)
        except Exception:
            pass
        self._aff.stop()

    def _close_impl(self):
        try:
            if self._ctx is not None:
                if self._attached:
                    self._browser.close()      # disconnect only
                else:
                    self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._ctx = self._browser = None
        self._idx = 0

    def memory_mb(self):
        return _tree_memory_mb(self._profile)

    # -- tabs -----------------------------------------------------------
    def _page(self):
        pages = self._ctx.pages
        if not pages:
            page = self._ctx.new_page()
            self._hook_page(page)
            return page
        self._idx = max(0, min(self._idx, len(pages) - 1))
        return pages[self._idx]

    @_pw_method
    def tabs(self):
        self._page()
        out = []
        for i, p in enumerate(self._ctx.pages):
            try:
                title = p.title()
            except Exception:
                title = ""
            out.append({"index": i, "url": p.url or "", "title": title,
                        "current": i == self._idx})
        return out

    @_pw_method
    def switch(self, index: int):
        pages = self._ctx.pages
        if not 0 <= index < len(pages):
            raise ComputerError(f"no tab {index} "
                                f"({len(pages)} open)", "bad_arguments")
        self._idx = index
        try:
            pages[index].bring_to_front()
        except Exception:
            pass
        return {"index": index, "url": pages[index].url}

    @_pw_method
    def close_tab(self, index=None):
        pages = self._ctx.pages
        i = self._idx if index is None else index
        if not 0 <= i < len(pages):
            raise ComputerError(f"no tab {i}", "bad_arguments")
        pages[i].close()
        self._idx = min(i, max(0, len(self._ctx.pages) - 1))
        self._page()
        return {"closed": i, "open": len(self._ctx.pages)}

    # -- actions --------------------------------------------------------
    @_pw_method
    def goto(self, url):
        page = self._page()
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            raise ComputerError(
                f"navigation failed: {str(e)[:200]}", "goto_failed")
        return {"url": page.url, "title": page.title()}

    @_pw_method
    def current_url(self):
        try:
            return self._page().url or ""
        except Exception:
            return ""

    @_pw_method
    def snapshot(self):
        page = self._page()
        try:
            nodes = page.evaluate(_TAG_JS)
            text = page.evaluate(
                "() => (document.body && document.body.innerText)"
                " || ''")
        except Exception as e:
            raise ComputerError(
                f"snapshot failed: {str(e)[:200]}", "snapshot_failed")
        lines = []
        for n in nodes or []:
            bits = [f"@{n.get('ref')}"]
            role = n.get("role") or n.get("tag") or "?"
            bits.append(role)
            if n.get("type") and n["type"] not in ("text", ""):
                bits.append(f"type={n['type']}")
            label = (n.get("text") or n.get("name") or "").strip()
            if label:
                bits.append(f'"{label[:60]}"')
            lines.append(" ".join(bits))
        return {"url": page.url, "title": page.title(),
                "nodes": lines,
                "text": truncate_middle((text or "").strip(),
                                        SNAPSHOT_TEXT_CAP)}

    @_pw_method
    def inspect(self, target=None):
        sel = self._selector(target)
        try:
            facts = self._page().evaluate(_INSPECT_JS, sel)
        except Exception as e:
            raise ComputerError(
                f"inspect failed: {str(e)[:200]}", "inspect_failed")
        facts = facts or {}
        facts["url"] = self.current_url()
        return facts

    def _selector(self, target):
        """'@e3'/'e3' -> our injected ref attr; anything else is taken
        as a CSS selector (an argument to fixed JS, never eval'd)."""
        t = (target or "").strip()
        m = _REF_RE.match(t)
        if m:
            return f'[data-pa-ref="e{m.group(1)}"]'
        if not t:
            return ""
        return t[:300]

    @_pw_method
    def click(self, target):
        sel = self._selector(target)
        if not sel:
            raise ComputerError("click needs a selector or @ref",
                                "bad_arguments")
        try:
            self._page().click(sel, timeout=8000)
        except Exception as e:
            raise ComputerError(
                f"click failed: {str(e)[:200]}", "click_failed")
        out = {"clicked": target, "url": self.current_url()}
        if self.last_download:
            out["download"] = self.last_download
            self.last_download = None
        return out

    @_pw_method
    def type_text(self, target, text):
        sel = self._selector(target)
        if not sel:
            raise ComputerError("type needs a selector or @ref",
                                "bad_arguments")
        text = (text or "")[:4000]
        try:
            self._page().fill(sel, text, timeout=8000)
        except Exception as e:
            raise ComputerError(
                f"type failed: {str(e)[:200]}", "type_failed")
        return {"typed": len(text), "into": target}

    @_pw_method
    def press(self, key):
        key = (key or "").strip()[:40]
        if not key:
            raise ComputerError("press needs a key", "bad_arguments")
        try:
            self._page().keyboard.press(key)
        except Exception as e:
            raise ComputerError(
                f"press failed: {str(e)[:200]}", "press_failed")
        return {"pressed": key, "url": self.current_url()}

    @_pw_method
    def screenshot(self, max_bytes=SCREENSHOT_MAX_BYTES):
        page = self._page()
        try:
            raw = page.screenshot(type="jpeg", quality=55)
            if len(raw) > max_bytes:
                raw = page.screenshot(type="jpeg", quality=30)
        except Exception as e:
            raise ComputerError(
                f"screenshot failed: {str(e)[:200]}",
                "screenshot_failed")
        if len(raw) > max_bytes:
            raise ComputerError(
                f"screenshot is {len(raw)} bytes (cap {max_bytes})",
                "screenshot_failed")
        return raw, "jpeg"

    @_pw_method
    def extract(self):
        page = self._page()
        try:
            text = page.evaluate(
                "() => (document.body && document.body.innerText)"
                " || ''")
            links = page.evaluate(_LINKS_JS)
            tables = page.evaluate(_TABLES_JS)
        except Exception as e:
            raise ComputerError(
                f"extract failed: {str(e)[:200]}", "extract_failed")
        return {"url": page.url, "title": page.title(),
                "text": truncate_middle((text or "").strip(),
                                        EXTRACT_TEXT_CAP),
                "links": links or [], "tables": tables or []}


# ---------------------------------------------------------------------------
# the service

class ComputerService:
    """Owns at most one Chromium via a driver (PlaywrightDriver in
    production, a fake in tests). Thread-safe; idle-exits; recycles.

    `desktop` is a DesktopManager (pa.desktop) — injected lazily and
    only ever built when a desktop_* action arrives, so this module
    stays usable standalone.
    """

    def __init__(self, settings, env=None, driver_factory=None,
                 desktop=None, resolver=None, now=time.monotonic):
        self.s = settings
        self.env = env if env is not None else {}
        self._driver_factory = driver_factory or \
            (lambda: PlaywrightDriver(env))
        self._desktop = desktop
        self._resolver = resolver or _default_resolver
        self._now = now
        self._lock = threading.Lock()
        self._drv = None
        self._last_used = self._now()
        self._watchdog_started = False
        self._wlock = threading.Lock()

    # -- state ---------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.s.computer_enabled)

    def _profile_dir(self) -> str:
        path = self.s.computer_profile or \
            os.path.join(self.s.data_dir, "browser-profile")
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    def _download_dir(self) -> str:
        path = os.path.join(self.s.chat_sandbox, "downloads")
        os.makedirs(path, exist_ok=True)
        return path

    def _desktop_mgr(self):
        if self._desktop is None:
            with self._wlock:
                if self._desktop is None:
                    from . import desktop as desktopmod
                    self._desktop = desktopmod.DesktopManager(
                        self.s, self.env,
                        before_start=self.stop_browser,
                        on_stop=self._desktop_stopped)
        return self._desktop

    def _desktop_stopped(self):
        pass        # driver notices CDP is gone on next is_alive()

    def desktop_cdp_url(self) -> str:
        """CDP URL of the live desktop session's headed chromium, or
        ''. The service attaches to it instead of launching headless
        while a /desktop session is up."""
        d = self._desktop
        if d is not None and d.active():
            return d.cdp_url() or ""
        return ""

    # -- driver lifecycle -------------------------------------------------
    def _driver(self):
        """Live driver or an error dict. Recycles on crash; attaches
        over CDP while a desktop session owns the profile."""
        if self._drv is not None and self._drv.is_alive():
            return self._drv
        if self._drv is not None:
            try:
                self._drv.close()
            except Exception:
                pass
            self._drv = None
        try:
            drv = self._driver_factory()
            drv.open(
                profile_dir=self._profile_dir(),
                download_dir=self._download_dir(),
                headless=True,
                executable=find_browser_bin(self.env, self.s),
                cdp_url=self.desktop_cdp_url(),
                download_max=self.s.computer_download_max,
                max_tabs=self.s.computer_max_tabs,
                nav_timeout=30.0)
            self._drv = drv
            return drv
        except Exception as e:
            log.warning("computer launch failed: %s", e)
            return {"error": "launch_failed",
                    "message": f"couldn't start Chromium: "
                               f"{str(e)[:200]} — is playwright "
                               "installed on the computer host? "
                               "(docs/COMPUTER.md)"}

    def _stop_locked(self):
        """Close the driver. Caller must hold (or not need) the lock."""
        if self._drv is not None:
            try:
                self._drv.close()
            except Exception:
                pass
            self._drv = None

    def stop_browser(self):
        """Stop the browser; the 0700 profile stays on disk. Used by
        idle-exit, the memory self-check, and /desktop (which relaunches
        the same profile headed under Xvfb)."""
        with self._lock:
            self._stop_locked()
        return {"stopped": True}

    def _memcheck(self):
        """Recycle when the browser tree's self-reported RSS exceeds
        PA_COMPUTER_MEMORY_MB. Informational: the cgroup is the real
        cap — a fat tab gets the browser unit OOM-killed there."""
        if self._drv is None:
            return
        try:
            mb = self._drv.memory_mb()
        except Exception:
            return
        if mb and mb > self.s.computer_memory_mb:
            log.warning("browser RSS %.0fMB > PA_COMPUTER_MEMORY_MB=%d "
                        "— recycling", mb, self.s.computer_memory_mb)
            try:
                self._drv.close()
            except Exception:
                pass
            self._drv = None

    def stop_if_idle(self, now=None):
        """Idle-exit: close Chromium after PA_COMPUTER_IDLE_SECONDS
        with no tool call and no live desktop session. Returns True
        when it stopped."""
        if self._drv is None:
            return False
        now = self._now() if now is None else now
        if now - self._last_used < self.s.computer_idle_seconds:
            return False
        d = self._desktop
        if d is not None and d.active():
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._drv is not None:
                try:
                    self._drv.close()
                except Exception:
                    pass
                self._drv = None
        finally:
            self._lock.release()
        log.info("computer idle-exit after %ds",
                 self.s.computer_idle_seconds)
        return True

    def _watchdog(self):
        period = max(5.0, min(60.0, self.s.computer_idle_seconds / 4.0))
        while True:
            time.sleep(period)
            if self._drv is None:
                continue
            try:
                self.stop_if_idle()
            except Exception:
                pass

    def _ensure_watchdog(self):
        if self._watchdog_started:
            return
        self._watchdog_started = True
        threading.Thread(target=self._watchdog, daemon=True,
                         name="pa-computer-idle").start()

    # -- url policy -----------------------------------------------------
    def _vet_url(self, url: str):
        """Public http(s) on 80/443, public DNS answers only — the same
        egress policy as fetch_url, enforced before the browser ever
        navigates. (The browser does its own DNS; a post-navigation
        re-check in _post_nav covers redirects.)"""
        scheme, host, port, path = _vet_url(url)
        _resolve(host, port, self._resolver)
        return url

    def _post_nav(self, drv, result: dict):
        """After any navigation/click, re-vet the page the browser
        actually landed on; a redirect to a private/metadata host gets
        walked back to about:blank."""
        try:
            cur = drv.current_url()
            if cur and cur.startswith(("http://", "https://")):
                self._vet_url(cur)
            if cur:
                result["url"] = cur
        except (FetchBlocked, FetchFailed) as e:
            try:
                drv.goto("about:blank")
            except Exception:
                pass
            return {"error": getattr(e, "kind", "fetch_blocked"),
                    "message": f"redirect landed on a blocked "
                               f"address: {e}"}
        return result

    # -- the entry point --------------------------------------------------
    def call(self, action: str, args: dict = None) -> dict:
        """One browser action. Serialised: a second caller waits up to
        PA_COMPUTER_LOCK_WAIT seconds, then gets a busy error."""
        if not self.enabled():
            return not_configured()
        args = args or {}
        if not self._lock.acquire(timeout=self.s.computer_lock_wait):
            return {"error": "busy",
                    "message": "the browser is busy right now — try "
                               "again in a moment"}
        try:
            self._ensure_watchdog()
            out = self._call_locked(action, args)
            self._last_used = self._now()
            return out
        finally:
            self._lock.release()

    def _call_locked(self, action, args):
        if action == "status":
            return self._status()
        if action == "stop_browser":
            self._stop_locked()
            return {"stopped": True}
        if action in ("desktop_start", "desktop_stop", "desktop_status"):
            return self._desktop_action(action, args)
        drv = self._driver()
        if isinstance(drv, dict):
            return drv
        try:
            if action == "goto":
                self._vet_url(str(args.get("url") or ""))
                out = self._post_nav(drv, drv.goto(str(args["url"])))
            elif action == "tabs":
                out = {"tabs": drv.tabs()}
                if drv.tabs_capped:
                    out["note"] = (f"tab cap {self.s.computer_max_tabs} "
                                   "hit — an extra tab was closed")
                    drv.tabs_capped = False
            elif action == "switch":
                out = self._post_nav(drv, drv.switch(int(
                    args.get("index") or 0)))
            elif action == "close_tab":
                idx = args.get("index")
                out = drv.close_tab(
                    None if idx is None else int(idx))
            elif action == "snapshot":
                out = drv.snapshot()
                out["flags"] = page_flags(
                    out.get("url"), out.get("title"), out.get("text"))
            elif action == "inspect":
                facts = drv.inspect(args.get("target"))
                risk, reason = classify(facts)
                out = {"found": bool(facts.get("found")),
                       "risk": risk, "reason": reason,
                       "preview": self._preview(facts),
                       "url": facts.get("url") or ""}
            elif action == "press_check":
                facts = drv.inspect(None)
                risk, reason = press_risk(
                    str(args.get("key") or ""), facts)
                out = {"found": bool(facts.get("found")),
                       "risk": risk, "reason": reason,
                       "preview": self._preview(facts),
                       "url": facts.get("url") or ""}
            elif action == "click":
                out = self._post_nav(drv, drv.click(
                    str(args.get("target") or "")))
            elif action == "type":
                out = drv.type_text(str(args.get("target") or ""),
                                    str(args.get("text") or ""))
            elif action == "press":
                out = self._post_nav(drv, drv.press(
                    str(args.get("key") or "")))
            elif action == "screenshot":
                raw, ext = drv.screenshot()
                out = {"b64": base64.b64encode(raw).decode("ascii"),
                       "ext": ext, "bytes": len(raw)}
            elif action == "extract":
                out = drv.extract()
            else:
                return {"error": "unknown_action",
                        "message": f"no such computer action {action!r}"}
        except ComputerError as e:
            return {"error": e.kind, "message": str(e)[:300]}
        except (FetchBlocked, FetchFailed) as e:
            return {"error": getattr(e, "kind", "fetch_blocked"),
                    "message": str(e)[:300]}
        except Exception as e:
            log.exception("computer action %s failed", action)
            return {"error": "computer_error",
                    "message": str(e)[:300]}
        self._memcheck()
        return out

    def _preview(self, facts: dict) -> str:
        """One-line element description for the approval DM."""
        label = (facts.get("text") or facts.get("name") or
                 facts.get("href") or "")
        tag = facts.get("tag") or "?"
        ident = f' "{label[:80]}"' if label else ""
        where = f' on "{(facts.get("title") or "")[:60]}"' \
            if facts.get("title") else ""
        return f"<{tag}{ident}>{where}"

    def _status(self):
        return {"enabled": self.enabled(),
                "running": bool(self._drv and self._drv.is_alive()),
                "remote": bool(getattr(self.s, "computer_url", "")),
                "desktop": bool(self._desktop and self._desktop.active()),
                "profile": os.path.basename(
                    self.s.computer_profile or "browser-profile"),
                "idle_seconds": self.s.computer_idle_seconds,
                "max_tabs": self.s.computer_max_tabs}

    def _desktop_action(self, action, args):
        d = self._desktop_mgr()
        if action == "desktop_status":
            return d.status()
        if action == "desktop_stop":
            return d.stop()
        # refuse a new browser session when today's browser_min
        # budget is spent — owner chat still works, the desktop
        # doesn't silently burn the daily allowance.
        try:
            from . import budget as _budget, db as dbmod
            conn = dbmod.connect(self.s.db_path)
            try:
                dbmod.migrate(conn)
                want = int(args.get("minutes") or
                           self.s.computer_idle_seconds / 60 or 5)
                if _budget.would_exceed(
                        conn, "owner-chat", "browser_min", want,
                        s=self.s):
                    return {"error": "budget_exceeded",
                            "message": "today's browser budget is "
                                       "spent — try again tomorrow"}
            finally:
                conn.close()
        except Exception:
            pass
        return d.start(minutes=args.get("minutes"))


# ---------------------------------------------------------------------------
# remote computer-agent: same wire shape as pa/freebuff_sidecar.py —
# loopback/tailnet bind + a shared bearer token on every request.
#
#     GET  /healthz -> {"ok": true, "enabled": bool}
#     POST /call    {"action": str, "args": {...}} -> {"result": {...}}

AGENT_MAX_BODY = 96 * 1024
AGENT_MAX_RESPONSE = 2 * 1024 * 1024    # screenshots ride in responses
TOKEN_HEADER = "x-pa-computer-token"


def make_agent_handler(service, token: str):
    """Request handler bound to a service.call. `service` is a
    ComputerService — injectable so tests never launch a browser."""
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            log.debug("computer-agent: " + fmt, *a)

        def _reply(self, status, obj):
            body = json.dumps(obj).encode("utf-8")[:AGENT_MAX_RESPONSE]
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            return self.headers.get(TOKEN_HEADER) == token

        def do_GET(self):
            if not self._authorized():
                return self._reply(401, {"error": {
                    "kind": "agent_auth", "message": "bad token"}})
            if urllib.parse.urlsplit(self.path).path != "/healthz":
                return self._reply(404, {"error": {
                    "kind": "not_found", "message": "unknown path"}})
            self._reply(200, {"ok": True, "enabled": service.enabled()})

        def do_POST(self):
            if not self._authorized():
                return self._reply(401, {"error": {
                    "kind": "agent_auth", "message": "bad token"}})
            if urllib.parse.urlsplit(self.path).path != "/call":
                return self._reply(404, {"error": {
                    "kind": "not_found", "message": "unknown path"}})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= AGENT_MAX_BODY:
                return self._reply(400, {"error": {
                    "kind": "bad_request",
                    "message": "missing or oversized body"}})
            try:
                req = json_loads_bounded(
                    self.rfile.read(length).decode("utf-8", "replace"),
                    max_len=AGENT_MAX_BODY)
                action = req["action"]
                args = req.get("args") or {}
                if not isinstance(action, str) or not action.strip():
                    raise ValueError("action must be a non-empty string")
                if not isinstance(args, dict):
                    raise ValueError("args must be an object")
            except (ValueError, KeyError, TypeError):
                return self._reply(400, {"error": {
                    "kind": "bad_request",
                    "message": "malformed request"}})
            try:
                out = service.call(action.strip(), args)
            except Exception as e:
                log.exception("computer-agent call failed")
                return self._reply(500, {"error": {
                    "kind": "agent_error",
                    "message": str(e)[:500]}})
            self._reply(200, {"result": out})

    return Handler


class _AgentServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve_agent(host: str, port: int, token: str, service):
    """Blocking agent server. The caller (cmd_computer_agent) already
    validated the bind is loopback or a 100.64/10 tailnet address."""
    handler = make_agent_handler(service, token)
    srv = _AgentServer((host, port), handler)
    log.info("computer agent listening on %s:%d", host, port)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


class RemoteComputer:
    """pal-side client for `pa computer-agent`. Same call(action,args)
    surface as ComputerService — the chat tools can't tell them apart."""

    def __init__(self, url, token, enabled=True, timeout=60.0):
        self.client = APIClient(url, timeout=timeout)
        self.token = token
        self.url = url
        self._enabled = enabled

    def enabled(self) -> bool:
        return self._enabled

    def call(self, action: str, args: dict = None) -> dict:
        if not self._enabled:
            return not_configured()
        try:
            _h, body = self.client.post_json(
                "/call", {"action": action, "args": args or {}},
                headers={TOKEN_HEADER: self.token},
                timeout=70.0)
        except HTTPError as e:
            kind = "agent_error"
            msg = f"HTTP {e.status}"
            try:
                err = json_loads_bounded(
                    e.body.decode("utf-8", "replace"))["error"]
                kind = err.get("kind") or kind
                msg = (err.get("message") or msg)[:300]
            except Exception:
                pass
            if e.status in (401, 403):
                kind = "agent_auth"
            return {"error": kind,
                    "message": f"computer-agent: {msg}"}
        except (TransportError, ValueError) as e:
            return {"error": "agent_unreachable",
                    "message": f"computer host unreachable: {e} — is "
                               "the tailnet up and "
                               "`pa computer-agent` running?"}
        if not isinstance(body, dict) or "result" not in body:
            return {"error": "agent_bad_response",
                    "message": "computer-agent returned a malformed "
                               "response"}
        return body["result"]


# ---------------------------------------------------------------------------
# service factory — one service per (db_path, url) so the chat engine,
# the /desktop command and job replays all share the same browser.

_SERVICES = {}


def service_for(settings, env=None, driver_factory=None):
    """The process-wide computer service. RemoteComputer when
    PA_COMPUTER_URL is set (pal is the client), else an in-process
    ComputerService. Memoized per data dir + URL."""
    key = f"{settings.db_path}|{settings.computer_url}"
    svc = _SERVICES.get(key)
    if svc is None:
        if settings.computer_url:
            svc = RemoteComputer(settings.computer_url,
                                 settings.computer_token,
                                 enabled=settings.computer_enabled)
        else:
            svc = ComputerService(settings, env,
                                  driver_factory=driver_factory)
        _SERVICES[key] = svc
    return svc

"""/desktop — the owner's time-limited remote desktop, tailnet only.

One session at a time: Xvfb + openbox + x11vnc + noVNC/websockify +
the SAME Chromium profile pal's headless browser uses, so a login the
owner completes here (2FA and all) is shared with pal — and while the
session is live pal's service attaches to this headed Chromium over
CDP instead of launching its own.

Security, non-negotiable:
  - Fails CLOSED when Tailscale is down or absent — the noVNC port
    binds the tailnet IPv4 (100.64.0.0/10) only, never 0.0.0.0, never
    the public VNIC, never the Cloudflare quick tunnel, never Funnel.
  - The VNC password is a per-session random token in the URL; the
    owner gets it by DM. Expiry (PA_DESKTOP_MINUTES, cap 120) or
    `/desktop off` tears the whole tree down; the 0700 profile stays.
  - The link goes to the owner's DMs only — never a guild channel.

Every external piece (tailscale probe, process spawn, clock) is
injectable so tests run nowhere near Xvfb/Chromium/Tailscale.
"""

import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time

from .config import is_tailnet_host

log = logging.getLogger("pa.desktop")

_DISPLAY = ":77"                    # fixed X display for the session
_VNC_PORT = 5900                    # x11vnc, loopback only
_NOVNC_DIRS = ("/usr/share/novnc", "/usr/local/share/novnc",
               "/opt/novnc")
_HARD_CAP_MINUTES = 120

NO_TAILSCALE = (
    "Tailscale isn't running on the computer host — install it and "
    "`sudo tailscale up`, then retry (docs/COMPUTER.md). The desktop "
    "never binds a public interface.")
MISSING_TOOLS = (
    "needs Xvfb + x11vnc + websockify/noVNC + Chromium — run "
    "deploy/computer/install.sh on the computer host "
    "(docs/COMPUTER.md). Missing: {missing}")


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _spawn(argv, env=None):
    """One session process — detached from our process group so the
    teardown can take the whole tree down."""
    return subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)


def _tailscale_status():
    """(up, ipv4, magic_dns_name). Anything odd -> not up; callers
    fail closed."""
    if not shutil.which("tailscale"):
        return False, "", ""
    try:
        out = subprocess.run(["tailscale", "status", "--json"],
                             capture_output=True, text=True, timeout=8)
        if out.returncode != 0:
            return False, "", ""
        st = json.loads(out.stdout or "{}")
        if st.get("BackendState") != "Running":
            return False, "", ""
        node = st.get("Self") or {}
        ip4 = next((ip for ip in node.get("TailscaleIPs") or []
                    if ":" not in ip), "")
        dns = (node.get("DNSName") or "").rstrip(".")
        return bool(ip4 and is_tailnet_host(ip4)), ip4, dns
    except Exception:
        return False, "", ""


def _novnc_dir() -> str:
    for d in _NOVNC_DIRS:
        if os.path.isfile(os.path.join(d, "vnc.html")):
            return d
    return ""


class DesktopManager:
    """Owns at most one desktop session. ComputerService calls
    start/stop/status; `before_start` stops pal's headless driver so
    the headed Chromium can take the profile lock; `on_stop` runs
    after teardown (the service's driver lazily relaunches on the
    next tool call)."""

    def __init__(self, settings, env=None, before_start=None,
                 on_stop=None, runner=None, tailscale=None, novnc=None,
                 now=time.monotonic):
        self.s = settings
        self.env = env if env is not None else {}
        self._before_start = before_start
        self._on_stop = on_stop
        self._run = runner or _spawn
        self._ts = tailscale or _tailscale_status
        self._novnc = novnc or _novnc_dir
        self._now = now
        self._lock = threading.Lock()
        self._procs = []                # [(name, proc)] spawn order
        self._token = ""
        self._url = ""
        self._bind = ""
        self._cdp_port = 0
        self._deadline = 0.0

    # -- state ----------------------------------------------------------
    def active(self) -> bool:
        with self._lock:
            self._sweep_locked()
            return bool(self._procs)

    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self._cdp_port}" if self.active() \
            else ""

    def status(self) -> dict:
        with self._lock:
            self._sweep_locked()
            live = bool(self._procs)
            return {"active": live,
                    "url": self._url if live else "",
                    "bind": self._bind,
                    "expires_in": max(0, int(self._deadline
                                           - self._now())) if live
                                  else 0}

    # -- lifecycle --------------------------------------------------------
    def start(self, minutes=None) -> dict:
        """Bring the desktop up; return the owner-DM URL + expiry.
        Idempotent — a second /desktop while live returns the same
        session (with its remaining time)."""
        with self._lock:
            self._sweep_locked()
            if self._procs:
                return self._live_result()
            up, ip4, dns = self._ts()
            if not up or not (ip4 and is_tailnet_host(ip4)):
                # down, no IPv4, or a probe claiming a non-tailnet
                # address — all fail the same closed way
                return {"error": "no_tailscale", "message": NO_TAILSCALE}
            missing = self._missing()
            if missing:
                return {"error": "missing_tools",
                        "message": MISSING_TOOLS.format(
                            missing=", ".join(missing))}
            mins = max(1, min(int(minutes or self.s.desktop_minutes),
                              _HARD_CAP_MINUTES))
            if self._before_start:
                try:
                    self._before_start()      # release the profile lock
                except Exception:
                    pass
            token = secrets.token_urlsafe(12)
            port = int(self.s.desktop_port)
            cdp = _free_port()
            try:
                self._launch(token, ip4, port, cdp)
            except Exception as e:
                self._stop_locked()
                log.warning("desktop launch failed: %s", e)
                return {"error": "start_failed",
                        "message": f"desktop didn't come up: "
                                   f"{str(e)[:200]}"}
            host = dns or ip4
            self._token = token
            self._bind = ip4
            self._cdp_port = cdp
            self._deadline = self._now() + mins * 60
            self._url = (f"http://{host}:{port}/vnc.html"
                         f"?autoconnect=true&resize=scale"
                         f"&password={token}")
            threading.Thread(target=self._sweeper, daemon=True,
                             name="pa-desktop-ttl").start()
            return self._live_result()

    def stop(self) -> dict:
        with self._lock:
            was = bool(self._procs)
            self._stop_locked()
        return {"stopped": was}

    def _live_result(self) -> dict:
        st = {"url": self._url,
              "expires_in": max(0, int(self._deadline - self._now())),
              "bind": self._bind,
              "note": "link is for your DMs only — never post it to a "
                      "channel; it stops working when the session ends"}
        return st

    # -- internals ---------------------------------------------------------
    def _missing(self):
        """Binaries the session needs (openbox is nice-to-have)."""
        out = [b for b in ("Xvfb", "x11vnc", "websockify")
               if not shutil.which(b)]
        if not self._novnc():
            out.append("novnc")
        from . import computer
        if not computer.find_browser_bin(self.env, self.s):
            out.append("chromium")
        return out

    def _launch(self, token, bind_ip, port, cdp_port):
        """Spawn the whole chain; raise if a critical piece dies
        immediately."""
        from . import computer
        profile = self.s.computer_profile or os.path.join(
            self.s.data_dir, "browser-profile")
        os.makedirs(profile, mode=0o700, exist_ok=True)
        xenv = dict(os.environ)
        xenv["DISPLAY"] = _DISPLAY
        chrome = computer.find_browser_bin(self.env, self.s)
        steps = [
            ("xvfb", ["Xvfb", _DISPLAY, "-screen", "0", "1280x800x24"],
             None),
            ("openbox", ["openbox"], xenv),          # optional WM
            ("x11vnc", ["x11vnc", "-display", _DISPLAY,
                        "-rfbport", str(_VNC_PORT), "-localhost",
                        "-passwd", token, "-forever", "-shared"],
             xenv),
            ("websockify", ["websockify", "--web", self._novnc(),
                            f"{bind_ip}:{port}",
                            f"127.0.0.1:{_VNC_PORT}"], None),
            ("chromium", [chrome, f"--user-data-dir={profile}",
                          "--remote-debugging-address=127.0.0.1",
                          f"--remote-debugging-port={cdp_port}",
                          "--no-first-run", "--no-default-browser-check",
                          "--password-store=basic", "--start-maximized",
                          "--disable-dev-shm-usage", "about:blank"],
             xenv),
        ]
        for name, argv, env in steps:
            if name == "openbox" and not shutil.which("openbox"):
                continue                          # WM is a nicety
            proc = self._run(argv, env=env)
            self._procs.append((name, proc))
        time.sleep(0.8)
        dead = [n for n, p in self._procs
                if n != "openbox" and p.poll() is not None]
        if dead:
            raise RuntimeError(", ".join(dead) + " exited")

    def _sweep_locked(self):
        if self._procs and self._now() >= self._deadline:
            log.info("desktop session expired — tearing down")
            self._stop_locked()

    def _sweeper(self):
        while True:
            time.sleep(15)
            with self._lock:
                if not self._procs:
                    return
                self._sweep_locked()

    def _stop_locked(self):
        was = bool(self._procs)
        for _name, proc in reversed(self._procs):
            try:
                proc.terminate()
            except Exception:
                pass
        for _name, proc in reversed(self._procs):
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._procs = []
        self._token = self._url = ""
        self._cdp_port = 0
        self._deadline = 0.0
        if was and self._on_stop:
            try:
                self._on_stop()
            except Exception:
                pass

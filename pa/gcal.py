"""Google Calendar adapter (official Calendar API, owner OAuth grant).

Reuses the same GMAIL_* OAuth client/refresh-token env as pa.gmail —
the grant just needs calendar.events in its scope set (granted by
`pa gmail-auth` on this branch). Read = list_events; write =
create_event, which is always behind an owner approval at the tool
layer. Missing config fails closed with a needs-config hint.
"""

import datetime
import urllib.parse

from . import gmail as gmailmod
from .netclient import APIClient, HTTPError, TransportError

MAX_RESULTS = 25


class GCalNotConfigured(Exception):
    kind = "calendar_not_configured"


class GCalError(Exception):
    kind = "calendar_error"


def _fmt_dt(dt: datetime.datetime) -> str:
    return dt.isoformat(timespec="seconds")


class GCalClient:
    """Minimal client for the owner's primary calendar. The access token
    comes from the same refresh grant as Gmail (calendar.events scope)."""

    def __init__(self, settings, env=None, conn=None):
        try:
            self._gmail = gmailmod.GmailClient(settings, env, conn=conn)
        except gmailmod.GmailNotConfigured as e:
            raise GCalNotConfigured(str(e))
        self.api = APIClient(settings.gcal_api_base, timeout=20)

    def _headers(self):
        return {"Authorization": f"Bearer {self._gmail.access_token()}"}

    def _get(self, path):
        try:
            return self.api.get_json(path, headers=self._headers())
        except HTTPError as e:
            raise self._err(e)
        except TransportError as e:
            raise GCalError(f"calendar API unreachable: {e}")

    def _post(self, path, obj):
        try:
            _h, data = self.api.post_json(path, obj,
                                          headers=self._headers())
            return data or {}
        except HTTPError as e:
            raise self._err(e)
        except TransportError as e:
            raise GCalError(f"calendar API unreachable: {e}")

    def _err(self, e: HTTPError):
        if e.status in (401, 403):
            return GCalError(
                f"calendar API auth failed (HTTP {e.status}) — re-run "
                "`python -m pa gmail-auth` to grant the calendar scope")
        if e.status == 429:
            return GCalError("calendar API rate limited (HTTP 429)")
        return GCalError(f"calendar API HTTP {e.status}")

    # -- endpoints ---------------------------------------------------------
    def list_events(self, days_ahead: int = 7, max_results: int = 10,
                    time_min: str = None):
        """Upcoming events on the primary calendar. Returns
        [{id,summary,start,end,location}]."""
        now = datetime.datetime.now(datetime.timezone.utc)
        tmin = time_min or _fmt_dt(now)
        tmax = _fmt_dt(now + datetime.timedelta(
            days=max(1, min(int(days_ahead), 60))))
        q = urllib.parse.urlencode({
            "timeMin": tmin, "timeMax": tmax,
            "singleEvents": "true", "orderBy": "startTime",
            "maxResults": max(1, min(int(max_results), MAX_RESULTS))})
        _h, data = self._get(
            f"/calendar/v3/calendars/primary/events?{q}")
        out = []
        for ev in data.get("items") or []:
            start = ev.get("start") or {}
            end = ev.get("end") or {}
            out.append({
                "id": ev.get("id"),
                "summary": (ev.get("summary") or "")[:300],
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
                "location": (ev.get("location") or "")[:300],
            })
        return out

    def create_event(self, summary: str, start: str, end: str,
                     timezone: str = None, description: str = "",
                     location: str = ""):
        """Insert one event (calendar.events scope). Approval-gated
        upstream. start/end are ISO 'YYYY-MM-DDTHH:MM[:SS]' local times
        interpreted in `timezone` (default PA_TIMEZONE)."""
        def block(v):
            d = {"dateTime": str(v)}
            if timezone:
                d["timeZone"] = timezone
            return d
        body = {
            "summary": str(summary or "")[:500],
            "start": block(start),
            "end": block(end),
        }
        if description:
            body["description"] = str(description)[:4000]
        if location:
            body["location"] = str(location)[:300]
        data = self._post("/calendar/v3/calendars/primary/events", body)
        return {"id": data.get("id"), "htmlLink": data.get("htmlLink"),
                "summary": body["summary"], "start": start, "end": end}

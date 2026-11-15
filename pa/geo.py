"""Weather + places tools (docs/PARITY.md #18).

Free, keyless public APIs only:

- `weather(place)`: Nominatim geocode -> open-meteo current +
  today forecast. Falls back to the configured briefing city when
  no place is given.
- `places(query, near)`: Nominatim search — name, kind,
  coordinates — capped at 5 results, bounded timeouts.

All replies are external data; the chat layer already treats tool
output as untrusted, and nothing here is ever an instruction.
"""

import urllib.parse

from .netclient import APIClient, HTTPError, TransportError

_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy",
        3: "overcast", 45: "fog", 48: "icy fog",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain",
        66: "freezing rain", 67: "heavy freezing rain",
        71: "light snow", 73: "snow", 75: "heavy snow",
        80: "light showers", 81: "showers", 82: "heavy showers",
        95: "thunderstorm", 96: "storm + hail", 99: "storm + hail"}

NOMINATIM = "https://nominatim.openstreetmap.org"


def geocode(s, query: str, limit: int = 1) -> list:
    """-> [{name, lat, lon, kind}] or [] (Nominatim search)."""
    q = urllib.parse.urlencode({
        "q": query[:200], "format": "jsonv2",
        "limit": max(1, min(int(limit), 5))})
    try:
        _h, data = APIClient(NOMINATIM, timeout=10).get_json(
            f"/search?{q}",
            headers={"Accept": "application/json"})
    except (HTTPError, TransportError):
        return []
    out = []
    for r in data or []:
        try:
            out.append({
                "name": (r.get("display_name") or "")[:160],
                "lat": float(r["lat"]), "lon": float(r["lon"]),
                "kind": f"{r.get('category','?')}/"
                        f"{r.get('type','?')}"})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def places(s, query: str, near: str = "") -> dict:
    """places(query, near?) — public-place search via Nominatim."""
    q = (query or "").strip()[:200]
    if near:
        q += " " + near.strip()[:120]
    if not q:
        return {"error": "bad_arguments", "message": "empty query"}
    hits = geocode(s, q, limit=5)
    if not hits:
        return {"error": "not_found",
                "message": f"no places match {q!r}"}
    return {"places": hits, "count": len(hits)}


def weather(s, place: str = "") -> dict:
    """weather(place?) — current + today's range for a named place,
    or the configured briefing city."""
    if place:
        hits = geocode(s, place, limit=1)
        if not hits:
            return {"error": "not_found",
                    "message": f"can't geocode {place!r}"}
        lat, lon, name = hits[0]["lat"], hits[0]["lon"], \
            hits[0]["name"]
    else:
        lat, lon, name = s.briefing_lat, s.briefing_lon, \
            s.briefing_city
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max",
        "timezone": "auto", "forecast_days": 1})
    try:
        _h, data = APIClient(s.open_meteo_base, timeout=10).get_json(
            f"/v1/forecast?{q}")
    except (HTTPError, TransportError) as e:
        return {"error": "weather_error", "message": str(e)[:200]}
    cur = data.get("current") or {}
    day = data.get("daily") or {}
    code = int(cur.get("weather_code") or 0)
    return {
        "place": name,
        "now_c": cur.get("temperature_2m"),
        "condition": _WMO.get(code, f"code {code}"),
        "wind_kph": cur.get("wind_speed_10m"),
        "high_c": (day.get("temperature_2m_max") or [None])[0],
        "low_c": (day.get("temperature_2m_min") or [None])[0],
        "rain_pct": (day.get("precipitation_probability_max")
                     or [None])[0]}

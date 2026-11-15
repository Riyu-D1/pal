"""weather + places tools."""

import unittest

from pa import geo
from tests.helpers import TmpEnv

GEO_HIT = [{"display_name": "Clifton Suspension Bridge, Bristol",
            "lat": "51.45", "lon": "-2.62",
            "category": "tourism", "type": "attraction"}]
METEO = {"current": {"temperature_2m": 14.5, "weather_code": 61,
                     "wind_speed_10m": 12.0},
         "daily": {"temperature_2m_max": [17.0],
                   "temperature_2m_min": [9.0],
                   "precipitation_probability_max": [80]}}


class _Cli:
    def __init__(self, base, timeout=30):
        self.base = base

    def get_json(self, path, headers=None, timeout=None):
        if "nominatim" in self.base or "/search" in path:
            return {}, GEO_HIT
        return {}, METEO


class TestGeo(unittest.TestCase):
    def setUp(self):
        self.env = TmpEnv()
        self.s = self.env.settings
        self.orig = geo.APIClient
        geo.APIClient = _Cli

    def tearDown(self):
        geo.APIClient = self.orig

    def test_weather_named_place(self):
        out = geo.weather(self.s, "Bristol")
        self.assertEqual(out["now_c"], 14.5)
        self.assertEqual(out["condition"], "light rain")
        self.assertEqual(out["high_c"], 17.0)
        self.assertEqual(out["rain_pct"], 80)
        self.assertIn("Bristol", out["place"])

    def test_weather_default_city(self):
        out = geo.weather(self.s, "")
        self.assertEqual(out["place"], self.s.briefing_city)
        self.assertIsNotNone(out["now_c"])

    def test_places(self):
        out = geo.places(self.s, "suspension bridge", "bristol")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["places"][0]["lat"], 51.45)
        self.assertIn("tourism", out["places"][0]["kind"])

    def test_empty_query_and_no_hits(self):
        self.assertEqual(geo.places(self.s, "")["error"],
                         "bad_arguments")

        class _None(_Cli):
            def get_json(self, *a, **k):
                return {}, []

        geo.APIClient = _None
        self.assertEqual(geo.places(self.s, "zzz")["error"],
                         "not_found")
        self.assertEqual(geo.weather(self.s, "zzz")["error"],
                         "not_found")


if __name__ == "__main__":
    unittest.main()

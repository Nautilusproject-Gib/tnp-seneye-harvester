"""Unit tests. Run with: python -m unittest discover -s tests -v"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvester.export import _daily_stats, build_payload
from harvester.seneye import parse_reading
from harvester.store import Store

# Shape of one device from GET /v1/devices?IncludeState=1
SAMPLE = {
    "id": "12345",
    "description": "Sump SA12",
    "type": 1,
    "status": {
        "disconnected": "0",
        "slide_serial": "SLD-987",
        "slide_expires": 1790000000,
        "out_of_water": "0",
        "wrong_slide": 0,
        "last_experiment": 1789990000,
    },
    "exps": {
        "temperature": {"trend": 1, "critical_in": -1, "avg": "21.1", "status": 0, "curr": "21.35", "advises": []},
        "ph": {"trend": 0, "critical_in": -1, "avg": "8.10", "status": 0, "curr": "8.12", "advises": []},
        "nh3": {"trend": -1, "critical_in": -1, "avg": "0.006", "status": 0, "curr": "0.004", "advises": []},
        "light": {"curr": "142", "max_value": "180", "status": 0, "advises": []},
    },
}


class TestParsing(unittest.TestCase):
    def test_parses_values_and_state(self):
        r = parse_reading("12345", SAMPLE["exps"], SAMPLE["status"], 1789990000, 1789990600)
        self.assertEqual(r.device_id, "12345")
        self.assertAlmostEqual(r.values["temperature"], 21.35)
        self.assertAlmostEqual(r.values["ph"], 8.12)
        self.assertAlmostEqual(r.values["nh3"], 0.004)
        self.assertAlmostEqual(r.values["par"], 142.0)
        self.assertEqual(r.slide_serial, "SLD-987")
        self.assertEqual(r.out_of_water, 0)
        self.assertEqual(r.trends["temperature"], 1)

    def test_missing_parameters_are_absent_not_zero(self):
        r = parse_reading("1", {"ph": {"curr": "8.0"}}, {}, 1, 2)
        self.assertNotIn("temperature", r.values)
        self.assertIsNone(r.as_row()["temperature"])

    def test_garbage_values_are_dropped(self):
        r = parse_reading("1", {"ph": {"curr": "n/a"}, "nh3": {"curr": ""}}, {}, 1, 2)
        self.assertEqual(r.values, {})


class TestStore(unittest.TestCase):
    def setUp(self):
        self.store = Store("sqlite://:memory:")
        self.store.migrate()

    def tearDown(self):
        self.store.close()

    def test_readings_deduplicate(self):
        r = parse_reading("1", SAMPLE["exps"], SAMPLE["status"], 1789990000, 1789990600)
        self.assertEqual(self.store.insert_readings([r.as_row()]), 1)
        self.assertEqual(self.store.insert_readings([r.as_row()]), 0)
        rows = self.store.query("SELECT COUNT(*) AS n FROM readings")
        self.assertEqual(rows[0]["n"], 1)

    def test_device_upsert_updates_rather_than_duplicates(self):
        self.store.upsert_device("1", "SA12", 1, "SA12", "A", "SA12", 100)
        self.store.upsert_device("1", "SA12 renamed", 1, "SA12", "A", "SA12 renamed", 200)
        rows = self.store.query("SELECT * FROM devices")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "SA12 renamed")
        self.assertEqual(rows[0]["first_seen"], 100)
        self.assertEqual(rows[0]["last_seen"], 200)


class TestStats(unittest.TestCase):
    def test_daily_stats_use_sample_sd(self):
        day = 1789948800
        rows = [
            {"device_id": "1", "reading_time": day + 3600, "temperature": 20.0},
            {"device_id": "1", "reading_time": day + 7200, "temperature": 22.0},
            {"device_id": "1", "reading_time": day + 10800, "temperature": 24.0},
        ]
        stats = _daily_stats(rows, ["temperature"])
        self.assertEqual(len(stats), 1)
        t = stats[0]["temperature"]
        self.assertEqual(t["n"], 3)
        self.assertEqual(t["min"], 20.0)
        self.assertEqual(t["max"], 24.0)
        self.assertEqual(t["mean"], 22.0)
        self.assertEqual(t["sd"], 2.0)  # sample sd, not population


class TestPayload(unittest.TestCase):
    def test_payload_shape(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        now = int(time.time())
        store.upsert_device("1", "SA12", 1, "SA12", "A", "SA12", now)
        row = parse_reading("1", SAMPLE["exps"], SAMPLE["status"], now - 600, now).as_row()
        store.insert_readings([row])

        config = {
            "site": {"title": "t"},
            "systems": {"A": {"label": "System A"}},
            "sumps": {"SA12": {"system": "A", "tanks": ["A1", "A2"]}},
            "parameters": {"temperature": {"label": "Temperature", "unit": "C", "precision": 2}},
        }
        payload = build_payload(store, config, window_days=30, raw_days=7)
        json.dumps(payload)  # must be serialisable

        self.assertEqual(payload["devices"][0]["tanks"], ["A1", "A2"])
        self.assertEqual(payload["devices"][0]["system_label"], "System A")
        self.assertIn("temperature", payload["columns"])
        self.assertEqual(len(payload["readings"]), 1)
        self.assertIn("1", payload["latest"])
        store.close()


if __name__ == "__main__":
    unittest.main()

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



class TestNutrients(unittest.TestCase):
    """Parsing rules the TNP workbook actually exercises."""

    def test_excel_serial_and_typed_dates_both_parse(self):
        from harvester.nutrients import _excel_date
        self.assertEqual(_excel_date("46247").isoformat(), "2026-08-13")
        self.assertEqual(_excel_date("21/9/26").isoformat(), "2026-09-21")
        self.assertIsNone(_excel_date(""))

    def test_na_and_blanks_become_none(self):
        from harvester.nutrients import _number
        self.assertIsNone(_number("N/A"))
        self.assertIsNone(_number(""))
        self.assertIsNone(_number(None))
        self.assertEqual(_number("0.0"), 0.0)
        self.assertEqual(_number("<0.02"), 0.02)  # detection limit keeps its number

    def test_tank_id_maps_to_sump(self):
        from harvester.nutrients import sump_code
        sumps = {"SA12": {}, "SD345": {}}
        self.assertEqual(sump_code("A12", sumps), "SA12")
        self.assertEqual(sump_code("SD345", sumps), "SD345")
        self.assertIsNone(sump_code("Tank ID", sumps))
        self.assertIsNone(sump_code("Z99", sumps))

    def test_nutrients_upsert_replaces_a_corrected_value(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        base = {"sample_date": "2026-09-21", "sump_code": "SA12", "no3": 0.0}
        store.upsert_nutrients([base])
        store.upsert_nutrients([dict(base, no3=1.5)])
        rows = store.query("SELECT sample_date, sump_code, no3 FROM nutrients")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["no3"], 1.5)
        store.close()



class TestSheetSource(unittest.TestCase):
    """The CSV route from Google must agree with the .xlsx route exactly."""

    HEADERS = ",Date,Time,Tank ID,Temp (°C),Salinity (ppt),pH,dKH (°dKH),NO₃ (mg/L),NO₂ (mg/L),NH₃,PO₄³⁻,Ca2+ (mg/L),Mg2+ (mg/L)"

    def test_headings_normalise_past_units_and_subscripts(self):
        from harvester.nutrients import normalise_heading
        self.assertEqual(normalise_heading("NO₃ (mg/L)"), "no3")
        self.assertEqual(normalise_heading("Mg2+ (mg/L)"), "mg2")
        self.assertEqual(normalise_heading("dKH (°dKH)"), "dkh")
        self.assertEqual(normalise_heading("Temp (°C)"), "temp")

    def test_phosphate_does_not_steal_the_ph_column(self):
        from harvester.nutrients import map_headings
        headings = {"6": "pH", "11": "PO₄³⁻"}
        mapping = map_headings(headings)
        self.assertEqual(mapping["ph"], "6")
        self.assertEqual(mapping["po4"], "11")

    def test_csv_export_parses_with_carried_dates(self):
        from harvester.nutrients import parse_csv
        text = "\n".join([
            self.HEADERS,
            ",13/08/2026,12:38:00,A12,14.1,36.18,7.4,15,0,0,,,400,",
            ",,,A345,15.6,36.93,7.4,10,0,0,,,400,",
            ",,,,,,,,,,,,,",
            ",21/9/26,,E12,16.9,33.8,8.3,,,,,,573,1560",
        ])
        records = parse_csv(text, {"SA12": {}, "SA345": {}, "SE12": {}})
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["sample_date"], "2026-08-13")
        self.assertEqual(records[0]["sample_time"], "12:38")
        self.assertEqual(records[1]["sample_date"], "2026-08-13")  # carried down
        self.assertEqual(records[1]["sump_code"], "SA345")
        self.assertEqual(records[2]["sample_date"], "2026-09-21")
        self.assertEqual(records[2]["mg"], 1560.0)

    def test_a_reordered_sheet_still_maps_correctly(self):
        from harvester.nutrients import parse_csv
        text = "\n".join([
            "Tank ID,Date,pH,Temp (°C),NO₃ (mg/L),Salinity (ppt)",
            "A12,13/08/2026,7.4,14.1,0.5,36.18",
        ])
        record = parse_csv(text, {"SA12": {}})[0]
        self.assertEqual(record["ph"], 7.4)
        self.assertEqual(record["temp_c"], 14.1)
        self.assertEqual(record["no3"], 0.5)
        self.assertEqual(record["salinity_ppt"], 36.18)

    def test_sheet_urls_normalise_to_a_csv_endpoint(self):
        from harvester.nutrients import sheet_csv_url
        self.assertIn(
            "format=csv",
            sheet_csv_url("https://docs.google.com/spreadsheets/d/ABC123/edit?usp=sharing"),
        )
        published = "https://docs.google.com/spreadsheets/d/e/2PACX-1vABC/pubhtml"
        self.assertIn("output=csv", sheet_csv_url(published))
        already = "https://docs.google.com/spreadsheets/d/e/2PACX/pub?gid=7&single=true&output=csv"
        self.assertEqual(sheet_csv_url(already), already)

    def test_an_unparseable_sheet_raises_rather_than_returning_nothing(self):
        from harvester.nutrients import NutrientError, parse_csv
        with self.assertRaises(NutrientError):
            parse_csv("some,unrelated,csv\n1,2,3\n", {"SA12": {}})


if __name__ == "__main__":
    unittest.main()

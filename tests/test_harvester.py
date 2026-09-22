"""Unit tests. Run with: python -m unittest discover -s tests -v"""

import datetime as dt
import json
import math
import os
import tempfile
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvester.export import _daily_stats, _slides, build_payload
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
        self.assertNotIn("par", r.values)  # light metrics are no longer kept
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



class TestMaintenance(unittest.TestCase):
    """The issue log is the nursery's record, so parsing must not invent state."""

    ISSUE_HEADERS = ",Date,Time,Equipment,Tank ID,Fault/Maintenance,Action Taken,Resp. Person,Status,Severity"

    def _issues(self, *lines):
        from harvester.maintenance import parse_issues
        from harvester.nutrients import parse_csv_rows
        return parse_issues(parse_csv_rows("\n".join((self.ISSUE_HEADERS,) + lines)))

    def test_status_words_people_actually_type(self):
        rows = self._issues(
            ",13/08/2026,,Pump,SA12,Noise,,Jules,Closed,",
            ",13/08/2026,,Pump,SB12,Leak,,Jules,WIP,",
            ",13/08/2026,,Pump,SC12,Drip,,Jules,Outstanding,",
        )
        self.assertEqual([i["status"] for i in rows], ["resolved", "in_progress", "open"])

    def test_an_unknown_status_is_kept_not_discarded(self):
        row = self._issues(",13/08/2026,,Pump,SA12,Noise,,Jules,Waiting for Pedro,")[0]
        self.assertEqual(row["status"], "Waiting for Pedro")

    def test_a_blank_status_with_no_resolution_date_is_open(self):
        row = self._issues(",13/08/2026,,Pump,SA12,Noise,,,,")[0]
        self.assertEqual(row["status"], "open")

    def test_rows_without_a_description_are_not_issues(self):
        rows = self._issues(
            ",13/08/2026,,Pump,SA12,,,,,",
            ",,,,,,,,,",
            ",13/08/2026,,Pump,SB12,Real fault,,,,",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "Real fault")

    def test_issue_ids_are_stable_and_unique(self):
        rows = self._issues(
            ",13/08/2026,,Pump,SA12,One,,,,",
            ",13/08/2026,,Pump,SB12,Two,,,,",
        )
        self.assertEqual(len({r["issue_id"] for r in rows}), 2)

    def test_next_due_comes_from_frequency_when_not_given(self):
        from harvester.maintenance import parse_schedule
        from harvester.nutrients import parse_csv_rows
        import datetime as dt
        text = "\n".join([
            "Task ID,Task,Tank ID,Equipment,Frequency (days),Last done,Done by",
            "T1,Replace slide,SA12,Seneye,30,01/09/2026,Jules",
            "T2,Calibrate,,Refractometer,180,,",
        ])
        jobs = parse_schedule(parse_csv_rows(text), today=dt.date(2026, 10, 5))
        self.assertEqual(jobs[0]["next_due"], "2026-10-01")
        self.assertEqual(jobs[0]["days_until_due"], -4)
        self.assertIsNone(jobs[1]["next_due"])       # never done, no guess
        self.assertIsNone(jobs[1]["days_until_due"])

    def test_the_sheet_is_the_record_so_deleted_rows_disappear(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        cols = ("issue_id", "summary", "status")
        store.replace_table("issues", cols, [
            {"issue_id": "a", "summary": "one", "status": "open"},
            {"issue_id": "b", "summary": "two", "status": "open"},
        ])
        store.replace_table("issues", cols, [
            {"issue_id": "a", "summary": "one", "status": "resolved"},
        ])
        rows = store.query("SELECT issue_id, status FROM issues")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "resolved")
        store.close()

    def test_anonymise_strips_names_from_the_export(self):
        from harvester.maintenance import build_payload
        store = Store("sqlite://:memory:")
        store.migrate()
        store.replace_table(
            "issues",
            ("issue_id", "summary", "status", "reported_by", "assigned_to"),
            [{"issue_id": "a", "summary": "one", "status": "open",
              "reported_by": "Jules", "assigned_to": "Alice"}],
        )
        payload = build_payload(store, {"maintenance": {"anonymise": True}})
        self.assertIsNone(payload["issues"][0]["reported_by"])
        self.assertIsNone(payload["issues"][0]["assigned_to"])
        self.assertEqual(payload["issues"][0]["summary"], "one")
        self.assertTrue(payload["anonymised"])
        store.close()

class TestAnalyteGrouping(unittest.TestCase):
    """Physical and chemical measurements stay in their own families."""

    def test_every_analyte_has_a_known_group(self):
        from harvester.nutrients import ANALYTES, ANALYTE_GROUPS
        known = {key for key, _ in ANALYTE_GROUPS}
        for entry in ANALYTES:
            self.assertEqual(len(entry), 5, entry)
            self.assertIn(entry[4], known, entry[0])

    def test_physical_and_chemical_families(self):
        """pH and carbonate hardness sit with the physical measurements, as
        the nursery reads them: they come off the same handheld kit as
        temperature and salinity rather than a nutrient assay."""
        from harvester.nutrients import ANALYTES
        groups = {a[0]: a[4] for a in ANALYTES}
        for key in ("temp_c", "salinity_ppt", "ph", "dkh"):
            self.assertEqual(groups[key], "physical", key)
        for key in ("no3", "no2", "nh3", "po4", "ca", "mg"):
            self.assertEqual(groups[key], "chemical", key)

    def test_export_only_offers_groups_that_were_measured(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        store.upsert_nutrients([
            {"sample_date": "2026-09-21", "sump_code": "SA12", "no3": 0.4},
        ])
        payload = build_payload(store, {})["nutrients"]
        self.assertEqual([g["key"] for g in payload["groups"]], ["chemical"])
        self.assertEqual([a["key"] for a in payload["analytes"]], ["no3"])
        store.close()

class TestReferenceRanges(unittest.TestCase):
    """The seawater reference ranges must stay internally consistent."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "config.json"), encoding="utf-8") as fh:
            self.config = json.load(fh)
        self.reference = {
            k: v for k, v in self.config["insitu_reference"].items()
            if not k.startswith("_")
        }

    def test_every_analyte_has_a_reference(self):
        from harvester.nutrients import ANALYTES
        for entry in ANALYTES:
            self.assertIn(entry[0], self.reference, entry[0])

    def test_typical_sits_inside_outer(self):
        for key, ref in self.reference.items():
            t, o = ref["typical"], ref["outer"]
            self.assertLess(t[0], t[1], key)
            self.assertLess(o[0], o[1], key)
            self.assertLessEqual(o[0], t[0], key)
            self.assertGreaterEqual(o[1], t[1], key)

    def test_every_reference_records_its_basis(self):
        for key, ref in self.reference.items():
            self.assertTrue(ref.get("basis"), key)

    def test_known_seawater_values_land_in_the_typical_band(self):
        """Textbook seawater should read as typical, not flagged."""
        seawater = {
            "salinity_ppt": 36.5,   # Strait of Gibraltar surface
            "ph": 8.1,              # surface ocean
            "dkh": 7.4,             # ~2570 umol/kg alkalinity
            "ca": 430.0,            # 412 mg/L at S=35, scaled
            "mg": 1345.0,
            "no3": 0.06,            # ~1 umol/L
            "no2": 0.005,
            "po4": 0.015,
        }
        for key, value in seawater.items():
            t = self.reference[key]["typical"]
            self.assertTrue(t[0] <= value <= t[1],
                            f"{key}={value} outside typical {t}")

    def test_reference_reaches_the_dashboard_payload(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        store.upsert_nutrients([
            {"sample_date": "2026-09-21", "sump_code": "SA12", "ca": 578.0},
        ])
        analyte = build_payload(store, self.config)["nutrients"]["analytes"][0]
        self.assertEqual(analyte["key"], "ca")
        self.assertEqual(analyte["typical"], [400.0, 455.0])
        self.assertEqual(analyte["outer"], [360.0, 520.0])
        store.close()

class TestDerivedValues(unittest.TestCase):
    """Modelled values are checked against published tables, not just against
    themselves, and refuse to answer outside the fits' stated range."""

    def test_oxygen_saturation_matches_published_tables(self):
        from harvester.derived import oxygen_at_saturation
        # Benson and Krause values as tabulated by the USGS, mg/L at 1 atm
        for temp, salinity, expected in (
            (10.0, 0.0, 11.29),
            (20.0, 0.0, 9.08),
            (10.0, 35.0, 9.03),
            (20.0, 35.0, 7.38),
        ):
            got = oxygen_at_saturation(temp, salinity)
            self.assertAlmostEqual(got, expected, delta=0.03,
                                   msg=f"{temp}C S={salinity}: got {got}")

    def test_free_ammonia_fraction_is_a_few_percent_in_seawater(self):
        from harvester.derived import free_ammonia_fraction
        fraction = free_ammonia_fraction(17.0, 8.1, 36.5)
        self.assertTrue(0.02 < fraction < 0.05, fraction)

    def test_raising_ph_raises_the_free_ammonia_share(self):
        from harvester.derived import free_ammonia_fraction
        low = free_ammonia_fraction(17.0, 7.8, 36.5)
        high = free_ammonia_fraction(17.0, 8.4, 36.5)
        self.assertGreater(high, low * 2)

    def test_ammonium_needs_all_three_inputs(self):
        from harvester.derived import ammonium_from_free_ammonia
        self.assertIsNone(ammonium_from_free_ammonia(0.005, 17.0, None, 36.5))
        self.assertIsNone(ammonium_from_free_ammonia(0.005, None, 8.1, 36.5))
        self.assertIsNone(ammonium_from_free_ammonia(0.005, 17.0, 8.1, None))
        self.assertIsNone(ammonium_from_free_ammonia(None, 17.0, 8.1, 36.5))

    def test_zero_free_ammonia_gives_zero_ammonium(self):
        from harvester.derived import ammonium_from_free_ammonia
        self.assertEqual(ammonium_from_free_ammonia(0.0, 17.0, 8.1, 36.5), 0.0)

    def test_models_refuse_to_extrapolate(self):
        from harvester.derived import ammonium_from_free_ammonia, oxygen_at_saturation
        self.assertIsNone(oxygen_at_saturation(60.0, 36.0))
        self.assertIsNone(oxygen_at_saturation(20.0, 90.0))
        self.assertIsNone(ammonium_from_free_ammonia(0.005, 17.0, 12.0, 36.5))

    def test_ammonium_exceeds_free_ammonia_at_seawater_ph(self):
        """At pH ~8 most of the pool is ammonium, so NH4 should dwarf NH3."""
        from harvester.derived import ammonium_from_free_ammonia
        nh4 = ammonium_from_free_ammonia(0.005, 17.1, 7.98, 36.6)
        self.assertGreater(nh4, 0.005 * 10)

    def test_light_metrics_are_gone(self):
        from harvester.seneye import PARAMETERS
        for key in ("par", "lux", "kelvin"):
            self.assertNotIn(key, PARAMETERS)

    def test_derived_fields_reach_the_payload_and_are_flagged(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        now = int(time.time())
        store.upsert_device("1", "SA12", 1, "SA12", "A", "SA12", now)
        store.insert_readings([{
            "device_id": "1", "reading_time": now - 60, "fetched_at": now,
            "temperature": 17.1, "ph": 7.98, "nh3": 0.005,
        }])
        store.upsert_nutrients([
            {"sample_date": "2026-09-21", "sump_code": "SA12", "salinity_ppt": 36.6},
        ])
        config = {
            "sumps": {"SA12": {"system": "A", "tanks": ["A1"]}},
            "derived": {"enabled": True, "default_salinity": 36.5},
            "parameters": {"nh4": {"label": "Ammonium", "model_note": "note"}},
        }
        payload = build_payload(store, config)
        by_key = {p["key"]: p for p in payload["parameters"]}
        self.assertIn("nh4", by_key)
        self.assertIn("o2_sat", by_key)
        self.assertTrue(by_key["nh4"]["modelled"])
        self.assertFalse(by_key["temperature"]["modelled"])
        self.assertEqual(by_key["nh4"]["model_note"], "note")
        store.close()

    def test_derived_can_be_switched_off(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        now = int(time.time())
        store.upsert_device("1", "SA12", 1, "SA12", "A", "SA12", now)
        store.insert_readings([{
            "device_id": "1", "reading_time": now - 60, "fetched_at": now,
            "temperature": 17.1, "ph": 7.98, "nh3": 0.005,
        }])
        payload = build_payload(store, {"derived": {"enabled": False}})
        keys = [p["key"] for p in payload["parameters"]]
        self.assertNotIn("nh4", keys)
        self.assertNotIn("o2_sat", keys)
        store.close()


class TestSlideCountdown(unittest.TestCase):
    """When each sump's slide is due to be replaced."""

    DEVICES = [{"device_id": "1", "sump_code": "SA12"},
               {"device_id": "2", "sump_code": "SB34"}]
    NOW = int(dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.timezone.utc).timestamp())
    DUE_14_OCT = int(dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc).timestamp())

    def slides(self, cfg, latest=None):
        return _slides(self.DEVICES, latest or {}, {"slides": cfg}, self.NOW)

    def test_logged_change_date_plus_the_interval(self):
        out = self.slides({"interval_days": 30, "default_changed": "2026-09-14"})
        self.assertEqual(out["1"]["due"], self.DUE_14_OCT)
        self.assertEqual(out["1"]["source"], "logged")
        # The 14th of September plus 30 days, read on the 21st, is 23 days off.
        self.assertEqual(math.ceil((out["1"]["due"] - self.NOW) / 86400), 23)

    def test_a_per_sump_date_overrides_the_default(self):
        out = self.slides({"interval_days": 30, "default_changed": "2026-09-14",
                           "changed": {"SB34": "2026-09-20", "_comment": "ignored"}})
        self.assertEqual(out["1"]["due"], self.DUE_14_OCT)
        self.assertGreater(out["2"]["due"], out["1"]["due"])

    def test_the_sensor_expiry_wins_when_it_reports_one(self):
        reported = self.NOW + 5 * 86400
        out = self.slides({"interval_days": 30, "default_changed": "2026-09-14"},
                          {"1": {"slide_expires": reported, "slide_serial": "SLD-1"}})
        self.assertEqual(out["1"]["due"], reported)
        self.assertEqual(out["1"]["source"], "sensor")
        self.assertEqual(out["1"]["serial"], "SLD-1")
        self.assertEqual(out["2"]["source"], "logged")

    def test_an_absurd_sensor_expiry_is_ignored(self):
        out = self.slides({"interval_days": 30, "default_changed": "2026-09-14"},
                          {"1": {"slide_expires": 0}, "2": {"slide_expires": 4102444800}})
        self.assertEqual(out["1"]["source"], "logged")
        self.assertEqual(out["2"]["source"], "logged")

    def test_trust_sensor_false_goes_by_the_logged_dates_only(self):
        out = self.slides({"interval_days": 30, "default_changed": "2026-09-14",
                           "trust_sensor": False},
                          {"1": {"slide_expires": self.NOW + 5 * 86400}})
        self.assertEqual(out["1"]["due"], self.DUE_14_OCT)

    def test_no_date_anywhere_means_no_countdown_rather_than_a_guess(self):
        self.assertEqual(self.slides({"interval_days": 30}), {})
        self.assertEqual(self.slides({"default_changed": "not a date"}), {})

    def test_can_be_switched_off(self):
        self.assertEqual(
            self.slides({"enabled": False, "default_changed": "2026-09-14"}), {})

    def test_the_payload_carries_the_interval_and_warning_threshold(self):
        store = Store("sqlite://:memory:")
        store.migrate()
        now = int(time.time())
        store.upsert_device("1", "SA12", 1, "SA12", "A", "SA12", now)
        store.insert_readings([{
            "device_id": "1", "reading_time": now - 60, "fetched_at": now,
            "temperature": 17.1, "ph": 7.98, "nh3": 0.005,
        }])
        payload = build_payload(store, {"slides": {
            "interval_days": 30, "warn_days": 7, "default_changed": "2026-09-14"}})
        self.assertEqual(payload["slides"]["interval_days"], 30)
        self.assertEqual(payload["slides"]["warn_days"], 7)
        self.assertIn("1", payload["slides"]["by_device"])
        store.close()



# --------------------------------------------------------------------------
# Historical CSV import (tools/import_history.py)
# --------------------------------------------------------------------------

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import import_history as ih  # noqa: E402


class Args:
    """Stand-in for the argparse namespace import_file expects."""

    def __init__(self, **kw):
        self.timezone = "UTC"
        self.sump = None
        self.month_first = False
        self.tag = "IMPORTED"
        self.dry_run = False
        self.__dict__.update(kw)


CONFIG = {"devices": {
    "_comment": "ignored",
    "168779": {"sump": "SA12"},
    "166115": {"sump": "SA345"},
    "165185": {"sump": "SB12"},
}}


class TestImportColumns(unittest.TestCase):
    def test_matches_headings_with_units_and_punctuation(self):
        cols = ih.match_columns(
            ["Date/Time", "Device ID", "Temperature (\u00b0C)", "pH", "NH3 (mg/L)"])
        self.assertEqual(cols["timestamp"], 0)
        self.assertEqual(cols["device_id"], 1)
        self.assertEqual(cols["temperature"], 2)
        self.assertEqual(cols["ph"], 3)
        self.assertEqual(cols["nh3"], 4)

    def test_ammonium_is_not_mistaken_for_ammonia(self):
        cols = ih.match_columns(["Timestamp", "Free Ammonia", "Ammonium"])
        self.assertEqual(cols["nh3"], 1)
        self.assertEqual(cols["nh4"], 2)

    def test_unrecognised_columns_are_ignored(self):
        cols = ih.match_columns(["Timestamp", "Slide batch", "Kelvin", "PAR"])
        self.assertEqual(set(cols), {"timestamp"})


class TestImportDates(unittest.TestCase):
    def when(self, headings, row, **kw):
        return ih.parse_when(row, ih.match_columns(headings), **kw)

    def test_iso_timestamp(self):
        got = self.when(["Timestamp"], ["2025-03-04 09:15:00"])
        self.assertEqual(got, dt.datetime(2025, 3, 4, 9, 15))

    def test_unix_seconds_and_milliseconds(self):
        secs = self.when(["Timestamp"], ["1700000000"])
        millis = self.when(["Timestamp"], ["1700000000000"])
        self.assertEqual(secs, millis)

    def test_separate_date_and_time_columns(self):
        got = self.when(["Date", "Time"], ["04/03/2025", "09:15"])
        self.assertEqual(got, dt.datetime(2025, 3, 4, 9, 15))

    def test_date_and_time_columns_the_other_way_round(self):
        got = self.when(["Time", "Date"], ["09:15", "04/03/2025"])
        self.assertEqual(got, dt.datetime(2025, 3, 4, 9, 15))

    def test_day_first_is_the_default_and_month_first_is_opt_in(self):
        headings, row = ["Timestamp"], ["04/03/2025 09:15"]
        self.assertEqual(self.when(headings, row).month, 3)
        self.assertEqual(self.when(headings, row, dayfirst=False).month, 4)

    def test_unreadable_date_is_none_rather_than_a_guess(self):
        self.assertIsNone(self.when(["Timestamp"], ["last Tuesday"]))

    def test_timezone_is_applied_when_given(self):
        naive = dt.datetime(2025, 7, 1, 12, 0)
        utc = ih.to_unix(naive, "UTC")
        gib = ih.to_unix(naive, "Europe/Gibraltar")
        self.assertEqual(utc - gib, 7200)  # BST-equivalent summer offset


class TestImportDeviceMatching(unittest.TestCase):
    def resolve(self, headings, row, sump=None, filename="export.csv"):
        return ih.device_for(row, ih.match_columns(headings), CONFIG, sump, filename)

    def test_device_id_column(self):
        self.assertEqual(self.resolve(["Device ID"], ["166115"])[0], "166115")

    def test_sump_named_in_a_column(self):
        self.assertEqual(self.resolve(["Sump"], ["Sump SA12"])[0], "168779")

    def test_longer_sump_code_wins(self):
        self.assertEqual(self.resolve(["Sump"], ["SA345"])[0], "166115")

    def test_falls_back_to_the_sump_argument_then_the_filename(self):
        self.assertEqual(self.resolve(["Note"], ["x"], sump="SB12")[0], "165185")
        self.assertEqual(
            self.resolve(["Note"], ["x"], filename="/tmp/seneye_SA345_2025.csv")[0],
            "166115")

    def test_unknown_device_is_reported_not_guessed(self):
        device, problem = self.resolve(["Device ID"], ["999999"])
        self.assertIsNone(device)
        self.assertIn("999999", problem)


class TestImportEndToEnd(unittest.TestCase):
    def write(self, name, text):
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store("sqlite://:memory:")
        self.store.migrate()

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_imports_and_is_safe_to_run_twice(self):
        path = self.write("history.csv", (
            "Seneye Home export\n"
            "Date/Time,Device ID,Temperature (\u00b0C),pH,NH3 (mg/L)\n"
            "2025-03-04 09:15:00,168779,17.2,8.01,0.004\n"
            "2025-03-04 10:15:00,168779,17.4,8.02,0.005\n"
            "2025-03-04 09:15:00,166115,17.9,7.95,0.006\n"
        ))
        read, written, skipped = ih.import_file(path, self.store, CONFIG, Args())
        self.assertEqual((read, written), (3, 3))
        self.assertEqual(skipped, {})
        again = ih.import_file(path, self.store, CONFIG, Args())
        self.assertEqual(again[1], 0)
        rows = self.store.query("SELECT * FROM readings ORDER BY reading_time")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["slide_serial"], "IMPORTED")
        self.assertAlmostEqual(rows[0]["temperature"], 17.2)

    def test_dry_run_writes_nothing(self):
        path = self.write("history.csv", (
            "Timestamp,Device ID,Temp,pH,NH3\n"
            "2025-03-04 09:15,168779,17.2,8.01,0.004\n"
        ))
        read, written, _ = ih.import_file(path, self.store, CONFIG, Args(dry_run=True))
        self.assertEqual((read, written), (1, 0))
        self.assertEqual(self.store.query("SELECT * FROM readings"), [])

    def test_one_file_per_device_with_the_sump_in_the_filename(self):
        path = self.write("SA345.csv", (
            "Date,Time,Temperature,pH,Free Ammonia\n"
            "04/03/2025,09:15,17.9,7.95,0.006\n"
        ))
        read, written, _ = ih.import_file(path, self.store, CONFIG, Args())
        self.assertEqual((read, written), (1, 1))
        rows = self.store.query("SELECT device_id FROM readings")
        self.assertEqual(rows[0]["device_id"], "166115")

    def test_bad_rows_are_counted_rather_than_aborting_the_file(self):
        path = self.write("history.csv", (
            "Timestamp,Device ID,Temp,pH,NH3\n"
            "2025-03-04 09:15,168779,17.2,8.01,0.004\n"
            "not a date,168779,17.3,8.01,0.004\n"
            "2025-03-04 11:15,999999,17.4,8.01,0.004\n"
            "2025-03-04 12:15,168779,,,\n"
        ))
        read, written, skipped = ih.import_file(path, self.store, CONFIG, Args())
        self.assertEqual((read, written), (1, 1))
        self.assertEqual(skipped["unreadable date"], 1)
        self.assertEqual(skipped["no values"], 1)
        self.assertEqual(sum(skipped.values()), 3)

    def test_registers_a_device_the_harvester_has_never_polled(self):
        path = self.write("history.csv", (
            "Timestamp,Device ID,Temp,pH,NH3\n"
            "2025-03-04 09:15,166115,17.2,8.01,0.004\n"
        ))
        ih.import_file(path, self.store, dict(CONFIG, sumps={
            "SA345": {"system": "A", "tanks": ["A3", "A4", "A5"]}}), Args())
        rows = self.store.query("SELECT * FROM devices")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sump_code"], "SA345")
        self.assertEqual(rows[0]["system_code"], "A")
        self.assertIn("A3", rows[0]["label"])

    def test_an_import_does_not_wind_back_last_seen_on_a_known_device(self):
        now = int(time.time())
        self.store.upsert_device("168779", "SA12", 1, "SA12", "A", "SA12", now)
        path = self.write("history.csv", (
            "Timestamp,Device ID,Temp,pH,NH3\n"
            "2025-03-04 09:15,168779,17.2,8.01,0.004\n"
        ))
        ih.import_file(path, self.store, CONFIG, Args())
        rows = self.store.query("SELECT * FROM devices")
        self.assertEqual(rows[0]["last_seen"], now)

    def test_dry_run_reports_the_overlap_with_what_is_already_stored(self):
        now = int(time.time() // 1800 * 1800)
        self.store.upsert_device("168779", "SA12", 1, "SA12", "A", "SA12", now)
        self.store.insert_readings([{
            "device_id": "168779", "reading_time": now - 1800, "fetched_at": now,
            "temperature": 17.2, "ph": 8.01, "nh3": 0.004,
        }])
        when = dt.datetime.fromtimestamp(now - 1800, dt.timezone.utc)
        path = self.write("history.csv", (
            "Timestamp,Device ID,Temp,pH,NH3\n"
            + when.strftime("%Y-%m-%d %H:%M") + ",168779,17.2,8.01,0.004\n"
            + (when - dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
            + ",168779,17.3,8.02,0.004\n"
        ))
        read, written, skipped = ih.import_file(path, self.store, CONFIG, Args(dry_run=True))
        self.assertEqual((read, written), (2, 0))
        self.assertEqual(skipped["_already"], 1)

    def test_a_shifted_export_collides_but_does_not_agree(self):
        """The timezone check: same timestamps, different numbers."""
        now = int(time.time() // 1800 * 1800)
        self.store.upsert_device("168779", "SA12", 1, "SA12", "A", "SA12", now)
        rows = []
        for i in range(8):
            rows.append({"device_id": "168779", "reading_time": now - i * 1800,
                         "fetched_at": now, "temperature": 17.0 + i * 0.5,
                         "ph": 8.0, "nh3": 0.004})
        self.store.insert_readings(rows)
        batch_same = [{"device_id": "168779", "reading_time": r["reading_time"],
                       "temperature": r["temperature"], "ph": 8.0, "nh3": 0.004}
                      for r in rows]
        already, agreeing = ih.compare_existing(self.store, batch_same)
        self.assertEqual((already, agreeing), (8, 8))

        # The same readings shifted by an hour: they still land on stored
        # timestamps, but on the wrong ones, so the values no longer agree.
        batch_shifted = [{"device_id": "168779",
                          "reading_time": r["reading_time"] - 3600,
                          "temperature": r["temperature"], "ph": 8.0,
                          "nh3": 0.004} for r in rows]
        already, agreeing = ih.compare_existing(self.store, batch_shifted)
        self.assertGreater(already, 0)
        self.assertLess(agreeing, already)

    def test_a_file_with_no_usable_header_is_skipped_quietly(self):
        path = self.write("notes.csv", "some,random,notes\na,b,c\n")
        self.assertEqual(ih.import_file(path, self.store, CONFIG, Args()),
                         (0, 0, {}))



if __name__ == "__main__":
    unittest.main()

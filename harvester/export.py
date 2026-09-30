"""Export compact JSON for the dashboard.

The dashboard is a single static HTML file that reads one JSON document, so it
can be dropped on the TNP website (or GitHub Pages) with no server behind it.

Written to dashboard/data/nursery.json:

    generated_at   unix seconds
    window_days    how many days of raw readings are included
    devices[]      device_id, label, tank_code, type, n readings, last_seen
    parameters[]   key, label, unit, precision, reference band (from config)
    latest{}       device_id -> most recent reading + device health flags
    readings[]     raw rows in the window: [device_id, t, temperature, ph, nh3, ...]
    daily[]        per device per day: n, min, max, mean, sd for each parameter
    slides{}       when each sump's slide is due for replacement
"""

from __future__ import annotations

import datetime
import json
import math
import os
import time
from collections import defaultdict
from typing import Any

from .derived import DERIVED_PARAMETERS, enrich, salinity_by_sump
from .nutrients import ANALYTE_GROUPS as NUTRIENT_GROUPS
from .nutrients import ANALYTES as NUTRIENT_ANALYTES
from .seneye import PARAMETERS

DAY = 86400


def _parse_day(text: Any) -> int | None:
    """A YYYY-MM-DD date from config as unix seconds at midnight UTC."""
    if not text:
        return None
    try:
        day = datetime.datetime.strptime(str(text).strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return int(day.replace(tzinfo=datetime.timezone.utc).timestamp())


def _slides(devices, latest, config, now):
    """When each sump's slide is due to be replaced.

    A Seneye slide lasts 30 days and is changed at the unit in the sump, so
    this is per sump, not per tank. Two possible answers, in order:

    * the expiry the sensor itself reports, when it reports one and it is not
      absurd. The device knows when its own slide was registered, so this beats
      anything written down by hand.
    * the date the change was logged in config.json, plus `interval_days`.

    Which one was used is exported alongside the date, because "the sensor says
    so" and "someone wrote it down" are worth telling apart when a slide turns
    out to have been missed. The number of days left is deliberately NOT worked
    out here: the dashboard counts it in the browser, so the figure a student
    reads is right for the day they read it rather than the day of the last
    harvest.
    """
    cfg = config.get("slides", {}) or {}
    if not cfg.get("enabled", True):
        return {}
    interval = int(cfg.get("interval_days", 30) or 30)
    trust_sensor = bool(cfg.get("trust_sensor", True))
    changed_cfg = {k: v for k, v in (cfg.get("changed") or {}).items()
                   if not k.startswith("_")}
    default_changed = _parse_day(cfg.get("default_changed"))

    # A sensor expiry more than a year back or two months ahead is not a slide
    # date, it is a default or a glitch, so it is not allowed to drive the
    # countdown.
    floor, ceiling = now - 365 * DAY, now + 60 * DAY

    out = {}
    for d in devices:
        did, sump = d["device_id"], d.get("sump_code")
        logged = _parse_day(changed_cfg.get(sump)) if sump else None
        if logged is None:
            logged = default_changed

        entry = None
        if trust_sensor:
            reported = (latest.get(did) or {}).get("slide_expires")
            try:
                reported = int(reported) if reported else None
            except (TypeError, ValueError):
                reported = None
            if reported and floor <= reported <= ceiling:
                entry = {"due": reported, "source": "sensor",
                         "changed": reported - interval * DAY}

        if entry is None and logged is not None:
            entry = {"due": logged + interval * DAY, "source": "logged",
                     "changed": logged}

        if entry is None:
            continue
        entry["serial"] = (latest.get(did) or {}).get("slide_serial")
        out[did] = entry
    return out


def build_payload(
    store, config: dict[str, Any], window_days: int = 90, raw_days: int = 30
) -> dict[str, Any]:
    """Daily statistics cover the whole record; raw readings only the last
    `raw_days`, so the file the browser downloads stays small as the series
    grows. `window_days` caps how far back the daily statistics reach."""
    now = int(time.time())
    cutoff = now - window_days * DAY
    raw_cutoff = now - raw_days * DAY

    params_cfg = config.get("parameters", {})

    devices = store.query(
        "SELECT device_id, description, device_type, sump_code, system_code, "
        "label, first_seen, last_seen FROM devices ORDER BY system_code, sump_code"
    )

    all_rows = store.query(
        "SELECT * FROM readings WHERE reading_time >= ? ORDER BY reading_time",
        (cutoff,),
    )
    # Modelled fields are added to the rows here rather than stored, so a
    # change to the model shows up on the next export without re-harvesting.
    sump_of = {d["device_id"]: d.get("sump_code") for d in devices}
    derived_cfg = config.get("derived", {}) or {}
    produced = enrich(
        all_rows,
        sump_of,
        salinity_by_sump(store, derived_cfg.get("default_salinity")),
        config,
    )

    rows = [r for r in all_rows if int(r["reading_time"]) >= raw_cutoff]

    measured = [p for p in PARAMETERS
                if p not in DERIVED_PARAMETERS
                and any(r.get(p) is not None for r in all_rows)]
    active = measured + [p for p in produced]
    if not active:
        active = [p for p in ("temperature", "ph", "nh3") if p in PARAMETERS]

    readings = [
        [r["device_id"], int(r["reading_time"])] + [_round(r.get(p)) for p in active]
        for r in rows
    ]

    # Latest, daily statistics and reading counts all come from the WHOLE
    # window, not from the raw slice. Taking them from `rows` meant a year of
    # imported history produced thirty days of daily statistics and nothing
    # else: the dashboard's 90-day and All views had nothing to draw, and a
    # device that had stopped reporting lost its last reading entirely.
    latest: dict[str, Any] = {}
    for r in all_rows:
        did = r["device_id"]
        prev = latest.get(did)
        if prev is None or r["reading_time"] > prev["t"]:
            latest[did] = {
                "t": int(r["reading_time"]),
                "values": {p: _round(r.get(p)) for p in active},
                "statuses": {p: r.get(f"{p}_status") for p in active},
                "slide_serial": r.get("slide_serial"),
                "slide_expires": r.get("slide_expires"),
                "out_of_water": r.get("out_of_water"),
                "disconnected": r.get("disconnected"),
            }

    daily = _daily_stats(all_rows, active)

    counts: dict[str, int] = defaultdict(int)
    for r in all_rows:
        counts[r["device_id"]] += 1

    sumps_cfg = config.get("sumps", {})
    systems_cfg = config.get("systems", {})

    device_out = []
    for d in devices:
        did = d["device_id"]
        sump = d.get("sump_code")
        sump_cfg = sumps_cfg.get(sump, {}) if sump else {}
        system = d.get("system_code") or sump_cfg.get("system")
        device_out.append(
            {
                "device_id": did,
                "sump": sump,
                "system": system,
                "system_label": (systems_cfg.get(system, {}) or {}).get(
                    "label", f"System {system}" if system else None
                ),
                "tanks": sump_cfg.get("tanks", []),
                "group": sump_cfg.get("group"),
                "label": d.get("label") or sump or d.get("description"),
                "description": d.get("description"),
                "type": d.get("device_type"),
                "n_readings": counts.get(did, 0),
                "first_seen": d.get("first_seen"),
                "last_seen": d.get("last_seen"),
            }
        )

    parameters = [
        {
            "key": p,
            "label": params_cfg.get(p, {}).get("label", p),
            "unit": params_cfg.get(p, {}).get("unit", ""),
            "precision": params_cfg.get(p, {}).get("precision", 2),
            "band": params_cfg.get(p, {}).get("band"),
            "hard": params_cfg.get(p, {}).get("hard"),
            "modelled": p in DERIVED_PARAMETERS,
            "model_note": params_cfg.get(p, {}).get("model_note"),
        }
        for p in active
    ]

    nutrients = _nutrients(store, config)

    last_run = store.query(
        "SELECT started_at, finished_at, status, readings_inserted, message "
        "FROM harvest_runs ORDER BY run_id DESC LIMIT 1"
    )

    return {
        "generated_at": now,
        "window_days": window_days,
        "site": config.get("site", {}),
        "method_note": (config.get("nutrients", {}) or {}).get("method_note"),
        "devices": device_out,
        "parameters": parameters,
        "columns": ["device_id", "t"] + active,
        "readings": readings,
        "daily": daily,
        "latest": latest,
        "slides": {
            "interval_days": int((config.get("slides", {}) or {}).get("interval_days", 30) or 30),
            "warn_days": int((config.get("slides", {}) or {}).get("warn_days", 7) or 7),
            "by_device": _slides(devices, latest, config, now),
        },
        "nutrients": nutrients,
        "plugs": _plugs(store, config, now),
        "ambient": _ambient(store, config, now, window_days, raw_days),
        "last_run": last_run[0] if last_run else None,
    }


AMBIENT_PARAMETERS = (
    ("air_temperature", "Air temperature", "°C", 1),
    ("humidity", "Relative humidity", "%", 0),
)


def _plugs(store, config: dict[str, Any], now: int, history_days: int = 14) -> dict[str, Any]:
    """Chiller plug state per sump, read-only.

    The dashboard never switches anything. It is a static page on a public
    website, so it can hold no credential, and a button on it would be a button
    anyone could press. What it can usefully say is which chiller is on, since
    when, and whether the plug has stopped answering: "SC34 is 21.5 degrees and
    its chiller went off at 14:00" is a sentence that needs no ability to
    switch anything to be worth reading.
    """
    cfg = config.get("plugs") or {}
    if not cfg.get("enabled", False):
        return {"enabled": False}

    stale_after = int(float(cfg.get("stale_minutes", 90) or 90) * 60)

    try:
        rows = store.query(
            "SELECT device_id, socket, changed_at, last_seen, sump_code, role, "
            "on_state, online, power_w FROM plug_states ORDER BY changed_at"
        )
    except Exception:  # table not created yet on an older database
        return {"enabled": True, "by_sump": {}, "sockets": [], "history": []}

    # The current row for each socket is its most recent transition. Its
    # changed_at is therefore also the answer to "since when", which is why
    # the table is written as transitions in the first place.
    current: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        current[(r["device_id"], r["socket"])] = r

    labels = {}
    for did, dcfg in (cfg.get("devices") or {}).items():
        if did.startswith("_"):
            continue
        for code, scfg in (dcfg.get("sockets") or {}).items():
            if code.startswith("_"):
                continue
            labels[(did, code)] = {
                "label": scfg.get("label") or dcfg.get("label"),
                "role": scfg.get("role") or dcfg.get("role"),
                "sump": scfg.get("sump"),
            }

    sockets = []
    by_sump: dict[str, Any] = {}
    for (did, code), r in sorted(current.items()):
        meta = labels.get((did, code), {})
        sump = r.get("sump_code") or meta.get("sump")
        seen = _as_int(r.get("last_seen")) or _as_int(r.get("changed_at"))
        entry = {
            "device_id": did,
            "socket": code,
            "sump": sump,
            "role": r.get("role") or meta.get("role"),
            "label": meta.get("label"),
            "on": _as_int(r.get("on_state")),
            "online": _as_int(r.get("online")),
            "since": _as_int(r.get("changed_at")),
            "last_seen": seen,
            "power_w": _round(r.get("power_w")),
            # A plug the cloud has not heard from is not a plug that is off.
            # Saying so is the difference between a useful reading and a lie.
            "stale": bool(seen is not None and now - seen > stale_after),
        }
        sockets.append(entry)
        if sump:
            by_sump.setdefault(sump, []).append(entry)

    cutoff = now - history_days * DAY
    history = [
        {
            "sump": r.get("sump_code"),
            "device_id": r["device_id"],
            "socket": r["socket"],
            "t": int(r["changed_at"]),
            "on": _as_int(r.get("on_state")),
            "online": _as_int(r.get("online")),
        }
        for r in rows
        if int(r["changed_at"]) >= cutoff
    ]

    # Tuya put cloud access behind a subscription that has to be renewed. When
    # it lapses their API simply stops answering, and a chiller column that
    # quietly froze in October would be worse than no column at all, so the
    # expiry travels with the data and the dashboard says so before it bites.
    expires = _parse_day(cfg.get("subscription_expires"))

    return {
        "enabled": True,
        "stale_after": stale_after,
        "note": cfg.get("note"),
        "subscription_expires": expires,
        "subscription_warn_days": int(cfg.get("subscription_warn_days", 14) or 14),
        "sockets": sockets,
        "by_sump": by_sump,
        "history": history,
        "history_days": history_days,
    }


def _ambient(store, config: dict[str, Any], now: int, window_days: int,
             raw_days: int) -> dict[str, Any]:
    """Air temperature and humidity inside the nursery.

    Not water, and deliberately kept out of the sump table: the air is context
    for what the water is doing, not another sump to check.
    """
    cfg = config.get("plugs") or {}
    devices_cfg = {k: v for k, v in (cfg.get("devices") or {}).items()
                   if not k.startswith("_")
                   and str(v.get("kind", "")).lower() == "ambient"}
    if not devices_cfg:
        return {"enabled": False}

    try:
        rows = store.query(
            "SELECT * FROM ambient WHERE reading_time >= ? ORDER BY reading_time",
            (now - window_days * DAY,),
        )
    except Exception:
        return {"enabled": False}

    keys = [k for k, _, _, _ in AMBIENT_PARAMETERS
            if any(r.get(k) is not None for r in rows)]
    if not keys:
        return {"enabled": False}

    reference = cfg.get("reference") or {}
    parameters = [
        {
            "key": k,
            "label": label,
            "unit": unit,
            "precision": precision,
            "band": (reference.get(k) or {}).get("band"),
        }
        for k, label, unit, precision in AMBIENT_PARAMETERS
        if k in keys
    ]

    latest: dict[str, Any] = {}
    for r in rows:
        did = r["device_id"]
        prev = latest.get(did)
        if prev is None or int(r["reading_time"]) > prev["t"]:
            latest[did] = {
                "t": int(r["reading_time"]),
                "values": {k: _round(r.get(k)) for k in keys},
                "battery": _round(r.get("battery")),
                "online": _as_int(r.get("online")),
            }

    raw_cutoff = now - raw_days * DAY
    readings = [
        [r["device_id"], int(r["reading_time"])] + [_round(r.get(k)) for k in keys]
        for r in rows
        if int(r["reading_time"]) >= raw_cutoff
    ]

    devices = [
        {
            "device_id": did,
            "label": dcfg.get("label") or "Nursery air",
            "location": dcfg.get("location"),
        }
        for did, dcfg in devices_cfg.items()
    ]

    return {
        "enabled": True,
        "devices": devices,
        "parameters": parameters,
        "columns": ["device_id", "t"] + keys,
        "latest": latest,
        "readings": readings,
        "daily": _daily_stats(
            [dict(r, device_id=r["device_id"]) for r in rows], keys
        ),
    }


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _nutrients(store, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Hand-sampled lab measurements, keyed by sump rather than device.

    Every sample is exported: there are a few dozen a year, not thousands, and
    the whole point is to see the record end to end. Analytes with no values at
    all are dropped so the dashboard never offers an empty chart.
    """
    try:
        rows = store.query(
            "SELECT * FROM nutrients ORDER BY sample_date, sump_code"
        )
    except Exception:  # table not created yet on an older database
        return {"analytes": [], "samples": []}

    reference = (config or {}).get("insitu_reference", {}) or {}

    present = []
    for key, label, unit, precision, group in NUTRIENT_ANALYTES:
        if any(r.get(key) is not None for r in rows):
            ref = reference.get(key) or {}
            present.append(
                {
                    "key": key,
                    "label": label,
                    "unit": unit,
                    "precision": precision,
                    "group": group,
                    "typical": ref.get("typical"),
                    "outer": ref.get("outer"),
                    "basis": ref.get("basis"),
                }
            )

    samples = []
    for r in rows:
        sample = {
            "date": r["sample_date"],
            "sump": r["sump_code"],
            "time": r.get("sample_time"),
            "observer": r.get("observer"),
            "notes": r.get("notes"),
            "values": {
                a["key"]: _round(r.get(a["key"])) for a in present
                if r.get(a["key"]) is not None
            },
        }
        samples.append(sample)

    # Only offer a group heading if something in it was actually measured.
    groups = [
        {"key": key, "label": label}
        for key, label in NUTRIENT_GROUPS
        if any(a["group"] == key for a in present)
    ]

    return {"analytes": present, "groups": groups, "samples": samples}


def _daily_stats(rows: list[dict[str, Any]], params: list[str]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        day = (int(r["reading_time"]) // DAY) * DAY
        buckets[(r["device_id"], day)].append(r)

    out = []
    for (device_id, day), group in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        entry: dict[str, Any] = {"device_id": device_id, "day": day, "n": len(group)}
        for p in params:
            vals = [float(g[p]) for g in group if g.get(p) is not None]
            if not vals:
                continue
            mean = sum(vals) / len(vals)
            if len(vals) > 1:
                var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
                sd = math.sqrt(var)
            else:
                sd = 0.0
            entry[p] = {
                "n": len(vals),
                "min": _round(min(vals)),
                "max": _round(max(vals)),
                "mean": _round(mean),
                "sd": _round(sd),
            }
        out.append(entry)
    return out


def _round(value: Any, places: int = 4) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return None


def write_payload(payload: dict[str, Any], path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    return path


def write_csv(store, path: str) -> str:
    """Full raw export, for anyone who wants the numbers rather than the charts."""
    import csv

    rows = store.query("SELECT * FROM readings ORDER BY device_id, reading_time")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if not rows:
        open(path, "w").close()
        return path
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path

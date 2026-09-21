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
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict
from typing import Any

from .seneye import PARAMETERS

DAY = 86400


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
    rows = [r for r in all_rows if int(r["reading_time"]) >= raw_cutoff]

    active = [p for p in PARAMETERS if any(r.get(p) is not None for r in all_rows)]
    if not active:
        active = [p for p in ("temperature", "ph", "nh3") if p in PARAMETERS]

    readings = [
        [r["device_id"], int(r["reading_time"])] + [_round(r.get(p)) for p in active]
        for r in rows
    ]

    latest: dict[str, Any] = {}
    for r in rows:
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

    daily = _daily_stats(rows, active)

    counts: dict[str, int] = defaultdict(int)
    for r in rows:
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
        }
        for p in active
    ]

    last_run = store.query(
        "SELECT started_at, finished_at, status, readings_inserted, message "
        "FROM harvest_runs ORDER BY run_id DESC LIMIT 1"
    )

    return {
        "generated_at": now,
        "window_days": window_days,
        "site": config.get("site", {}),
        "devices": device_out,
        "parameters": parameters,
        "columns": ["device_id", "t"] + active,
        "readings": readings,
        "daily": daily,
        "latest": latest,
        "last_run": last_run[0] if last_run else None,
    }


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

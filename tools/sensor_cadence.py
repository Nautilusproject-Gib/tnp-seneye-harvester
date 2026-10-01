#!/usr/bin/env python3
"""How often has the air sensor actually reported?

Tuya temperature and humidity sensors do not report on a timer. They send a
reading when a value moves past some threshold the firmware decides, plus an
occasional heartbeat, so a steady room produces long silences that are not
faults. That makes "the last reading is two hours old" impossible to judge
without knowing what normal looks like for this particular sensor.

This reads the stored readings and says what normal looks like.

    python tools/sensor_cadence.py                   every ambient device
    python tools/sensor_cadence.py --days 30         over a longer window
    python tools/sensor_cadence.py --recent 20       list the last 20 reports

One limit worth stating plainly: the harvester polls every thirty minutes, so
it cannot see a sensor reporting more often than that. Gaps shorter than the
harvest interval are invisible, and the figures below are a floor on the
sensor's true rate, not a measurement of it. Gaps longer than thirty minutes
are real.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvester.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def human(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def when(unix: int, tz: str) -> str:
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(tz)
    except Exception:
        zone = datetime.timezone.utc
    return datetime.datetime.fromtimestamp(unix, zone).strftime("%d %b %H:%M")


def report(rows: list[dict], label: str, tz: str, harvest_minutes: int,
           recent: int) -> list[str]:
    out = [f"{label}: {len(rows)} reading(s) stored"]
    if len(rows) < 2:
        out.append("  not enough yet to say anything about the rate")
        return out

    times = sorted(int(r["reading_time"]) for r in rows)
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not gaps:
        out.append("  every reading carries the same timestamp")
        return out

    floor = harvest_minutes * 60
    under = sum(1 for g in gaps if g <= floor + 60)

    out.append(f"  first {when(times[0], tz)}, last {when(times[-1], tz)}")
    out.append(f"  median gap {human(statistics.median(gaps))}, "
               f"mean {human(sum(gaps) / len(gaps))}")
    out.append(f"  shortest {human(min(gaps))}, longest {human(max(gaps))}")
    out.append(f"  {under} of {len(gaps)} gaps are at or below the "
               f"{harvest_minutes}-minute harvest interval, so the sensor may "
               "well be reporting faster than that and we cannot see it")

    buckets = [
        ("within 1 harvest", lambda g: g <= floor + 60),
        ("1 to 2 hours", lambda g: floor + 60 < g <= 7200),
        ("2 to 6 hours", lambda g: 7200 < g <= 21600),
        ("6 to 24 hours", lambda g: 21600 < g <= 86400),
        ("over a day", lambda g: g > 86400),
    ]
    out.append("  gap distribution:")
    for name, test in buckets:
        n = sum(1 for g in gaps if test(g))
        if n:
            bar = "#" * min(40, max(1, round(40 * n / len(gaps))))
            out.append(f"    {name:<18} {n:>5}  {bar}")

    long_gaps = sorted((g for g in gaps if g > 21600), reverse=True)[:5]
    if long_gaps:
        out.append("  longest silences: " + ", ".join(human(g) for g in long_gaps))
        out.append("  a silence that long is either a very steady room or a "
                   "sensor that dropped off the network and came back")

    if recent:
        out.append(f"  last {recent} reports:")
        for t in times[-recent:]:
            out.append(f"    {when(t, tz)}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Report how often the air sensor sends readings")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--recent", type=int, default=0,
                    help="also list the last N report times")
    ap.add_argument("--harvest-minutes", type=int, default=30,
                    help="how often the harvester runs, which floors what we can see")
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as fh:
        config = json.load(fh)
    tz = (config.get("site") or {}).get("timezone") or "UTC"
    cfg = config.get("plugs") or {}

    devices = {k: v for k, v in (cfg.get("devices") or {}).items()
               if not k.startswith("_")
               and str(v.get("kind", "")).lower() == "ambient"}
    alias = {}
    for did, dcfg in devices.items():
        for old in dcfg.get("previous_ids") or []:
            alias[str(old)] = did

    store = Store(args.database_url)
    store.migrate()
    import time

    cutoff = int(time.time()) - args.days * 86400
    try:
        rows = store.query(
            "SELECT device_id, reading_time FROM ambient WHERE reading_time >= ? "
            "ORDER BY reading_time", (cutoff,)
        )
    except Exception as exc:
        print(f"Could not read the ambient readings: {exc}", file=sys.stderr)
        return 1

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        did = alias.get(r["device_id"], r["device_id"])
        grouped.setdefault(did, []).append(r)

    print(f"Air sensor reporting rate over the last {args.days} day(s), "
          f"times in {tz}")
    if not grouped:
        print("No readings stored in that window.")
        print("If the sensor was recently reset, its readings may be under an "
              "old device ID that is not listed in previous_ids.")
        store.close()
        return 0

    for did, items in grouped.items():
        label = (devices.get(did) or {}).get("label") or did
        print()
        for line in report(items, f"{label}  ({did})", tz,
                           args.harvest_minutes, args.recent):
            print(line)

    print()
    print("Reading this: a median gap close to the harvest interval means the "
          "sensor reports at least that often and probably more. A median of "
          "an hour or two with no long silences is a sensor reporting on "
          "change in a stable room, which is normal. Occasional gaps of many "
          "hours mixed with short ones usually mean the sensor dropped off the "
          "network rather than that the air stopped moving.")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

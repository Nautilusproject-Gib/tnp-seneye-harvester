#!/usr/bin/env python3
"""Fill the database with plausible nursery readings so the dashboard can be
developed and demonstrated before the real Seneye account is wired in.

    python tools/mock_data.py --days 60 --interval 30

Mock rows carry slide serials beginning MOCK- so they are easy to delete:

    DELETE FROM readings WHERE slide_serial LIKE 'MOCK-%';
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvester.store import Store  # noqa: E402

# device_id, sump, system, lit, baseline temperature / pH / NH3
DEVICES = [
    ("MOCK-SA12", "SA12", "A", True, 21.4, 8.12, 0.006),
    ("MOCK-SA345", "SA345", "A", True, 21.2, 8.10, 0.007),
    ("MOCK-SB12", "SB12", "B", True, 21.1, 8.08, 0.009),
    ("MOCK-SB34", "SB34", "B", True, 21.3, 8.11, 0.005),
    ("MOCK-SC12", "SC12", "C", True, 21.0, 8.09, 0.008),
    ("MOCK-SC34", "SC34", "C", False, 20.7, 8.01, 0.012),
    ("MOCK-SD12", "SD12", "D", False, 20.6, 7.98, 0.014),
    ("MOCK-SD345", "SD345", "D", False, 20.5, 7.97, 0.015),
    ("MOCK-SE12", "SE12", "E", True, 21.5, 8.13, 0.006),
]

TANKS = {
    "SA12": ["A1", "A2"],
    "SA345": ["A3", "A4", "A5"],
    "SB12": ["B1", "B2"],
    "SB34": ["B3", "B4"],
    "SC12": ["C1", "C2"],
    "SC34": ["C3", "C4"],
    "SD12": ["D1", "D2"],
    "SD345": ["D3", "D4", "D5"],
    "SE12": ["E1", "E2"],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--interval", type=int, default=30, help="minutes between readings")
    ap.add_argument("--database-url", default=None)
    args = ap.parse_args()

    random.seed(1204)
    store = Store(args.database_url)
    store.migrate()

    now = int(time.time())
    step = args.interval * 60
    n_steps = (args.days * 86400) // step

    for device_id, sump, system, lit, t_base, ph_base, nh3_base in DEVICES:
        label = f"{sump} → tanks {', '.join(TANKS[sump])}"
        store.upsert_device(device_id, label, 1, sump, system, label, now)
        rows = []
        for i in range(n_steps):
            t = now - (n_steps - i) * step
            hour = (t % 86400) / 3600.0
            day = i * step / 86400.0

            # seasonal drift + diel cycle + noise
            seasonal = 1.6 * math.sin(2 * math.pi * day / 120.0)
            diel = 0.45 * math.sin(2 * math.pi * (hour - 15) / 24.0)
            temperature = t_base + seasonal + diel + random.gauss(0, 0.12)

            ph = ph_base + 0.05 * math.sin(2 * math.pi * (hour - 16) / 24.0) + random.gauss(0, 0.015)
            nh3 = max(0.0, nh3_base + random.gauss(0, 0.004) + (0.02 if 18 < day < 21 else 0))

            par = 0.0
            if lit and 7 <= hour <= 19:
                par = 165 * math.sin(math.pi * (hour - 7) / 12.0) + random.gauss(0, 8)
                par = max(0.0, par)

            row = {
                "device_id": device_id,
                "reading_time": t,
                "fetched_at": t + 60,
                "temperature": round(temperature, 3),
                "ph": round(ph, 3),
                "nh3": round(nh3, 4),
                "nh4": None,
                "o2": None,
                "par": round(par, 1) if par else 0.0,
                "lux": None,
                "kelvin": None,
                "slide_serial": f"MOCK-{device_id}",
                "slide_expires": now + 21 * 86400,
                "out_of_water": 0,
                "disconnected": 0,
            }
            for p in ("temperature", "ph", "nh3", "nh4", "o2", "par", "lux", "kelvin"):
                row[f"{p}_status"] = 0
            rows.append(row)

        inserted = store.insert_readings(rows)
        print(f"{device_id}: {inserted} mock readings")

    store.finish_run(now, now, "ok", len(DEVICES), 0, "mock data")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

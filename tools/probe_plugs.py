#!/usr/bin/env python3
"""Print everything the Tuya cloud reports about the nursery's plugs.

Run this once before the socket mapping is written down. It reads, never
writes, and never switches anything. What it prints is the list of data points
each device actually reports, which is the only reliable way to know which
switch code is which physical socket.

    python tools/probe_plugs.py                 every device in config.json
    python tools/probe_plugs.py bf5b1b28...     just this one
    python tools/probe_plugs.py --toggle-test   how to work out socket order

Credentials come from the environment:

    TUYA_ACCESS_ID       "Access ID/Client ID" from the Tuya cloud project
    TUYA_ACCESS_SECRET   "Access Secret/Client Secret" from the same page
"""

from __future__ import annotations

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvester.plugs import TuyaClient, TuyaError, describe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOGGLE_HELP = """
Working out which switch code is which socket
---------------------------------------------
A double plug reports two codes, switch_1 and switch_2, and nothing in the API
says which of the two physical sockets each one drives. Rather than guess:

  1. Run this probe and note both values, e.g. switch_1 True, switch_2 True.
  2. In the Smart Life app, switch OFF the socket with the chiller for the
     LOWER-numbered sump (SD12 on the Row D plug, SA12 on Row A, and so on).
     Leave it off for a few seconds only.
  3. Run the probe again. Whichever code changed to False is that sump's
     socket.
  4. Switch it back on, and write the mapping into config.json.

Doing it that way takes two minutes per plug and means the dashboard is not
quietly reporting the wrong chiller for the next year. On a hot day, do it in
the morning.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Report what the Tuya devices say")
    ap.add_argument("device_ids", nargs="*", help="device IDs; default is all configured")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--region", default=None, help="override plugs.region")
    ap.add_argument("--toggle-test", action="store_true",
                    help="print how to identify which socket is which")
    args = ap.parse_args(argv)

    if args.toggle_test:
        print(TOGGLE_HELP)
        return 0

    with open(args.config, encoding="utf-8") as fh:
        config = json.load(fh)
    cfg = config.get("plugs") or {}

    ids = args.device_ids or [
        k for k in (cfg.get("devices") or {}) if not k.startswith("_")
    ]
    if not ids:
        print("No device IDs given and none in config.json > plugs > devices.",
              file=sys.stderr)
        return 1

    try:
        client = TuyaClient(
            access_id=os.environ.get("TUYA_ACCESS_ID", ""),
            access_secret=os.environ.get("TUYA_ACCESS_SECRET", ""),
            region=args.region or cfg.get("region", "eu"),
        )
        print(f"Asking Tuya about {len(ids)} device(s) "
              f"in region {args.region or cfg.get('region', 'eu')}")
        print(describe(client, ids))
    except TuyaError as exc:
        print(f"\nTuya said no: {exc}", file=sys.stderr)
        print(
            "\nThings worth checking, in order:\n"
            "  * the region. A cloud project in Central Europe is 'eu'; Western\n"
            "    Europe is 'weu'. Calls sent to the wrong one are refused.\n"
            "  * the two secrets, TUYA_ACCESS_ID and TUYA_ACCESS_SECRET.\n"
            "  * that the app account is still linked under Cloud > Development >\n"
            "    your project > Devices > Link Tuya App Account.\n"
            "  * that the IoT Core subscription has not lapsed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Point a configured device at a new Tuya device ID, keeping its history.

A factory reset gives a Tuya device a brand new ID, and rejoining one to a
different Wi-Fi network is enough to cause it. This moves the entry in
config.json across to the new ID and records the old one under `previous_ids`,
which is what keeps the readings from before the reset on the same chart
instead of starting the record again from the day it happened.

    python tools/swap_device.py --old bf0dee5c... --new bf79a8d7...
    python tools/swap_device.py --old bf0dee5c... --new bf79a8d7... --check

`--check` asks Tuya whether the new ID is really on the account and the old one
really is not, and refuses the swap if either is wrong. It needs
TUYA_ACCESS_ID and TUYA_ACCESS_SECRET; without them the swap still happens, on
the basis that a person reading the ID off the Tuya console knows what they
are doing.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvester.plugs import TuyaClient, TuyaError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tuya IDs are lower-case alphanumerics, around 20 to 22 characters. Checked
# because a typo here would silently point the harvester at nothing, which is
# the exact failure this tool exists to repair.
ID_PATTERN = re.compile(r"^[a-z0-9]{16,32}$")


def swap(config: dict, old: str, new: str) -> dict:
    """Return the config with `old` replaced by `new`, order preserved."""
    plugs = config.get("plugs") or {}
    devices = plugs.get("devices") or {}

    if old not in devices:
        known = [k for k in devices if not k.startswith("_")]
        raise SystemExit(
            f"{old} is not in config.json > plugs > devices.\n"
            "Configured devices are:\n  " + "\n  ".join(known)
        )
    if new in devices:
        raise SystemExit(f"{new} is already configured. Nothing to do.")

    entry = devices[old]
    previous = [str(x) for x in (entry.get("previous_ids") or [])]
    if old not in previous:
        previous.append(old)
    entry["previous_ids"] = previous

    rebuilt = collections.OrderedDict()
    for key, value in devices.items():
        rebuilt[new if key == old else key] = value
    plugs["devices"] = rebuilt
    return config


def verify(client: TuyaClient, old: str, new: str) -> None:
    account = {d["id"]: d for d in client.all_devices()}
    if new not in account:
        raise SystemExit(
            f"{new} is not on the Tuya account.\n"
            "Check the ID, and that the app account is still linked under "
            "Cloud > Development > your project > Devices."
        )
    if old in account:
        raise SystemExit(
            f"{old} is still on the account, so it has not been replaced.\n"
            "If you meant to add a second device, edit config.json instead: "
            "this tool only moves one entry onto a new ID."
        )
    found = account[new]
    print(f"Tuya confirms {new} is on the account: "
          f"{found.get('name') or '(no name)'} "
          f"[{found.get('product_name') or 'unknown product'}], "
          f"{'online' if found.get('online') else 'offline'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Move a configured device onto a new Tuya ID")
    ap.add_argument("--old", required=True, help="the ID currently in config.json")
    ap.add_argument("--new", required=True, help="the ID it has now")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--region", default=None)
    ap.add_argument("--check", action="store_true",
                    help="ask Tuya to confirm the new ID before writing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    old, new = args.old.strip(), args.new.strip()
    for label, value in (("--old", old), ("--new", new)):
        if not ID_PATTERN.match(value):
            raise SystemExit(
                f"{label} does not look like a Tuya device ID: {value!r}\n"
                "They are lower-case letters and digits, around 22 characters, "
                "with no spaces."
            )
    if old == new:
        raise SystemExit("--old and --new are the same ID.")

    with open(args.config, encoding="utf-8") as fh:
        config = json.load(fh, object_pairs_hook=collections.OrderedDict)

    if args.check:
        cfg = config.get("plugs") or {}
        client = TuyaClient(
            access_id=os.environ.get("TUYA_ACCESS_ID", ""),
            access_secret=os.environ.get("TUYA_ACCESS_SECRET", ""),
            region=args.region or cfg.get("region", "eu"),
        )
        try:
            verify(client, old, new)
        except TuyaError as exc:
            raise SystemExit(f"Could not check with Tuya: {exc}")

    label = ((config.get("plugs") or {}).get("devices") or {}).get(old, {}).get("label", old)
    config = swap(config, old, new)

    if args.dry_run:
        print(f"Would move {label!r} from {old} to {new}, "
              f"recording {old} under previous_ids. Nothing written.")
        return 0

    with open(args.config, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"Moved {label!r} from {old} to {new}.")
    print(f"{old} is recorded under previous_ids, so its readings stay on the "
          "same chart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

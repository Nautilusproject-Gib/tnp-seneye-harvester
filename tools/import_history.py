#!/usr/bin/env python3
"""Import a historical Seneye export into the nursery database.

The cloud API only ever serves the last reading, so a year of history has to
come from Seneye as a file. This loads those files into the same `readings`
table the harvester writes to, after which the dashboard treats old and new
readings identically.

    python tools/import_history.py --dry-run exports/*.csv
    python tools/import_history.py exports/*.csv
    python tools/import_history.py --sump SA12 --timezone Europe/Gibraltar one-device.csv

Safe to re-run: rows are keyed on (device_id, reading_time), so importing the
same file twice adds nothing the second time. Nothing is ever overwritten,
which means a historical file cannot clobber a reading the harvester collected.

Columns are matched by heading rather than position, so the exact export layout
does not matter as long as the headings are recognisable. Run with --dry-run
first: it prints what it matched, what it would import and what it would skip,
and writes nothing.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harvester.nutrients import normalise_heading  # noqa: E402
from harvester.store import Store  # noqa: E402

# Heading patterns, first match wins. Ordered so the more specific ones are
# tested before the looser ones.
COLUMNS = [
    # "Declared" is what Seneye's own export calls the reading time.
    ("timestamp", (r"^datetime", r"^timestamp", r"^dateandtime", r"^readingtime",
                   r"^recordedat", r"^measuredat", r"^declared", r"^logged",
                   r"^reading$", r"^time$")),
    ("date", (r"^date",)),
    ("time_only", (r"^time",)),
    ("device_id", (r"^deviceid", r"^device$", r"^sensorid", r"^seneyeid", r"^id$",
                   r"^unitid")),
    ("device_name", (r"^devicename", r"^description", r"^name$", r"^sump",
                     r"^tank", r"^location", r"^unit$")),
    ("temperature", (r"^temp",)),
    ("ph", (r"^ph$", r"^phvalue")),
    ("nh4", (r"^nh4", r"^ammonium")),
    ("nh3", (r"^nh3", r"^freeammonia", r"^ammonia", r"^unionisedammonia",
             r"^unionizedammonia")),
]

# Measured fields the importer will carry across, in the order they are
# reported. Anything else in the export is ignored rather than guessed at.
VALUE_FIELDS = ("temperature", "ph", "nh3", "nh4")

TIMESTAMP_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
    "%Y/%m/%d %H:%M:%S",
]
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"]


def match_columns(headings: list[str]) -> dict[str, int]:
    """{field: column index} from the header row."""
    mapping: dict[str, int] = {}
    for index, heading in enumerate(headings):
        clean = normalise_heading(heading)
        if not clean:
            continue
        for field, patterns in COLUMNS:
            if field in mapping:
                continue
            if any(re.match(pattern, clean) for pattern in patterns):
                mapping[field] = index
                break
    return mapping


def parse_number(text: str | None) -> float | None:
    if text is None:
        return None
    text = text.strip().replace(",", ".")
    if not text or text.upper() in {"N/A", "NA", "NULL", "-", "--"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _ordered(formats: list[str], dayfirst: bool) -> list[str]:
    """The same formats with the day/month order the export actually uses."""
    if dayfirst:
        return formats
    swapped = [f.replace("%d/%m", "%m/%d").replace("%d-%m", "%m-%d") for f in formats]
    # Keep the unambiguous ISO shapes first either way.
    return swapped


def parse_when(row: list[str], columns: dict[str, int],
               dayfirst: bool = True) -> dt.datetime | None:
    """A naive datetime from a timestamp column, or from a date + time pair.

    Which column holds what is not always obvious from the heading: an export
    with `Date` and `Time` columns can land either way round, and a column
    called `Date` sometimes holds a full timestamp. So rather than trusting the
    heading, every date-ish cell is tried as a whole timestamp first, and only
    then as a bare date needing a time from one of the others.
    """
    def cell(field: str) -> str:
        index = columns.get(field)
        return (row[index].strip() if index is not None and index < len(row) else "")

    cells = [c for c in (cell("timestamp"), cell("date"), cell("time_only")) if c]
    if not cells:
        return None

    for text in cells:
        # Unix seconds or milliseconds
        if re.fullmatch(r"\d{9,13}", text):
            value = int(text)
            if value > 10 ** 11:
                value //= 1000
            return dt.datetime.fromtimestamp(value, dt.timezone.utc).replace(tzinfo=None)
        for fmt in _ordered(TIMESTAMP_FORMATS, dayfirst):
            try:
                return dt.datetime.strptime(text, fmt)
            except ValueError:
                continue

    for index, text in enumerate(cells):
        day = None
        for fmt in _ordered(DATE_FORMATS, dayfirst):
            try:
                day = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if day is None:
            continue
        for other in cells[:index] + cells[index + 1:]:
            for tfmt in ("%H:%M:%S", "%H:%M"):
                try:
                    clock = dt.datetime.strptime(other, tfmt)
                except ValueError:
                    continue
                return day.replace(hour=clock.hour, minute=clock.minute,
                                   second=clock.second)
        return day
    return None


def to_unix(when: dt.datetime, tz_name: str | None) -> int:
    """Naive local time to unix seconds.

    Seneye exports are usually in the account's local time with no offset. The
    timezone has to be stated rather than assumed: getting it wrong shifts a
    whole year of readings by an hour or two, which is the kind of error that
    survives for years because nothing looks broken.
    """
    if tz_name in (None, "", "UTC", "utc"):
        return int(when.replace(tzinfo=dt.timezone.utc).timestamp())
    try:
        from zoneinfo import ZoneInfo
        return int(when.replace(tzinfo=ZoneInfo(tz_name)).timestamp())
    except Exception as exc:
        raise SystemExit(
            f"Could not use timezone {tz_name!r}: {exc}. Use an IANA name such "
            "as Europe/Gibraltar, or pass --timezone UTC."
        )


def device_for(row, columns, config, fallback_sump, filename):
    """Work out which device a row belongs to.

    In order: an id that matches config.json, a name or sump column, the
    --sump argument, then the filename. Anything else is reported rather than
    guessed at.
    """
    devices = {k: v for k, v in config.get("devices", {}).items()
               if not k.startswith("_")}
    sump_to_id = {}
    for device_id, entry in devices.items():
        if entry.get("sump"):
            sump_to_id[entry["sump"].upper()] = device_id

    def cell(field):
        index = columns.get(field)
        return (row[index].strip() if index is not None and index < len(row) else "")

    raw_id = cell("device_id")
    if raw_id and raw_id in devices:
        return raw_id, None

    for candidate in (cell("device_name"), fallback_sump,
                      os.path.splitext(os.path.basename(filename))[0]):
        if not candidate:
            continue
        text = candidate.upper().replace(" ", "").replace("-", "").replace("_", "")
        # Longest first, so SA345 is never claimed by a shorter code that
        # happens to be a prefix of it.
        for sump, device_id in sorted(sump_to_id.items(), key=lambda kv: -len(kv[0])):
            if sump in text:
                return device_id, None
    if raw_id:
        return None, f"device id {raw_id} is not in config.json"
    return None, "no sump or device could be identified"


# How close two readings have to be to count as the same measurement. Exports
# and the API sometimes round differently in the last place.
AGREEMENT = {"temperature": 0.05, "ph": 0.02, "nh3": 0.0005, "nh4": 0.01}


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def collapse_repeats(batch) -> tuple[list, int]:
    """One reading per device per timestamp, taking the median of repeats.

    Seneye's exports log the same minute more than once, and the repeats do not
    always agree: one file has 02:11 three times with pH 7.94, 7.94 and 8.62,
    and another has the same minute twice with 6.63 and 6.79. Keeping whichever
    came first in the file would let an outlier win on nothing better than row
    order, and the median of the repeats is both more defensible and stable
    whichever way round the file is sorted.

    Timestamps that appear once are untouched, which is most of them.
    """
    grouped: dict[tuple, list] = {}
    order: list = []
    for row in batch:
        key = (row["device_id"], row["reading_time"])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    out, collapsed = [], 0
    for key in order:
        rows = grouped[key]
        if len(rows) == 1:
            out.append(rows[0])
            continue
        collapsed += len(rows) - 1
        merged = dict(rows[0])
        for field in VALUE_FIELDS:
            merged[field] = _median([r.get(field) for r in rows])
        out.append(merged)
    return out, collapsed


def compare_existing(store, batch) -> tuple[int, int]:
    """(rows already in the database, rows that also agree with what is there).

    Both numbers matter, and for different reasons.

    The first says how much of the export the harvester has already collected.
    Those rows are skipped rather than duplicated, so an overlap costs nothing.

    The second is the timezone check, and it is the one worth reading. Counting
    collisions alone proves nothing: readings sit on a regular half-hourly
    grid, so an export an hour or two out still lands on timestamps that exist
    -- it just lands on the wrong ones. Comparing the values tells the two
    apart. Same timestamps AND same numbers means the export is being read in
    the timezone it was written in. Same timestamps but different numbers means
    it is being shifted onto its neighbours, and the whole year would go in
    displaced by that much.
    """
    if not batch:
        return 0, 0
    stamps = [row["reading_time"] for row in batch]
    try:
        rows = store.query(
            "SELECT * FROM readings "
            "WHERE reading_time >= ? AND reading_time <= ?",
            (min(stamps), max(stamps)),
        )
    except Exception:
        return 0, 0
    held = {(r["device_id"], int(r["reading_time"])): r for r in rows}

    already = agreeing = 0
    for row in batch:
        stored = held.get((row["device_id"], row["reading_time"]))
        if stored is None:
            continue
        already += 1
        checked = matched = 0
        for field, tolerance in AGREEMENT.items():
            mine, theirs = row.get(field), stored.get(field)
            if mine is None or theirs is None:
                continue
            checked += 1
            if abs(float(mine) - float(theirs)) <= tolerance:
                matched += 1
        if checked and matched == checked:
            agreeing += 1
    return already, agreeing


def _stamp_text(stamp: int, fmt: str) -> str:
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime(fmt)


def register_devices(store, config, latest_seen: dict[str, int]) -> list[str]:
    """Make sure every imported device has a row in `devices`.

    The dashboard lists tanks from that table, so readings imported for a unit
    the harvester has never polled would otherwise sit in the database
    invisibly. Devices already known are left alone: their `last_seen` records
    when the harvester last heard from them, and a year-old import must not
    wind that back.
    """
    known = {row["device_id"] for row in store.query("SELECT device_id FROM devices")}
    sumps = config.get("sumps", {}) or {}
    added = []
    for device_id, seen_at in latest_seen.items():
        if device_id in known:
            continue
        sump = (config.get("devices", {}).get(device_id) or {}).get("sump")
        sump_cfg = sumps.get(sump, {}) if sump else {}
        tanks = sump_cfg.get("tanks", [])
        label = (f"{sump} → tanks {', '.join(tanks)}" if sump and tanks
                 else sump or f"Seneye {device_id}")
        store.upsert_device(device_id, sump or f"Seneye {device_id}", None,
                            sump, sump_cfg.get("system"), label, seen_at)
        added.append(device_id)
    return added


def import_file(path, store, config, args):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        rows = list(reader)

    if not rows:
        print(f"  {os.path.basename(path)}: empty")
        return 0, 0, {}

    header_index, columns = None, {}
    for index, row in enumerate(rows[:15]):
        candidate = match_columns(row)
        has_time = "timestamp" in candidate or "date" in candidate
        measured = [f for f in VALUE_FIELDS if f in candidate]
        if has_time and measured:
            header_index, columns = index, candidate
            break
    if header_index is None:
        print(f"  {os.path.basename(path)}: no header row found with a date and a "
              "measurement column, skipped")
        return 0, 0, {}

    found = ", ".join(sorted(columns))
    print(f"  {os.path.basename(path)}: columns matched [{found}]")

    batch, skipped = [], {}
    earliest = latest = None
    for row in rows[header_index + 1:]:
        if not any(cell.strip() for cell in row):
            continue
        when = parse_when(row, columns, dayfirst=not args.month_first)
        if when is None:
            skipped["unreadable date"] = skipped.get("unreadable date", 0) + 1
            continue
        device_id, problem = device_for(row, columns, config, args.sump, path)
        if device_id is None:
            skipped[problem] = skipped.get(problem, 0) + 1
            continue

        values = {}
        for field in VALUE_FIELDS:
            index = columns.get(field)
            if index is not None and index < len(row):
                values[field] = parse_number(row[index])
        if all(v is None for v in values.values()):
            skipped["no values"] = skipped.get("no values", 0) + 1
            continue

        stamp = to_unix(when, args.timezone)
        earliest = stamp if earliest is None else min(earliest, stamp)
        latest = stamp if latest is None else max(latest, stamp)
        entry = {
            "device_id": device_id,
            "reading_time": stamp,
            "fetched_at": stamp,
            "slide_serial": args.tag,
        }
        entry.update({field: values.get(field) for field in VALUE_FIELDS})
        batch.append(entry)

    batch, collapsed = collapse_repeats(batch)
    if collapsed:
        print(f"    {collapsed} repeated timestamp(s) collapsed to their median")

    span = ""
    if earliest and latest:
        span = (" spanning " + _stamp_text(earliest, "%d %b %Y")
                + " to " + _stamp_text(latest, "%d %b %Y"))
    print(f"    {len(batch)} readable row(s){span}")

    if args.dry_run:
        for row in batch[:2]:
            print(f"      example: {row['device_id']} "
                  f"{_stamp_text(row['reading_time'], '%Y-%m-%d %H:%M')} "
                  f"temp={row['temperature']} pH={row['ph']} NH3={row['nh3']}")
        already, agreeing = compare_existing(store, batch)
        if already:
            print(f"      {already} of these are already in the database and "
                  f"would be skipped; {agreeing} of those match the stored "
                  "reading")
            if agreeing < already * 0.9:
                print("      WARNING: they land on readings the harvester "
                      "already has but the values disagree, which is what a "
                      "wrong --timezone looks like. Check before importing.")
        skipped["_already"] = already
        return len(batch), 0, skipped

    latest_seen: dict[str, int] = {}
    for entry in batch:
        device_id, stamp = entry["device_id"], entry["reading_time"]
        latest_seen[device_id] = max(latest_seen.get(device_id, 0), stamp)
    for device_id in register_devices(store, config, latest_seen):
        print(f"    registered device {device_id}, which the harvester had not "
              "polled yet")

    written = store.insert_readings(batch)
    print(f"    {written} new, {len(batch) - written} already present")
    return len(batch), written, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="CSV files, or folders of them")
    ap.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"))
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--timezone", default="UTC",
                    help="timezone the export's timestamps are in, e.g. "
                         "Europe/Gibraltar. Default UTC. Ask Seneye which it is.")
    ap.add_argument("--sump", default=None,
                    help="sump code for a file that does not name its device")
    ap.add_argument("--month-first", action="store_true",
                    help="read ambiguous dates as MM/DD rather than DD/MM")
    ap.add_argument("--tag", default="IMPORTED",
                    help="written to slide_serial so imported rows stay identifiable")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        config = json.load(fh)

    paths = []
    for entry in args.files:
        if os.path.isdir(entry):
            paths.extend(sorted(glob.glob(os.path.join(entry, "*.csv"))))
        else:
            paths.extend(sorted(glob.glob(entry)) or [entry])
    if not paths:
        raise SystemExit("no files matched")

    store = Store(args.database_url)
    store.migrate()

    print(f"{'Checking' if args.dry_run else 'Importing'} {len(paths)} file(s), "
          f"timestamps read as {args.timezone}")
    total_read = total_written = total_existing = 0
    all_skipped: dict[str, int] = {}
    for path in paths:
        read, written, skipped = import_file(path, store, config, args)
        total_read += read
        total_written += written
        if args.dry_run:
            total_existing += skipped.pop("_already", 0)
        for reason, count in skipped.items():
            all_skipped[reason] = all_skipped.get(reason, 0) + count

    print()
    if args.dry_run:
        print(f"Would import {total_read} reading(s), of which {total_existing} "
              "are already in the database and would be skipped. "
              f"That leaves {total_read - total_existing} new. Nothing was "
              "written.")
    else:
        print(f"{total_written} reading(s) added, {total_read - total_written} "
              "already in the database.")
    for reason, count in sorted(all_skipped.items(), key=lambda kv: -kv[1]):
        print(f"  skipped {count}: {reason}")

    if not args.dry_run and total_written:
        print("\nNow rebuild the dashboard export:")
        print("  python -m harvester.harvest --export-only")
        print("and raise export.window_days in config.json if the history is "
              "longer than it allows.")
    store.close()


if __name__ == "__main__":
    main()

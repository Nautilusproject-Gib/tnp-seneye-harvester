"""Maintenance issues and planned jobs for the nursery.

Two tabs in the same Google Sheet, read the same way as the sampling data:

* **Maintenance** - unplanned faults. One row per issue, raised by whoever
  finds it (usually through a Google Form), then edited as it is worked on.
* **Schedule** - planned recurring jobs. One row per job, with how often it
  falls due and when it was last done, from which the next due date follows.

Nothing here is invented: an issue is open until somebody says otherwise, a
job is overdue only if its own frequency says so, and a row with no date is
reported as such rather than quietly defaulted to today.

This log names people, so it is kept out of the published dashboard by
default. See `maintenance.publish` in config.json.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .nutrients import (
    NutrientError,
    _excel_date,
    _excel_time,
    _number,
    map_headings,
    normalise_heading,
)

# Issue columns. As with the sampling sheet, headings are matched by content
# so a column can be inserted or moved without breaking anything.
ISSUE_PATTERNS = [
    ("issue_id", (r"^(issue)?(id|ref|number|no)$", r"^issueid")),
    ("raised_on", (r"^date$", r"^dateraised", r"^raised", r"^reported$", r"^reportedon")),
    ("raised_at", (r"^time$",)),
    ("location", (r"^tank", r"^sump", r"^system", r"^location")),
    ("equipment", (r"^equipment", r"^asset", r"^item")),
    ("summary", (r"^fault", r"^issue$", r"^problem", r"^description", r"^summary", r"^title")),
    ("severity", (r"^severity", r"^priority", r"^urgency")),
    ("status", (r"^status", r"^state$")),
    ("assigned_to", (r"^assigned", r"^owner", r"^respperson", r"^responsible", r"^who")),
    ("action_taken", (r"^action", r"^resolution", r"^fix", r"^workdone")),
    ("resolved_on", (r"^dateresolved", r"^resolved", r"^closed", r"^completed")),
    ("reported_by", (r"^reportedby", r"^raisedby", r"^observer", r"^name$")),
    ("notes", (r"^notes?$", r"^comment")),
]

SCHEDULE_PATTERNS = [
    ("task_id", (r"^(task)?(id|ref|number|no)$", r"^taskid")),
    ("task", (r"^task", r"^job", r"^maintenance$", r"^activity", r"^description")),
    ("location", (r"^tank", r"^sump", r"^system", r"^location")),
    ("equipment", (r"^equipment", r"^asset", r"^item")),
    ("frequency_days", (r"^frequency", r"^everydays", r"^interval", r"^period", r"^cycle")),
    ("last_done", (r"^lastdone", r"^lastcompleted", r"^last$", r"^datedone", r"^completed")),
    ("done_by", (r"^doneby", r"^completedby", r"^assigned", r"^owner", r"^respperson", r"^responsible")),
    ("next_due", (r"^nextdue", r"^due$", r"^duedate")),
    ("notes", (r"^notes?$", r"^comment")),
]

ISSUE_FIELDS = tuple(f for f, _ in ISSUE_PATTERNS)
SCHEDULE_FIELDS = tuple(f for f, _ in SCHEDULE_PATTERNS) + ("frequency_days",)

# Status wording people actually type, mapped to the three states. Anything
# unrecognised is kept verbatim and treated as open, so a typo never hides a
# fault from the board.
STATUS_ALIASES = {
    "open": "open",
    "new": "open",
    "reported": "open",
    "outstanding": "open",
    "todo": "open",
    "inprogress": "in_progress",
    "progress": "in_progress",
    "started": "in_progress",
    "wip": "in_progress",
    "ongoing": "in_progress",
    "awaitingparts": "in_progress",
    "onhold": "in_progress",
    "resolved": "resolved",
    "closed": "resolved",
    "done": "resolved",
    "fixed": "resolved",
    "complete": "resolved",
    "completed": "resolved",
}

SEVERITY_ALIASES = {
    "critical": "critical",
    "urgent": "critical",
    "high": "critical",
    "major": "critical",
    "needsattention": "attention",
    "attention": "attention",
    "medium": "attention",
    "moderate": "attention",
    "routine": "routine",
    "low": "routine",
    "minor": "routine",
}


def _classify(value: str | None, aliases: dict[str, str], default: str | None) -> str | None:
    if not value:
        return default
    key = re.sub(r"[^a-z]", "", value.lower())
    return aliases.get(key, value.strip())


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _find_header_row(rows: list[dict[str, str]], patterns, required: tuple[str, ...]):
    """Locate the header row for a tab whose title row may sit above it."""
    for index, row in enumerate(rows[:12]):
        mapping = map_headings_for(row, patterns)
        if all(field in mapping for field in required):
            return index, mapping
    raise NutrientError(
        "could not find the header row; expected columns including "
        + ", ".join(required)
    )


def map_headings_for(headings: dict[str, str], patterns) -> dict[str, str]:
    """Same matching rule as the sampling sheet, against a given pattern set."""
    mapping: dict[str, str] = {}
    for key, heading in headings.items():
        clean = normalise_heading(heading)
        if not clean:
            continue
        for field, field_patterns in patterns:
            if field in mapping:
                continue
            if any(re.match(pattern, clean) for pattern in field_patterns):
                mapping[field] = key
                break
    return mapping


def parse_issues(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Rows from the Maintenance tab into issue records."""
    header_index, columns = _find_header_row(rows, ISSUE_PATTERNS, ("summary",))
    issues: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for position, row in enumerate(rows[header_index + 1:], start=1):
        def cell(field: str) -> str | None:
            key = columns.get(field)
            return _text(row.get(key)) if key else None

        summary = cell("summary")
        if not summary:
            continue  # a row with no description is not an issue

        raised = _excel_date(cell("raised_on") or "")
        resolved = _excel_date(cell("resolved_on") or "")
        status = _classify(cell("status"), STATUS_ALIASES, None)
        # A resolution date with no status still means resolved; a status of
        # resolved with no date is fine, we just cannot say when.
        if status is None:
            status = "resolved" if resolved else "open"
        severity = _classify(cell("severity"), SEVERITY_ALIASES, None)

        issue_id = cell("issue_id")
        if not issue_id:
            stem = (raised.isoformat() if raised else "undated")
            issue_id = f"{stem}-{position:03d}"
        if issue_id in seen_ids:
            issue_id = f"{issue_id}-{position:03d}"
        seen_ids.add(issue_id)

        issues.append(
            {
                "issue_id": issue_id,
                "raised_on": raised.isoformat() if raised else None,
                "raised_at": _excel_time(cell("raised_at")),
                "location": cell("location"),
                "equipment": cell("equipment"),
                "summary": summary,
                "severity": severity,
                "status": status,
                "assigned_to": cell("assigned_to"),
                "action_taken": cell("action_taken"),
                "resolved_on": resolved.isoformat() if resolved else None,
                "reported_by": cell("reported_by"),
                "notes": cell("notes"),
            }
        )
    return issues


def parse_schedule(rows: list[dict[str, str]], today: dt.date | None = None) -> list[dict[str, Any]]:
    """Rows from the Schedule tab into planned-job records.

    Next due is taken from the sheet when it is filled in, and otherwise
    derived from the last completion plus the job's frequency. A job with
    neither is listed with no due date rather than guessed at.
    """
    header_index, columns = _find_header_row(rows, SCHEDULE_PATTERNS, ("task",))
    today = today or dt.date.today()
    jobs: list[dict[str, Any]] = []

    for position, row in enumerate(rows[header_index + 1:], start=1):
        def cell(field: str) -> str | None:
            key = columns.get(field)
            return _text(row.get(key)) if key else None

        task = cell("task")
        if not task:
            continue

        frequency = _number(cell("frequency_days"))
        last_done = _excel_date(cell("last_done") or "")
        next_due = _excel_date(cell("next_due") or "")
        if next_due is None and last_done and frequency:
            next_due = last_done + dt.timedelta(days=int(frequency))

        days_until = (next_due - today).days if next_due else None

        jobs.append(
            {
                "task_id": cell("task_id") or f"task-{position:03d}",
                "task": task,
                "location": cell("location"),
                "equipment": cell("equipment"),
                "frequency_days": int(frequency) if frequency else None,
                "last_done": last_done.isoformat() if last_done else None,
                "done_by": cell("done_by"),
                "next_due": next_due.isoformat() if next_due else None,
                "days_until_due": days_until,
                "notes": cell("notes"),
            }
        )
    return jobs


# -- loading ---------------------------------------------------------------


def _rows_from_source(cfg: dict[str, Any], tab_key: str, root: str) -> list[dict[str, str]] | None:
    """Rows for one tab, from the sheet if configured, else the workbook."""
    from .nutrients import fetch_sheet_csv, parse_csv_rows, read_workbook_rows
    import os

    tab = cfg.get(tab_key) or {}
    url = tab.get("sheet_url") or cfg.get("sheet_url")
    gid = tab.get("gid")
    if url:
        try:
            target = url
            if gid is not None and "gid=" not in str(url):
                joiner = "&" if "?" in url else "?"
                target = f"{url}{joiner}gid={gid}"
            return parse_csv_rows(fetch_sheet_csv(target))
        except NutrientError as exc:
            print(f"maintenance: could not fetch the {tab_key} tab ({exc})")

    workbook = cfg.get("workbook")
    if workbook:
        path = os.path.join(root, workbook)
        if os.path.exists(path):
            try:
                return read_workbook_rows(path, tab.get("sheet"))
            except Exception as exc:
                print(f"maintenance: could not read {path} ({exc})")
    return None


def load(store, config: dict[str, Any], root: str) -> dict[str, int]:
    """Refresh issues and planned jobs from the sheet.

    The sheet is the record of truth, so each tab replaces its table wholesale:
    a row deleted in the sheet disappears here. A tab that cannot be read is
    left alone entirely rather than emptied.
    """
    cfg = config.get("maintenance", {}) or {}
    counts = {"issues": 0, "schedule": 0}
    if not cfg.get("enabled", True):
        return counts

    issue_rows = _rows_from_source(cfg, "issues", root)
    if issue_rows:
        try:
            issues = parse_issues(issue_rows)
            counts["issues"] = store.replace_table(
                "issues",
                ("issue_id", "raised_on", "raised_at", "location", "equipment",
                 "summary", "severity", "status", "assigned_to", "action_taken",
                 "resolved_on", "reported_by", "notes"),
                issues,
            )
            open_now = sum(1 for i in issues if i["status"] != "resolved")
            print(f"maintenance: {counts['issues']} issue(s), {open_now} not yet resolved")
        except NutrientError as exc:
            print(f"maintenance: issues tab not understood ({exc})")

    schedule_rows = _rows_from_source(cfg, "schedule", root)
    if schedule_rows:
        try:
            jobs = parse_schedule(schedule_rows)
            counts["schedule"] = store.replace_table(
                "schedule",
                ("task_id", "task", "location", "equipment", "frequency_days",
                 "last_done", "done_by", "next_due", "notes"),
                jobs,
            )
            overdue = sum(1 for j in jobs if (j["days_until_due"] or 0) < 0)
            print(f"maintenance: {counts['schedule']} planned job(s), {overdue} overdue")
        except NutrientError as exc:
            print(f"maintenance: schedule tab not understood ({exc})")

    return counts


def build_payload(store, config: dict[str, Any]) -> dict[str, Any]:
    """The board's own JSON. Kept separate from the public dashboard export."""
    import time

    cfg = config.get("maintenance", {}) or {}
    anonymise = bool(cfg.get("anonymise"))

    issues = store.query("SELECT * FROM issues")
    jobs = store.query("SELECT * FROM schedule")
    today = dt.date.today()

    def scrub(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        if not anonymise:
            return record
        return {k: (None if k in fields else v) for k, v in record.items()}

    rank = {"open": 0, "in_progress": 1, "resolved": 2}
    issues = [scrub(dict(i), ("reported_by", "assigned_to")) for i in issues]
    issues.sort(key=lambda i: (
        rank.get(i.get("status"), 0),
        {"critical": 0, "attention": 1, "routine": 2}.get(i.get("severity"), 1),
        i.get("raised_on") or "",
    ))

    for job in jobs:
        due = job.get("next_due")
        job["days_until_due"] = (
            (dt.date.fromisoformat(due) - today).days if due else None
        )
    jobs = [scrub(dict(j), ("done_by",)) for j in jobs]
    jobs.sort(key=lambda j: (
        j["days_until_due"] if j["days_until_due"] is not None else 9999
    ))

    counts = {
        "open": sum(1 for i in issues if i.get("status") == "open"),
        "in_progress": sum(1 for i in issues if i.get("status") == "in_progress"),
        "resolved": sum(1 for i in issues if i.get("status") == "resolved"),
        "overdue": sum(1 for j in jobs if (j["days_until_due"] or 0) < 0),
        "due_soon": sum(
            1 for j in jobs
            if j["days_until_due"] is not None and 0 <= j["days_until_due"] <= 7
        ),
    }

    return {
        "generated_at": int(time.time()),
        "today": today.isoformat(),
        "site": config.get("site", {}),
        "anonymised": anonymise,
        "counts": counts,
        "issues": issues,
        "schedule": jobs,
    }

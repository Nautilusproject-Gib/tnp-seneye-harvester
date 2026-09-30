"""Database-agnostic storage for Seneye readings.

Default backend is SQLite (standard library, no install). PostgreSQL and MySQL
are supported when their driver happens to be present, so the same harvester can
write straight into whatever the TNP website sits on without changing the code.

Pick the backend with DATABASE_URL:

    sqlite:///data/nursery.db                 (default)
    postgresql://user:pwd@host:5432/dbname    needs psycopg or psycopg2
    mysql://user:pwd@host:3306/dbname         needs mysql-connector-python or PyMySQL

Every write is an upsert on (device_id, reading_time), so polling the same last
reading several times never duplicates a row.
"""

from __future__ import annotations

import os
import sqlite3
import urllib.parse
from contextlib import contextmanager
from typing import Any, Iterable, Sequence

from .nutrients import NUMERIC_FIELDS as NUTRIENT_FIELDS
from .seneye import PARAMETERS

READING_COLUMNS: tuple[str, ...] = (
    ("device_id", "reading_time", "fetched_at")
    + PARAMETERS
    + tuple(f"{p}_status" for p in PARAMETERS)
    + ("slide_serial", "slide_expires", "out_of_water", "disconnected")
)


class Store:
    """Thin wrapper over a DB-API connection with one paramstyle smoothed out."""

    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("DATABASE_URL") or "sqlite:///data/nursery.db"
        self.scheme = self.url.split("://", 1)[0].split("+", 1)[0].lower()
        self._conn = self._connect()

    # -- connection --------------------------------------------------------

    def _connect(self):
        if self.scheme == "sqlite":
            path = self.url.split("://", 1)[1]
            path = path.lstrip("/") if not path.startswith("//") else path[1:]
            if path and path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            conn = sqlite3.connect(path or ":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            return conn

        parts = urllib.parse.urlparse(self.url)
        kwargs = {
            "host": parts.hostname,
            "port": parts.port,
            "user": urllib.parse.unquote(parts.username or ""),
            "password": urllib.parse.unquote(parts.password or ""),
            "database": parts.path.lstrip("/"),
        }

        if self.scheme in ("postgresql", "postgres"):
            try:
                import psycopg  # type: ignore

                return psycopg.connect(
                    host=kwargs["host"],
                    port=kwargs["port"] or 5432,
                    user=kwargs["user"],
                    password=kwargs["password"],
                    dbname=kwargs["database"],
                )
            except ImportError:
                import psycopg2  # type: ignore

                return psycopg2.connect(
                    host=kwargs["host"],
                    port=kwargs["port"] or 5432,
                    user=kwargs["user"],
                    password=kwargs["password"],
                    dbname=kwargs["database"],
                )

        if self.scheme in ("mysql", "mariadb"):
            try:
                import mysql.connector  # type: ignore

                return mysql.connector.connect(
                    host=kwargs["host"],
                    port=kwargs["port"] or 3306,
                    user=kwargs["user"],
                    password=kwargs["password"],
                    database=kwargs["database"],
                )
            except ImportError:
                import pymysql  # type: ignore

                return pymysql.connect(
                    host=kwargs["host"],
                    port=kwargs["port"] or 3306,
                    user=kwargs["user"],
                    password=kwargs["password"],
                    database=kwargs["database"],
                )

        raise ValueError(f"Unsupported DATABASE_URL scheme: {self.scheme}")

    @property
    def placeholder(self) -> str:
        return "?" if self.scheme == "sqlite" else "%s"

    def sql(self, statement: str) -> str:
        """Rewrite '?' placeholders for drivers that want '%s'."""
        return statement if self.placeholder == "?" else statement.replace("?", "%s")

    @contextmanager
    def cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    # -- schema ------------------------------------------------------------

    def migrate(self) -> None:
        numeric = "REAL" if self.scheme == "sqlite" else "DOUBLE PRECISION"
        if self.scheme in ("mysql", "mariadb"):
            numeric = "DOUBLE"
        text = "TEXT" if self.scheme != "mysql" else "VARCHAR(255)"

        param_cols = ",\n            ".join(
            f"{p} {numeric}" for p in PARAMETERS
        )
        status_cols = ",\n            ".join(
            f"{p}_status INTEGER" for p in PARAMETERS
        )

        with self.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS devices (
                    device_id {text} PRIMARY KEY,
                    description {text},
                    device_type INTEGER,
                    sump_code {text},
                    system_code {text},
                    label {text},
                    first_seen INTEGER,
                    last_seen INTEGER
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS readings (
                    device_id {text} NOT NULL,
                    reading_time INTEGER NOT NULL,
                    fetched_at INTEGER NOT NULL,
                    {param_cols},
                    {status_cols},
                    slide_serial {text},
                    slide_expires INTEGER,
                    out_of_water INTEGER,
                    disconnected INTEGER,
                    PRIMARY KEY (device_id, reading_time)
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS harvest_runs (
                    run_id INTEGER PRIMARY KEY {"AUTOINCREMENT" if self.scheme == "sqlite" else "AUTO_INCREMENT" if self.scheme in ("mysql", "mariadb") else ""},
                    started_at INTEGER,
                    finished_at INTEGER,
                    status {text},
                    devices_polled INTEGER,
                    readings_inserted INTEGER,
                    message {text}
                )
                """
                if self.scheme != "postgresql"
                else """
                CREATE TABLE IF NOT EXISTS harvest_runs (
                    run_id SERIAL PRIMARY KEY,
                    started_at INTEGER,
                    finished_at INTEGER,
                    status TEXT,
                    devices_polled INTEGER,
                    readings_inserted INTEGER,
                    message TEXT
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_readings_time ON readings (reading_time)"
            )
            nutrient_cols = ",\n            ".join(
                f"{f} {numeric}" for f in NUTRIENT_FIELDS
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS nutrients (
                    sample_date {text} NOT NULL,
                    sump_code {text} NOT NULL,
                    sample_time {text},
                    {nutrient_cols},
                    observer {text},
                    notes {text},
                    PRIMARY KEY (sample_date, sump_code)
                )
                """
            )

            # Smart plugs are stored as a transition log, not a sample every
            # half hour: a row is written when a socket changes state and its
            # last_seen is bumped otherwise. Eleven sockets polled every
            # thirty minutes would be two hundred thousand rows a year to say
            # "still on"; this way the table holds the switching history, which
            # is the thing anyone would actually want to read back.
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS plug_states (
                    device_id {text} NOT NULL,
                    socket {text} NOT NULL,
                    changed_at INTEGER NOT NULL,
                    last_seen INTEGER,
                    sump_code {text},
                    role {text},
                    on_state INTEGER,
                    online INTEGER,
                    power_w {numeric},
                    PRIMARY KEY (device_id, socket, changed_at)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_plug_states_seen "
                "ON plug_states (device_id, socket, changed_at)"
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS ambient (
                    device_id {text} NOT NULL,
                    reading_time INTEGER NOT NULL,
                    air_temperature {numeric},
                    humidity {numeric},
                    battery {numeric},
                    online INTEGER,
                    PRIMARY KEY (device_id, reading_time)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_ambient_time ON ambient (reading_time)"
            )

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS issues (
                    issue_id {text} PRIMARY KEY,
                    raised_on {text},
                    raised_at {text},
                    location {text},
                    equipment {text},
                    summary {text},
                    severity {text},
                    status {text},
                    assigned_to {text},
                    action_taken {text},
                    resolved_on {text},
                    reported_by {text},
                    notes {text}
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS schedule (
                    task_id {text} PRIMARY KEY,
                    task {text},
                    location {text},
                    equipment {text},
                    frequency_days INTEGER,
                    last_done {text},
                    done_by {text},
                    next_due {text},
                    notes {text}
                )
                """
            )

    # -- writes ------------------------------------------------------------

    def upsert_device(
        self,
        device_id: str,
        description: str,
        device_type: int | None,
        sump_code: str | None,
        system_code: str | None,
        label: str | None,
        seen_at: int,
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                self.sql("SELECT device_id FROM devices WHERE device_id = ?"),
                (device_id,),
            )
            exists = cur.fetchone() is not None
            if exists:
                cur.execute(
                    self.sql(
                        "UPDATE devices SET description = ?, device_type = ?, "
                        "sump_code = ?, system_code = ?, label = ?, last_seen = ? "
                        "WHERE device_id = ?"
                    ),
                    (
                        description,
                        device_type,
                        sump_code,
                        system_code,
                        label,
                        seen_at,
                        device_id,
                    ),
                )
            else:
                cur.execute(
                    self.sql(
                        "INSERT INTO devices (device_id, description, device_type, "
                        "sump_code, system_code, label, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        device_id,
                        description,
                        device_type,
                        sump_code,
                        system_code,
                        label,
                        seen_at,
                        seen_at,
                    ),
                )

    def insert_readings(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        cols = ", ".join(READING_COLUMNS)
        marks = ", ".join("?" for _ in READING_COLUMNS)
        inserted = 0
        with self.cursor() as cur:
            for row in rows:
                values = tuple(row.get(c) for c in READING_COLUMNS)
                cur.execute(
                    self.sql(
                        "SELECT 1 FROM readings WHERE device_id = ? AND reading_time = ?"
                    ),
                    (row["device_id"], row["reading_time"]),
                )
                if cur.fetchone() is not None:
                    continue
                cur.execute(
                    self.sql(f"INSERT INTO readings ({cols}) VALUES ({marks})"), values
                )
                inserted += 1
        return inserted

    def upsert_nutrients(self, records: Iterable[dict[str, Any]]) -> int:
        """Insert or replace hand-sampled nutrient rows, keyed on date + sump.

        Replacing rather than skipping means a corrected value in the workbook
        overwrites what was loaded before, which is what you want for data that
        gets checked and revised after the fact.
        """
        records = list(records)
        if not records:
            return 0
        columns = (
            ("sample_date", "sump_code", "sample_time")
            + tuple(NUTRIENT_FIELDS)
            + ("observer", "notes")
        )
        cols = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        written = 0
        with self.cursor() as cur:
            for record in records:
                cur.execute(
                    self.sql(
                        "DELETE FROM nutrients WHERE sample_date = ? AND sump_code = ?"
                    ),
                    (record["sample_date"], record["sump_code"]),
                )
                cur.execute(
                    self.sql(f"INSERT INTO nutrients ({cols}) VALUES ({marks})"),
                    tuple(record.get(c) for c in columns),
                )
                written += 1
        return written


    def insert_plug_states(self, rows: Iterable[dict[str, Any]]) -> int:
        """Append a row per socket only when its state has actually changed.

        Returns the number of transitions recorded, so a run that found
        everything as it left it reports zero rather than eleven.
        """
        rows = list(rows)
        if not rows:
            return 0
        changes = 0
        with self.cursor() as cur:
            for row in rows:
                cur.execute(
                    self.sql(
                        "SELECT changed_at, on_state, online FROM plug_states "
                        "WHERE device_id = ? AND socket = ? "
                        "ORDER BY changed_at DESC LIMIT 1"
                    ),
                    (row["device_id"], row["socket"]),
                )
                prev = cur.fetchone()
                seen = int(row["reading_time"])
                if prev is not None:
                    prev_changed = int(prev[0])
                    same = (_same(prev[1], row.get("on_state"))
                            and _same(prev[2], row.get("online")))
                    if same:
                        cur.execute(
                            self.sql(
                                "UPDATE plug_states SET last_seen = ?, power_w = ?, "
                                "sump_code = ?, role = ? WHERE device_id = ? AND "
                                "socket = ? AND changed_at = ?"
                            ),
                            (
                                max(seen, int(prev[0])),
                                row.get("power_w"),
                                row.get("sump_code"),
                                row.get("role"),
                                row["device_id"],
                                row["socket"],
                                prev_changed,
                            ),
                        )
                        continue
                    # A change that reports an older timestamp than the row it
                    # supersedes would sort behind it and read as history
                    # running backwards, so it is clamped forward.
                    if seen <= prev_changed:
                        seen = prev_changed + 1

                cur.execute(
                    self.sql(
                        "INSERT INTO plug_states (device_id, socket, changed_at, "
                        "last_seen, sump_code, role, on_state, online, power_w) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        row["device_id"],
                        row["socket"],
                        seen,
                        seen,
                        row.get("sump_code"),
                        row.get("role"),
                        row.get("on_state"),
                        row.get("online"),
                        row.get("power_w"),
                    ),
                )
                changes += 1
        return changes

    def insert_ambient(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        columns = ("device_id", "reading_time", "air_temperature", "humidity",
                   "battery", "online")
        cols = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        inserted = 0
        with self.cursor() as cur:
            for row in rows:
                if row.get("air_temperature") is None and row.get("humidity") is None:
                    continue
                cur.execute(
                    self.sql(
                        "SELECT 1 FROM ambient WHERE device_id = ? AND reading_time = ?"
                    ),
                    (row["device_id"], int(row["reading_time"])),
                )
                if cur.fetchone() is not None:
                    continue
                cur.execute(
                    self.sql(f"INSERT INTO ambient ({cols}) VALUES ({marks})"),
                    tuple(row.get(c) for c in columns),
                )
                inserted += 1
        return inserted

    def replace_table(self, table: str, columns: tuple, records) -> int:
        """Replace a sheet-backed table wholesale.

        The sheet is the record of truth for issues and planned jobs: a row
        deleted there should disappear here too, which an upsert would not do.
        The replace runs inside one transaction, so a failure part way through
        leaves the previous contents intact rather than an empty table.
        """
        records = list(records)
        if table not in {"issues", "schedule"}:
            raise ValueError(f"replace_table refuses to touch {table}")
        cols = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        with self.cursor() as cur:
            cur.execute(f"DELETE FROM {table}")
            for record in records:
                cur.execute(
                    self.sql(f"INSERT INTO {table} ({cols}) VALUES ({marks})"),
                    tuple(record.get(c) for c in columns),
                )
        return len(records)

    def start_run(self, started_at: int) -> None:
        self._run_started = started_at

    def finish_run(
        self,
        started_at: int,
        finished_at: int,
        status: str,
        devices_polled: int,
        readings_inserted: int,
        message: str = "",
    ) -> None:
        with self.cursor() as cur:
            cur.execute(
                self.sql(
                    "INSERT INTO harvest_runs (started_at, finished_at, status, "
                    "devices_polled, readings_inserted, message) VALUES (?, ?, ?, ?, ?, ?)"
                ),
                (
                    started_at,
                    finished_at,
                    status,
                    devices_polled,
                    readings_inserted,
                    message[:900],
                ),
            )

    # -- reads -------------------------------------------------------------

    def query(self, statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(self.sql(statement), tuple(params))
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def _same(a: Any, b: Any) -> bool:
    """Compare two nullable flags without NULL swallowing the comparison."""
    if a is None or b is None:
        return a is None and b is None
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return a == b

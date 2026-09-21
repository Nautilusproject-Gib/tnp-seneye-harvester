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

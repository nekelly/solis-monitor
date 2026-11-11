"""SQLite storage for inverter readings: one row per poll in a `readings` table."""

import re
import sqlite3
import threading
from contextlib import closing

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def flatten(data):
    """Turn a read_data() dict into {column: number}.

    Every numeric value becomes a column (booleans as 0/1), and each energy counter becomes
    <name>_today and <name>_total. "yesterday" is skipped because it's just the previous day's
    "today", and inverter_time is skipped because rows are timestamped by the server clock.
    """
    row = {key: int(value) if isinstance(value, bool) else value
           for key, value in data.items() if isinstance(value, (int, float))}
    for name, counter in data["energy"].items():
        row[f"{name}_today"] = counter["today"]
        row[f"{name}_total"] = counter["total"]
    return row


class Store:
    def __init__(self, path):
        self.path = path
        self._columns_lock = threading.Lock()
        with closing(self._connect()) as conn, conn:
            # WAL lets the web requests read while the poller writes
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS readings (ts INTEGER PRIMARY KEY)")
            self._columns = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}

    def _connect(self):
        # A new connection per call keeps this safe to use from any thread
        return sqlite3.connect(self.path, timeout=10)

    def insert(self, ts, data):
        """Save one reading. ts is Unix time in seconds (UTC)."""
        row = flatten(data)
        with closing(self._connect()) as conn, conn:
            self._add_missing_columns(conn, row)
            columns = ", ".join(["ts", *row])
            placeholders = ", ".join("?" * (len(row) + 1))
            conn.execute(f"INSERT OR REPLACE INTO readings ({columns}) VALUES ({placeholders})",
                         [ts, *row.values()])

    def _add_missing_columns(self, conn, row):
        # New fields added to read_data() later get a column automatically, so the schema never needs migrating by hand
        with self._columns_lock:
            for column in row:
                if column not in self._columns:
                    if not _IDENTIFIER.match(column):
                        raise ValueError(f"unsafe column name {column!r}")
                    conn.execute(f"ALTER TABLE readings ADD COLUMN {column} REAL")
                    self._columns.add(column)

    def columns(self):
        """Names of the stored value columns (everything except ts)."""
        with self._columns_lock:
            return self._columns - {"ts"}

    def history(self, column, start_ts, end_ts, bucket_seconds):
        """Readings of one column between start_ts and end_ts, averaged into buckets of bucket_seconds.

        Returns (points, peak, low): points is a list of (bucket_start_ts, average); peak and low
        are (ts, value) for the single highest and lowest readings in the range, or None if there
        are no readings.
        """
        if column not in self.columns():
            raise KeyError(column)
        # column is checked against the table's own column names above, so it's safe to put in the SQL
        with closing(self._connect()) as conn:
            points = conn.execute(
                f"SELECT (ts / ?) * ? AS bucket, AVG({column}) FROM readings "
                f"WHERE ts >= ? AND ts < ? AND {column} IS NOT NULL GROUP BY bucket ORDER BY bucket",
                (bucket_seconds, bucket_seconds, start_ts, end_ts)).fetchall()
            # SQLite returns the ts of the row holding the MAX/MIN alongside it
            peak = conn.execute(
                f"SELECT ts, MAX({column}) FROM readings WHERE ts >= ? AND ts < ?",
                (start_ts, end_ts)).fetchone()
            low = conn.execute(
                f"SELECT ts, MIN({column}) FROM readings WHERE ts >= ? AND ts < ?",
                (start_ts, end_ts)).fetchone()
        return points, (peak if peak[1] is not None else None), (low if low[1] is not None else None)

    def energy_since(self, columns, start_ts, max_gap_seconds):
        """Estimate energy in kWh for power columns (W) from start_ts until now.

        Adds up average power x time between consecutive readings (the trapezoidal rule).
        Intervals longer than max_gap_seconds are gaps in polling and aren't counted.
        """
        unknown = [c for c in columns if c not in self.columns()]
        if unknown:
            raise KeyError(unknown[0])
        with closing(self._connect()) as conn:
            rows = conn.execute(f"SELECT ts, {', '.join(columns)} FROM readings WHERE ts >= ? ORDER BY ts",
                                (start_ts,)).fetchall()
        totals = [0.0] * len(columns)
        for previous, current in zip(rows, rows[1:]):
            seconds = current[0] - previous[0]
            if seconds <= 0 or seconds > max_gap_seconds:
                continue
            for i, (a, b) in enumerate(zip(previous[1:], current[1:])):
                if a is not None and b is not None:
                    totals[i] += (a + b) / 2 * seconds / 3_600_000   # W·s -> kWh
        return dict(zip(columns, totals))

    def prune(self, older_than_ts):
        """Delete readings older than older_than_ts; returns how many were removed."""
        with closing(self._connect()) as conn, conn:
            return conn.execute("DELETE FROM readings WHERE ts < ?", (older_than_ts,)).rowcount

    def count(self):
        with closing(self._connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

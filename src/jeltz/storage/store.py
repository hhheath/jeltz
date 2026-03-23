"""SQLite time-series store for sensor readings."""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
from pydantic import BaseModel


class Reading(BaseModel):
    """A single sensor reading."""

    timestamp: float
    device_id: str
    sensor_id: str
    value: float
    unit: str


class AnomalyResult(BaseModel):
    """A sensor reading flagged as anomalous relative to its rolling baseline."""

    device_id: str
    sensor_id: str
    current_value: float
    mean: float
    stddev: float
    sigma_distance: float
    unit: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    timestamp   REAL    NOT NULL,
    device_id   TEXT    NOT NULL,
    sensor_id   TEXT    NOT NULL,
    value       REAL    NOT NULL,
    unit        TEXT    NOT NULL,
    downsampled INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_readings_device_sensor_time
    ON readings (device_id, sensor_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_readings_downsampled
    ON readings (downsampled, timestamp);
"""


class ReadingStore:
    """Async SQLite store for sensor readings."""

    def __init__(self, db_path: str | Path = "jeltz.db") -> None:
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the database and create tables if needed."""
        self._db = await aiosqlite.connect(self._db_path, isolation_level=None)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def __aenter__(self) -> ReadingStore:
        await self.init()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store not initialized — call init() first")
        return self._db

    async def open_retention_conn(self) -> tuple[aiosqlite.Connection, bool]:
        """Open a connection for retention cleanup.

        Returns (connection, should_close). For file-backed databases,
        opens a separate connection so retention transactions don't
        interfere with the main connection's record() commits. For
        in-memory databases (tests), returns the main connection.
        """
        if self._db_path == ":memory:":
            return self._conn(), False
        conn = await aiosqlite.connect(self._db_path, isolation_level=None)
        await conn.execute("PRAGMA journal_mode=WAL")
        return conn, True

    async def record(
        self,
        device_id: str,
        sensor_id: str,
        value: float,
        unit: str,
        timestamp: float | None = None,
    ) -> Reading:
        """Record a sensor reading. Returns the stored reading."""
        ts = timestamp if timestamp is not None else time.time()
        db = self._conn()
        await db.execute(
            "INSERT INTO readings (timestamp, device_id, sensor_id, value, unit) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, device_id, sensor_id, value, unit),
        )
        await db.commit()
        return Reading(
            timestamp=ts, device_id=device_id, sensor_id=sensor_id, value=value, unit=unit
        )

    async def record_batch(
        self,
        readings: list[tuple[str, str, float, str, float | None]],
    ) -> int:
        """Record multiple readings at once.

        Each tuple: (device_id, sensor_id, value, unit, timestamp|None).
        Returns the number of readings inserted.
        """
        db = self._conn()
        now = time.time()
        rows = [
            (ts if ts is not None else now, did, sid, val, unit)
            for did, sid, val, unit, ts in readings
        ]
        await db.executemany(
            "INSERT INTO readings (timestamp, device_id, sensor_id, value, unit) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await db.commit()
        return len(rows)

    async def get_history(
        self,
        device_id: str,
        sensor_id: str,
        start: float | None = None,
        end: float | None = None,
        limit: int = 1000,
    ) -> list[Reading]:
        """Query time-series readings for a specific sensor."""
        db = self._conn()
        clauses = ["device_id = ?", "sensor_id = ?"]
        params: list[object] = [device_id, sensor_id]

        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp <= ?")
            params.append(end)

        where = " AND ".join(clauses)
        params.append(limit)

        cursor = await db.execute(
            f"SELECT timestamp, device_id, sensor_id, value, unit "
            f"FROM readings WHERE {where} "
            f"ORDER BY timestamp DESC LIMIT ?",
            params,
        )
        rows = await cursor.fetchall()
        return [
            Reading(timestamp=r[0], device_id=r[1], sensor_id=r[2], value=r[3], unit=r[4])
            for r in rows
        ]

    async def get_latest(
        self,
        device_id: str,
        sensor_id: str,
    ) -> Reading | None:
        """Get the most recent reading for a specific sensor."""
        db = self._conn()
        cursor = await db.execute(
            "SELECT timestamp, device_id, sensor_id, value, unit "
            "FROM readings "
            "WHERE device_id = ? AND sensor_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (device_id, sensor_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return Reading(
            timestamp=row[0], device_id=row[1], sensor_id=row[2],
            value=row[3], unit=row[4],
        )

    async def get_all_latest(self) -> list[Reading]:
        """Get the most recent reading for every device/sensor pair.

        Backs fleet.get_all_readings — one call gives the LLM the full system state.
        """
        db = self._conn()
        cursor = await db.execute(
            "SELECT timestamp, device_id, sensor_id, value, unit "
            "FROM ("
            "  SELECT timestamp, device_id, sensor_id, value, unit, "
            "    ROW_NUMBER() OVER ("
            "      PARTITION BY device_id, sensor_id "
            "      ORDER BY timestamp DESC, rowid DESC"
            "    ) AS rn "
            "  FROM readings"
            ") "
            "WHERE rn = 1 "
            "ORDER BY device_id, sensor_id",
        )
        rows = await cursor.fetchall()
        return [
            Reading(timestamp=r[0], device_id=r[1], sensor_id=r[2], value=r[3], unit=r[4])
            for r in rows
        ]

    async def search_anomalies(
        self,
        threshold_sigma: float = 2.0,
        baseline_days: int = 30,
    ) -> list[AnomalyResult]:
        """Find sensors whose latest reading deviates from their rolling baseline.

        Computes mean and stddev over the last `baseline_days` days per sensor,
        then flags any sensor whose most recent reading exceeds `threshold_sigma`
        standard deviations from the mean.

        This is simple statistical detection, not ML-based. It will false-positive
        on periodic signals (HVAC cycles, batch processes). Good enough for v1.
        """
        db = self._conn()
        cutoff = time.time() - (baseline_days * 86400)

        cursor = await db.execute(
            """
            WITH baseline AS (
                SELECT
                    device_id,
                    sensor_id,
                    AVG(value) AS mean,
                    -- population stddev; coalesce to 0 for single-reading sensors
                    COALESCE(
                        SQRT(AVG(value * value) - AVG(value) * AVG(value)),
                        0
                    ) AS stddev,
                    COUNT(*) AS sample_count
                FROM readings
                WHERE timestamp >= ?
                  AND downsampled = 0
                GROUP BY device_id, sensor_id
                HAVING COUNT(*) >= 2
            ),
            latest AS (
                SELECT device_id, sensor_id, value, unit
                FROM (
                    SELECT device_id, sensor_id, value, unit,
                        ROW_NUMBER() OVER (
                            PARTITION BY device_id, sensor_id
                            ORDER BY timestamp DESC, rowid DESC
                        ) AS rn
                    FROM readings
                    WHERE downsampled = 0
                )
                WHERE rn = 1
            )
            SELECT
                l.device_id,
                l.sensor_id,
                l.value AS current_value,
                b.mean,
                b.stddev,
                l.unit
            FROM latest l
            JOIN baseline b
                ON l.device_id = b.device_id
                AND l.sensor_id = b.sensor_id
            WHERE b.stddev > 0
              AND ABS(l.value - b.mean) > ? * b.stddev
            ORDER BY ABS(l.value - b.mean) / b.stddev DESC
            """,
            (cutoff, threshold_sigma),
        )
        rows = await cursor.fetchall()
        return [
            AnomalyResult(
                device_id=r[0],
                sensor_id=r[1],
                current_value=r[2],
                mean=r[3],
                stddev=r[4],
                sigma_distance=abs(r[2] - r[3]) / r[4] if r[4] > 0 else 0,
                unit=r[5],
            )
            for r in rows
        ]

"""Data retention — downsample old readings, purge expired data."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from jeltz.storage.store import ReadingStore

# Defaults from ARCHITECTURE.md
DEFAULT_FULL_RES_DAYS = 30
DEFAULT_KEEP_DOWNSAMPLED_DAYS = 365


async def run_cleanup(
    store: ReadingStore,
    full_res_days: int = DEFAULT_FULL_RES_DAYS,
    keep_downsampled_days: int = DEFAULT_KEEP_DOWNSAMPLED_DAYS,
) -> dict[str, int]:
    """Run the full retention pipeline: downsample, then purge.

    Opens a separate database connection so retention transactions
    don't interfere with the main connection's record() commits.

    Returns counts of rows affected for observability.
    """
    db, should_close = await store.open_retention_conn()
    try:
        downsampled = await _downsample_old_readings(db, full_res_days)
        purged = await _purge_expired(db, keep_downsampled_days)
        return {"downsampled": downsampled, "purged": purged}
    finally:
        if should_close:
            await db.close()


async def _downsample_old_readings(
    db: aiosqlite.Connection,
    full_res_days: int,
) -> int:
    """Replace full-resolution readings older than `full_res_days` with hourly averages.

    Only operates on rows where downsampled = 0 (original readings).
    Inserts hourly averages with downsampled = 1, then deletes the originals.
    """
    cutoff = time.time() - (full_res_days * 86400)

    # Use an explicit transaction so the INSERT + DELETE are atomic.
    # This runs on a dedicated connection so no other caller can
    # commit or roll back our partial work.
    await db.execute("BEGIN IMMEDIATE")
    try:
        # Insert hourly averages for old full-res data
        await db.execute(
            """
            INSERT INTO readings (timestamp, device_id, sensor_id, value, unit, downsampled)
            SELECT
                -- Use the start of each hour as the timestamp
                CAST(CAST(timestamp / 3600 AS INTEGER) * 3600 AS REAL),
                device_id,
                sensor_id,
                AVG(value),
                unit,
                1
            FROM readings
            WHERE timestamp < ?
              AND downsampled = 0
            GROUP BY device_id, sensor_id, unit, CAST(timestamp / 3600 AS INTEGER)
            """,
            (cutoff,),
        )

        # Delete the original full-res rows that were just downsampled
        cursor = await db.execute(
            "DELETE FROM readings WHERE timestamp < ? AND downsampled = 0",
            (cutoff,),
        )
        deleted = cursor.rowcount

        await db.commit()
    except BaseException:
        # Shield rollback from CancelledError so the connection
        # is left in a clean state even during task cancellation.
        try:
            await asyncio.shield(db.rollback())
        except Exception:
            pass
        raise

    return deleted


async def _purge_expired(
    db: aiosqlite.Connection,
    keep_downsampled_days: int,
) -> int:
    """Delete downsampled readings older than `keep_downsampled_days`."""
    cutoff = time.time() - (keep_downsampled_days * 86400)

    cursor = await db.execute(
        "DELETE FROM readings WHERE timestamp < ? AND downsampled = 1",
        (cutoff,),
    )
    purged = cursor.rowcount

    await db.commit()
    return purged

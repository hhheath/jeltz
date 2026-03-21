"""Tests for data retention — downsample and purge."""

import time

import pytest

from jeltz.storage.retention import run_cleanup
from jeltz.storage.store import ReadingStore


@pytest.fixture
async def store():
    s = ReadingStore(db_path=":memory:")
    await s.init()
    yield s
    await s.close()


async def test_downsample_replaces_old_readings(store: ReadingStore):
    now = time.time()
    old = now - (31 * 86400)  # 31 days ago

    # Insert 60 readings across one hour, 31 days ago
    for i in range(60):
        await store.record("tank1", "temp", 22.0 + i * 0.01, "celsius", timestamp=old + i * 60)

    result = await run_cleanup(store, full_res_days=30)
    assert result["downsampled"] == 60

    # Original readings should be gone, replaced by hourly averages
    history = await store.get_history("tank1", "temp", start=old - 3600, end=old + 7200)
    assert len(history) > 0
    assert len(history) < 60  # Should be condensed to hourly buckets


async def test_downsample_leaves_recent_data_alone(store: ReadingStore):
    now = time.time()

    # Insert recent readings (within retention window)
    for i in range(10):
        await store.record("tank1", "temp", 22.0, "celsius", timestamp=now - i * 60)

    result = await run_cleanup(store, full_res_days=30)
    assert result["downsampled"] == 0

    history = await store.get_history("tank1", "temp")
    assert len(history) == 10


async def test_purge_deletes_old_downsampled(store: ReadingStore):
    db = store._conn()
    now = time.time()
    very_old = now - (400 * 86400)  # 400 days ago

    # Manually insert a downsampled reading
    await db.execute(
        "INSERT INTO readings (timestamp, device_id, sensor_id, value, unit, downsampled) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (very_old, "tank1", "temp", 22.0, "celsius"),
    )
    await db.commit()

    result = await run_cleanup(store, keep_downsampled_days=365)
    assert result["purged"] == 1


async def test_purge_keeps_recent_downsampled(store: ReadingStore):
    db = store._conn()
    now = time.time()
    recent_ds = now - (100 * 86400)  # 100 days ago

    await db.execute(
        "INSERT INTO readings (timestamp, device_id, sensor_id, value, unit, downsampled) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (recent_ds, "tank1", "temp", 22.0, "celsius"),
    )
    await db.commit()

    result = await run_cleanup(store, keep_downsampled_days=365)
    assert result["purged"] == 0


async def test_full_cleanup_pipeline(store: ReadingStore):
    """End-to-end: recent stays, old gets downsampled, ancient gets purged."""
    db = store._conn()
    now = time.time()

    # Recent data — should be untouched
    for i in range(5):
        await store.record("tank1", "temp", 22.0, "celsius", timestamp=now - i * 60)

    # Old data (31 days) — should get downsampled
    old = now - (31 * 86400)
    for i in range(10):
        await store.record("tank1", "temp", 23.0, "celsius", timestamp=old + i * 60)

    # Ancient downsampled data (400 days) — should get purged
    ancient = now - (400 * 86400)
    await db.execute(
        "INSERT INTO readings (timestamp, device_id, sensor_id, value, unit, downsampled) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (ancient, "tank1", "temp", 20.0, "celsius"),
    )
    await db.commit()

    result = await run_cleanup(store, full_res_days=30, keep_downsampled_days=365)
    assert result["downsampled"] == 10
    assert result["purged"] == 1

    # Recent data still intact
    recent = await store.get_history("tank1", "temp", start=now - 3600)
    assert len(recent) == 5


async def test_anomalies_correct_after_retention(store: ReadingStore):
    """search_anomalies should not be skewed by downsampled data."""
    now = time.time()
    old = now - (31 * 86400)

    # 60 old readings with tight variance — will become downsampled hourly averages
    for i in range(60):
        await store.record("tank1", "temp", 22.0 + i * 0.001, "celsius", timestamp=old + i * 60)

    # 30 recent readings with wider but normal variance
    for i in range(30):
        ts = now - 3000 + i * 100
        await store.record("tank1", "temp", 22.0 + (i % 5) * 0.5, "celsius", timestamp=ts)

    # Run retention — old data becomes hourly averages with near-zero variance
    await run_cleanup(store, full_res_days=30)

    # Latest reading within the recent distribution — should NOT be flagged
    await store.record("tank1", "temp", 22.8, "celsius", timestamp=now)
    anomalies = await store.search_anomalies(threshold_sigma=2.0)
    assert len(anomalies) == 0


async def test_get_all_latest_with_duplicate_timestamps(store: ReadingStore):
    """When two readings share a max timestamp, only one should be returned."""
    await store.record("tank1", "temp", 22.0, "celsius", timestamp=1000.0)
    await store.record("tank1", "temp", 23.0, "celsius", timestamp=1000.0)

    latest = await store.get_all_latest()
    assert len(latest) == 1
    # Should return the last-inserted row (highest rowid)
    assert latest[0].value == 23.0

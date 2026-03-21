"""Tests for the SQLite reading store."""

import time

import pytest

from jeltz.storage.store import ReadingStore


@pytest.fixture
async def store():
    """In-memory SQLite store for testing."""
    s = ReadingStore(db_path=":memory:")
    await s.init()
    yield s
    await s.close()


async def test_record_and_get_latest(store: ReadingStore):
    await store.record("tank1", "temp", 22.5, "celsius", timestamp=1000.0)
    await store.record("tank1", "temp", 23.0, "celsius", timestamp=2000.0)

    latest = await store.get_latest("tank1", "temp")
    assert latest is not None
    assert latest.value == 23.0
    assert latest.timestamp == 2000.0


async def test_get_latest_returns_none_for_unknown(store: ReadingStore):
    result = await store.get_latest("nonexistent", "temp")
    assert result is None


async def test_record_returns_reading(store: ReadingStore):
    reading = await store.record("tank1", "temp", 22.5, "celsius", timestamp=1000.0)
    assert reading.device_id == "tank1"
    assert reading.sensor_id == "temp"
    assert reading.value == 22.5
    assert reading.unit == "celsius"
    assert reading.timestamp == 1000.0


async def test_record_auto_timestamps(store: ReadingStore):
    before = time.time()
    reading = await store.record("tank1", "temp", 22.5, "celsius")
    after = time.time()
    assert before <= reading.timestamp <= after


async def test_get_history_returns_newest_first(store: ReadingStore):
    for i in range(5):
        await store.record("tank1", "temp", 20.0 + i, "celsius", timestamp=1000.0 + i)

    history = await store.get_history("tank1", "temp")
    assert len(history) == 5
    assert history[0].timestamp > history[-1].timestamp


async def test_get_history_time_range(store: ReadingStore):
    for i in range(10):
        await store.record("tank1", "temp", 20.0 + i, "celsius", timestamp=1000.0 + i)

    history = await store.get_history("tank1", "temp", start=1003.0, end=1006.0)
    assert len(history) == 4
    assert all(1003.0 <= r.timestamp <= 1006.0 for r in history)


async def test_get_history_limit(store: ReadingStore):
    for i in range(20):
        await store.record("tank1", "temp", 20.0 + i, "celsius", timestamp=1000.0 + i)

    history = await store.get_history("tank1", "temp", limit=5)
    assert len(history) == 5


async def test_get_history_isolates_devices(store: ReadingStore):
    await store.record("tank1", "temp", 22.0, "celsius", timestamp=1000.0)
    await store.record("tank2", "temp", 25.0, "celsius", timestamp=1000.0)

    history = await store.get_history("tank1", "temp")
    assert len(history) == 1
    assert history[0].device_id == "tank1"


async def test_get_all_latest(store: ReadingStore):
    await store.record("tank1", "temp", 22.0, "celsius", timestamp=1000.0)
    await store.record("tank1", "temp", 23.0, "celsius", timestamp=2000.0)
    await store.record("tank2", "temp", 25.0, "celsius", timestamp=1500.0)
    await store.record("tank1", "humidity", 60.0, "percent", timestamp=1800.0)

    latest = await store.get_all_latest()
    assert len(latest) == 3

    by_key = {(r.device_id, r.sensor_id): r for r in latest}
    assert by_key[("tank1", "temp")].value == 23.0
    assert by_key[("tank2", "temp")].value == 25.0
    assert by_key[("tank1", "humidity")].value == 60.0


async def test_get_all_latest_empty(store: ReadingStore):
    latest = await store.get_all_latest()
    assert latest == []


async def test_record_batch(store: ReadingStore):
    readings = [
        ("tank1", "temp", 22.0, "celsius", 1000.0),
        ("tank1", "temp", 23.0, "celsius", 2000.0),
        ("tank2", "temp", 25.0, "celsius", 1500.0),
    ]
    count = await store.record_batch(readings)
    assert count == 3

    latest = await store.get_all_latest()
    assert len(latest) == 2


async def test_search_anomalies_flags_outlier(store: ReadingStore):
    now = time.time()
    # Build a stable baseline: 50 readings around 22.0
    for i in range(50):
        value = 22.0 + (i % 3) * 0.1
        await store.record("tank1", "temp", value, "celsius", timestamp=now - 5000 + i * 100)

    # Spike the latest reading
    await store.record("tank1", "temp", 35.0, "celsius", timestamp=now)

    anomalies = await store.search_anomalies(threshold_sigma=2.0)
    assert len(anomalies) == 1
    assert anomalies[0].device_id == "tank1"
    assert anomalies[0].sensor_id == "temp"
    assert anomalies[0].current_value == 35.0
    assert anomalies[0].sigma_distance > 2.0


async def test_search_anomalies_ignores_normal(store: ReadingStore):
    now = time.time()
    for i in range(50):
        value = 22.0 + (i % 3) * 0.1
        await store.record("tank1", "temp", value, "celsius", timestamp=now - 5000 + i * 100)

    # Latest reading is within normal range
    await store.record("tank1", "temp", 22.1, "celsius", timestamp=now)

    anomalies = await store.search_anomalies(threshold_sigma=2.0)
    assert len(anomalies) == 0


async def test_search_anomalies_needs_multiple_readings(store: ReadingStore):
    """A single reading has no baseline to compare against."""
    await store.record("tank1", "temp", 22.0, "celsius", timestamp=time.time())
    anomalies = await store.search_anomalies()
    assert len(anomalies) == 0


async def test_store_not_initialized_raises(store: ReadingStore):
    closed_store = ReadingStore(db_path=":memory:")
    with pytest.raises(RuntimeError, match="not initialized"):
        await closed_store.record("tank1", "temp", 22.0, "celsius")

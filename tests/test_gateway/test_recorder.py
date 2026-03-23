"""Tests for the background sensor recorder."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jeltz.adapters.mock import MockAdapter
from jeltz.gateway.aggregator import Aggregator
from jeltz.gateway.discovery import discover_profiles
from jeltz.gateway.recorder import _recordable_routes, run_recorder
from jeltz.storage.store import ReadingStore


@pytest.fixture
def profiles_dir(tmp_path: Path) -> Path:
    """Profiles dir with one numeric sensor and one string sensor."""
    (tmp_path / "temp.toml").write_text(
        '[device]\n'
        'name = "temp_sensor"\n'
        'description = "Temperature sensor"\n'
        '[connection]\n'
        'protocol = "mock"\n'
        '[[tools]]\n'
        'name = "get_reading"\n'
        'description = "Get temperature"\n'
        'command = "READ_TEMP"\n'
        '[tools.returns]\n'
        'type = "float"\n'
        'unit = "celsius"\n'
    )
    (tmp_path / "status.toml").write_text(
        '[device]\n'
        'name = "status_device"\n'
        'description = "Device with string tool"\n'
        '[connection]\n'
        'protocol = "mock"\n'
        '[[tools]]\n'
        'name = "get_status"\n'
        'description = "Get status"\n'
        'command = "STATUS"\n'
        '[tools.returns]\n'
        'type = "string"\n'
    )
    return tmp_path


@pytest.fixture
async def aggregator(profiles_dir: Path):
    discovery = discover_profiles(profiles_dir)
    agg = Aggregator(discovery.devices)
    await agg.connect_all()

    # Configure mock responses
    for name in agg.device_names:
        status = agg.get_status(name)
        if status and isinstance(status.device.adapter, MockAdapter):
            status.device.adapter.responses = {
                "READ_TEMP": "22.5",
                "STATUS": "OK",
            }

    yield agg
    await agg.disconnect_all()


@pytest.fixture
async def store():
    s = ReadingStore(db_path=":memory:")
    await s.init()
    yield s
    await s.close()


class TestRecordableRoutes:
    def test_only_numeric_tools_are_recordable(self, aggregator: Aggregator) -> None:
        by_device = _recordable_routes(aggregator)
        # temp_sensor.get_reading is numeric (float) — should be present
        assert "temp_sensor" in by_device
        # status_device.get_status is string — should not be present
        assert "status_device" not in by_device

    def test_skips_tools_with_required_params(self, tmp_path: Path) -> None:
        """Tools requiring parameters can't be auto-polled."""
        (tmp_path / "param_tool.toml").write_text(
            '[device]\n'
            'name = "param_sensor"\n'
            '[connection]\n'
            'protocol = "mock"\n'
            '[[tools]]\n'
            'name = "get_reading"\n'
            'description = "Get reading for a channel"\n'
            'command = "READ {channel}"\n'
            '[tools.params.channel]\n'
            'type = "int"\n'
            'required = true\n'
            '[tools.returns]\n'
            'type = "float"\n'
            'unit = "volts"\n'
        )
        discovery = discover_profiles(tmp_path)
        agg = Aggregator(discovery.devices)
        by_device = _recordable_routes(agg)
        assert "param_sensor" not in by_device

    def test_returns_correct_route_info(self, aggregator: Aggregator) -> None:
        by_device = _recordable_routes(aggregator)
        routes = by_device["temp_sensor"]
        assert len(routes) == 1
        assert routes[0].tool_name == "get_reading"
        assert routes[0].returns is not None
        assert routes[0].returns.unit == "celsius"


class TestRunRecorder:
    async def test_records_readings_to_store(
        self, aggregator: Aggregator, store: ReadingStore
    ) -> None:
        stop_event = asyncio.Event()

        async def stop_after_delay():
            await asyncio.sleep(0.3)
            stop_event.set()

        await asyncio.gather(
            run_recorder(aggregator, store, stop_event),
            stop_after_delay(),
        )

        # Should have recorded at least one reading
        readings = await store.get_history("temp_sensor", "get_reading", start=0)
        assert len(readings) >= 1
        assert readings[0].unit == "celsius"
        assert readings[0].value == 22.5

    async def test_stops_on_event(
        self, aggregator: Aggregator, store: ReadingStore
    ) -> None:
        stop_event = asyncio.Event()
        stop_event.set()  # Set immediately

        # Should return quickly
        await asyncio.wait_for(
            run_recorder(aggregator, store, stop_event),
            timeout=2.0,
        )

    async def test_no_recordable_sensors(self, store: ReadingStore) -> None:
        """Recorder with no numeric sensors should still run and stop cleanly."""
        # Create a profiles dir with only string tools
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            Path(td, "status.toml").write_text(
                '[device]\n'
                'name = "status_only"\n'
                '[connection]\n'
                'protocol = "mock"\n'
                '[[tools]]\n'
                'name = "get_status"\n'
                'description = "Get status"\n'
                'command = "STATUS"\n'
                '[tools.returns]\n'
                'type = "string"\n'
            )
            discovery = discover_profiles(Path(td))
            agg = Aggregator(discovery.devices)
            await agg.connect_all()

            stop_event = asyncio.Event()

            async def stop_soon():
                await asyncio.sleep(0.1)
                stop_event.set()

            await asyncio.gather(
                run_recorder(agg, store, stop_event),
                stop_soon(),
            )
            await agg.disconnect_all()

    async def test_handles_adapter_errors(self, store: ReadingStore) -> None:
        """Recorder continues polling even if a device returns errors."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            Path(td, "broken.toml").write_text(
                '[device]\n'
                'name = "broken_sensor"\n'
                '[connection]\n'
                'protocol = "mock"\n'
                '[[tools]]\n'
                'name = "get_reading"\n'
                'description = "Get value"\n'
                'command = "READ"\n'
                '[tools.returns]\n'
                'type = "float"\n'
                'unit = "units"\n'
            )
            discovery = discover_profiles(Path(td))
            agg = Aggregator(discovery.devices)
            await agg.connect_all()

            # Mock adapter with no configured responses — will return error
            stop_event = asyncio.Event()

            async def stop_soon():
                await asyncio.sleep(0.3)
                stop_event.set()

            # Should not raise
            await asyncio.gather(
                run_recorder(agg, store, stop_event),
                stop_soon(),
            )

            # No readings recorded (all failed)
            readings = await store.get_history("broken_sensor", "get_reading", start=0)
            assert len(readings) == 0
            await agg.disconnect_all()


    async def test_cancellation_cleans_up_tasks(self, store: ReadingStore) -> None:
        """When run_recorder is cancelled, all child tasks should be cancelled."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            Path(td, "sensor.toml").write_text(
                '[device]\n'
                'name = "slow_sensor"\n'
                '[connection]\n'
                'protocol = "mock"\n'
                '[recording]\n'
                'poll_interval_ms = 100\n'
                '[[tools]]\n'
                'name = "get_reading"\n'
                'description = "Get value"\n'
                'command = "READ"\n'
                '[tools.returns]\n'
                'type = "float"\n'
                'unit = "units"\n'
            )
            discovery = discover_profiles(Path(td))
            agg = Aggregator(discovery.devices)
            await agg.connect_all()

            for name in agg.device_names:
                status = agg.get_status(name)
                if status and isinstance(status.device.adapter, MockAdapter):
                    status.device.adapter.responses = {"READ": "42.0"}

            stop_event = asyncio.Event()
            task = asyncio.create_task(run_recorder(agg, store, stop_event))

            # Let it run briefly
            await asyncio.sleep(0.2)

            # Cancel it
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # The task should be done — no dangling tasks
            assert task.done()
            await agg.disconnect_all()


class TestRecordingConfig:
    def test_custom_poll_interval(self, tmp_path: Path) -> None:
        """Profile with [recording] section should parse correctly."""
        (tmp_path / "fast.toml").write_text(
            '[device]\n'
            'name = "fast_sensor"\n'
            '[connection]\n'
            'protocol = "mock"\n'
            '[recording]\n'
            'poll_interval_ms = 1000\n'
            '[[tools]]\n'
            'name = "get_reading"\n'
            'description = "Get value"\n'
            'command = "READ"\n'
            '[tools.returns]\n'
            'type = "float"\n'
        )
        discovery = discover_profiles(tmp_path)
        assert len(discovery.devices) == 1
        assert discovery.devices[0].model.recording.poll_interval_ms == 1000

    def test_default_poll_interval(self, tmp_path: Path) -> None:
        """Profile without [recording] should get default 5000ms."""
        (tmp_path / "default.toml").write_text(
            '[device]\n'
            'name = "default_sensor"\n'
            '[connection]\n'
            'protocol = "mock"\n'
        )
        discovery = discover_profiles(tmp_path)
        assert len(discovery.devices) == 1
        assert discovery.devices[0].model.recording.poll_interval_ms == 5000
        assert discovery.devices[0].model.recording.enabled is True

    def test_recording_disabled(self, tmp_path: Path) -> None:
        """Profile with recording.enabled = false."""
        (tmp_path / "quiet.toml").write_text(
            '[device]\n'
            'name = "quiet_sensor"\n'
            '[connection]\n'
            'protocol = "mock"\n'
            '[recording]\n'
            'enabled = false\n'
        )
        discovery = discover_profiles(tmp_path)
        assert len(discovery.devices) == 1
        assert discovery.devices[0].model.recording.enabled is False

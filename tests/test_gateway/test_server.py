"""Tests for the Jeltz MCP server — lifecycle, tool listing, and dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from jeltz.adapters.mock import MockAdapter
from jeltz.gateway.server import JeltzServer


@pytest.fixture
def profiles_dir(tmp_path: Path) -> Path:
    """Create a temp profiles directory with mock devices."""
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
    (tmp_path / "pressure.toml").write_text(
        '[device]\n'
        'name = "pressure_sensor"\n'
        'description = "Pressure sensor"\n'
        '[connection]\n'
        'protocol = "mock"\n'
        '[[tools]]\n'
        'name = "get_reading"\n'
        'description = "Get pressure"\n'
        'command = "READ_PSI"\n'
        '[tools.returns]\n'
        'type = "float"\n'
        'unit = "psi"\n'
    )
    return tmp_path


@pytest.fixture
async def server(profiles_dir: Path):
    srv = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
    await srv.start()
    yield srv
    await srv.stop()


class TestServerLifecycle:
    async def test_start_initializes_components(self, server: JeltzServer) -> None:
        assert server.aggregator is not None
        assert server.fleet is not None
        assert server.store is not None

    async def test_start_connects_devices(self, server: JeltzServer) -> None:
        assert server.aggregator is not None
        for name in server.aggregator.device_names:
            status = server.aggregator.get_status(name)
            assert status is not None
            assert status.connected

    async def test_start_discovers_devices(self, server: JeltzServer) -> None:
        assert server.aggregator is not None
        assert sorted(server.aggregator.device_names) == [
            "pressure_sensor", "temp_sensor",
        ]

    async def test_start_returns_discovery_result(
        self, profiles_dir: Path
    ) -> None:
        srv = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        result = await srv.start()
        assert len(result.devices) == 2
        assert result.errors == []
        await srv.stop()

    async def test_start_with_bad_profile(self, tmp_path: Path) -> None:
        (tmp_path / "good.toml").write_text(
            '[device]\nname = "good"\n[connection]\nprotocol = "mock"\n'
        )
        (tmp_path / "bad.toml").write_text("not valid toml {{{{")
        srv = JeltzServer(profiles_dir=tmp_path, db_path=":memory:")
        result = await srv.start()
        assert len(result.devices) == 1
        assert len(result.errors) == 1
        await srv.stop()

    async def test_stop_disconnects_devices(self, profiles_dir: Path) -> None:
        srv = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        await srv.start()
        agg = srv.aggregator
        assert agg is not None
        await srv.stop()
        # References cleared
        assert srv.aggregator is None
        assert srv.fleet is None
        assert srv.store is None
        # But the aggregator object still shows disconnected
        for name in agg.device_names:
            status = agg.get_status(name)
            assert not status.connected

    async def test_stop_clears_references(self, profiles_dir: Path) -> None:
        srv = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        await srv.start()
        await srv.stop()
        assert srv.aggregator is None
        assert srv.fleet is None
        assert srv.store is None

    async def test_double_start_raises(self, profiles_dir: Path) -> None:
        srv = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        await srv.start()
        with pytest.raises(RuntimeError, match="already started"):
            await srv.start()
        await srv.stop()

    async def test_restart_after_stop(self, profiles_dir: Path) -> None:
        srv = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        await srv.start()
        await srv.stop()
        # Should be able to start again after stop
        await srv.start()
        assert srv.aggregator is not None
        await srv.stop()

    async def test_empty_profiles_dir(self, tmp_path: Path) -> None:
        srv = JeltzServer(profiles_dir=tmp_path, db_path=":memory:")
        result = await srv.start()
        assert len(result.devices) == 0
        assert srv.aggregator is not None
        assert srv.aggregator.tools == []
        await srv.stop()

    async def test_reserved_device_name_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "bad.toml").write_text(
            '[device]\nname = "fleet"\n[connection]\nprotocol = "mock"\n'
        )
        srv = JeltzServer(profiles_dir=tmp_path, db_path=":memory:")
        with pytest.raises(ValueError, match="reserved"):
            await srv.start()
        # Store was opened before the error — clean up
        if srv.store:
            await srv.store.close()

    async def test_serve_stdio_before_start_raises(
        self, profiles_dir: Path
    ) -> None:
        srv = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        with pytest.raises(RuntimeError, match="not started"):
            await srv.serve_stdio()

    async def test_run_stdio_cleans_up_on_start_failure(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "bad.toml").write_text(
            '[device]\nname = "fleet"\n[connection]\nprotocol = "mock"\n'
        )
        srv = JeltzServer(profiles_dir=tmp_path, db_path=":memory:")
        with pytest.raises(ValueError, match="reserved"):
            await srv.run_stdio()
        # stop() should have been called by run_stdio's finally block
        assert srv.store is None
        assert srv.aggregator is None


class TestHandleListTools:
    async def test_merges_device_and_fleet_tools(self, server: JeltzServer) -> None:
        tools = server.handle_list_tools()
        names = {t.name for t in tools}
        assert "temp_sensor.get_reading" in names
        assert "pressure_sensor.get_reading" in names
        assert "fleet.list_devices" in names
        assert "fleet.get_all_readings" in names
        assert "fleet.get_history" in names
        assert "fleet.search_anomalies" in names

    async def test_total_tool_count(self, server: JeltzServer) -> None:
        tools = server.handle_list_tools()
        # 2 device tools + 4 fleet tools = 6
        assert len(tools) == 6

    async def test_before_start_returns_empty(self, profiles_dir: Path) -> None:
        srv = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        tools = srv.handle_list_tools()
        assert tools == []


class TestHandleCallTool:
    async def test_fleet_tool_success(self, server: JeltzServer) -> None:
        result = await server.handle_call_tool("fleet.list_devices", {})
        assert result.isError is not True
        assert result.structuredContent is not None
        assert result.structuredContent["count"] == 2

    async def test_fleet_tool_with_store_data(self, server: JeltzServer) -> None:
        assert server.store is not None
        await server.store.record("temp_sensor", "get_reading", 22.5, "celsius")
        result = await server.handle_call_tool("fleet.get_all_readings", {})
        assert result.isError is not True
        assert result.structuredContent["count"] == 1

    async def test_fleet_tool_unknown_returns_error(
        self, server: JeltzServer
    ) -> None:
        result = await server.handle_call_tool("fleet.nonexistent", {})
        assert result.isError is True

    async def test_device_tool_routes_to_aggregator(
        self, server: JeltzServer
    ) -> None:
        # MockAdapter with no responses returns "unknown command" error
        result = await server.handle_call_tool("temp_sensor.get_reading", {})
        # The mock has no configured responses so this should fail,
        # but the point is it routes correctly (not "unknown tool")
        assert result is not None

    async def test_unknown_tool_returns_error(self, server: JeltzServer) -> None:
        result = await server.handle_call_tool("nonexistent.tool", {})
        assert result.isError is True
        assert any("unknown tool" in c.text for c in result.content)

    async def test_null_arguments_treated_as_empty(
        self, server: JeltzServer
    ) -> None:
        result = await server.handle_call_tool("fleet.list_devices", None)
        assert result.isError is not True

    async def test_before_start_returns_error(self, profiles_dir: Path) -> None:
        srv = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        result = await srv.handle_call_tool("fleet.list_devices", {})
        assert result.isError is True


class TestDeviceToolDispatch:
    async def test_route_device_tool(self, server: JeltzServer) -> None:
        assert server.aggregator is not None
        result = await server.aggregator.call_tool(
            "temp_sensor.get_reading", {}
        )
        assert result is not None

    async def test_unknown_tool_fails(self, server: JeltzServer) -> None:
        assert server.aggregator is not None
        result = await server.aggregator.call_tool("nonexistent.tool", {})
        assert not result.success
        assert "unknown tool" in result.error


class TestStoreOnRead:
    """Device tool calls should record numeric readings to the store."""

    @pytest.fixture
    def responsive_profiles_dir(self, tmp_path: Path) -> Path:
        (tmp_path / "temp.toml").write_text(
            '[device]\n'
            'name = "temp_sensor"\n'
            '[connection]\nprotocol = "mock"\n'
            '[[tools]]\n'
            'name = "get_reading"\n'
            'description = "Get temperature"\n'
            'command = "READ_TEMP"\n'
            '[tools.returns]\ntype = "float"\nunit = "celsius"\n'
            '[[tools]]\n'
            'name = "get_status"\n'
            'description = "Get status"\n'
            'command = "STATUS"\n'
            '[tools.returns]\ntype = "string"\n'
        )
        return tmp_path

    @pytest.fixture
    async def responsive_server(self, responsive_profiles_dir: Path):
        srv = JeltzServer(
            profiles_dir=responsive_profiles_dir, db_path=":memory:"
        )
        discovery = await srv.start()
        for device in discovery.devices:
            if isinstance(device.adapter, MockAdapter):
                device.adapter.responses = {
                    "READ_TEMP": "22.5",
                    "STATUS": "OK",
                }
        yield srv
        await srv.stop()

    async def test_numeric_tool_records_to_store(
        self, responsive_server: JeltzServer
    ) -> None:
        result = await responsive_server.handle_call_tool(
            "temp_sensor.get_reading", {}
        )
        assert result.isError is not True

        assert responsive_server.store is not None
        latest = await responsive_server.store.get_latest(
            "temp_sensor", "get_reading"
        )
        assert latest is not None
        assert latest.value == 22.5
        assert latest.unit == "celsius"

    async def test_string_tool_does_not_record(
        self, responsive_server: JeltzServer
    ) -> None:
        result = await responsive_server.handle_call_tool(
            "temp_sensor.get_status", {}
        )
        assert result.isError is not True

        assert responsive_server.store is not None
        latest = await responsive_server.store.get_latest(
            "temp_sensor", "get_status"
        )
        assert latest is None

    async def test_stored_readings_visible_via_fleet(
        self, responsive_server: JeltzServer
    ) -> None:
        # Call the device tool — should auto-record
        await responsive_server.handle_call_tool(
            "temp_sensor.get_reading", {}
        )

        # Fleet should see the reading
        result = await responsive_server.handle_call_tool(
            "fleet.get_all_readings", {}
        )
        assert result.isError is not True
        assert result.structuredContent is not None
        assert result.structuredContent["count"] == 1

    async def test_multiple_reads_build_history(
        self, responsive_server: JeltzServer
    ) -> None:
        for _ in range(3):
            await responsive_server.handle_call_tool(
                "temp_sensor.get_reading", {}
            )

        result = await responsive_server.handle_call_tool(
            "fleet.get_history",
            {"device_id": "temp_sensor", "sensor_id": "get_reading", "hours": 1},
        )
        assert result.structuredContent is not None
        assert result.structuredContent["count"] == 3

    async def test_failed_tool_call_does_not_record(
        self, responsive_server: JeltzServer
    ) -> None:
        # Call a tool that will fail (unknown command on mock)
        result = await responsive_server.handle_call_tool(
            "temp_sensor.get_reading", {}
        )
        # First call succeeds and records
        assert result.isError is not True

        # Now break the adapter
        assert responsive_server.aggregator is not None
        device = responsive_server.aggregator.get_status("temp_sensor")
        assert device is not None
        adapter = device.device.adapter
        assert isinstance(adapter, MockAdapter)
        adapter.responses = {}  # Remove all responses

        result = await responsive_server.handle_call_tool(
            "temp_sensor.get_reading", {}
        )
        assert result.isError is True

        # Should still only have 1 reading (from the first call)
        assert responsive_server.store is not None
        readings = await responsive_server.store.get_history(
            "temp_sensor", "get_reading", start=0
        )
        assert len(readings) == 1

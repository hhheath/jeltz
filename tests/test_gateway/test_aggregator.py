"""Tests for the gateway aggregator — unified tool catalog and routing."""

from __future__ import annotations

import pytest

from jeltz.adapters.mock import MockAdapter
from jeltz.devices.model import (
    ConnectionConfig,
    DeviceConfig,
    DeviceModel,
    ToolDefinition,
    ToolParam,
    ToolReturns,
)
from jeltz.gateway.aggregator import Aggregator
from jeltz.gateway.discovery import DiscoveredDevice


def _mock_device(
    name: str,
    tools: list[ToolDefinition],
    responses: dict | None = None,
    healthy: bool = True,
) -> DiscoveredDevice:
    model = DeviceModel(
        device=DeviceConfig(name=name, description=f"Mock {name}"),
        connection=ConnectionConfig(protocol="mock"),
        tools=tools,
    )
    adapter = MockAdapter(model.connection, responses=responses, healthy=healthy)
    return DiscoveredDevice(model, adapter)


def _temp_device(responses: dict | None = None) -> DiscoveredDevice:
    return _mock_device(
        name="temp_sensor",
        tools=[
            ToolDefinition(
                name="get_reading",
                description="Get temperature",
                command="READ_TEMP",
                returns=ToolReturns(type="float", unit="celsius"),
            ),
            ToolDefinition(
                name="get_all",
                description="Get all readings",
                command="READ_ALL",
                returns=ToolReturns(type="array", unit="celsius"),
            ),
        ],
        responses=responses or {"READ_TEMP": 22.5, "READ_ALL": [22.5, 23.1, 21.8]},
    )


def _pressure_device(responses: dict | None = None) -> DiscoveredDevice:
    return _mock_device(
        name="pressure_sensor",
        tools=[
            ToolDefinition(
                name="get_reading",
                description="Get pressure for a channel",
                command="READ_PSI {channel}",
                params={"channel": ToolParam(type="int", min=0, max=3)},
                returns=ToolReturns(type="float", unit="psi"),
            ),
        ],
        responses=responses or {"READ_PSI 2": 14.7},
    )


class TestToolCatalog:
    def test_generates_namespaced_tools(self) -> None:
        agg = Aggregator([_temp_device()])
        names = [t.name for t in agg.tools]
        assert names == ["temp_sensor.get_reading", "temp_sensor.get_all"]

    def test_merges_multiple_devices(self) -> None:
        agg = Aggregator([_temp_device(), _pressure_device()])
        names = [t.name for t in agg.tools]
        assert "temp_sensor.get_reading" in names
        assert "temp_sensor.get_all" in names
        assert "pressure_sensor.get_reading" in names
        assert len(names) == 3

    def test_empty_devices_produces_empty_catalog(self) -> None:
        agg = Aggregator([])
        assert agg.tools == []

    def test_device_names(self) -> None:
        agg = Aggregator([_temp_device(), _pressure_device()])
        assert sorted(agg.device_names) == ["pressure_sensor", "temp_sensor"]

    def test_tools_returns_copy(self) -> None:
        agg = Aggregator([_temp_device()])
        tools1 = agg.tools
        tools2 = agg.tools
        assert tools1 is not tools2

    def test_route_carries_params(self) -> None:
        agg = Aggregator([_pressure_device()])
        route = agg.get_route("pressure_sensor.get_reading")
        assert route is not None
        assert route.params is not None
        assert "channel" in route.params
        assert route.params["channel"].type == "int"

    def test_route_params_none_when_no_params(self) -> None:
        agg = Aggregator([_temp_device()])
        route = agg.get_route("temp_sensor.get_reading")
        assert route is not None
        assert route.params is None  # no params defined

    def test_duplicate_device_names_raises(self) -> None:
        d1 = _mock_device("sensor", tools=[])
        d2 = _mock_device("sensor", tools=[])
        with pytest.raises(ValueError, match="duplicate device name"):
            Aggregator([d1, d2])


class TestConnectDisconnect:
    async def test_connect_all(self) -> None:
        agg = Aggregator([_temp_device(), _pressure_device()])
        results = await agg.connect_all()
        assert all(r.success for r in results.values())
        for name in agg.device_names:
            status = agg.get_status(name)
            assert status is not None
            assert status.connected

    async def test_disconnect_all(self) -> None:
        agg = Aggregator([_temp_device()])
        await agg.connect_all()
        results = await agg.disconnect_all()
        assert all(r.success for r in results.values())
        status = agg.get_status("temp_sensor")
        assert status is not None
        assert not status.connected

    async def test_connect_does_not_set_healthy(self) -> None:
        """Healthy should only be set by health_check, not by connect."""
        agg = Aggregator([_temp_device()])
        await agg.connect_all()
        status = agg.get_status("temp_sensor")
        assert status.connected
        assert not status.healthy


class TestToolRouting:
    async def test_routes_tool_call(self) -> None:
        agg = Aggregator([_temp_device()])
        await agg.connect_all()
        result = await agg.call_tool("temp_sensor.get_reading", {})
        assert result.success
        assert result.data == 22.5

    async def test_routes_to_correct_device(self) -> None:
        agg = Aggregator([
            _temp_device(),
            _pressure_device(responses={"READ_PSI 2": 14.7}),
        ])
        await agg.connect_all()

        temp = await agg.call_tool("temp_sensor.get_reading", {})
        assert temp.success
        assert temp.data == 22.5

        pressure = await agg.call_tool(
            "pressure_sensor.get_reading", {"channel": 2}
        )
        assert pressure.success
        assert pressure.data == 14.7

    async def test_arguments_interpolated_into_command(self) -> None:
        """Arguments should be interpolated into the command template."""
        device = _mock_device(
            name="sensor",
            tools=[
                ToolDefinition(
                    name="read",
                    description="Read channel",
                    command="READ {channel} {mode}",
                    params={
                        "channel": ToolParam(type="int"),
                        "mode": ToolParam(type="str", default="raw"),
                    },
                ),
            ],
            responses={"READ 3 fast": 42.0},
        )
        agg = Aggregator([device])
        await agg.connect_all()
        result = await agg.call_tool("sensor.read", {"channel": 3, "mode": "fast"})
        assert result.success
        assert result.data == 42.0

    async def test_unknown_tool_fails(self) -> None:
        agg = Aggregator([_temp_device()])
        await agg.connect_all()
        result = await agg.call_tool("nonexistent.tool", {})
        assert not result.success
        assert "unknown tool" in result.error

    async def test_disconnected_device_fails(self) -> None:
        agg = Aggregator([_temp_device()])
        result = await agg.call_tool("temp_sensor.get_reading", {})
        assert not result.success
        assert "not connected" in result.error

    async def test_tool_without_command_fails(self) -> None:
        device = _mock_device(
            name="custom",
            tools=[ToolDefinition(name="process", description="Custom handler tool")],
        )
        agg = Aggregator([device])
        await agg.connect_all()
        result = await agg.call_tool("custom.process", {})
        assert not result.success
        assert "handler dispatch" in result.error


class TestHealthChecks:
    async def test_health_check_all_healthy(self) -> None:
        agg = Aggregator([_temp_device(), _pressure_device()])
        await agg.connect_all()
        results = await agg.health_check_all()
        assert all(r.success for r in results.values())
        for name in agg.device_names:
            status = agg.get_status(name)
            assert status.healthy

    async def test_health_check_unhealthy_device(self) -> None:
        unhealthy = _mock_device(
            name="bad_sensor",
            tools=[ToolDefinition(name="read", description="Read", command="READ")],
            healthy=False,
        )
        agg = Aggregator([_temp_device(), unhealthy])
        await agg.connect_all()
        results = await agg.health_check_all()

        assert results["temp_sensor"].success
        assert not results["bad_sensor"].success

        assert agg.get_status("temp_sensor").healthy
        assert not agg.get_status("bad_sensor").healthy

    async def test_health_check_skips_disconnected(self) -> None:
        agg = Aggregator([_temp_device()])
        results = await agg.health_check_all()
        assert not results["temp_sensor"].success
        assert "not connected" in results["temp_sensor"].error

    async def test_health_check_clears_error_on_recovery(self) -> None:
        """After an unhealthy check, a subsequent healthy check should clear the error."""
        device = _mock_device(
            name="sensor",
            tools=[ToolDefinition(name="read", description="Read", command="READ")],
            healthy=False,
        )
        agg = Aggregator([device])
        await agg.connect_all()

        # First check — unhealthy
        await agg.health_check_all()
        status = agg.get_status("sensor")
        assert not status.healthy
        assert status.error is not None

        # Recover
        device.adapter.healthy = True  # type: ignore[attr-defined]
        await agg.health_check_all()
        status = agg.get_status("sensor")
        assert status.healthy
        assert status.error is None


class TestDeviceStatus:
    async def test_status_after_connect(self) -> None:
        agg = Aggregator([_temp_device()])
        await agg.connect_all()
        status = agg.get_status("temp_sensor")
        assert status.connected
        assert status.error is None

    async def test_status_unknown_device(self) -> None:
        agg = Aggregator([_temp_device()])
        assert agg.get_status("nonexistent") is None

    async def test_all_statuses(self) -> None:
        agg = Aggregator([_temp_device(), _pressure_device()])
        statuses = agg.all_statuses()
        assert len(statuses) == 2
        assert "temp_sensor" in statuses
        assert "pressure_sensor" in statuses

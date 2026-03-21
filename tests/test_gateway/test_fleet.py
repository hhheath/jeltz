"""Tests for fleet-level tools — cross-device queries."""

from __future__ import annotations

import time

import pytest

from jeltz.adapters.mock import MockAdapter
from jeltz.devices.model import (
    ConnectionConfig,
    DeviceConfig,
    DeviceModel,
    ToolDefinition,
    ToolReturns,
)
from jeltz.gateway.aggregator import Aggregator
from jeltz.gateway.discovery import DiscoveredDevice
from jeltz.gateway.fleet import FLEET_TOOLS, FleetTools
from jeltz.storage.store import ReadingStore


def _mock_device(
    name: str,
    tools: list[ToolDefinition] | None = None,
    healthy: bool = True,
) -> DiscoveredDevice:
    model = DeviceModel(
        device=DeviceConfig(name=name, description=f"Mock {name}"),
        connection=ConnectionConfig(protocol="mock"),
        tools=tools or [],
    )
    adapter = MockAdapter(model.connection, healthy=healthy)
    return DiscoveredDevice(model, adapter)


@pytest.fixture
async def store():
    s = ReadingStore(db_path=":memory:")
    await s.init()
    yield s
    await s.close()


@pytest.fixture
def aggregator() -> Aggregator:
    return Aggregator([
        _mock_device("temp_sensor", tools=[
            ToolDefinition(
                name="get_reading",
                description="Get temperature",
                command="READ",
                returns=ToolReturns(type="float", unit="celsius"),
            ),
        ]),
        _mock_device("pressure_sensor", tools=[
            ToolDefinition(
                name="get_reading",
                description="Get pressure",
                command="READ",
                returns=ToolReturns(type="float", unit="psi"),
            ),
        ]),
    ])


@pytest.fixture
def fleet(aggregator: Aggregator, store: ReadingStore) -> FleetTools:
    return FleetTools(aggregator, store)


class TestFleetToolSchemas:
    def test_four_fleet_tools_defined(self) -> None:
        assert len(FLEET_TOOLS) == 4

    def test_tool_names(self) -> None:
        names = {t.name for t in FLEET_TOOLS}
        assert names == {
            "fleet.list_devices",
            "fleet.get_all_readings",
            "fleet.get_history",
            "fleet.search_anomalies",
        }

    def test_get_history_requires_device_and_sensor(self) -> None:
        history_tool = next(t for t in FLEET_TOOLS if t.name == "fleet.get_history")
        assert history_tool.inputSchema["required"] == ["device_id", "sensor_id"]

    def test_fleet_tools_property_returns_copy(self, fleet: FleetTools) -> None:
        t1 = fleet.tools
        t2 = fleet.tools
        assert t1 is not t2


class TestListDevices:
    async def test_lists_all_devices(self, fleet: FleetTools) -> None:
        result = await fleet.call("fleet.list_devices", {})
        assert result["count"] == 2
        names = {d["name"] for d in result["devices"]}
        assert names == {"temp_sensor", "pressure_sensor"}

    async def test_includes_connection_status(
        self, fleet: FleetTools, aggregator: Aggregator
    ) -> None:
        await aggregator.connect_all()
        result = await fleet.call("fleet.list_devices", {})
        for device in result["devices"]:
            assert device["connected"] is True

    async def test_includes_namespaced_device_tools(self, fleet: FleetTools) -> None:
        result = await fleet.call("fleet.list_devices", {})
        temp = next(d for d in result["devices"] if d["name"] == "temp_sensor")
        assert "temp_sensor.get_reading" in temp["tools"]

    async def test_includes_protocol(self, fleet: FleetTools) -> None:
        result = await fleet.call("fleet.list_devices", {})
        for device in result["devices"]:
            assert device["protocol"] == "mock"

    async def test_shows_unhealthy_status(self, store: ReadingStore) -> None:
        unhealthy = _mock_device("bad", healthy=False)
        agg = Aggregator([unhealthy])
        await agg.connect_all()
        await agg.health_check_all()
        fleet = FleetTools(agg, store)
        result = await fleet.call("fleet.list_devices", {})
        assert result["devices"][0]["healthy"] is False
        assert result["devices"][0]["error"] is not None


class TestGetAllReadings:
    async def test_returns_all_latest(
        self, fleet: FleetTools, store: ReadingStore
    ) -> None:
        await store.record("temp_sensor", "get_reading", 22.5, "celsius")
        await store.record("pressure_sensor", "get_reading", 14.7, "psi")
        result = await fleet.call("fleet.get_all_readings", {})
        assert result["count"] == 2

    async def test_empty_store(self, fleet: FleetTools) -> None:
        result = await fleet.call("fleet.get_all_readings", {})
        assert result["count"] == 0
        assert result["readings"] == []

    async def test_returns_latest_not_all(
        self, fleet: FleetTools, store: ReadingStore
    ) -> None:
        await store.record("temp_sensor", "get_reading", 20.0, "celsius")
        await store.record("temp_sensor", "get_reading", 22.5, "celsius")
        result = await fleet.call("fleet.get_all_readings", {})
        assert result["count"] == 1
        assert result["readings"][0]["value"] == 22.5


class TestGetHistory:
    async def test_returns_history(
        self, fleet: FleetTools, store: ReadingStore
    ) -> None:
        now = time.time()
        for i in range(5):
            await store.record(
                "temp_sensor", "get_reading", 20.0 + i, "celsius",
                timestamp=now - (i * 60),
            )
        result = await fleet.call("fleet.get_history", {
            "device_id": "temp_sensor",
            "sensor_id": "get_reading",
        })
        assert result["count"] == 5
        assert result["device_id"] == "temp_sensor"

    async def test_respects_hours_param(
        self, fleet: FleetTools, store: ReadingStore
    ) -> None:
        now = time.time()
        # One reading now, one from 48 hours ago
        await store.record(
            "temp_sensor", "get_reading", 22.5, "celsius", timestamp=now,
        )
        await store.record(
            "temp_sensor", "get_reading", 20.0, "celsius",
            timestamp=now - (48 * 3600),
        )
        result = await fleet.call("fleet.get_history", {
            "device_id": "temp_sensor",
            "sensor_id": "get_reading",
            "hours": 1,
        })
        assert result["count"] == 1

    async def test_respects_limit_param(
        self, fleet: FleetTools, store: ReadingStore
    ) -> None:
        now = time.time()
        for i in range(10):
            await store.record(
                "temp_sensor", "get_reading", 20.0 + i, "celsius",
                timestamp=now - (i * 60),
            )
        result = await fleet.call("fleet.get_history", {
            "device_id": "temp_sensor",
            "sensor_id": "get_reading",
            "limit": 3,
        })
        assert result["count"] == 3

    async def test_empty_history(self, fleet: FleetTools) -> None:
        result = await fleet.call("fleet.get_history", {
            "device_id": "temp_sensor",
            "sensor_id": "get_reading",
        })
        assert result["count"] == 0


class TestSearchAnomalies:
    async def test_finds_anomaly(
        self, fleet: FleetTools, store: ReadingStore
    ) -> None:
        now = time.time()
        # Build a baseline of stable readings
        for i in range(20):
            await store.record(
                "temp_sensor", "get_reading", 22.0, "celsius",
                timestamp=now - ((i + 1) * 3600),
            )
        # Add a spike
        await store.record("temp_sensor", "get_reading", 50.0, "celsius", timestamp=now)

        result = await fleet.call("fleet.search_anomalies", {})
        assert result["count"] >= 1
        anomaly = result["anomalies"][0]
        assert anomaly["device_id"] == "temp_sensor"
        assert anomaly["current_value"] == 50.0

    async def test_no_anomalies_when_stable(
        self, fleet: FleetTools, store: ReadingStore
    ) -> None:
        now = time.time()
        for i in range(20):
            await store.record(
                "temp_sensor", "get_reading", 22.0, "celsius",
                timestamp=now - (i * 3600),
            )
        result = await fleet.call("fleet.search_anomalies", {})
        assert result["count"] == 0

    async def test_custom_threshold(
        self, fleet: FleetTools, store: ReadingStore
    ) -> None:
        now = time.time()
        # Readings with some variance
        for i in range(20):
            await store.record(
                "temp_sensor", "get_reading", 22.0 + (i % 3), "celsius",
                timestamp=now - ((i + 1) * 3600),
            )
        # Moderate deviation — might be flagged at 1σ but not at 5σ
        await store.record("temp_sensor", "get_reading", 26.0, "celsius", timestamp=now)

        strict = await fleet.call("fleet.search_anomalies", {"threshold_sigma": 5.0})
        assert strict["count"] == 0

    async def test_empty_store(self, fleet: FleetTools) -> None:
        result = await fleet.call("fleet.search_anomalies", {})
        assert result["count"] == 0


class TestUnknownTool:
    async def test_unknown_fleet_tool_raises(self, fleet: FleetTools) -> None:
        with pytest.raises(ValueError, match="unknown fleet tool"):
            await fleet.call("fleet.nonexistent", {})

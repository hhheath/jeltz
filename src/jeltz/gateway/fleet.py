"""Fleet-level tools — cross-device queries that make Jeltz valuable.

These tools give an LLM the full picture in a single call rather than
requiring sequential per-device tool calls. They bridge the aggregator
(live device access) and the store (historical data).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import Tool

from jeltz.gateway.aggregator import Aggregator
from jeltz.storage.store import ReadingStore

# The four fleet tools, defined as MCP Tool schemas
FLEET_TOOLS: list[Tool] = [
    Tool(
        name="fleet.list_devices",
        description=(
            "List all connected devices with their health status. "
            "Use this to understand what hardware is available before "
            "querying specific devices."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="fleet.get_all_readings",
        description=(
            "Get the most recent stored reading from every sensor across "
            "every device in one call. Reads from the gateway's time-series "
            "store, not live devices. Returns the full system snapshot — "
            "use this to spot anomalies, compare across devices, or answer "
            "'anything weird?'"
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="fleet.get_history",
        description=(
            "Get time-series history for a specific sensor. Use this for "
            "trend analysis, drift detection, and baseline comparison. "
            "Returns newest readings first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": "Device name (from fleet.list_devices)",
                },
                "sensor_id": {
                    "type": "string",
                    "description": "Sensor/tool name on the device",
                },
                "hours": {
                    "type": "number",
                    "description": "How many hours of history to return (default: 24)",
                    "default": 24,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of readings to return (default: 500)",
                    "default": 500,
                },
            },
            "required": ["device_id", "sensor_id"],
        },
    ),
    Tool(
        name="fleet.search_anomalies",
        description=(
            "Find sensors whose latest reading deviates significantly from "
            "their rolling baseline (mean ± standard deviations over the "
            "baseline window). Simple statistical detection — good for "
            "catching stuck sensors, drift, and sudden shifts. Not ML-based."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "threshold_sigma": {
                    "type": "number",
                    "description": (
                        "How many standard deviations from the mean to flag "
                        "as anomalous (default: 2.0)"
                    ),
                    "default": 2.0,
                },
                "baseline_days": {
                    "type": "integer",
                    "description": (
                        "Number of days of history to compute the baseline from "
                        "(default: 30)"
                    ),
                    "default": 30,
                },
            },
        },
    ),
]

_HandlerFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class FleetTools:
    """Implements the fleet-level tool handlers."""

    def __init__(self, aggregator: Aggregator, store: ReadingStore) -> None:
        self._aggregator = aggregator
        self._store = store
        self._handlers: dict[str, _HandlerFn] = {
            "fleet.list_devices": self._list_devices,
            "fleet.get_all_readings": self._get_all_readings,
            "fleet.get_history": self._get_history,
            "fleet.search_anomalies": self._search_anomalies,
        }

    @property
    def tools(self) -> list[Tool]:
        return list(FLEET_TOOLS)

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a fleet tool call. Returns a JSON-serializable result dict."""
        handler = self._handlers.get(tool_name)
        if handler is None:
            raise ValueError(f"unknown fleet tool: {tool_name}")
        return await handler(arguments)

    async def _list_devices(self, arguments: dict[str, Any]) -> dict[str, Any]:
        statuses = self._aggregator.all_statuses()
        devices = []
        for name, status in statuses.items():
            devices.append({
                "name": name,
                "description": status.device.model.device.description,
                "protocol": status.device.model.connection.protocol,
                "connected": status.connected,
                "healthy": status.healthy,
                "error": status.error,
                "tools": [f"{name}.{t.name}" for t in status.device.model.tools],
            })
        return {"devices": devices, "count": len(devices)}

    async def _get_all_readings(self, arguments: dict[str, Any]) -> dict[str, Any]:
        readings = await self._store.get_all_latest()
        return {
            "readings": [r.model_dump() for r in readings],
            "count": len(readings),
        }

    async def _get_history(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device_id: str = arguments["device_id"]
        sensor_id: str = arguments["sensor_id"]
        hours: float = arguments.get("hours", 24)
        limit: int = arguments.get("limit", 500)

        start = time.time() - (hours * 3600)

        readings = await self._store.get_history(
            device_id=device_id,
            sensor_id=sensor_id,
            start=start,
            limit=limit,
        )
        return {
            "device_id": device_id,
            "sensor_id": sensor_id,
            "readings": [r.model_dump() for r in readings],
            "count": len(readings),
            "hours_requested": hours,
        }

    async def _search_anomalies(self, arguments: dict[str, Any]) -> dict[str, Any]:
        threshold: float = arguments.get("threshold_sigma", 2.0)
        baseline_days: int = arguments.get("baseline_days", 30)

        anomalies = await self._store.search_anomalies(
            threshold_sigma=threshold,
            baseline_days=baseline_days,
        )
        return {
            "anomalies": [a.model_dump() for a in anomalies],
            "count": len(anomalies),
            "threshold_sigma": threshold,
            "baseline_days": baseline_days,
        }

"""Gateway aggregator — merges device tools into a unified MCP tool catalog."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from mcp.types import Tool

from jeltz.adapters.base import AdapterResult
from jeltz.devices.model import ToolParam, ToolReturns
from jeltz.gateway.discovery import DiscoveredDevice
from jeltz.profiles.generator import generate_tools


@dataclass
class DeviceStatus:
    """Tracks a device's connection state and health."""

    device: DiscoveredDevice
    connected: bool = False
    healthy: bool = False
    error: str | None = None


@dataclass
class ToolRoute:
    """Maps a namespaced tool name back to its device and tool definition."""

    device: DiscoveredDevice
    tool_name: str  # original un-namespaced name
    command: str | None  # the command string to send over the adapter
    returns: ToolReturns | None = None  # return type spec for storage
    params: dict[str, ToolParam] | None = None  # parameter definitions


class Aggregator:
    """Merges multiple devices into a single unified tool catalog.

    Handles:
    - Generating namespaced MCP tools from all discovered devices
    - Routing tool calls to the correct adapter
    - Connecting/disconnecting all devices
    - Per-device locking for safe concurrent tool calls
    """

    def __init__(self, devices: list[DiscoveredDevice]) -> None:
        # Check for duplicate device names
        seen: set[str] = set()
        for d in devices:
            if d.name in seen:
                raise ValueError(f"duplicate device name: {d.name!r}")
            seen.add(d.name)

        self._devices = {d.name: d for d in devices}
        self._tools: list[Tool] = []
        self._routes: dict[str, ToolRoute] = {}
        self._status: dict[str, DeviceStatus] = {
            d.name: DeviceStatus(device=d) for d in devices
        }
        self._device_locks: dict[str, asyncio.Lock] = {
            d.name: asyncio.Lock() for d in devices
        }
        self._build_catalog()

    def _build_catalog(self) -> None:
        """Generate the unified tool catalog from all devices."""
        self._tools = []
        self._routes = {}

        for device in self._devices.values():
            tools = generate_tools(device.model)
            self._tools.extend(tools)

            # Build routing table from the generated Tool names
            # to avoid duplicating the namespacing logic
            for mcp_tool, tool_def in zip(tools, device.model.tools):
                self._routes[mcp_tool.name] = ToolRoute(
                    device=device,
                    tool_name=tool_def.name,
                    command=tool_def.command,
                    returns=tool_def.returns,
                    params=tool_def.params if tool_def.params else None,
                )

    @property
    def tools(self) -> list[Tool]:
        """The full unified tool catalog."""
        return list(self._tools)

    @property
    def device_names(self) -> list[str]:
        return list(self._devices.keys())

    @property
    def route_names(self) -> list[str]:
        """All namespaced tool names in the routing table."""
        return list(self._routes.keys())

    def get_route(self, namespaced_name: str) -> ToolRoute | None:
        return self._routes.get(namespaced_name)

    def get_status(self, device_name: str) -> DeviceStatus | None:
        return self._status.get(device_name)

    def all_statuses(self) -> dict[str, DeviceStatus]:
        return dict(self._status)

    async def connect_all(self) -> dict[str, AdapterResult]:
        """Connect all device adapters concurrently."""
        results: dict[str, AdapterResult] = {}

        async def _connect(name: str, device: DiscoveredDevice) -> None:
            try:
                result = await device.adapter.connect()
            except Exception as e:
                result = AdapterResult.fail(str(e))
            results[name] = result
            status = self._status[name]
            if result.success:
                status.connected = True
                status.error = None
            else:
                status.connected = False
                status.error = result.error

        await asyncio.gather(
            *[_connect(name, d) for name, d in self._devices.items()]
        )
        return results

    async def disconnect_all(self) -> dict[str, AdapterResult]:
        """Disconnect all device adapters concurrently."""
        results: dict[str, AdapterResult] = {}

        async def _disconnect(name: str, device: DiscoveredDevice) -> None:
            try:
                result = await device.adapter.disconnect()
            except Exception as e:
                result = AdapterResult.fail(str(e))
            results[name] = result
            status = self._status[name]
            if result.success:
                status.connected = False
                status.healthy = False
            else:
                status.error = result.error

        await asyncio.gather(
            *[_disconnect(name, d) for name, d in self._devices.items()]
        )
        return results

    async def call_tool(
        self, namespaced_name: str, arguments: dict[str, Any]
    ) -> AdapterResult:
        """Route a tool call to the correct device adapter.

        Acquires a per-device lock to prevent interleaved send/receive on
        the same adapter from concurrent calls.
        """
        route = self._routes.get(namespaced_name)
        if route is None:
            return AdapterResult.fail(f"unknown tool: {namespaced_name}")

        device = route.device
        status = self._status[device.name]

        if not status.connected:
            return AdapterResult.fail(f"device {device.name!r} is not connected")

        async with self._device_locks[device.name]:
            # If the tool has a command, interpolate arguments and send
            if route.command is not None:
                command = route.command.format(**arguments) if arguments else route.command
                send_result = await device.adapter.send(command)
                if not send_result.success:
                    return send_result
                return await device.adapter.receive()

            # Tools without a command (handler-based) — not yet implemented
            return AdapterResult.fail(
                f"tool {namespaced_name!r} has no command"
                " and handler dispatch is not yet implemented"
            )

    async def health_check_all(self) -> dict[str, AdapterResult]:
        """Run health checks on all connected devices concurrently.

        Acquires the per-device lock to avoid interleaving health check
        commands with in-flight tool calls on the same serial port.
        """
        results: dict[str, AdapterResult] = {}

        async def _check(name: str, device: DiscoveredDevice) -> None:
            status = self._status[name]
            if not status.connected:
                results[name] = AdapterResult.fail("not connected")
                return
            async with self._device_locks[name]:
                try:
                    result = await device.adapter.health_check()
                except Exception as e:
                    result = AdapterResult.fail(str(e))
            results[name] = result
            status.healthy = result.success
            if result.success:
                status.error = None
            else:
                status.error = result.error

        await asyncio.gather(
            *[_check(name, d) for name, d in self._devices.items()]
        )
        return results

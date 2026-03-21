"""MCP server — the unified endpoint that wires everything together.

Connects discovery, aggregator, fleet tools, and storage behind a single
MCP server. Clients see one flat tool catalog: namespaced device tools
plus fleet-level tools.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from jeltz.gateway.aggregator import Aggregator
from jeltz.gateway.discovery import DiscoveryResult, discover_profiles
from jeltz.gateway.fleet import FleetTools
from jeltz.storage.store import ReadingStore

logger = logging.getLogger(__name__)

# Device names that would collide with fleet tool routing
_RESERVED_NAMES = {"fleet"}


class JeltzServer:
    """The Jeltz MCP gateway server.

    Wires together:
    - Discovery: scans profiles dir, instantiates adapters
    - Aggregator: unified tool catalog, per-device routing
    - Fleet tools: cross-device queries backed by the store
    - Store: SQLite time-series storage
    """

    def __init__(
        self,
        profiles_dir: Path,
        db_path: str | Path = "jeltz.db",
    ) -> None:
        self._profiles_dir = profiles_dir
        self._db_path = db_path
        self._aggregator: Aggregator | None = None
        self._fleet: FleetTools | None = None
        self._store: ReadingStore | None = None
        self._server = Server(name="jeltz", version="0.1.0")
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self._server.list_tools()
        async def list_tools() -> list[Tool]:
            return self.handle_list_tools()

        @self._server.call_tool()
        async def call_tool(
            name: str, arguments: dict[str, Any] | None
        ) -> CallToolResult:
            return await self.handle_call_tool(name, arguments)

    def handle_list_tools(self) -> list[Tool]:
        """Build the unified tool catalog from device + fleet tools."""
        tools: list[Tool] = []
        if self._aggregator:
            tools.extend(self._aggregator.tools)
        if self._fleet:
            tools.extend(self._fleet.tools)
        return tools

    async def handle_call_tool(
        self, name: str, arguments: dict[str, Any] | None
    ) -> CallToolResult:
        """Route a tool call to the correct handler."""
        args = arguments or {}

        # Fleet tools
        if name.startswith("fleet.") and self._fleet:
            try:
                result = await self._fleet.call(name, args)
                return CallToolResult(
                    content=[TextContent(type="text", text=str(result))],
                    structuredContent=result,
                )
            except Exception as e:
                logger.exception("fleet tool %s failed", name)
                return CallToolResult(
                    content=[TextContent(type="text", text=str(e))],
                    isError=True,
                )

        # Device tools
        if self._aggregator:
            result = await self._aggregator.call_tool(name, args)
            if result.success:
                data = {"data": result.data}
                return CallToolResult(
                    content=[TextContent(type="text", text=str(data))],
                    structuredContent=data,
                )
            else:
                return CallToolResult(
                    content=[TextContent(type="text", text=result.error or "unknown error")],
                    isError=True,
                )

        return CallToolResult(
            content=[TextContent(type="text", text=f"unknown tool: {name}")],
            isError=True,
        )

    async def start(self) -> DiscoveryResult:
        """Initialize store, discover devices, connect adapters.

        Returns the discovery result so callers can inspect errors.
        """
        if self._store is not None:
            raise RuntimeError("server already started — call stop() first")

        # Initialize storage
        self._store = ReadingStore(db_path=self._db_path)
        await self._store.init()

        # Discover devices
        discovery = discover_profiles(self._profiles_dir)
        for path, error in discovery.errors:
            logger.warning("skipped profile %s: %s", path, error)

        # Reject reserved device names
        for device in discovery.devices:
            if device.name in _RESERVED_NAMES:
                raise ValueError(
                    f"device name {device.name!r} is reserved"
                    f" (reserved names: {sorted(_RESERVED_NAMES)})"
                )

        # Build aggregator and connect
        self._aggregator = Aggregator(discovery.devices)
        results = await self._aggregator.connect_all()
        for name, result in results.items():
            if result.success:
                logger.info("connected: %s", name)
            else:
                logger.warning("failed to connect %s: %s", name, result.error)

        # Wire up fleet tools
        self._fleet = FleetTools(self._aggregator, self._store)

        return discovery

    async def stop(self) -> None:
        """Disconnect devices and close the store."""
        if self._aggregator:
            await self._aggregator.disconnect_all()
        if self._store:
            await self._store.close()
        self._aggregator = None
        self._fleet = None
        self._store = None

    async def run_stdio(self) -> None:
        """Run the server over stdio transport."""
        await self.start()
        try:
            async with stdio_server() as (read_stream, write_stream):
                await self._server.run(read_stream, write_stream)
        finally:
            await self.stop()

    @property
    def aggregator(self) -> Aggregator | None:
        return self._aggregator

    @property
    def fleet(self) -> FleetTools | None:
        return self._fleet

    @property
    def store(self) -> ReadingStore | None:
        return self._store

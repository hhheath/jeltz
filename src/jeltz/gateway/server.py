"""MCP server — the unified endpoint that wires everything together.

Connects discovery, aggregator, fleet tools, and storage behind a single
MCP server. Clients see one flat tool catalog: namespaced device tools
plus fleet-level tools.

Supports two modes:
- **stdio**: Ephemeral — one MCP client owns the process (``jeltz start``)
- **daemon**: Long-running — background recording + SSE endpoint (``jeltz daemon``)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from jeltz.gateway.aggregator import Aggregator
from jeltz.gateway.discovery import DiscoveryResult, discover_profiles
from jeltz.gateway.fleet import FleetTools
from jeltz.gateway.recorder import run_recorder
from jeltz.storage.retention import run_cleanup
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
                await self._maybe_record(name, result.data)
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

    # Types that can be stored as float in the time-series store
    _NUMERIC_TYPES = {"float", "number", "int", "integer"}

    async def _maybe_record(self, tool_name: str, raw_data: Any) -> None:
        """Record a sensor reading to the store if the tool returns a numeric type.

        Best-effort — failures are logged, never raised. The tool call
        has already succeeded; storage is a side effect.
        """
        if not self._store or not self._aggregator:
            return

        route = self._aggregator.get_route(tool_name)
        if not route or not route.returns:
            return

        if route.returns.type not in self._NUMERIC_TYPES:
            return

        try:
            value = float(raw_data)
        except (TypeError, ValueError):
            logger.debug(
                "skipping store for %s: cannot convert %r to float",
                tool_name, raw_data,
            )
            return

        unit = route.returns.unit or ""
        try:
            await self._store.record(
                device_id=route.device.name,
                sensor_id=route.tool_name,
                value=value,
                unit=unit,
            )
        except Exception:
            logger.warning("failed to record reading for %s", tool_name, exc_info=True)

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

    async def serve_stdio(self) -> None:
        """Serve MCP over stdio transport. Call start() first."""
        if self._store is None:
            raise RuntimeError("server not started — call start() first")
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(read_stream, write_stream)

    async def run_stdio(self) -> None:
        """Start the server and run over stdio transport."""
        try:
            await self.start()
            await self.serve_stdio()
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    # Daemon mode: background recording + SSE transport
    # ------------------------------------------------------------------

    async def _run_retention_loop(
        self, stop_event: asyncio.Event, interval_hours: float = 6.0
    ) -> None:
        """Run retention cleanup on startup and periodically."""
        if not self._store:
            return
        while not stop_event.is_set():
            try:
                counts = await run_cleanup(self._store)
                if counts["downsampled"] or counts["purged"]:
                    logger.info(
                        "retention: downsampled %d, purged %d",
                        counts["downsampled"], counts["purged"],
                    )
            except Exception:
                logger.warning("retention cleanup failed", exc_info=True)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=interval_hours * 3600
                )
                break
            except asyncio.TimeoutError:
                pass

    def _build_sse_app(self) -> tuple[Starlette, SseServerTransport]:
        """Build a Starlette ASGI app that serves MCP over SSE."""
        sse_transport = SseServerTransport("/messages/")
        mcp_server = self._server

        async def handle_sse(request: Request) -> Response:
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await mcp_server.run(
                    streams[0], streams[1], mcp_server.create_initialization_options()
                )
            return Response()

        app = Starlette(
            routes=[
                Route("/sse", endpoint=handle_sse, methods=["GET"]),
                Mount("/messages/", app=sse_transport.handle_post_message),
            ],
        )
        return app, sse_transport

    async def serve_sse(self, host: str = "127.0.0.1", port: int = 8374) -> None:
        """Serve MCP over SSE transport. Call start() first.

        Runs a Starlette/uvicorn HTTP server. Clients connect to GET /sse
        and post messages to /messages/.
        """
        import uvicorn

        if self._store is None:
            raise RuntimeError("server not started — call start() first")

        app, _ = self._build_sse_app()
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    async def run_daemon(
        self,
        host: str = "127.0.0.1",
        port: int = 8374,
    ) -> None:
        """Start the server in daemon mode: background recording + SSE endpoint.

        Runs until interrupted. Continuously polls sensors and records readings
        to the store. MCP clients connect/disconnect over SSE without affecting
        the recording loop.
        """
        import uvicorn

        stop_event = asyncio.Event()

        try:
            await self.start()
        except Exception:
            await self.stop()
            raise

        if not self._aggregator or not self._store:
            raise RuntimeError("server failed to initialize")

        app, _ = self._build_sse_app()
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        sse_server = uvicorn.Server(config)

        try:
            await asyncio.gather(
                run_recorder(self._aggregator, self._store, stop_event),
                self._run_retention_loop(stop_event),
                sse_server.serve(),
            )
        finally:
            stop_event.set()
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

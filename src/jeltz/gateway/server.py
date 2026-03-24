"""MCP server — the unified endpoint that wires everything together.

Connects discovery, aggregator, fleet tools, and storage behind a single
MCP server. Clients see one flat tool catalog: namespaced device tools
plus fleet-level tools.

Supports two modes:
- **stdio**: Ephemeral — one MCP client owns the process (``jeltz start``)
- **daemon**: Long-running — background recording + Streamable HTTP endpoint (``jeltz daemon``)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ServerCapabilities, TextContent, Tool

try:
    from mcp.server import InitializationOptions
except ImportError:
    InitializationOptions = None  # type: ignore[assignment,misc]

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
        self._daemon_active = False
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
                # Only record on client tool calls — the background
                # recorder handles periodic recording in daemon mode.
                if not self._daemon_active:
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
            if InitializationOptions is not None:
                init_options = InitializationOptions(
                    server_name="jeltz",
                    server_version="0.1.0",
                    capabilities=ServerCapabilities(tools={}),
                )
                await self._server.run(
                    read_stream, write_stream, init_options
                )
            else:
                await self._server.run(read_stream, write_stream)

    async def run_stdio(self) -> None:
        """Start the server and run over stdio transport."""
        try:
            await self.start()
            await self.serve_stdio()
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    # Daemon mode: background recording + Streamable HTTP transport
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
            except TimeoutError:
                pass

    def _build_http_app(self):  # type: ignore[no-untyped-def]
        """Build a Starlette ASGI app that serves MCP over Streamable HTTP.

        Imports starlette and the MCP StreamableHTTPSessionManager lazily
        so that stdio mode doesn't require these dependencies.
        """
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.types import Receive, Scope, Send

        session_manager = StreamableHTTPSessionManager(
            app=self._server,
            stateless=False,
        )

        class _MCPEndpoint:
            """ASGI app that delegates to the session manager."""

            def __init__(self, mgr: StreamableHTTPSessionManager) -> None:
                self._mgr = mgr

            async def __call__(
                self, scope: Scope, receive: Receive, send: Send
            ) -> None:
                await self._mgr.handle_request(scope, receive, send)

        async def lifespan(app: Starlette):  # type: ignore[no-untyped-def]
            async with session_manager.run():
                yield

        app = Starlette(
            routes=[
                Route(
                    "/mcp",
                    endpoint=_MCPEndpoint(session_manager),
                    methods=["GET", "POST", "DELETE"],
                ),
            ],
            lifespan=lifespan,
        )
        return app, session_manager

    async def serve_http(self, host: str = "127.0.0.1", port: int = 8374) -> None:
        """Serve MCP over Streamable HTTP transport. Call start() first.

        Runs a Starlette/uvicorn HTTP server. Clients interact via
        POST/GET/DELETE on /mcp (MCP Streamable HTTP spec).
        """
        import uvicorn

        if self._store is None:
            raise RuntimeError("server not started — call start() first")

        app, _ = self._build_http_app()
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    async def run_daemon_loops(
        self,
        host: str = "127.0.0.1",
        port: int = 8374,
    ) -> None:
        """Run daemon background tasks: recording, retention, and HTTP server.

        Call ``start()`` first. Installs signal handlers for SIGINT/SIGTERM
        to trigger graceful shutdown: sets stop_event so recorder and
        retention loops can finish their current cycle, then tells uvicorn
        to exit, which collapses the TaskGroup cleanly.
        """
        import signal

        import uvicorn

        if not self._aggregator or not self._store:
            raise RuntimeError("server not started — call start() first")

        stop_event = asyncio.Event()
        self._daemon_active = True

        app, _ = self._build_http_app()
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        http_server = uvicorn.Server(config)

        loop = asyncio.get_running_loop()

        def _request_shutdown() -> None:
            """Signal handler: set stop_event and tell uvicorn to exit.

            This lets recorder/retention finish their current poll cycle
            before TaskGroup teardown, instead of hard-cancelling them.
            """
            stop_event.set()
            http_server.should_exit = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_shutdown)

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(
                    run_recorder(self._aggregator, self._store, stop_event),
                    name="recorder",
                )
                tg.create_task(
                    self._run_retention_loop(stop_event),
                    name="retention",
                )
                tg.create_task(
                    http_server.serve(),
                    name="http",
                )
        finally:
            stop_event.set()
            self._daemon_active = False
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            await self.stop()

    async def run_daemon(
        self,
        host: str = "127.0.0.1",
        port: int = 8374,
    ) -> None:
        """Start the server in daemon mode: background recording + HTTP endpoint.

        Full lifecycle: calls ``start()``, runs daemon loops, then ``stop()``.
        """
        try:
            await self.start()
        except Exception:
            await self.stop()
            raise

        await self.run_daemon_loops(host=host, port=port)

    @property
    def aggregator(self) -> Aggregator | None:
        return self._aggregator

    @property
    def fleet(self) -> FleetTools | None:
        return self._fleet

    @property
    def store(self) -> ReadingStore | None:
        return self._store

"""Jeltz CLI."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import click

_LOG_LEVELS = {
    0: logging.WARNING,
    1: logging.INFO,
    2: logging.DEBUG,
}


@click.group()
@click.version_option()
@click.option(
    "-v", "--verbose",
    count=True,
    help="Increase log verbosity (-v for INFO, -vv for DEBUG).",
)
def main(verbose: int) -> None:
    """Jeltz — MCP gateway for physical devices."""
    level = _LOG_LEVELS.get(verbose, logging.DEBUG)
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )


@main.command()
@click.option(
    "--profiles-dir",
    "-p",
    type=click.Path(file_okay=False, path_type=Path),
    default="profiles",
    show_default=True,
    help="Directory containing TOML device profiles.",
)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default="jeltz.db",
    show_default=True,
    help="Path to SQLite database for time-series storage.",
)
def start(profiles_dir: Path, db_path: Path) -> None:
    """Start the Jeltz MCP gateway.

    Discovers devices from TOML profiles, connects adapters,
    and serves MCP tools over stdio.
    """
    from jeltz.gateway.server import JeltzServer

    if not profiles_dir.is_dir():
        click.echo(f"Profiles directory not found: {profiles_dir}", err=True)
        raise SystemExit(1)

    server = JeltzServer(profiles_dir=profiles_dir, db_path=db_path)

    async def _run() -> None:
        discovery = await server.start()
        try:
            for path, error in discovery.errors:
                click.echo(f"⚠ Skipped {path.name}: {error}", err=True)

            device_count = len(discovery.devices)
            tool_count = len(server.handle_list_tools())

            if device_count == 0:
                if discovery.errors:
                    click.echo(
                        "No devices loaded — all profiles failed to parse.",
                        err=True,
                    )
                else:
                    click.echo(
                        f"No devices found. Add TOML profiles to"
                        f" {profiles_dir.resolve()}",
                        err=True,
                    )
                raise SystemExit(1)

            click.echo(
                f"✓ Discovered {device_count} device(s), exposing {tool_count} tools",
                err=True,
            )

            if server.aggregator:
                for name in server.aggregator.device_names:
                    s = server.aggregator.get_status(name)
                    if s and s.connected:
                        click.echo(f"  ✓ {name}", err=True)
                    elif s:
                        click.echo(f"  ✗ {name}: {s.error}", err=True)

            click.echo("✓ MCP server ready on stdio", err=True)

            await server.serve_stdio()
        finally:
            await server.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\nShutting down.", err=True)


@main.command()
@click.option(
    "--profiles-dir",
    "-p",
    type=click.Path(file_okay=False, path_type=Path),
    default="profiles",
    show_default=True,
    help="Directory containing TOML device profiles.",
)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default="jeltz.db",
    show_default=True,
    help="Path to SQLite database for time-series storage.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Host to bind the HTTP server to.",
)
@click.option(
    "--port",
    default=8374,
    show_default=True,
    help="Port for the MCP HTTP server.",
)
def daemon(profiles_dir: Path, db_path: Path, host: str, port: int) -> None:
    """Run Jeltz as a long-running daemon.

    Continuously polls sensors and records readings to the store.
    Serves MCP over Streamable HTTP so clients can connect and disconnect
    without affecting the recording loop.
    """
    from jeltz.gateway.server import JeltzServer

    if not profiles_dir.is_dir():
        click.echo(f"Profiles directory not found: {profiles_dir}", err=True)
        raise SystemExit(1)

    server = JeltzServer(profiles_dir=profiles_dir, db_path=db_path)

    async def _run() -> None:
        discovery = await server.start()

        for path, error in discovery.errors:
            click.echo(f"⚠ Skipped {path.name}: {error}", err=True)

        device_count = len(discovery.devices)
        tool_count = len(server.handle_list_tools())

        if device_count == 0:
            if discovery.errors:
                click.echo(
                    "No devices loaded — all profiles failed to parse.",
                    err=True,
                )
            else:
                click.echo(
                    f"No devices found. Add TOML profiles to"
                    f" {profiles_dir.resolve()}",
                    err=True,
                )
            await server.stop()
            raise SystemExit(1)

        click.echo(
            f"✓ Discovered {device_count} device(s), exposing {tool_count} tools",
            err=True,
        )

        if server.aggregator:
            for name in server.aggregator.device_names:
                s = server.aggregator.get_status(name)
                if s and s.connected:
                    click.echo(f"  ✓ {name}", err=True)
                elif s:
                    click.echo(f"  ✗ {name}: {s.error}", err=True)

        click.echo("✓ Background recording active", err=True)
        click.echo(f"✓ MCP server ready on http://{host}:{port}/mcp", err=True)

        await server.run_daemon_loops(host=host, port=port)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\nShutting down.", err=True)


@main.command()
@click.option(
    "--profiles-dir",
    "-p",
    type=click.Path(file_okay=False, path_type=Path),
    default="profiles",
    show_default=True,
    help="Directory containing TOML device profiles.",
)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default="jeltz.db",
    show_default=True,
    help="Path to SQLite database for time-series storage.",
)
@click.option(
    "--api-url",
    default="http://localhost:11434/v1",
    show_default=True,
    help="OpenAI-compatible API endpoint (Ollama, llama.cpp, LM Studio, etc.).",
)
@click.option(
    "--model",
    "-m",
    default="llama3.2",
    show_default=True,
    help="Model name to use.",
)
@click.option(
    "--api-key",
    default="jeltz",
    envvar="JELTZ_API_KEY",
    help="API key (most local servers ignore this).",
)
@click.option(
    "--temperature",
    default=0.1,
    show_default=True,
    type=float,
    help="LLM temperature.",
)
@click.option(
    "--system-prompt",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to a custom system prompt file.",
)
@click.option(
    "--daemon-url",
    default=None,
    help="Connect to a running jeltz daemon instead of starting in-process (e.g. http://localhost:8374/mcp).",
)
def chat(
    profiles_dir: Path,
    db_path: Path,
    api_url: str,
    model: str,
    api_key: str,
    temperature: float,
    system_prompt: Path | None,
    daemon_url: str | None,
) -> None:
    """Chat with your sensor fleet via a local LLM.

    Connects to an OpenAI-compatible API (Ollama, llama.cpp, LM Studio, vLLM)
    and routes tool calls through the Jeltz gateway. By default starts a
    gateway in-process; use --daemon-url to connect to a running daemon.
    """
    try:
        from openai import OpenAI as _OpenAI  # noqa: F401
    except ImportError:
        click.echo(
            "jeltz chat requires the openai package.\n"
            "Install it with: pip install 'jeltz[chat]'",
            err=True,
        )
        raise SystemExit(1)

    from jeltz.chat.client import ChatClient, StreamChunkEvent
    from jeltz.chat.render import print_banner, print_error, render_event, render_stream_start
    from jeltz.gateway.server import JeltzServer

    custom_prompt: str | None = None
    if system_prompt is not None:
        custom_prompt = system_prompt.read_text()

    if daemon_url:
        # Daemon mode — connect to a running jeltz daemon
        client = ChatClient(
            daemon_url=daemon_url,
            api_url=api_url,
            model=model,
            api_key=api_key,
            temperature=temperature,
            system_prompt=custom_prompt,
        )
    else:
        # In-process mode — start a local JeltzServer
        if not profiles_dir.is_dir():
            click.echo(f"Profiles directory not found: {profiles_dir}", err=True)
            raise SystemExit(1)

        server = JeltzServer(profiles_dir=profiles_dir, db_path=db_path)
        client = ChatClient(
            server=server,
            api_url=api_url,
            model=model,
            api_key=api_key,
            temperature=temperature,
            system_prompt=custom_prompt,
        )

    async def _run() -> None:
        await client.initialize()
        try:
            tool_count = client.tool_count
            # In daemon mode, tool_count is our only indicator of device count
            device_tools = tool_count - 4  # subtract 4 fleet tools
            device_count = max(device_tools, 0)  # rough estimate
            if client.server and client.server.aggregator:
                device_count = len(client.server.aggregator.device_names)
            print_banner(device_count, tool_count, model, api_url)

            while True:
                try:
                    user_input = click.prompt("You", prompt_suffix=": ")
                except click.Abort:
                    break

                if not user_input.strip():
                    continue

                try:
                    streaming = False
                    async for event in client.send_message(user_input):
                        if isinstance(event, StreamChunkEvent) and not streaming:
                            render_stream_start()
                            streaming = True
                        render_event(event)
                except Exception as e:
                    print_error(str(e))
        finally:
            await client.shutdown()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("\nBye.", err=True)


@main.command()
@click.option(
    "--profiles-dir",
    "-p",
    type=click.Path(file_okay=False, path_type=Path),
    default="profiles",
    show_default=True,
    help="Directory containing TOML device profiles.",
)
def status(profiles_dir: Path) -> None:
    """Show discovered devices and their connection status."""
    from jeltz.gateway.aggregator import Aggregator
    from jeltz.gateway.discovery import discover_profiles

    if not profiles_dir.is_dir():
        click.echo(f"Profiles directory not found: {profiles_dir}", err=True)
        raise SystemExit(1)

    discovery = discover_profiles(profiles_dir)

    for path, error in discovery.errors:
        click.echo(f"⚠ Skipped {path.name}: {error}", err=True)

    if not discovery.devices:
        click.echo(
            f"No devices found. Add TOML profiles to {profiles_dir.resolve()}",
            err=True,
        )
        return

    async def _check() -> None:
        aggregator = Aggregator(discovery.devices)
        await aggregator.connect_all()
        try:
            await aggregator.health_check_all()

            click.echo(f"Devices ({len(discovery.devices)}):\n")
            for name in aggregator.device_names:
                ds = aggregator.get_status(name)
                if ds is None:
                    continue

                protocol = ds.device.model.connection.protocol

                if ds.connected and ds.healthy:
                    icon = "✓"
                    state = "healthy"
                elif ds.connected:
                    icon = "~"
                    state = "connected (unhealthy)"
                else:
                    icon = "✗"
                    state = f"disconnected: {ds.error or 'unknown'}"

                click.echo(f"  {icon} {name} [{protocol}] — {state}")

                tool_names = ", ".join(
                    f"{name}.{t.name}" for t in ds.device.model.tools
                )
                if tool_names:
                    click.echo(f"    tools: {tool_names}")
        finally:
            await aggregator.disconnect_all()

    asyncio.run(_check())


def _resolve_profile(device: str, profiles_dir: Path) -> Path:
    """Resolve a device argument to a profile path.

    Accepts a TOML file path or a device name to look up in the profiles directory.
    """
    from jeltz.profiles.parser import ProfileError, parse_profile

    path = Path(device)
    if path.is_file():
        return path

    if not profiles_dir.is_dir():
        click.echo(
            f"Could not find device {device!r}"
            f" — not a file path, and profiles dir not found: {profiles_dir}",
            err=True,
        )
        raise SystemExit(1)

    available: list[str] = []
    for candidate in sorted(profiles_dir.glob("**/*.toml")):
        try:
            model = parse_profile(candidate)
            if model.device.name == device:
                return candidate
            available.append(model.device.name)
        except ProfileError:
            continue

    click.echo(f"Device not found: {device!r}", err=True)
    if available:
        click.echo(f"Available devices: {', '.join(available)}", err=True)
    else:
        click.echo(f"No valid profiles in {profiles_dir}", err=True)
    raise SystemExit(1)


@main.command("test")
@click.argument("device")
@click.option(
    "--profiles-dir",
    "-p",
    type=click.Path(file_okay=False, path_type=Path),
    default="profiles",
    show_default=True,
    help="Directory containing TOML device profiles.",
)
def test_device(device: str, profiles_dir: Path) -> None:
    """Test a device connection.

    DEVICE is a TOML profile path or a device name in the profiles directory.
    Connects the adapter and runs a health check.
    """
    from jeltz.gateway.discovery import create_adapter
    from jeltz.profiles.parser import ProfileError, parse_profile

    profile_path = _resolve_profile(device, profiles_dir)

    try:
        model = parse_profile(profile_path)
    except ProfileError as e:
        click.echo(f"✗ Invalid profile: {e}", err=True)
        raise SystemExit(1)

    click.echo(f"Device:   {model.device.name}")
    click.echo(f"Protocol: {model.connection.protocol}")
    click.echo(f"Tools:    {len(model.tools)}")

    try:
        adapter = create_adapter(model)
    except ValueError as e:
        click.echo(f"✗ {e}", err=True)
        raise SystemExit(1)

    async def _test() -> None:
        result = await adapter.connect()
        if not result.success:
            click.echo(f"✗ Connect failed: {result.error}")
            raise SystemExit(1)
        click.echo("✓ Connected")

        try:
            if model.health:
                result = await adapter.health_check()
                if result.success:
                    click.echo("✓ Health check passed")
                else:
                    click.echo(f"✗ Health check failed: {result.error}")
            else:
                click.echo("~ No health check configured")
        finally:
            await adapter.disconnect()
            click.echo("✓ Disconnected")

    asyncio.run(_test())


@main.command("add-device")
@click.argument("profile", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--profiles-dir",
    "-p",
    type=click.Path(file_okay=False, path_type=Path),
    default="profiles",
    show_default=True,
    help="Directory to copy the profile into.",
)
def add_device(profile: Path, profiles_dir: Path) -> None:
    """Register a device by adding its TOML profile.

    Validates the profile and copies it to the profiles directory.
    """
    from jeltz.profiles.parser import ProfileError, parse_profile

    try:
        model = parse_profile(profile)
    except ProfileError as e:
        click.echo(f"✗ Invalid profile: {e}", err=True)
        raise SystemExit(1)

    profiles_dir.mkdir(parents=True, exist_ok=True)

    dest = profiles_dir / profile.name
    if dest.exists():
        click.confirm(f"{dest} already exists. Overwrite?", abort=True)

    shutil.copy2(profile, dest)
    click.echo(f"✓ Added {model.device.name} ({dest})")


_INIT_MOCK_PROFILE = """\
# Mock sensor — simulated device for testing without hardware.
# Customize the mock_responses to match your real device's protocol,
# then swap protocol = "mock" for protocol = "serial" or "mqtt".

[device]
name = "mock_sensor"
description = "Simulated temperature + humidity sensor"

[connection]
protocol = "mock"
timeout_ms = 1000

[connection.mock_responses]
READ_TEMP = "22.5"
READ_HUMID = "47.3"
PING = "PONG"

[[tools]]
name = "get_temperature"
description = "Get current temperature reading"
command = "READ_TEMP"

[tools.returns]
type = "float"
unit = "celsius"

[[tools]]
name = "get_humidity"
description = "Get current relative humidity reading"
command = "READ_HUMID"

[tools.returns]
type = "float"
unit = "percent"

[health]
check_command = "PING"
expected = "PONG"
interval_ms = 10000
"""


@main.command()
@click.argument("directory", default=".", type=click.Path(path_type=Path))
def init(directory: Path) -> None:
    """Scaffold a new Jeltz project.

    Creates a profiles/ directory with a mock sensor profile to get started.
    DIRECTORY defaults to the current directory.
    """
    profiles_dir = directory / "profiles"

    if profiles_dir.is_dir() and list(profiles_dir.glob("*.toml")):
        click.echo(
            f"profiles/ already exists with TOML files in {directory.resolve()}",
            err=True,
        )
        raise SystemExit(1)

    directory.mkdir(parents=True, exist_ok=True)
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / "mock_sensor.toml").write_text(_INIT_MOCK_PROFILE)

    click.echo(f"✓ Initialized Jeltz project in {directory.resolve()}")
    click.echo("  profiles/mock_sensor.toml — simulated sensor for testing")
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  jeltz test {profiles_dir / 'mock_sensor.toml'}")
    click.echo(f"  jeltz status -p {profiles_dir}")
    click.echo(f"  jeltz start -p {profiles_dir}")

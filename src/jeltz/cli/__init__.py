"""Jeltz CLI."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import click


@click.group()
@click.version_option()
def main() -> None:
    """Jeltz — MCP gateway for physical devices."""


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

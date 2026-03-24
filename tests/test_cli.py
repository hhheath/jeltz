"""Tests for the Jeltz CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from jeltz.cli import main

MOCK_PROFILE = """\
[device]
name = "test_sensor"
description = "Test sensor"

[connection]
protocol = "mock"

[[tools]]
name = "get_reading"
description = "Get test reading"
command = "READ"

[tools.returns]
type = "float"
unit = "celsius"

[health]
check_command = "PING"
expected = "PONG"
interval_ms = 10000
"""

MOCK_PROFILE_NO_HEALTH = """\
[device]
name = "bare_sensor"
description = "Sensor with no health check"

[connection]
protocol = "mock"

[[tools]]
name = "get_reading"
description = "Get reading"
command = "READ"
"""

SECOND_MOCK_PROFILE = """\
[device]
name = "other_sensor"
description = "Another sensor"

[connection]
protocol = "mock"

[[tools]]
name = "get_value"
description = "Get value"
command = "READ_VAL"
"""

INVALID_TOML = "not valid toml {{{{"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def profiles_dir(tmp_path: Path) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    (d / "sensor.toml").write_text(MOCK_PROFILE)
    return d


class TestMainGroup:
    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Jeltz" in result.output

    def test_version(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0

    def test_commands_registered(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        for cmd in ("start", "daemon", "chat", "status", "test", "add-device", "init"):
            assert cmd in result.output


class TestVerbose:
    def test_default_no_verbose(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "--verbose" in result.output or "-v" in result.output

    def test_verbose_flag_accepted(
        self, runner: CliRunner, profiles_dir: Path,
    ) -> None:
        result = runner.invoke(main, ["-v", "status", "-p", str(profiles_dir)])
        assert result.exit_code == 0

    def test_double_verbose(
        self, runner: CliRunner, profiles_dir: Path,
    ) -> None:
        result = runner.invoke(main, ["-vv", "status", "-p", str(profiles_dir)])
        assert result.exit_code == 0


class TestStart:
    def test_missing_profiles_dir(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["start", "-p", "/nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_no_devices_empty_dir(self, runner: CliRunner, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        result = runner.invoke(main, ["start", "-p", str(d)])
        assert result.exit_code == 1
        assert "No devices found" in result.output
        assert str(d.resolve()) in result.output

    def test_no_devices_all_broken(self, runner: CliRunner, tmp_path: Path) -> None:
        d = tmp_path / "profiles"
        d.mkdir()
        (d / "bad.toml").write_text(INVALID_TOML)
        result = runner.invoke(main, ["start", "-p", str(d)])
        assert result.exit_code == 1
        assert "Skipped" in result.output
        assert "all profiles failed" in result.output

    @patch("jeltz.gateway.server.JeltzServer.serve_stdio", new_callable=AsyncMock)
    def test_happy_path(
        self, mock_serve: AsyncMock, runner: CliRunner, profiles_dir: Path
    ) -> None:
        result = runner.invoke(main, ["start", "-p", str(profiles_dir)])
        assert result.exit_code == 0
        assert "Discovered 1 device(s)" in result.output
        assert "test_sensor" in result.output
        assert "MCP server ready on stdio" in result.output
        mock_serve.assert_awaited_once()

    @patch("jeltz.gateway.server.JeltzServer.serve_stdio", new_callable=AsyncMock)
    def test_shows_device_count_and_tools(
        self, mock_serve: AsyncMock, runner: CliRunner, profiles_dir: Path
    ) -> None:
        (profiles_dir / "other.toml").write_text(SECOND_MOCK_PROFILE)
        result = runner.invoke(main, ["start", "-p", str(profiles_dir)])
        assert result.exit_code == 0
        # 2 device tools + 4 fleet tools = 6
        assert "Discovered 2 device(s), exposing 6 tools" in result.output

    @patch("jeltz.gateway.server.JeltzServer.serve_stdio", new_callable=AsyncMock)
    def test_skipped_profiles_still_starts(
        self, mock_serve: AsyncMock, runner: CliRunner, profiles_dir: Path
    ) -> None:
        (profiles_dir / "bad.toml").write_text(INVALID_TOML)
        result = runner.invoke(main, ["start", "-p", str(profiles_dir)])
        assert result.exit_code == 0
        assert "Skipped" in result.output
        assert "test_sensor" in result.output

    @patch("jeltz.gateway.server.JeltzServer.serve_stdio", new_callable=AsyncMock)
    def test_db_path_option(
        self, mock_serve: AsyncMock, runner: CliRunner, profiles_dir: Path, tmp_path: Path
    ) -> None:
        db = tmp_path / "custom.db"
        result = runner.invoke(
            main, ["start", "-p", str(profiles_dir), "--db-path", str(db)]
        )
        assert result.exit_code == 0

    @patch(
        "jeltz.gateway.server.JeltzServer.serve_stdio",
        side_effect=KeyboardInterrupt,
    )
    def test_keyboard_interrupt(
        self, mock_serve: AsyncMock, runner: CliRunner, profiles_dir: Path
    ) -> None:
        result = runner.invoke(main, ["start", "-p", str(profiles_dir)])
        assert "Shutting down" in result.output


class TestDaemon:
    def test_missing_profiles_dir(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["daemon", "-p", "/nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_no_devices_empty_dir(self, runner: CliRunner, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        result = runner.invoke(main, ["daemon", "-p", str(d)])
        assert result.exit_code == 1
        assert "No devices found" in result.output

    def test_no_devices_all_broken(self, runner: CliRunner, tmp_path: Path) -> None:
        d = tmp_path / "profiles"
        d.mkdir()
        (d / "bad.toml").write_text(INVALID_TOML)
        result = runner.invoke(main, ["daemon", "-p", str(d)])
        assert result.exit_code == 1
        assert "Skipped" in result.output
        assert "all profiles failed" in result.output

    @patch(
        "jeltz.gateway.server.JeltzServer.run_daemon_loops",
        new_callable=AsyncMock,
    )
    def test_happy_path(
        self, mock_daemon: AsyncMock, runner: CliRunner, profiles_dir: Path
    ) -> None:
        result = runner.invoke(main, ["daemon", "-p", str(profiles_dir)])
        assert result.exit_code == 0
        assert "Discovered 1 device(s)" in result.output
        assert "Background recording active" in result.output
        assert "MCP server ready on http://127.0.0.1:8374/mcp" in result.output
        mock_daemon.assert_awaited_once()

    @patch(
        "jeltz.gateway.server.JeltzServer.run_daemon_loops",
        new_callable=AsyncMock,
    )
    def test_custom_host_port(
        self, mock_daemon: AsyncMock, runner: CliRunner, profiles_dir: Path
    ) -> None:
        result = runner.invoke(
            main,
            ["daemon", "-p", str(profiles_dir), "--host", "0.0.0.0", "--port", "9999"],
        )
        assert result.exit_code == 0
        assert "http://0.0.0.0:9999/mcp" in result.output
        mock_daemon.assert_awaited_once_with(host="0.0.0.0", port=9999)

    @patch(
        "jeltz.gateway.server.JeltzServer.run_daemon_loops",
        side_effect=KeyboardInterrupt,
    )
    def test_keyboard_interrupt(
        self, mock_daemon: AsyncMock, runner: CliRunner, profiles_dir: Path
    ) -> None:
        result = runner.invoke(main, ["daemon", "-p", str(profiles_dir)])
        assert "Shutting down" in result.output


class TestStatus:
    def test_shows_healthy_device(self, runner: CliRunner, profiles_dir: Path) -> None:
        result = runner.invoke(main, ["status", "-p", str(profiles_dir)])
        assert result.exit_code == 0
        assert "✓ test_sensor [mock] — healthy" in result.output

    def test_shows_tools(self, runner: CliRunner, profiles_dir: Path) -> None:
        result = runner.invoke(main, ["status", "-p", str(profiles_dir)])
        assert result.exit_code == 0
        assert "test_sensor.get_reading" in result.output

    def test_shows_device_count(self, runner: CliRunner, profiles_dir: Path) -> None:
        result = runner.invoke(main, ["status", "-p", str(profiles_dir)])
        assert result.exit_code == 0
        assert "Devices (1)" in result.output

    def test_missing_profiles_dir(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["status", "-p", "/nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_empty_profiles_dir(self, runner: CliRunner, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        result = runner.invoke(main, ["status", "-p", str(d)])
        assert result.exit_code == 0
        assert "No devices found" in result.output
        assert str(d.resolve()) in result.output

    def test_bad_profile_skipped(self, runner: CliRunner, profiles_dir: Path) -> None:
        (profiles_dir / "bad.toml").write_text(INVALID_TOML)
        result = runner.invoke(main, ["status", "-p", str(profiles_dir)])
        assert result.exit_code == 0
        assert "test_sensor" in result.output
        assert "Skipped bad.toml" in result.output

    def test_all_profiles_broken(self, runner: CliRunner, tmp_path: Path) -> None:
        d = tmp_path / "profiles"
        d.mkdir()
        (d / "bad.toml").write_text(INVALID_TOML)
        result = runner.invoke(main, ["status", "-p", str(d)])
        assert result.exit_code == 0
        assert "No devices found" in result.output
        assert "Skipped" in result.output

    def test_multiple_devices(self, runner: CliRunner, profiles_dir: Path) -> None:
        (profiles_dir / "other.toml").write_text(SECOND_MOCK_PROFILE)
        result = runner.invoke(main, ["status", "-p", str(profiles_dir)])
        assert result.exit_code == 0
        assert "test_sensor" in result.output
        assert "other_sensor" in result.output
        assert "Devices (2)" in result.output

    def test_unhealthy_device(self, runner: CliRunner, tmp_path: Path) -> None:
        """MockAdapter defaults to healthy=True, so we patch it."""
        d = tmp_path / "profiles"
        d.mkdir()
        (d / "sensor.toml").write_text(MOCK_PROFILE)

        with patch(
            "jeltz.adapters.mock.MockAdapter.health_check",
            new_callable=AsyncMock,
        ) as mock_hc:
            from jeltz.adapters.base import AdapterResult

            mock_hc.return_value = AdapterResult.fail("device unhealthy")
            result = runner.invoke(main, ["status", "-p", str(d)])

        assert result.exit_code == 0
        assert "connected (unhealthy)" in result.output


class TestTestDevice:
    def test_with_profile_path(self, runner: CliRunner, profiles_dir: Path) -> None:
        result = runner.invoke(
            main, ["test", str(profiles_dir / "sensor.toml")]
        )
        assert result.exit_code == 0
        assert "✓ Connected" in result.output
        assert "✓ Health check passed" in result.output
        assert "✓ Disconnected" in result.output

    def test_shows_device_info(self, runner: CliRunner, profiles_dir: Path) -> None:
        result = runner.invoke(
            main, ["test", str(profiles_dir / "sensor.toml")]
        )
        assert result.exit_code == 0
        assert "Device:   test_sensor" in result.output
        assert "Protocol: mock" in result.output
        assert "Tools:    1" in result.output

    def test_with_device_name(self, runner: CliRunner, profiles_dir: Path) -> None:
        result = runner.invoke(
            main, ["test", "test_sensor", "-p", str(profiles_dir)]
        )
        assert result.exit_code == 0
        assert "✓ Connected" in result.output

    def test_no_health_check(self, runner: CliRunner, tmp_path: Path) -> None:
        profile = tmp_path / "bare.toml"
        profile.write_text(MOCK_PROFILE_NO_HEALTH)
        result = runner.invoke(main, ["test", str(profile)])
        assert result.exit_code == 0
        assert "No health check configured" in result.output
        assert "✓ Disconnected" in result.output

    def test_device_not_found(self, runner: CliRunner, profiles_dir: Path) -> None:
        result = runner.invoke(
            main, ["test", "nonexistent", "-p", str(profiles_dir)]
        )
        assert result.exit_code != 0
        assert "Device not found" in result.output
        assert "Available devices: test_sensor" in result.output

    def test_device_not_found_no_profiles_dir(
        self, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            main, ["test", "foo", "-p", "/nonexistent"]
        )
        assert result.exit_code != 0
        assert "not a file path" in result.output

    def test_invalid_profile(self, runner: CliRunner, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(INVALID_TOML)
        result = runner.invoke(main, ["test", str(bad)])
        assert result.exit_code != 0
        assert "Invalid profile" in result.output

    def test_unknown_protocol(self, runner: CliRunner, tmp_path: Path) -> None:
        profile = tmp_path / "modbus.toml"
        profile.write_text(
            '[device]\nname = "modbus_device"\n'
            '[connection]\nprotocol = "modbus"\n'
        )
        result = runner.invoke(main, ["test", str(profile)])
        assert result.exit_code != 0
        assert "unknown protocol" in result.output

    def test_disconnect_runs_after_health_failure(
        self, runner: CliRunner, profiles_dir: Path
    ) -> None:
        """Disconnect must run even if health check fails."""
        with patch(
            "jeltz.adapters.mock.MockAdapter.health_check",
            new_callable=AsyncMock,
        ) as mock_hc:
            from jeltz.adapters.base import AdapterResult

            mock_hc.return_value = AdapterResult.fail("sensor offline")
            result = runner.invoke(
                main, ["test", str(profiles_dir / "sensor.toml")]
            )

        assert result.exit_code == 0
        assert "Health check failed: sensor offline" in result.output
        assert "✓ Disconnected" in result.output


class TestAddDevice:
    def test_adds_profile(self, runner: CliRunner, tmp_path: Path) -> None:
        profile = tmp_path / "sensor.toml"
        profile.write_text(MOCK_PROFILE)
        dest = tmp_path / "profiles"

        result = runner.invoke(
            main, ["add-device", str(profile), "-p", str(dest)]
        )
        assert result.exit_code == 0
        assert "Added test_sensor" in result.output
        assert (dest / "sensor.toml").exists()
        assert (dest / "sensor.toml").read_text() == MOCK_PROFILE

    def test_creates_profiles_dir(self, runner: CliRunner, tmp_path: Path) -> None:
        profile = tmp_path / "sensor.toml"
        profile.write_text(MOCK_PROFILE)
        dest = tmp_path / "new_dir"

        result = runner.invoke(
            main, ["add-device", str(profile), "-p", str(dest)]
        )
        assert result.exit_code == 0
        assert dest.is_dir()

    def test_invalid_profile_rejected(self, runner: CliRunner, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(INVALID_TOML)
        dest = tmp_path / "profiles"

        result = runner.invoke(
            main, ["add-device", str(bad), "-p", str(dest)]
        )
        assert result.exit_code != 0
        assert "Invalid profile" in result.output
        assert not dest.exists()

    def test_overwrite_confirmed(self, runner: CliRunner, tmp_path: Path) -> None:
        profile = tmp_path / "sensor.toml"
        profile.write_text(MOCK_PROFILE)
        dest = tmp_path / "profiles"
        dest.mkdir()
        (dest / "sensor.toml").write_text("old content")

        result = runner.invoke(
            main, ["add-device", str(profile), "-p", str(dest)],
            input="y\n",
        )
        assert result.exit_code == 0
        assert (dest / "sensor.toml").read_text() == MOCK_PROFILE

    def test_overwrite_declined(self, runner: CliRunner, tmp_path: Path) -> None:
        profile = tmp_path / "sensor.toml"
        profile.write_text(MOCK_PROFILE)
        dest = tmp_path / "profiles"
        dest.mkdir()
        (dest / "sensor.toml").write_text("old content")

        result = runner.invoke(
            main, ["add-device", str(profile), "-p", str(dest)],
            input="n\n",
        )
        assert result.exit_code != 0
        assert (dest / "sensor.toml").read_text() == "old content"

    def test_nonexistent_profile(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["add-device", "/nonexistent.toml"])
        assert result.exit_code != 0


class TestInit:
    def test_creates_profiles_dir(self, runner: CliRunner, tmp_path: Path) -> None:
        d = tmp_path / "myproject"
        result = runner.invoke(main, ["init", str(d)])
        assert result.exit_code == 0
        assert (d / "profiles" / "mock_sensor.toml").exists()
        assert "Initialized" in result.output

    def test_default_current_dir(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(main, ["init", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / "profiles" / "mock_sensor.toml").exists()

    def test_mock_profile_is_valid(self, runner: CliRunner, tmp_path: Path) -> None:
        """The scaffolded profile should parse and pass jeltz test."""
        d = tmp_path / "proj"
        runner.invoke(main, ["init", str(d)])
        result = runner.invoke(
            main, ["test", str(d / "profiles" / "mock_sensor.toml")]
        )
        assert result.exit_code == 0
        assert "✓ Connected" in result.output
        assert "✓ Health check passed" in result.output

    def test_refuses_existing_profiles(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        d = tmp_path / "existing"
        (d / "profiles").mkdir(parents=True)
        (d / "profiles" / "sensor.toml").write_text(MOCK_PROFILE)
        result = runner.invoke(main, ["init", str(d)])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_shows_next_steps(self, runner: CliRunner, tmp_path: Path) -> None:
        d = tmp_path / "proj"
        result = runner.invoke(main, ["init", str(d)])
        assert "Next steps" in result.output
        assert "jeltz test" in result.output
        assert "jeltz start" in result.output

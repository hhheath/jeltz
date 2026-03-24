"""Tests for the jeltz chat CLI command."""

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


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def profiles_dir(tmp_path: Path) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    (d / "sensor.toml").write_text(MOCK_PROFILE)
    return d


class TestChatCommand:
    def test_registered(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        assert "chat" in result.output

    def test_missing_profiles_dir(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["chat", "-p", "/nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output

    @patch("jeltz.chat.client.AsyncOpenAI")
    def test_starts_and_shows_banner(
        self, mock_openai_cls: AsyncMock, runner: CliRunner, profiles_dir: Path,
    ) -> None:
        """Starts server, shows banner, exits on EOF."""
        mock_api = AsyncMock()
        mock_openai_cls.return_value = mock_api
        mock_api.close = AsyncMock()

        result = runner.invoke(
            main,
            ["chat", "-p", str(profiles_dir)],
            input="",  # EOF
        )
        assert result.exit_code == 0
        assert "Connected to" in result.output
        assert "1 device(s)" in result.output

    @patch("jeltz.chat.client.AsyncOpenAI")
    def test_banner_shows_model(
        self, mock_openai_cls: AsyncMock, runner: CliRunner, profiles_dir: Path,
    ) -> None:
        mock_api = AsyncMock()
        mock_openai_cls.return_value = mock_api
        mock_api.close = AsyncMock()

        result = runner.invoke(
            main,
            ["chat", "-p", str(profiles_dir), "-m", "mistral"],
            input="",
        )
        assert result.exit_code == 0
        assert "mistral" in result.output

    @patch("jeltz.chat.client.AsyncOpenAI")
    def test_banner_shows_api_url(
        self, mock_openai_cls: AsyncMock, runner: CliRunner, profiles_dir: Path,
    ) -> None:
        mock_api = AsyncMock()
        mock_openai_cls.return_value = mock_api
        mock_api.close = AsyncMock()

        result = runner.invoke(
            main,
            [
                "chat", "-p", str(profiles_dir),
                "--api-url", "http://localhost:8080/v1",
            ],
            input="",
        )
        assert result.exit_code == 0
        assert "http://localhost:8080/v1" in result.output

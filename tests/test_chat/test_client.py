"""Tests for the chat client — tool conversion, system prompt, chat loop."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import Tool

from jeltz.chat.client import (
    AssistantMessageEvent,
    ChatClient,
    ToolCallEvent,
    ToolResultEvent,
    api_name_to_mcp,
    build_system_prompt,
    convert_tools,
    mcp_name_to_api,
)
from jeltz.gateway.server import JeltzServer

MOCK_PROFILE = """\
[device]
name = "test_sensor"
description = "Test temperature sensor"

[connection]
protocol = "mock"

[[tools]]
name = "get_reading"
description = "Get current temperature"
command = "READ_TEMP"

[tools.returns]
type = "float"
unit = "celsius"

[health]
check_command = "PING"
expected = "PONG"
interval_ms = 10000
"""


@pytest.fixture
def profiles_dir(tmp_path: Path) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    (d / "sensor.toml").write_text(MOCK_PROFILE)
    return d


# ---------------------------------------------------------------------------
# Name mapping
# ---------------------------------------------------------------------------


class TestNameMapping:
    def test_mcp_to_api_with_dot(self) -> None:
        assert mcp_name_to_api("fleet.list_devices") == "fleet__list_devices"

    def test_mcp_to_api_no_dot(self) -> None:
        assert mcp_name_to_api("simple_tool") == "simple_tool"

    def test_mcp_to_api_only_first_dot(self) -> None:
        # MCP names should only have one dot, but verify only first is replaced
        assert mcp_name_to_api("a.b.c") == "a__b.c"

    def test_api_to_mcp_with_double_underscore(self) -> None:
        assert api_name_to_mcp("fleet__list_devices") == "fleet.list_devices"

    def test_api_to_mcp_only_first(self) -> None:
        assert api_name_to_mcp("my_device__get_reading") == "my_device.get_reading"

    def test_roundtrip(self) -> None:
        names = [
            "fleet.list_devices",
            "fleet.get_all_readings",
            "temp_sensor.get_reading",
            "pressure_line.get_psi",
        ]
        for name in names:
            assert api_name_to_mcp(mcp_name_to_api(name)) == name

    def test_double_underscore_in_device_name_breaks_roundtrip(self) -> None:
        """Document known limitation: device names with __ break the mapping."""
        name = "my__sensor.get_reading"
        api = mcp_name_to_api(name)
        # Forward: "my__sensor.get_reading" → "my__sensor__get_reading"
        assert api == "my__sensor__get_reading"
        # Reverse: replaces first __ → "my.sensor__get_reading" (wrong!)
        assert api_name_to_mcp(api) != name

    def test_no_dot_no_underscore(self) -> None:
        assert mcp_name_to_api("simple") == "simple"
        assert api_name_to_mcp("simple") == "simple"


# ---------------------------------------------------------------------------
# Tool conversion
# ---------------------------------------------------------------------------


class TestConvertTools:
    def test_basic_conversion(self) -> None:
        mcp_tools = [
            Tool(
                name="fleet.list_devices",
                description="List all devices",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]
        result = convert_tools(mcp_tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "fleet__list_devices"
        assert result[0]["function"]["description"] == "List all devices"
        assert result[0]["function"]["parameters"] == {
            "type": "object", "properties": {},
        }

    def test_with_parameters(self) -> None:
        mcp_tools = [
            Tool(
                name="fleet.get_history",
                description="Get history",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "device_id": {"type": "string"},
                        "hours": {"type": "number", "default": 24},
                    },
                    "required": ["device_id"],
                },
            ),
        ]
        result = convert_tools(mcp_tools)
        params = result[0]["function"]["parameters"]
        assert "device_id" in params["properties"]
        assert params["required"] == ["device_id"]

    def test_empty_list(self) -> None:
        assert convert_tools([]) == []

    def test_no_description(self) -> None:
        mcp_tools = [
            Tool(
                name="test.tool",
                description=None,
                inputSchema={"type": "object", "properties": {}},
            ),
        ]
        result = convert_tools(mcp_tools)
        assert result[0]["function"]["description"] == ""

    def test_empty_input_schema(self) -> None:
        """Tools with empty properties (like fleet.list_devices)."""
        mcp_tools = [
            Tool(
                name="test.tool", description="Test",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]
        result = convert_tools(mcp_tools)
        assert result[0]["function"]["parameters"] == {
            "type": "object", "properties": {},
        }

    def test_multiple_tools(self) -> None:
        mcp_tools = [
            Tool(
                name="a.b", description="A",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="c.d", description="C",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]
        result = convert_tools(mcp_tools)
        assert len(result) == 2
        assert result[0]["function"]["name"] == "a__b"
        assert result[1]["function"]["name"] == "c__d"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    async def test_includes_device_names(self, profiles_dir: Path) -> None:
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        await server.start()
        try:
            prompt = build_system_prompt(server)
            assert "test_sensor" in prompt
            assert "mock" in prompt
            assert "Test temperature sensor" in prompt
        finally:
            await server.stop()

    async def test_includes_fleet_tools(self, profiles_dir: Path) -> None:
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        await server.start()
        try:
            prompt = build_system_prompt(server)
            assert "fleet.list_devices" in prompt
            assert "fleet.get_all_readings" in prompt
            assert "fleet.get_history" in prompt
            assert "fleet.search_anomalies" in prompt
        finally:
            await server.stop()

    async def test_includes_tool_descriptions(self, profiles_dir: Path) -> None:
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        await server.start()
        try:
            prompt = build_system_prompt(server)
            assert "test_sensor.get_reading" in prompt
            assert "Get current temperature" in prompt
        finally:
            await server.stop()

    def test_no_aggregator(self) -> None:
        server = JeltzServer(
            profiles_dir=Path("/tmp/nonexistent"), db_path=":memory:",
        )
        prompt = build_system_prompt(server)
        assert "No devices connected" in prompt


# ---------------------------------------------------------------------------
# Helpers for mocking OpenAI responses
# ---------------------------------------------------------------------------


def _make_tool_call(
    name: str,
    arguments: dict[str, Any] | str | None = None,
    call_id: str = "call_1",
) -> SimpleNamespace:
    """Build a mock tool call using SimpleNamespace for strict attribute access."""
    if arguments is None:
        args_str = "{}"
    elif isinstance(arguments, str):
        args_str = arguments
    else:
        args_str = json.dumps(arguments)
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=args_str),
    )


def _make_completion(
    content: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    """Build a mock ChatCompletion response using SimpleNamespace."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


# ---------------------------------------------------------------------------
# Chat loop
# ---------------------------------------------------------------------------


class TestChatLoop:
    async def test_simple_response(self, profiles_dir: Path) -> None:
        """LLM responds without calling any tools."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(
                return_value=_make_completion(content="All sensors look fine.")
            )
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                events = [e async for e in client.send_message("anything weird?")]
                assert len(events) == 1
                assert isinstance(events[0], AssistantMessageEvent)
                assert events[0].content == "All sensors look fine."
            finally:
                await client.shutdown()

    async def test_single_tool_call(self, profiles_dir: Path) -> None:
        """LLM calls one tool, gets result, then responds."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc = _make_tool_call("fleet__list_devices", {})
        responses = [
            _make_completion(tool_calls=[tc]),
            _make_completion(content="You have 1 device connected."),
        ]

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(side_effect=responses)
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                events = [e async for e in client.send_message("what devices?")]

                tool_call = next(
                    e for e in events if isinstance(e, ToolCallEvent)
                )
                assert tool_call.name == "fleet.list_devices"

                tool_result = next(
                    e for e in events if isinstance(e, ToolResultEvent)
                )
                assert not tool_result.is_error
                assert "test_sensor" in tool_result.result

                assistant = next(
                    e for e in events if isinstance(e, AssistantMessageEvent)
                )
                assert assistant.content == "You have 1 device connected."
            finally:
                await client.shutdown()

    async def test_parallel_tool_calls(self, profiles_dir: Path) -> None:
        """LLM calls multiple tools in a single response."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc1 = _make_tool_call("fleet__list_devices", {}, call_id="call_1")
        tc2 = _make_tool_call(
            "fleet__search_anomalies", {}, call_id="call_2",
        )
        responses = [
            _make_completion(tool_calls=[tc1, tc2]),
            _make_completion(content="Found some anomalies."),
        ]

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(side_effect=responses)
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                events = [e async for e in client.send_message("check it")]
                tool_calls = [
                    e for e in events if isinstance(e, ToolCallEvent)
                ]
                tool_results = [
                    e for e in events if isinstance(e, ToolResultEvent)
                ]
                assert len(tool_calls) == 2
                assert len(tool_results) == 2
                assert tool_calls[0].name == "fleet.list_devices"
                assert tool_calls[1].name == "fleet.search_anomalies"

                # Verify tool_call_ids are correctly tracked in history
                tool_msgs = [
                    m for m in client.history if m.get("role") == "tool"
                ]
                assert len(tool_msgs) == 2
                assert {m["tool_call_id"] for m in tool_msgs} == {
                    "call_1", "call_2",
                }
            finally:
                await client.shutdown()

    async def test_tool_call_error(self, profiles_dir: Path) -> None:
        """Tool call fails — error is passed to the LLM."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc = _make_tool_call("nonexistent__tool", {})
        responses = [
            _make_completion(tool_calls=[tc]),
            _make_completion(content="That tool doesn't exist."),
        ]

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(side_effect=responses)
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                events = [e async for e in client.send_message("call something")]
                error_events = [
                    e for e in events
                    if isinstance(e, ToolResultEvent) and e.is_error
                ]
                assert len(error_events) == 1
                assert "unknown tool" in error_events[0].result.lower()
            finally:
                await client.shutdown()

    async def test_tool_call_invalid_json_arguments(
        self, profiles_dir: Path,
    ) -> None:
        """LLM returns malformed JSON for tool arguments."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc = _make_tool_call("fleet__list_devices", "not valid json")
        responses = [
            _make_completion(tool_calls=[tc]),
            _make_completion(content="Done."),
        ]

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(side_effect=responses)
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                events = [e async for e in client.send_message("go")]
                # Tool call should still proceed with empty args
                tool_call = next(
                    e for e in events if isinstance(e, ToolCallEvent)
                )
                assert tool_call.arguments == {}
                # Should still produce a result and record it in history
                assert any(isinstance(e, ToolResultEvent) for e in events)
                tool_msgs = [
                    m for m in client.history if m.get("role") == "tool"
                ]
                assert len(tool_msgs) == 1
            finally:
                await client.shutdown()

    async def test_multi_round_tool_calling(self, profiles_dir: Path) -> None:
        """LLM calls a tool, sees result, calls another tool, then responds."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc1 = _make_tool_call("fleet__list_devices", {}, call_id="call_1")
        tc2 = _make_tool_call(
            "test_sensor__get_reading", {}, call_id="call_2",
        )
        responses = [
            _make_completion(tool_calls=[tc1]),
            _make_completion(tool_calls=[tc2]),
            _make_completion(content="Temperature is 22.5C."),
        ]

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(side_effect=responses)
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                events = [e async for e in client.send_message("read temp")]
                tool_calls = [
                    e for e in events if isinstance(e, ToolCallEvent)
                ]
                assert len(tool_calls) == 2
                assert tool_calls[0].name == "fleet.list_devices"
                assert tool_calls[1].name == "test_sensor.get_reading"
            finally:
                await client.shutdown()

    async def test_conversation_history_accumulates(
        self, profiles_dir: Path,
    ) -> None:
        """History includes system, user, assistant messages across turns."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(
                return_value=_make_completion(content="Hello!")
            )
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                assert client.history[0]["role"] == "system"

                _ = [e async for e in client.send_message("hi")]
                assert client.history[1]["role"] == "user"
                assert client.history[1]["content"] == "hi"
                assert client.history[2]["role"] == "assistant"
                assert client.history[2]["content"] == "Hello!"

                _ = [e async for e in client.send_message("bye")]
                assert client.history[3]["role"] == "user"
                assert client.history[3]["content"] == "bye"
            finally:
                await client.shutdown()

    async def test_history_includes_tool_messages(
        self, profiles_dir: Path,
    ) -> None:
        """After a tool call, history contains role=tool with tool_call_id."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc = _make_tool_call("fleet__list_devices", {}, call_id="call_abc")
        responses = [
            _make_completion(tool_calls=[tc]),
            _make_completion(content="Done."),
        ]

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(side_effect=responses)
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                _ = [e async for e in client.send_message("check")]
                tool_msgs = [
                    m for m in client.history if m.get("role") == "tool"
                ]
                assert len(tool_msgs) == 1
                assert tool_msgs[0]["tool_call_id"] == "call_abc"
                assert isinstance(tool_msgs[0]["content"], str)

                # Assistant message with tool_calls should include type: function
                assistant_with_tools = [
                    m for m in client.history
                    if m.get("role") == "assistant" and "tool_calls" in m
                ]
                assert len(assistant_with_tools) == 1
                assert assistant_with_tools[0]["tool_calls"][0]["type"] == "function"
            finally:
                await client.shutdown()

    async def test_max_iterations_safety_valve(
        self, profiles_dir: Path,
    ) -> None:
        """Runaway tool-calling loop hits MAX_TOOL_ROUNDS and forces a response."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc = _make_tool_call("fleet__list_devices", {})
        endless_tool_calls = _make_completion(tool_calls=[tc])
        final_response = _make_completion(content="Done summarizing.")

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(
                side_effect=[endless_tool_calls] * 10 + [final_response]
            )
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                events = [e async for e in client.send_message("loop forever")]
                assistant_msgs = [
                    e for e in events if isinstance(e, AssistantMessageEvent)
                ]
                assert len(assistant_msgs) == 1
                assert assistant_msgs[0].content == "Done summarizing."

                # 10 tool rounds + 1 forced summary = 11 API calls
                assert mock_api.chat.completions.create.call_count == 11

                # History should contain the safety valve system message
                safety_msgs = [
                    m for m in client.history
                    if m.get("role") == "system"
                    and "many tool calls" in m.get("content", "")
                ]
                assert len(safety_msgs) == 1
            finally:
                await client.shutdown()

    async def test_tool_count(self, profiles_dir: Path) -> None:
        """tool_count reflects device + fleet tools."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                # 1 device tool + 4 fleet tools = 5
                assert client.tool_count == 5
            finally:
                await client.shutdown()

    async def test_custom_system_prompt(self, profiles_dir: Path) -> None:
        """Custom system prompt overrides the auto-generated one."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(
            server=server,
            system_prompt="You are a helpful brewery assistant.",
        )

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                assert (
                    client.history[0]["content"]
                    == "You are a helpful brewery assistant."
                )
            finally:
                await client.shutdown()

    async def test_send_message_before_initialize(
        self, profiles_dir: Path,
    ) -> None:
        """Calling send_message before initialize raises RuntimeError."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        with pytest.raises(RuntimeError, match="not initialized"):
            _ = [e async for e in client.send_message("hello")]

    async def test_api_error_rolls_back_user_message(
        self, profiles_dir: Path,
    ) -> None:
        """If the API call fails, the user message is removed from history."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(
                side_effect=ConnectionError("Ollama not running")
            )
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                history_before = len(client.history)
                with pytest.raises(ConnectionError):
                    _ = [e async for e in client.send_message("hello")]
                # User message should be rolled back
                assert len(client.history) == history_before
            finally:
                await client.shutdown()

    async def test_empty_content_response(self, profiles_dir: Path) -> None:
        """LLM returns no content and no tool calls — yields nothing."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(
                return_value=_make_completion(content=None, tool_calls=None)
            )
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                events = [e async for e in client.send_message("hello")]
                assert events == []
                # History should still have: system, user, assistant (empty)
                assert len(client.history) == 3
                assert client.history[2]["role"] == "assistant"
            finally:
                await client.shutdown()

    async def test_api_key_not_in_repr(self, profiles_dir: Path) -> None:
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server, api_key="secret_key_123")
        assert "secret_key_123" not in repr(client)

    async def test_keyboard_interrupt_rolls_back_history(
        self, profiles_dir: Path,
    ) -> None:
        """Ctrl+C during tool execution should roll back history."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc = _make_tool_call("fleet__list_devices", {})

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(
                return_value=_make_completion(tool_calls=[tc])
            )
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                history_before = len(client.history)
                with patch.object(
                    server, "handle_call_tool",
                    side_effect=KeyboardInterrupt,
                ):
                    with pytest.raises(KeyboardInterrupt):
                        _ = [e async for e in client.send_message("check")]
                assert len(client.history) == history_before
            finally:
                await client.shutdown()

    async def test_cancelled_error_rolls_back_history(
        self, profiles_dir: Path,
    ) -> None:
        """asyncio.CancelledError during tool call should roll back."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc = _make_tool_call("fleet__list_devices", {})

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(
                return_value=_make_completion(tool_calls=[tc])
            )
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                history_before = len(client.history)
                with patch.object(
                    server, "handle_call_tool",
                    side_effect=asyncio.CancelledError,
                ):
                    with pytest.raises(asyncio.CancelledError):
                        _ = [e async for e in client.send_message("check")]
                assert len(client.history) == history_before
            finally:
                await client.shutdown()

    async def test_handle_call_tool_raises_exception(
        self, profiles_dir: Path,
    ) -> None:
        """handle_call_tool raising (not returning error) still yields error event."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc = _make_tool_call("fleet__list_devices", {})
        responses = [
            _make_completion(tool_calls=[tc]),
            _make_completion(content="Sorry about that."),
        ]

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(side_effect=responses)
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                with patch.object(
                    server, "handle_call_tool",
                    side_effect=RuntimeError("server crashed"),
                ):
                    events = [
                        e async for e in client.send_message("go")
                    ]
                error_events = [
                    e for e in events
                    if isinstance(e, ToolResultEvent) and e.is_error
                ]
                assert len(error_events) == 1
                assert "server crashed" in error_events[0].result
            finally:
                await client.shutdown()

    async def test_message_with_content_and_tool_calls(
        self, profiles_dir: Path,
    ) -> None:
        """Assistant message with both content and tool_calls — both preserved."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc = _make_tool_call("fleet__list_devices", {})
        responses = [
            _make_completion(content="Let me check...", tool_calls=[tc]),
            _make_completion(content="Found 1 device."),
        ]

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(side_effect=responses)
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                _ = [e async for e in client.send_message("check")]
                assistant_with_tools = [
                    m for m in client.history
                    if m.get("role") == "assistant" and "tool_calls" in m
                ]
                assert len(assistant_with_tools) == 1
                assert assistant_with_tools[0]["content"] == "Let me check..."
                assert len(assistant_with_tools[0]["tool_calls"]) == 1
            finally:
                await client.shutdown()

    async def test_tool_call_with_real_arguments(
        self, profiles_dir: Path,
    ) -> None:
        """Arguments from the LLM are parsed and forwarded to handle_call_tool."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        tc = _make_tool_call(
            "fleet__get_history",
            {"device_id": "test_sensor", "sensor_id": "get_reading", "hours": 12},
        )
        responses = [
            _make_completion(tool_calls=[tc]),
            _make_completion(content="No history yet."),
        ]

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(side_effect=responses)
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                events = [e async for e in client.send_message("show history")]
                tool_call = next(
                    e for e in events if isinstance(e, ToolCallEvent)
                )
                assert tool_call.arguments == {
                    "device_id": "test_sensor",
                    "sensor_id": "get_reading",
                    "hours": 12,
                }
                tool_result = next(
                    e for e in events if isinstance(e, ToolResultEvent)
                )
                assert not tool_result.is_error
            finally:
                await client.shutdown()

    async def test_api_timeout_rolls_back(self, profiles_dir: Path) -> None:
        """Timeout during API call should roll back user message."""
        server = JeltzServer(profiles_dir=profiles_dir, db_path=":memory:")
        client = ChatClient(server=server)

        with patch("jeltz.chat.client.AsyncOpenAI") as MockOpenAI:
            mock_api = AsyncMock()
            MockOpenAI.return_value = mock_api
            mock_api.chat.completions.create = AsyncMock(
                side_effect=TimeoutError()
            )
            mock_api.close = AsyncMock()

            await client.initialize()
            try:
                history_before = len(client.history)
                with pytest.raises(TimeoutError):
                    _ = [e async for e in client.send_message("hello")]
                assert len(client.history) == history_before
            finally:
                await client.shutdown()

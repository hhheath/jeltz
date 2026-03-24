"""Tests for the chat terminal renderer."""

from __future__ import annotations

from jeltz.chat.client import (
    AssistantMessageEvent,
    StreamChunkEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from jeltz.chat.render import print_banner, print_error, render_event


class TestRenderEvent:
    def test_tool_call_no_args(self, capsys) -> None:  # type: ignore[no-untyped-def]
        render_event(ToolCallEvent(name="fleet.list_devices", arguments={}))
        captured = capsys.readouterr()
        assert "fleet.list_devices" in captured.out
        assert "=" not in captured.out

    def test_tool_call_with_args(self, capsys) -> None:  # type: ignore[no-untyped-def]
        render_event(
            ToolCallEvent(
                name="fleet.get_history",
                arguments={"device_id": "sensor1", "hours": 24},
            )
        )
        captured = capsys.readouterr()
        assert "fleet.get_history" in captured.out
        assert "device_id" in captured.out

    def test_tool_result_error(self, capsys) -> None:  # type: ignore[no-untyped-def]
        render_event(
            ToolResultEvent(
                name="fleet.list_devices",
                result="Error: connection refused",
                is_error=True,
            )
        )
        captured = capsys.readouterr()
        assert "Error: connection refused" in captured.out

    def test_tool_result_success_suppressed(self, capsys) -> None:  # type: ignore[no-untyped-def]
        render_event(
            ToolResultEvent(
                name="fleet.list_devices",
                result="{'devices': [...]}",
                is_error=False,
            )
        )
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_assistant_message(self, capsys) -> None:  # type: ignore[no-untyped-def]
        render_event(AssistantMessageEvent(content="All sensors nominal."))
        captured = capsys.readouterr()
        assert "All sensors nominal." in captured.out

    def test_stream_chunk(self, capsys) -> None:  # type: ignore[no-untyped-def]
        render_event(StreamChunkEvent(text="Hello "))
        render_event(StreamChunkEvent(text="world"))
        captured = capsys.readouterr()
        assert captured.out == "Hello world"

    def test_stream_chunk_done(self, capsys) -> None:  # type: ignore[no-untyped-def]
        render_event(StreamChunkEvent(text="", done=True))
        captured = capsys.readouterr()
        # Done marker prints trailing newlines
        assert "\n" in captured.out


class TestPrintBanner:
    def test_banner_content(self, capsys) -> None:  # type: ignore[no-untyped-def]
        print_banner(
            device_count=3,
            tool_count=15,
            model="llama3.2",
            api_url="http://localhost:11434/v1",
        )
        captured = capsys.readouterr()
        assert "3 device(s)" in captured.out
        assert "15 tools" in captured.out
        assert "llama3.2" in captured.out
        assert "http://localhost:11434/v1" in captured.out
        assert "Ready" in captured.out


class TestPrintError:
    def test_error_output(self, capsys) -> None:  # type: ignore[no-untyped-def]
        print_error("something broke")
        captured = capsys.readouterr()
        assert "something broke" in captured.err

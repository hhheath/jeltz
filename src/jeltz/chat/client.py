"""Chat client — bridges a local LLM to Jeltz's MCP tools.

Talks to any OpenAI-compatible API (Ollama, llama.cpp, LM Studio, vLLM)
and routes tool calls through Jeltz — either in-process (JeltzServer) or
remotely (MCP client connected to a running ``jeltz daemon``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp.types import CallToolResult, Tool
from openai import AsyncOpenAI

from jeltz.gateway.server import JeltzServer

logger = logging.getLogger(__name__)

# Maximum tool-call round-trips before forcing the LLM to respond.
MAX_TOOL_ROUNDS = 10


# ---------------------------------------------------------------------------
# Chat events — yielded by send_message so the renderer can display them
# ---------------------------------------------------------------------------


@dataclass
class ToolCallEvent:
    """The LLM wants to call a tool."""

    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResultEvent:
    """A tool call completed."""

    name: str
    result: str
    is_error: bool = False


@dataclass
class AssistantMessageEvent:
    """The LLM produced a complete text response (non-streaming fallback)."""

    content: str


@dataclass
class StreamChunkEvent:
    """A partial text chunk from a streaming response."""

    text: str
    done: bool = False


ChatEvent = ToolCallEvent | ToolResultEvent | AssistantMessageEvent | StreamChunkEvent


# ---------------------------------------------------------------------------
# Name mapping — dots in MCP names aren't valid in all OpenAI-compatible APIs
#
# MCP names use exactly one dot: "device_name.tool_name".
# We replace only that dot with "__" for the API, and reverse on the first
# "__" occurrence. This is safe as long as device names don't contain "__".
# Profile validation should enforce this.
# ---------------------------------------------------------------------------


_API_SEP = "__"


def mcp_name_to_api(name: str) -> str:
    """Convert ``fleet.list_devices`` → ``fleet__list_devices``.

    Only replaces the first dot (MCP names have exactly one).
    """
    return name.replace(".", _API_SEP, 1)


def api_name_to_mcp(name: str) -> str:
    """Convert ``fleet__list_devices`` → ``fleet.list_devices``.

    Only replaces the first ``__`` occurrence.
    """
    return name.replace(_API_SEP, ".", 1)


# ---------------------------------------------------------------------------
# Tool conversion
# ---------------------------------------------------------------------------


def convert_tools(mcp_tools: list[Tool]) -> list[dict[str, Any]]:
    """Convert MCP Tool objects to OpenAI function-calling format."""
    openai_tools: list[dict[str, Any]] = []
    for tool in mcp_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": mcp_name_to_api(tool.name),
                "description": tool.description or "",
                "parameters": tool.inputSchema or {"type": "object", "properties": {}},
            },
        })
    return openai_tools


# ---------------------------------------------------------------------------
# System prompt — works for both in-process and daemon modes
# ---------------------------------------------------------------------------


def build_system_prompt_from_tools(mcp_tools: list[Tool]) -> str:
    """Generate a system prompt from a list of MCP tools.

    Used by both in-process and daemon modes.
    """
    device_tools: list[str] = []
    fleet_tools: list[str] = []
    for tool in mcp_tools:
        desc = tool.description or ""
        if tool.name.startswith("fleet."):
            fleet_tools.append(f"- {tool.name}: {desc}")
        else:
            device_tools.append(f"- {tool.name}: {desc}")

    device_section = "\n".join(device_tools) if device_tools else "No device tools available."
    fleet_section = "\n".join(fleet_tools) if fleet_tools else "No fleet tools available."

    return f"""\
You are an AI assistant with access to a fleet of physical sensors and devices \
via the Jeltz gateway. You can call tools to read sensors, query history, and \
detect anomalies.

Device tools:
{device_section}

Fleet tools:
{fleet_section}

When the user asks about their sensors or devices, use the tools to get real \
data before answering. For broad questions like "anything weird?", start with \
fleet.get_all_readings or fleet.search_anomalies. For specific device questions, \
use the device-specific tools.

Always ground your responses in actual sensor data. If a tool call fails, say so \
honestly."""


def build_system_prompt(server: JeltzServer) -> str:
    """Generate a system prompt from live server state (in-process mode)."""
    return build_system_prompt_from_tools(server.handle_list_tools())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_assistant_message(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a history-safe assistant message dict.

    Only includes fields the API expects back: role, content, tool_calls.
    """
    entry: dict[str, Any] = {"role": "assistant"}
    if content:
        entry["content"] = content
    if tool_calls:
        entry["tool_calls"] = tool_calls
    return entry


def _extract_text(result: CallToolResult) -> str:
    """Extract text content from a CallToolResult."""
    if result.content:
        first = result.content[0]
        if hasattr(first, "text"):
            return str(first.text)
    return str(result.content)


# ---------------------------------------------------------------------------
# Chat client
# ---------------------------------------------------------------------------


@dataclass
class ChatClient:
    """Bridges a local LLM to Jeltz's MCP tools via an OpenAI-compatible API.

    Two modes:
    - **In-process**: pass ``server`` — starts JeltzServer, calls tools directly.
    - **Daemon**: pass ``daemon_url`` — connects to a running ``jeltz daemon``
      via MCP over HTTP and calls tools remotely.
    """

    server: JeltzServer | None = None
    daemon_url: str | None = None
    api_url: str = "http://localhost:11434/v1"
    model: str = "llama3.2"
    api_key: str = field(default="jeltz", repr=False)
    temperature: float = 0.1
    system_prompt: str | None = None

    # Private state
    _openai_tools: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _history: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _client: AsyncOpenAI | None = field(default=None, init=False, repr=False)
    _mcp_session: Any = field(default=None, init=False, repr=False)
    _exit_stack: AsyncExitStack | None = field(default=None, init=False, repr=False)

    async def initialize(self) -> None:
        """Start the backend and create the OpenAI client."""
        if self.daemon_url:
            await self._init_daemon()
        elif self.server:
            await self._init_in_process()
        else:
            raise ValueError("either server or daemon_url is required")

        # Create OpenAI client
        self._client = AsyncOpenAI(base_url=self.api_url, api_key=self.api_key)

    async def _init_in_process(self) -> None:
        """Initialize in-process mode: start JeltzServer."""
        assert self.server is not None
        discovery = await self.server.start()
        for path, error in discovery.errors:
            logger.warning("skipped profile %s: %s", path, error)

        mcp_tools = self.server.handle_list_tools()
        self._openai_tools = convert_tools(mcp_tools)

        prompt = self.system_prompt or build_system_prompt(self.server)
        self._history = [{"role": "system", "content": prompt}]

    async def _init_daemon(self) -> None:
        """Initialize daemon mode: connect to a running jeltz daemon via MCP."""
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        assert self.daemon_url is not None

        self._exit_stack = AsyncExitStack()
        read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
            streamablehttp_client(self.daemon_url)
        )
        session: ClientSession = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self._mcp_session = session

        # List tools from the daemon
        result = await session.list_tools()
        mcp_tools = result.tools
        self._openai_tools = convert_tools(mcp_tools)

        prompt = self.system_prompt or build_system_prompt_from_tools(mcp_tools)
        self._history = [{"role": "system", "content": prompt}]

    async def shutdown(self) -> None:
        """Stop the backend and clean up."""
        if self.server:
            await self.server.stop()
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None
        self._mcp_session = None
        if self._client:
            await self._client.close()
            self._client = None

    async def _call_tool(
        self, name: str, arguments: dict[str, Any],
    ) -> CallToolResult:
        """Call a tool via the appropriate backend."""
        if self._mcp_session:
            return await self._mcp_session.call_tool(name, arguments)  # type: ignore[no-any-return]
        if self.server:
            return await self.server.handle_call_tool(name, arguments)
        raise RuntimeError("no backend available")

    async def send_message(self, user_message: str) -> AsyncGenerator[ChatEvent, None]:
        """Send a user message, handle tool calls, yield events for rendering.

        The loop continues until the LLM responds without tool calls or
        the safety valve (MAX_TOOL_ROUNDS) is hit.
        """
        if not self._client:
            raise RuntimeError("client not initialized — call initialize() first")

        # Snapshot history length so we can roll back everything on error —
        # the user message, any assistant messages, and any partial tool results.
        history_snapshot = len(self._history)
        self._history.append({"role": "user", "content": user_message})

        try:
            async for event in self._run_tool_loop():
                yield event
        except BaseException:
            # Roll back to pre-message state so history stays consistent.
            # Catches Exception, KeyboardInterrupt, and CancelledError
            # so Ctrl+C during a tool call doesn't leave corrupted history.
            del self._history[history_snapshot:]
            raise

    async def _run_tool_loop(self) -> AsyncGenerator[ChatEvent, None]:
        """Inner loop: call LLM, execute tool calls, repeat until done.

        Uses streaming for all API calls. Streams text chunks to the renderer
        as they arrive, and accumulates tool call deltas until complete.
        """
        assert self._client is not None

        for _round in range(MAX_TOOL_ROUNDS):
            content_parts: list[str] = []
            # tool_calls_acc: {index: {"id": ..., "name": ..., "arguments": ...}}
            tool_calls_acc: dict[int, dict[str, str]] = {}

            stream = await self._client.chat.completions.create(
                model=self.model,
                messages=self._history,  # type: ignore[arg-type]
                tools=self._openai_tools if self._openai_tools else None,  # type: ignore[arg-type]
                temperature=self.temperature,
                stream=True,
            )

            async for chunk in stream:  # type: ignore[union-attr]
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # Accumulate text content and stream it to the renderer
                if delta.content:
                    content_parts.append(delta.content)
                    yield StreamChunkEvent(text=delta.content)

                # Accumulate tool call deltas
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": "",
                                "name": "",
                                "arguments": "",
                            }
                        acc = tool_calls_acc[idx]
                        if tc_delta.id:
                            acc["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                acc["name"] += tc_delta.function.name
                            if tc_delta.function.arguments:
                                acc["arguments"] += tc_delta.function.arguments

            full_content = "".join(content_parts) or None

            # Signal end of streamed text
            if content_parts:
                yield StreamChunkEvent(text="", done=True)

            # Build history entry
            serialized_tool_calls: list[dict[str, Any]] | None = None
            if tool_calls_acc:
                serialized_tool_calls = [
                    {
                        "id": acc["id"],
                        "type": "function",
                        "function": {
                            "name": acc["name"],
                            "arguments": acc["arguments"] or "{}",
                        },
                    }
                    for acc in (
                        tool_calls_acc[i]
                        for i in sorted(tool_calls_acc)
                    )
                ]

            self._history.append(
                _serialize_assistant_message(full_content, serialized_tool_calls)
            )

            # No tool calls — we're done
            if not tool_calls_acc:
                # If we didn't stream any content, yield it as a single message
                if full_content and not content_parts:
                    yield AssistantMessageEvent(content=full_content)
                return

            # Execute each tool call
            for acc in (tool_calls_acc[i] for i in sorted(tool_calls_acc)):
                mcp_name = api_name_to_mcp(acc["name"])
                try:
                    args = json.loads(acc["arguments"]) if acc["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}
                    logger.warning(
                        "LLM returned invalid JSON arguments for %s: %r",
                        mcp_name, acc["arguments"],
                    )

                yield ToolCallEvent(name=mcp_name, arguments=args)

                try:
                    result = await self._call_tool(mcp_name, args)
                    content_text = _extract_text(result)
                    if result.isError:
                        result_text = f"Error: {content_text}"
                        yield ToolResultEvent(
                            name=mcp_name, result=result_text, is_error=True,
                        )
                    else:
                        result_text = content_text
                        yield ToolResultEvent(
                            name=mcp_name, result=result_text, is_error=False,
                        )
                except Exception as e:
                    result_text = f"Error: {e}"
                    yield ToolResultEvent(
                        name=mcp_name, result=result_text, is_error=True,
                    )

                self._history.append({
                    "role": "tool",
                    "tool_call_id": acc["id"],
                    "content": result_text,
                })

        # Safety valve — force the LLM to summarize without tools
        self._history.append({
            "role": "system",
            "content": (
                "You have made many tool calls. Please summarize your findings "
                "and respond to the user now."
            ),
        })

        content_parts = []
        stream = await self._client.chat.completions.create(
            model=self.model,
            messages=self._history,  # type: ignore[arg-type]
            temperature=self.temperature,
            stream=True,
        )
        async for chunk in stream:  # type: ignore[union-attr]
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                content_parts.append(delta.content)
                yield StreamChunkEvent(text=delta.content)

        final_content = "".join(content_parts) or None
        if content_parts:
            yield StreamChunkEvent(text="", done=True)
        self._history.append(_serialize_assistant_message(final_content))

    @property
    def history(self) -> list[dict[str, Any]]:
        """Current conversation history (read-only view)."""
        return list(self._history)

    @property
    def tool_count(self) -> int:
        return len(self._openai_tools)

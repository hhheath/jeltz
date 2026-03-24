"""Chat client — bridges a local LLM to Jeltz's MCP tools.

Talks to any OpenAI-compatible API (Ollama, llama.cpp, LM Studio, vLLM)
and routes tool calls through JeltzServer in-process. The LLM reasons
about the sensor fleet; Jeltz executes the tool calls against real hardware.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from mcp.types import Tool
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
    """The LLM produced a text response."""

    content: str


ChatEvent = ToolCallEvent | ToolResultEvent | AssistantMessageEvent


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
# System prompt
# ---------------------------------------------------------------------------


def build_system_prompt(server: JeltzServer) -> str:
    """Generate a system prompt from live server state.

    Lists connected devices, their protocols, health, and available tools
    so the LLM knows what it's working with.
    """
    lines: list[str] = []

    if server.aggregator:
        statuses = server.aggregator.all_statuses()
        for name, status in statuses.items():
            if status.healthy:
                health = "healthy"
            elif status.connected:
                health = "connected"
            else:
                health = "disconnected"
            desc = status.device.model.device.description or "no description"
            protocol = status.device.model.connection.protocol
            lines.append(f"- {name} ({protocol}, {health}): {desc}")
            for t in status.device.model.tools:
                lines.append(f"  - {name}.{t.name}: {t.description or ''}")

    device_section = "\n".join(lines) if lines else "No devices connected."

    return f"""\
You are an AI assistant with access to a fleet of physical sensors and devices \
via the Jeltz gateway. You can call tools to read sensors, query history, and \
detect anomalies.

Connected devices:
{device_section}

Fleet-level tools:
- fleet.list_devices: List all devices and their health status
- fleet.get_all_readings: Get latest readings from every sensor at once
- fleet.get_history: Get time-series data for a specific sensor (for trends, drift)
- fleet.search_anomalies: Find sensors deviating from their rolling baseline

When the user asks about their sensors or devices, use the tools to get real \
data before answering. For broad questions like "anything weird?", start with \
fleet.get_all_readings or fleet.search_anomalies. For specific device questions, \
use the device-specific tools.

Always ground your responses in actual sensor data. If a tool call fails, say so \
honestly."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_assistant_message(message: Any) -> dict[str, Any]:
    """Build a history-safe dict from an OpenAI ChatCompletionMessage.

    Only includes fields the API expects back: role, content, tool_calls.
    Avoids dumping the entire Pydantic model which may include fields
    (refusal, audio, annotations) that local APIs reject.
    """
    entry: dict[str, Any] = {"role": "assistant"}
    if message.content:
        entry["content"] = message.content
    if message.tool_calls:
        entry["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in message.tool_calls
        ]
    return entry


def _extract_text(result: Any) -> str:
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
    """Bridges a local LLM to Jeltz's MCP tools via an OpenAI-compatible API."""

    server: JeltzServer
    api_url: str = "http://localhost:11434/v1"
    model: str = "llama3.2"
    api_key: str = field(default="jeltz", repr=False)
    temperature: float = 0.1
    system_prompt: str | None = None

    # Private state
    _openai_tools: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _history: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _client: AsyncOpenAI | None = field(default=None, init=False, repr=False)

    async def initialize(self) -> None:
        """Start the server, load tools, build system prompt, create API client."""
        discovery = await self.server.start()
        for path, error in discovery.errors:
            logger.warning("skipped profile %s: %s", path, error)

        # Convert MCP tools to OpenAI format
        mcp_tools = self.server.handle_list_tools()
        self._openai_tools = convert_tools(mcp_tools)

        # Build system prompt (custom or auto-generated)
        prompt = self.system_prompt or build_system_prompt(self.server)
        self._history = [{"role": "system", "content": prompt}]

        # Create OpenAI client
        self._client = AsyncOpenAI(base_url=self.api_url, api_key=self.api_key)

    async def shutdown(self) -> None:
        """Stop the server and clean up."""
        await self.server.stop()
        if self._client:
            await self._client.close()
            self._client = None

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
        """Inner loop: call LLM, execute tool calls, repeat until done."""
        assert self._client is not None

        for _round in range(MAX_TOOL_ROUNDS):
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=self._history,  # type: ignore[arg-type]
                tools=self._openai_tools if self._openai_tools else None,  # type: ignore[arg-type]
                temperature=self.temperature,
            )

            message = response.choices[0].message
            self._history.append(_serialize_assistant_message(message))

            # No tool calls — yield final response and stop
            if not message.tool_calls:
                if message.content:
                    yield AssistantMessageEvent(content=message.content)
                return

            # Execute each tool call
            for tc in message.tool_calls:
                fn = tc.function  # type: ignore[union-attr]
                mcp_name = api_name_to_mcp(fn.name)
                try:
                    args = json.loads(fn.arguments) if fn.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                    logger.warning(
                        "LLM returned invalid JSON arguments for %s: %r",
                        mcp_name, fn.arguments,
                    )

                yield ToolCallEvent(name=mcp_name, arguments=args)

                try:
                    result = await self.server.handle_call_tool(mcp_name, args)
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
                    "tool_call_id": tc.id,
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
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=self._history,  # type: ignore[arg-type]
            temperature=self.temperature,
        )
        final = response.choices[0].message
        self._history.append(_serialize_assistant_message(final))
        if final.content:
            yield AssistantMessageEvent(content=final.content)

    @property
    def history(self) -> list[dict[str, Any]]:
        """Current conversation history (read-only view)."""
        return list(self._history)

    @property
    def tool_count(self) -> int:
        return len(self._openai_tools)

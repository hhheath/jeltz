"""Profile generator — converts DeviceModel into MCP tool schemas."""

from __future__ import annotations

from typing import Any

from mcp.types import Tool

from jeltz.devices.model import DeviceModel, ToolDefinition, ToolParam

# Map Jeltz profile types to JSON Schema types
_TYPE_MAP: dict[str, str] = {
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "str": "string",
    "string": "string",
    "bool": "boolean",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


def _param_to_json_schema(param: ToolParam) -> dict[str, Any]:
    """Convert a ToolParam to a JSON Schema property definition."""
    schema: dict[str, Any] = {}

    schema["type"] = _TYPE_MAP[param.type]

    if param.description:
        schema["description"] = param.description

    if param.min is not None:
        schema["minimum"] = param.min

    if param.max is not None:
        schema["maximum"] = param.max

    if param.default is not None:
        schema["default"] = param.default

    return schema


def _tool_input_schema(tool_def: ToolDefinition) -> dict[str, Any]:
    """Build a JSON Schema input_schema from a ToolDefinition's params."""
    if not tool_def.params:
        return {"type": "object", "properties": {}}

    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in tool_def.params.items():
        properties[name] = _param_to_json_schema(param)
        if param.required:
            required.append(name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    return schema


def _build_description(tool_def: ToolDefinition) -> str:
    """Build tool description, appending return type/unit info if available."""
    desc = tool_def.description
    if tool_def.returns:
        returns_info: str = tool_def.returns.type
        if tool_def.returns.unit:
            returns_info = f"{returns_info} ({tool_def.returns.unit})"
        desc = f"{desc}. Returns: {returns_info}"
    return desc


def generate_tool(tool_def: ToolDefinition, device_name: str) -> Tool:
    """Generate an MCP Tool from a ToolDefinition, namespaced to a device."""
    namespaced_name = f"{device_name}.{tool_def.name}"

    return Tool(
        name=namespaced_name,
        description=_build_description(tool_def),
        inputSchema=_tool_input_schema(tool_def),
    )


def generate_tools(model: DeviceModel) -> list[Tool]:
    """Generate all MCP Tools for a device profile."""
    device_name = model.device.name
    return [generate_tool(tool, device_name) for tool in model.tools]

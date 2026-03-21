"""Tests for profile generator — DeviceModel → MCP tool schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jeltz.devices.model import (
    ConnectionConfig,
    DeviceConfig,
    DeviceModel,
    HealthConfig,
    ToolDefinition,
    ToolParam,
    ToolReturns,
)
from jeltz.profiles.generator import generate_tool, generate_tools


def _make_device(
    name: str = "test_sensor",
    tools: list[ToolDefinition] | None = None,
) -> DeviceModel:
    return DeviceModel(
        device=DeviceConfig(name=name, description="A test device"),
        connection=ConnectionConfig(protocol="mock"),
        tools=tools or [],
    )


class TestGenerateTool:
    def test_namespaces_tool_name(self) -> None:
        tool_def = ToolDefinition(name="get_reading", description="Read sensor")
        result = generate_tool(tool_def, "my_sensor")
        assert result.name == "my_sensor.get_reading"

    def test_passes_description(self) -> None:
        tool_def = ToolDefinition(name="read", description="Read the value")
        result = generate_tool(tool_def, "dev")
        assert result.description == "Read the value"

    def test_description_includes_returns_type_and_unit(self) -> None:
        tool_def = ToolDefinition(
            name="read",
            description="Read temperature",
            returns=ToolReturns(type="float", unit="celsius"),
        )
        result = generate_tool(tool_def, "dev")
        assert result.description == "Read temperature. Returns: float (celsius)"

    def test_description_includes_returns_type_without_unit(self) -> None:
        tool_def = ToolDefinition(
            name="read",
            description="Read data",
            returns=ToolReturns(type="array"),
        )
        result = generate_tool(tool_def, "dev")
        assert result.description == "Read data. Returns: array"

    def test_description_unchanged_without_returns(self) -> None:
        tool_def = ToolDefinition(name="ping", description="Ping device")
        result = generate_tool(tool_def, "dev")
        assert result.description == "Ping device"

    def test_empty_params_produces_empty_properties(self) -> None:
        tool_def = ToolDefinition(name="ping", description="Ping device")
        result = generate_tool(tool_def, "dev")
        assert result.inputSchema == {"type": "object", "properties": {}}

    def test_int_param_maps_to_integer(self) -> None:
        tool_def = ToolDefinition(
            name="read",
            description="Read",
            params={"channel": ToolParam(type="int", description="Channel number")},
        )
        result = generate_tool(tool_def, "dev")
        props = result.inputSchema["properties"]
        assert props["channel"]["type"] == "integer"
        assert props["channel"]["description"] == "Channel number"

    def test_float_param_maps_to_number(self) -> None:
        tool_def = ToolDefinition(
            name="set_threshold",
            description="Set threshold",
            params={"value": ToolParam(type="float")},
        )
        result = generate_tool(tool_def, "dev")
        assert result.inputSchema["properties"]["value"]["type"] == "number"

    def test_string_param(self) -> None:
        tool_def = ToolDefinition(
            name="cmd",
            description="Send command",
            params={"text": ToolParam(type="str")},
        )
        result = generate_tool(tool_def, "dev")
        assert result.inputSchema["properties"]["text"]["type"] == "string"

    def test_bool_param(self) -> None:
        tool_def = ToolDefinition(
            name="toggle",
            description="Toggle",
            params={"on": ToolParam(type="bool")},
        )
        result = generate_tool(tool_def, "dev")
        assert result.inputSchema["properties"]["on"]["type"] == "boolean"

    def test_min_max_constraints(self) -> None:
        tool_def = ToolDefinition(
            name="read",
            description="Read",
            params={"tank": ToolParam(type="int", min=0, max=5)},
        )
        result = generate_tool(tool_def, "dev")
        prop = result.inputSchema["properties"]["tank"]
        assert prop["minimum"] == 0
        assert prop["maximum"] == 5

    def test_required_params_in_schema(self) -> None:
        tool_def = ToolDefinition(
            name="read",
            description="Read",
            params={
                "channel": ToolParam(type="int", required=True),
                "unit": ToolParam(type="str", default="celsius"),
            },
        )
        result = generate_tool(tool_def, "dev")
        assert result.inputSchema["required"] == ["channel"]

    def test_no_required_field_when_all_optional(self) -> None:
        tool_def = ToolDefinition(
            name="read",
            description="Read",
            params={"mode": ToolParam(type="str", default="auto")},
        )
        result = generate_tool(tool_def, "dev")
        assert "required" not in result.inputSchema

    def test_default_value_included(self) -> None:
        tool_def = ToolDefinition(
            name="read",
            description="Read",
            params={"unit": ToolParam(type="str", default="celsius")},
        )
        result = generate_tool(tool_def, "dev")
        assert result.inputSchema["properties"]["unit"]["default"] == "celsius"

    def test_unknown_type_rejected_at_model_layer(self) -> None:
        with pytest.raises(ValidationError):
            ToolParam(type="bytes")

    def test_multiple_params(self) -> None:
        tool_def = ToolDefinition(
            name="configure",
            description="Configure sensor",
            params={
                "channel": ToolParam(type="int", min=0, max=7),
                "gain": ToolParam(type="float", default=1.0),
                "label": ToolParam(type="str", required=False),
            },
        )
        result = generate_tool(tool_def, "dev")
        props = result.inputSchema["properties"]
        assert len(props) == 3
        assert props["channel"]["type"] == "integer"
        assert props["gain"]["type"] == "number"
        assert props["label"]["type"] == "string"
        assert result.inputSchema["required"] == ["channel"]


class TestGenerateTools:
    def test_empty_tools_list(self) -> None:
        model = _make_device(tools=[])
        result = generate_tools(model)
        assert result == []

    def test_generates_all_tools(self) -> None:
        model = _make_device(
            name="fermentation_temps",
            tools=[
                ToolDefinition(name="get_reading", description="Get one reading"),
                ToolDefinition(name="get_all", description="Get all readings"),
            ],
        )
        result = generate_tools(model)
        assert len(result) == 2
        assert result[0].name == "fermentation_temps.get_reading"
        assert result[1].name == "fermentation_temps.get_all"

    def test_full_profile_round_trip(self) -> None:
        """Parse-like model → generate tools → verify complete schema."""
        model = DeviceModel(
            device=DeviceConfig(
                name="tank_monitor",
                description="6-tank temperature monitoring",
            ),
            connection=ConnectionConfig(
                protocol="serial",
                port="/dev/ttyUSB0",
                baud_rate=115200,
            ),
            tools=[
                ToolDefinition(
                    name="get_reading",
                    description="Get temperature for a specific tank",
                    params={
                        "tank_index": ToolParam(
                            type="int",
                            description="Tank number (0-5)",
                            min=0,
                            max=5,
                        ),
                    },
                    returns=ToolReturns(type="float", unit="celsius"),
                ),
                ToolDefinition(
                    name="get_all_readings",
                    description="Get all 6 tank temperatures",
                    returns=ToolReturns(type="array", unit="celsius"),
                ),
            ],
            health=HealthConfig(
                check_command="PING",
                expected="PONG",
                interval_ms=10000,
            ),
        )
        tools = generate_tools(model)

        assert len(tools) == 2

        t0 = tools[0]
        assert t0.name == "tank_monitor.get_reading"
        assert t0.description == "Get temperature for a specific tank. Returns: float (celsius)"
        assert t0.inputSchema["properties"]["tank_index"] == {
            "type": "integer",
            "description": "Tank number (0-5)",
            "minimum": 0,
            "maximum": 5,
        }
        assert t0.inputSchema["required"] == ["tank_index"]

        t1 = tools[1]
        assert t1.name == "tank_monitor.get_all_readings"
        assert t1.description == "Get all 6 tank temperatures. Returns: array (celsius)"
        assert t1.inputSchema == {"type": "object", "properties": {}}

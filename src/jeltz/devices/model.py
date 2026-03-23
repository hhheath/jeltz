"""Device models — Pydantic representations of parsed TOML profiles."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ParamType = Literal[
    "int", "integer", "float", "number",
    "str", "string", "bool", "boolean",
    "array", "object",
]


class ConnectionConfig(BaseModel):
    """How to connect to a physical device."""

    model_config = {"extra": "allow"}

    protocol: str
    port: str | None = None
    baud_rate: int | None = None
    timeout_ms: int = 5000


class ToolParam(BaseModel):
    """A single parameter for a device tool."""

    type: ParamType
    description: str | None = None
    min: float | None = None
    max: float | None = None
    required: bool = True
    default: Any = None

    @model_validator(mode="after")
    def _check_required_vs_default(self) -> ToolParam:
        if self.default is not None and self.required:
            self.required = False
        return self


class ToolReturns(BaseModel):
    """Return type specification for a device tool."""

    type: ParamType
    unit: str | None = None


class ToolDefinition(BaseModel):
    """A tool exposed by a device."""

    name: str
    description: str
    command: str | None = None
    params: dict[str, ToolParam] = Field(default_factory=dict)
    returns: ToolReturns | None = None


class HealthConfig(BaseModel):
    """Health check configuration for a device."""

    check_command: str
    expected: str
    interval_ms: int = 10000


class RecordingConfig(BaseModel):
    """Controls background recording when running in daemon mode."""

    poll_interval_ms: int = Field(default=5000, ge=100)
    """How often to poll this device's sensors (milliseconds). Default 5s."""

    enabled: bool = True
    """Set to false to disable background recording for this device."""


class DeviceConfig(BaseModel):
    """Top-level device metadata."""

    name: str
    description: str = ""
    handler: str | None = None


class DeviceModel(BaseModel):
    """Complete parsed device profile."""

    device: DeviceConfig
    connection: ConnectionConfig
    tools: list[ToolDefinition] = Field(default_factory=list)
    health: HealthConfig | None = None
    recording: RecordingConfig = Field(default_factory=RecordingConfig)

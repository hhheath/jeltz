"""Mock adapter for testing and development without physical hardware."""

from __future__ import annotations

import asyncio
from typing import Any

from jeltz.adapters.base import AdapterResult, BaseAdapter
from jeltz.devices.model import ConnectionConfig


class MockAdapter(BaseAdapter):
    """Simulated device adapter. Maps commands to canned responses.

    Responses can be provided programmatically (via the ``responses`` kwarg)
    or declaratively in a TOML profile under ``[connection.mock_responses]``.
    Programmatic responses take precedence over profile responses.
    """

    def __init__(
        self,
        config: ConnectionConfig,
        responses: dict[str, Any] | None = None,
        healthy: bool = True,
        delay_ms: float = 0,
    ) -> None:
        super().__init__(config)
        # Start with profile-defined responses, overlay programmatic ones.
        profile_responses: dict[str, Any] = {}
        raw = getattr(config, "mock_responses", None)
        if raw and isinstance(raw, dict):
            profile_responses = {k: str(v) for k, v in raw.items()}
        self.responses: dict[str, Any] = {**profile_responses, **(responses or {})}
        self.healthy = healthy
        self.delay_ms = delay_ms
        self.connected = False
        self.last_command: str | bytes | None = None
        self.send_history: list[str | bytes] = []

    async def _maybe_delay(self) -> None:
        if self.delay_ms > 0:
            await asyncio.sleep(self.delay_ms / 1000)

    async def connect(self) -> AdapterResult:
        await self._maybe_delay()
        self.connected = True
        return AdapterResult.ok()

    async def disconnect(self) -> AdapterResult:
        self.connected = False
        return AdapterResult.ok()

    async def send(self, data: bytes | str) -> AdapterResult:
        if not self.connected:
            return AdapterResult.fail("not connected")
        await self._maybe_delay()
        self.last_command = data
        self.send_history.append(data)
        return AdapterResult.ok()

    async def receive(
        self, length: int | None = None, timeout: float | None = None
    ) -> AdapterResult:
        if not self.connected:
            return AdapterResult.fail("not connected")
        await self._maybe_delay()

        cmd = self.last_command
        if cmd is None:
            return AdapterResult.fail("no command sent")

        key = cmd if isinstance(cmd, str) else cmd.decode("utf-8", errors="replace")
        if key in self.responses:
            return AdapterResult.ok(self.responses[key])

        return AdapterResult.fail(f"unknown command: {key}")

    async def health_check(self) -> AdapterResult:
        if not self.connected:
            return AdapterResult.fail("not connected")
        if self.healthy:
            return AdapterResult.ok({"status": "healthy"})
        return AdapterResult.fail("device unhealthy")

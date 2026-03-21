# Writing Adapters

An adapter is a Python class that handles communication with a device over a specific protocol. Jeltz ships with three adapters:

- **`MockAdapter`** — simulated device for testing
- **`SerialAdapter`** — UART/USB serial via pyserial-asyncio
- **`MQTTAdapter`** — MQTT pub/sub via paho-mqtt

To support a new protocol (Modbus, BLE, CAN bus, HTTP, etc.), implement the `BaseAdapter` interface and register it.

## The interface

Every adapter extends `BaseAdapter` and implements five async methods:

```python
from jeltz.adapters.base import AdapterResult, BaseAdapter
from jeltz.devices.model import ConnectionConfig


class MyAdapter(BaseAdapter):
    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        # Your connection state here

    async def connect(self) -> AdapterResult:
        """Establish the connection."""
        ...

    async def disconnect(self) -> AdapterResult:
        """Close the connection."""
        ...

    async def send(self, data: bytes | str) -> AdapterResult:
        """Send a command to the device."""
        ...

    async def receive(
        self, length: int | None = None, timeout: float | None = None
    ) -> AdapterResult:
        """Read a response from the device."""
        ...

    async def health_check(self) -> AdapterResult:
        """Verify the device is responsive."""
        ...
```

## `AdapterResult`

Adapters **never raise exceptions**. Every method returns an `AdapterResult`:

```python
# Success
return AdapterResult.ok(data)         # data can be any type
return AdapterResult.ok()             # no data (e.g., connect/disconnect)

# Failure
return AdapterResult.fail("reason")   # always include a message
```

This is the core contract. The gateway, aggregator, and fleet tools all depend on adapters returning structured results instead of throwing.

## Method semantics

### `connect()`

- Establish the connection to the device
- Return `ok()` on success, `fail()` on error
- Should be idempotent — if already connected, return `fail("already connected")`
- Timeout: use `self.config.timeout_ms / 1000` for consistency

### `disconnect()`

- Close the connection and release resources
- Should be idempotent — if not connected, return `ok()`
- Best-effort cleanup — if the close itself fails, log it and return `ok()` anyway

### `send(data)`

- `data` is `str` (text command) or `bytes` (binary)
- For text protocols, append a newline or delimiter as needed
- If the connection is broken, return `fail("not connected")`
- On I/O error, transition to disconnected state (see [Error handling](#error-handling))

### `receive(length, timeout)`

- If `length` is `None`: read a complete message (line, frame, etc.) — return text
- If `length` is set: read exactly that many bytes — return raw `bytes`
- If `timeout` is `None`: use `self.config.timeout_ms / 1000`
- Timeout is non-fatal — return `fail("receive timed out")` but stay connected
- I/O errors are fatal — transition to disconnected state

### `health_check()`

- Verify the device is responsive
- Typically sends a command and checks the response
- Return `ok({"status": "healthy", ...})` or `fail("reason")`

## Error handling

**Transition to disconnected on I/O errors.** If a send or receive fails due to a broken connection (not a timeout), the adapter should mark itself as disconnected so subsequent calls fail fast instead of hanging.

Pattern from the serial adapter:

```python
def _mark_disconnected(self) -> None:
    """Clean up and transition to disconnected state."""
    self._connection = None  # or close it

async def send(self, data: bytes | str) -> AdapterResult:
    if not self.connected:
        return AdapterResult.fail("not connected")
    try:
        # ... send data ...
    except Exception as e:
        self._mark_disconnected()
        return AdapterResult.fail(f"send failed: {e}")
    return AdapterResult.ok()
```

**Timeouts are different from errors.** A timeout means the device is slow, not necessarily dead. Don't disconnect on timeout — let the caller decide whether to retry.

## Connection config

Your adapter receives a `ConnectionConfig` in its constructor:

```python
class ConnectionConfig:
    protocol: str           # "serial", "mqtt", "myprotocol"
    port: str | None        # device path (serial) or None
    baud_rate: int | None   # serial baud rate or None
    timeout_ms: int         # default 5000
    # Extra fields allowed — access via getattr
```

The config allows extra fields (`extra = "allow"` in the Pydantic model), so protocol-specific settings go right in the TOML profile:

```toml
[connection]
protocol = "modbus"
port = "/dev/ttyUSB0"
slave_id = 1
register_start = 100
timeout_ms = 3000
```

Access them in your adapter:

```python
slave_id = getattr(self.config, "slave_id", 1)
```

## Registering your adapter

Add your adapter to the registry in `src/jeltz/gateway/discovery.py`:

```python
from jeltz.adapters.base import BaseAdapter
from jeltz.adapters.mock import MockAdapter
from jeltz.adapters.mqtt import MQTTAdapter
from jeltz.adapters.serial import SerialAdapter
from jeltz.adapters.myprotocol import MyProtocolAdapter  # your adapter

_ADAPTER_REGISTRY: dict[str, type[BaseAdapter]] = {
    "mock": MockAdapter,
    "mqtt": MQTTAdapter,
    "serial": SerialAdapter,
    "myprotocol": MyProtocolAdapter,  # register here
}
```

Or register dynamically at runtime:

```python
from jeltz.gateway.discovery import register_adapter
register_adapter("myprotocol", MyProtocolAdapter)
```

Once registered, any profile with `protocol = "myprotocol"` will use your adapter.

## Testing

### Mock the I/O layer

Don't test against real hardware in CI. Mock the underlying I/O library and test your adapter's logic:

```python
from unittest.mock import AsyncMock, MagicMock, patch

async def test_connect_success():
    config = ConnectionConfig(protocol="myprotocol", timeout_ms=1000)

    with patch("jeltz.adapters.myprotocol.some_io_lib.connect") as mock_conn:
        mock_conn.return_value = MagicMock()
        adapter = MyProtocolAdapter(config)
        result = await adapter.connect()
        assert result.success
```

### What to test

Follow the patterns in `tests/test_adapters/test_serial.py`:

- **Connect:** success, already connected, connection refused, timeout
- **Disconnect:** clean, idempotent, handles close errors
- **Send:** success, not connected, I/O error → disconnects
- **Receive:** success, timeout (non-fatal), I/O error → disconnects, empty response
- **Health check:** success, timeout, not connected
- **Round-trip:** send command → receive response (proves the full path works)
- **Reconnect:** error → disconnected → connect again → works

### Test file structure

```
tests/test_adapters/
├── test_mock.py
├── test_serial.py
├── test_mqtt.py
└── test_myprotocol.py   # your tests
```

## Example: skeleton adapter

Here's a minimal adapter you can start from:

```python
"""My protocol adapter."""

from __future__ import annotations

import asyncio
import logging

from jeltz.adapters.base import AdapterResult, BaseAdapter
from jeltz.devices.model import ConnectionConfig

logger = logging.getLogger(__name__)


class MyProtocolAdapter(BaseAdapter):

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._connection = None

    @property
    def connected(self) -> bool:
        return self._connection is not None

    def _mark_disconnected(self) -> None:
        conn = self._connection
        self._connection = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    async def connect(self) -> AdapterResult:
        if self.connected:
            return AdapterResult.fail("already connected")
        try:
            # Replace with your actual connection logic
            self._connection = await asyncio.wait_for(
                self._do_connect(),
                timeout=self.config.timeout_ms / 1000,
            )
        except TimeoutError:
            return AdapterResult.fail("connection timed out")
        except Exception as e:
            return AdapterResult.fail(f"connection failed: {e}")
        return AdapterResult.ok()

    async def disconnect(self) -> AdapterResult:
        if not self.connected:
            return AdapterResult.ok()
        self._mark_disconnected()
        return AdapterResult.ok()

    async def send(self, data: bytes | str) -> AdapterResult:
        if not self.connected:
            return AdapterResult.fail("not connected")
        try:
            # Replace with your actual send logic
            pass
        except Exception as e:
            self._mark_disconnected()
            return AdapterResult.fail(f"send failed: {e}")
        return AdapterResult.ok()

    async def receive(
        self, length: int | None = None, timeout: float | None = None
    ) -> AdapterResult:
        if not self.connected:
            return AdapterResult.fail("not connected")
        if timeout is None:
            timeout = self.config.timeout_ms / 1000
        try:
            # Replace with your actual receive logic
            response = await asyncio.wait_for(
                self._do_receive(),
                timeout=timeout,
            )
        except TimeoutError:
            return AdapterResult.fail("receive timed out")
        except Exception as e:
            self._mark_disconnected()
            return AdapterResult.fail(f"receive failed: {e}")
        return AdapterResult.ok(response)

    async def health_check(self) -> AdapterResult:
        if not self.connected:
            return AdapterResult.fail("not connected")
        send_result = await self.send("PING")
        if not send_result.success:
            return send_result
        recv_result = await self.receive()
        if not recv_result.success:
            return recv_result
        return AdapterResult.ok({"status": "healthy"})

    async def _do_connect(self):
        """Replace with actual connection logic."""
        raise NotImplementedError

    async def _do_receive(self):
        """Replace with actual receive logic."""
        raise NotImplementedError
```

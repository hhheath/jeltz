# Writing Profiles

A profile is a TOML file that tells Jeltz how to talk to a device: what protocol it speaks, what commands it understands, and what MCP tools to generate from it.

This guide covers every field in the profile schema, with examples.

## Profile anatomy

Every profile has four sections:

```toml
[device]         # What is this device?
[connection]     # How do I connect to it?
[[tools]]        # What can I ask it to do?
[health]         # How do I check if it's alive? (optional)
```

## `[device]` — metadata

```toml
[device]
name = "tank_pressure"
description = "Pressure sensor on fermentation tank 1"
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique identifier. Used as the tool namespace (`tank_pressure.get_reading`). No spaces — use underscores. Must not be `fleet` (reserved). |
| `description` | no | Human-readable description. Included in the MCP tool metadata — LLMs read this to understand what the device does. |

## `[connection]` — how to connect

### Serial

```toml
[connection]
protocol = "serial"
port = "/dev/ttyUSB0"
baud_rate = 115200
timeout_ms = 3000
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `protocol` | yes | — | `"serial"`, `"mqtt"`, or `"mock"` |
| `port` | yes (serial) | — | Serial device path (`/dev/ttyUSB0`, `/dev/cu.usbserial-0001`) |
| `baud_rate` | no | 9600 | Must match the firmware's `Serial.begin()` rate |
| `timeout_ms` | no | 5000 | How long to wait for a response before timing out |

### MQTT

```toml
[connection]
protocol = "mqtt"
broker = "localhost"
mqtt_port = 1883
timeout_ms = 5000
topic_prefix = "jeltz/my_device"
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `protocol` | yes | — | `"mqtt"` |
| `broker` | no | `"localhost"` | MQTT broker hostname or IP |
| `mqtt_port` | no | 1883 | Broker port |
| `timeout_ms` | no | 5000 | How long to wait for a response |
| `topic_prefix` | no | `"jeltz/device"` | Base topic. Commands go to `{prefix}/cmd`, responses come from `{prefix}/response` |

### Mock (for testing)

```toml
[connection]
protocol = "mock"
```

The mock adapter connects instantly, passes health checks, and returns canned responses. Use it to test profile parsing and MCP tool generation without hardware.

## `[[tools]]` — what the device can do

Each `[[tools]]` block defines one MCP tool. The gateway sends the `command` string to the device and returns the response.

### Basic tool (no parameters)

```toml
[[tools]]
name = "get_temperature"
description = "Get current temperature reading"
command = "READ_TEMP"

[tools.returns]
type = "float"
unit = "celsius"
```

When an LLM calls `my_device.get_temperature`, the gateway sends `READ_TEMP\n` over serial (or publishes it to the MQTT command topic) and returns whatever the device responds.

### Tool with parameters

```toml
[[tools]]
name = "get_reading"
description = "Get temperature for a specific tank"
command = "READ_TEMP {tank_index}"

[tools.params.tank_index]
type = "int"
description = "Tank number (0-5)"
min = 0
max = 5
```

Parameters are interpolated into the command string using Python's `str.format()`. When the LLM calls this tool with `{"tank_index": 3}`, the gateway sends `READ_TEMP 3\n`.

### Parameter fields

Define parameters under `[tools.params.<name>]`:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `type` | yes | — | `"int"`, `"float"`, `"str"`, `"bool"` |
| `description` | no | — | Shown to the LLM to help it use the parameter correctly |
| `min` | no | — | Minimum value (numeric types only) |
| `max` | no | — | Maximum value (numeric types only) |
| `required` | no | `true` | Whether the parameter must be provided |
| `default` | no | — | Default value if not provided. Setting a default makes the param optional. |

### Multiple parameters

```toml
[[tools]]
name = "set_threshold"
description = "Set alert threshold for a sensor"
command = "SET_THRESH {sensor_id} {value}"

[tools.params.sensor_id]
type = "int"
description = "Sensor index"
min = 0
max = 7

[tools.params.value]
type = "float"
description = "Threshold value"
```

### Return types

```toml
[tools.returns]
type = "float"
unit = "celsius"
```

| Field | Required | Description |
|-------|----------|-------------|
| `type` | yes | `"int"`, `"float"`, `"str"`, `"bool"`, `"array"`, `"object"` |
| `unit` | no | Unit label (e.g., `"celsius"`, `"psi"`, `"percent"`). Appended to the tool description so the LLM knows what it's looking at. |

**Important:** Only `float`, `int`, `number`, and `integer` return types are automatically recorded to the time-series store. Tools that return `string`, `array`, or `object` are not stored — fleet tools like `fleet.get_history` and `fleet.search_anomalies` won't include them.

### Tool descriptions matter

The LLM reads the `description` field to decide when and how to use a tool. Write descriptions that help it reason:

```toml
# Bad — the LLM doesn't know when to use this
description = "Get data"

# Good — the LLM knows this is temperature, from a specific place
description = "Get current temperature from the glycol chiller return line"
```

## `[health]` — liveness check

```toml
[health]
check_command = "PING"
expected = "PONG"
interval_ms = 10000
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `check_command` | yes | — | Command to send for the health check |
| `expected` | yes | — | Expected response (exact match) |
| `interval_ms` | no | 10000 | How often to check (milliseconds) |

Health checks are optional but recommended. Without them, `jeltz status` can't tell you if a device is responsive.

## The firmware contract

The profile defines what commands the gateway sends. The firmware defines what the device does with them. They must agree.

**Rules:**
1. Commands are newline-terminated strings (serial) or plain string payloads (MQTT)
2. Responses are newline-terminated strings (serial) or plain string payloads (MQTT)
3. One command → one response. The gateway sends a command and waits for a single line back.
4. Responses should be plain values: `22.5`, `OK`, `PONG`. No JSON wrapping needed.
5. For numeric tools (`type = "float"` or `"int"`), the response must be parseable as a number.

**Example firmware (Arduino):**
```cpp
if (cmd == "READ_TEMP") {
  Serial.println(dht.readTemperature());   // prints "22.50"
} else if (cmd == "PING") {
  Serial.println("PONG");
}
```

**Example firmware (MicroPython over MQTT):**
```python
if cmd == "READ_TEMP":
    sensor.measure()
    client.publish(RESP_TOPIC, str(sensor.temperature()))  # publishes "22.5"
elif cmd == "PING":
    client.publish(RESP_TOPIC, "PONG")
```

## Complete examples

See the built-in profiles for fully worked examples with firmware:
- [`profiles/serial_sensor.toml`](../profiles/serial_sensor.toml) — serial protocol, Arduino + MicroPython firmware
- [`profiles/mqtt_sensor.toml`](../profiles/mqtt_sensor.toml) — MQTT protocol, MicroPython firmware

## Testing your profile

Test without hardware using the mock adapter:

1. Copy your profile and change `protocol = "mock"`:

```bash
cp profiles/my_device.toml profiles/my_device_mock.toml
# Edit: change protocol to "mock"
```

2. Check it parses:

```bash
jeltz test profiles/my_device_mock.toml
```

3. Check the generated tools look right:

```bash
jeltz status -p profiles
```

When you're ready to test with real hardware, use the real profile:

```bash
jeltz test profiles/my_device.toml
```

## Tips

- **Keep device names short and descriptive.** They become tool prefixes: `tank_1.get_temperature` reads better than `fermentation_tank_temperature_sensor_bay_a.get_temperature`.
- **One profile per physical device.** If you have 3 ESP32s, create 3 profiles with different names and ports.
- **Use `timeout_ms` generously.** Some sensors (DHT22, load cells) take 1-2 seconds to read. Set the timeout to at least 2x the expected read time.
- **Don't namespace commands.** The gateway handles namespacing — your firmware just needs to handle `READ_TEMP`, not `tank_1.READ_TEMP`.

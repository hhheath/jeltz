# Self-Describing Device Protocol

Jeltz devices can describe themselves to the gateway over serial, eliminating the need for a TOML profile. The gateway sends a describe command, the device responds with a JSON capability description, and from that point on the device behaves identically to a TOML-defined device.

This protocol is implemented by the [jeltz-arduino](https://github.com/heath0xFF/jeltz-arduino) C++ library.

## Protocol version

Current version: **1**

## Handshake

```
Gateway → Device:   JELTZ_DESCRIBE\n
Device  → Gateway:  {"v":1,"name":"dht22",...}\n
```

The gateway sends `JELTZ_DESCRIBE\n` to a serial port. If a Jeltz device is listening, it responds with a single line of compact JSON (no embedded newlines) terminated by `\n`. The JSON contains everything the gateway needs to construct a `DeviceModel` — device name, description, tools, parameters, return types, and health check config.

Non-Jeltz devices will either not respond (timeout) or respond with something that isn't valid JSON. Both cases are handled gracefully — the gateway skips the port and logs a warning.

After a successful describe handshake, all subsequent commands use the standard Jeltz text protocol (`COMMAND\n` → `RESPONSE\n`), identical to TOML-defined devices.

## Describe response schema

```json
{
  "v": 1,
  "name": "fermentation_temps",
  "desc": "DHT22 temperature and humidity sensor",
  "health": {
    "cmd": "PING",
    "expect": "PONG",
    "ms": 10000
  },
  "tools": [
    {
      "name": "get_temperature",
      "desc": "Get current temperature reading",
      "cmd": "READ_TEMP",
      "returns": { "type": "float", "unit": "celsius" }
    },
    {
      "name": "get_humidity",
      "desc": "Get current relative humidity reading",
      "cmd": "READ_HUMID",
      "returns": { "type": "float", "unit": "percent" }
    },
    {
      "name": "read_channel",
      "desc": "Read a specific ADC channel",
      "cmd": "READ {channel}",
      "params": {
        "channel": { "type": "int", "min": 0, "max": 3, "desc": "ADC channel number" }
      },
      "returns": { "type": "float", "unit": "volts" }
    }
  ]
}
```

### Field reference

Field names are deliberately short to minimize payload size on memory-constrained devices. A typical 2-tool device produces 350-450 bytes.

#### Top-level fields

| Field | Required | Description |
|-------|----------|-------------|
| `v` | yes | Protocol version. Must be `1`. |
| `name` | yes | Device name. Used for MCP tool namespacing (`name.tool_name`). Must be unique across the fleet. Must not be `fleet` (reserved). |
| `desc` | no | Human-readable device description. Defaults to `""`. |
| `health` | no | Health check configuration. |
| `tools` | yes | Array of tool definitions. Must contain at least one tool. |

#### Health object

| Field | Required | Description |
|-------|----------|-------------|
| `cmd` | yes | Command string sent for health checks (e.g., `"PING"`). |
| `expect` | yes | Expected response (exact match, e.g., `"PONG"`). |
| `ms` | no | Health check interval in milliseconds. Defaults to `10000`. |

#### Tool object

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Tool name. Combined with device name for MCP namespacing. |
| `desc` | no | Human-readable description. Helps LLMs understand what the tool does. |
| `cmd` | yes | Command template sent to the device. Supports `{param_name}` placeholders for parameter interpolation. |
| `params` | no | Dictionary of parameter definitions, keyed by parameter name. |
| `returns` | no | Return type specification. |

#### Parameter object

| Field | Required | Description |
|-------|----------|-------------|
| `type` | yes | Parameter type: `"int"`, `"float"`, `"str"`, `"bool"`. |
| `desc` | no | Human-readable parameter description. |
| `min` | no | Minimum value (numeric types only). |
| `max` | no | Maximum value (numeric types only). |
| `required` | no | Whether the parameter is required. Defaults to `true`. |
| `default` | no | Default value if not provided. Setting this implicitly makes the parameter optional. |

#### Returns object

| Field | Required | Description |
|-------|----------|-------------|
| `type` | yes | Return type: `"int"`, `"float"`, `"str"`, `"bool"`. |
| `unit` | no | Unit of measurement (e.g., `"celsius"`, `"percent"`, `"psi"`). |

## Mapping to DeviceModel

The gateway parses the describe response into the same `DeviceModel` used by TOML profiles. Connection config (protocol, port, baud rate) comes from the port the gateway probed, not from the device.

| JSON field | DeviceModel field |
|------------|-------------------|
| `name` | `device.name` |
| `desc` | `device.description` |
| `health.cmd` | `health.check_command` |
| `health.expect` | `health.expected` |
| `health.ms` | `health.interval_ms` |
| `tools[].name` | `tools[].name` |
| `tools[].desc` | `tools[].description` |
| `tools[].cmd` | `tools[].command` |
| `tools[].params` | `tools[].params` (dict of `ToolParam`) |
| `tools[].returns.type` | `tools[].returns.type` |
| `tools[].returns.unit` | `tools[].returns.unit` |

## Command execution

After discovery, commands work identically to TOML-defined devices:

1. LLM calls `fermentation_temps.read_channel` with `{"channel": 2}`
2. Aggregator interpolates the command template: `"READ {channel}"` → `"READ 2"`
3. Gateway sends over serial: `READ 2\n`
4. Device parses the command, reads channel 2, responds: `3.14\n`
5. Gateway returns the value to the LLM

The device firmware handles command parsing in its own command loop. The jeltz-arduino library provides a helper (`Jeltz::arg()`) for extracting positional arguments from command strings, but it's optional.

## Gateway discovery

The gateway probes specific serial ports for self-describing devices using the `--probe` CLI flag:

```bash
jeltz start -p profiles --probe /dev/ttyUSB0 --probe /dev/ttyACM0
jeltz daemon -p profiles --probe /dev/ttyUSB0
```

For each probed port, the gateway:

1. Opens the serial connection
2. Sends `JELTZ_DESCRIBE\n`
3. Reads the response (with timeout)
4. Parses JSON → `DeviceModel`
5. Creates a `DiscoveredDevice` with the existing `SerialAdapter`
6. Merges into the fleet alongside TOML-defined devices

Self-describing and TOML-defined devices coexist in the same fleet. The Aggregator doesn't distinguish between them — both are `DiscoveredDevice(model, adapter)`.

Interactive discovery is also available:

```bash
jeltz scan                          # Probe common ports
jeltz scan --port /dev/ttyUSB0      # Probe a specific port
```

## Error handling

| Scenario | Gateway behavior |
|----------|-----------------|
| No response within timeout | Skip port, log warning |
| Response is not valid JSON | Skip port, log warning |
| Missing `v` field | Skip, log warning |
| `v` is not `1` | Skip, log warning ("device requires newer gateway") |
| Missing required fields (`name`, `tools`) | Skip, log validation error |
| Duplicate device name | Skip, log error |
| Device name is `fleet` (reserved) | Skip, log error |

All errors are non-fatal. The gateway continues loading other devices.

## Arduino library usage

The [jeltz-arduino](https://github.com/heath0xFF/jeltz-arduino) C++ library implements the device side of this protocol:

```cpp
#include <Jeltz.h>

Jeltz babel("dht22_sensor", "DHT22 temp + humidity");

void setup() {
    Serial.begin(115200);

    babel.tool("get_temperature", "Get current temperature")
         .command("READ_TEMP")
         .returns("float", "celsius");

    babel.tool("get_humidity", "Get current humidity")
         .command("READ_HUMID")
         .returns("float", "percent");

    babel.health("PING", "PONG");
    babel.begin(Serial);
}

void loop() {
    if (Serial.available()) {
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();

        if (babel.handle(cmd)) return;

        if (cmd == "READ_TEMP") {
            Serial.println(dht.readTemperature());
        } else if (cmd == "READ_HUMID") {
            Serial.println(dht.readHumidity());
        }
    }
}
```

`babel.handle(cmd)` returns `true` if the command was `JELTZ_DESCRIBE` or the configured health check command, handling the response internally. All other commands fall through to the firmware author's dispatch logic.

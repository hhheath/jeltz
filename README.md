# Jeltz

> **Status: Early development.** Profile parsing, adapter interface, and mock adapter are working. Gateway, CLI, storage, and fleet tools are in progress. Star/watch to follow along.

**Your sensors will be processed.**

Jeltz is an open source framework that connects AI assistants to physical devices — sensors, actuators, cameras, and edge ML boards — via the [Model Context Protocol](https://modelcontextprotocol.io/) (MCP).

Write a TOML profile describing your hardware. Jeltz generates an MCP server. Plug in your device. Now any MCP client can reason across your entire sensor fleet.

```
$ jeltz start
✓ Gateway running on http://localhost:8420
✓ Discovered 8 devices, exposing 23 tools
✓ MCP endpoint ready
```

A dashboard shows you numbers. Jeltz lets an LLM *reason about what they mean*:

```
You: Anything weird going on?

LLM: Mostly nominal, but two things worth attention:

1. Tanks 2 and 5 are both climbing at 0.3°C/hr, but tank 5
   started rising 40 minutes after tank 2. They share glycol
   loop B — this looks like a chiller issue, not individual
   tank problems. I'd check the loop B circulation pump.

2. The humidity sensor in Bay 6 has been flatlined at exactly
   45.0% for 9 hours. That's almost certainly a stuck sensor,
   not perfect stability. Recommend recalibration.

Everything else is within baseline.
```

That's cross-device correlation, anomaly interpretation, and root cause inference — the stuff a Grafana panel can't do.

## Why Jeltz exists

Dashboards show you numbers. They require you to know which panel to look at, already suspect something is wrong, and manually correlate across devices. Jeltz gives your sensor fleet a voice.

The MCP ecosystem has hundreds of integrations for SaaS tools — Slack, GitHub, databases, APIs. But nothing for the physical world. Meanwhile, the people with the most data (factories, labs, breweries, farms) are the ones with the least tooling to make sense of it.

Jeltz fixes this by making physical devices first-class participants in the MCP ecosystem. The value isn't "read a sensor value in English" — it's what an LLM can do when it has access to *all* your devices at once:

- **Cross-device reasoning.** Correlate readings across sensors to identify shared root causes (failing chiller, not individual tank problems).
- **Anomaly interpretation.** Not just "this number is red" — but *why* it's anomalous and what it probably means (stuck sensor vs real reading, bearing wear vs imbalance).
- **Multi-step investigation.** "Check all sensors on Line 4, find any drifting from their 30-day baseline, and tell me if the drift correlates with the ambient temp sensor." One sentence, 45 minutes of manual work eliminated.
- **Proactive insight.** An operator who says "anything weird?" gets answers about devices they weren't thinking to check.

## How it works

Jeltz runs on a gateway device (Raspberry Pi, laptop, Qualcomm IQ9, any Linux box). It connects to your physical devices over their native protocols (serial, MQTT, USB, HTTP) and exposes them as MCP tools through a single unified endpoint.

![Jeltz architecture](architecture.svg)

**The AI stays in the cloud (or on a local LLM). Jeltz is just the plumbing that connects it to your devices.** An MCP server is not an AI model — it's a lightweight protocol endpoint. It uses negligible resources on the gateway.

## Two ways to add devices

### Path 1: TOML profiles (no firmware changes)

For devices you don't control the firmware on, or simple sensors with text-based command protocols. Write a TOML file:

```toml
[device]
name = "fermentation_temps"
description = "Temperature monitoring for 6 fermentation tanks"

[connection]
protocol = "serial"
port = "/dev/ttyUSB0"
baud_rate = 115200
timeout_ms = 1000

[[tools]]
name = "get_reading"
description = "Get current temperature for a specific tank"

[tools.params.tank_index]
type = "int"
min = 0
max = 5

[tools.returns]
type = "float"
unit = "celsius"

[[tools]]
name = "get_all_readings"
description = "Get all 6 tank temperatures at once"

[tools.returns]
type = "array"
unit = "celsius"

[health]
check_command = "PING"
expected = "PONG"
interval_ms = 10000
```

```
$ jeltz add-device fermentation_temps.toml
$ jeltz start
```

Done. Any LLM can now read your fermentation temps.

### Path 2: Arduino library (self-describing devices)

For devices where you write the firmware. Add the `jeltz-arduino` library and register your tools in C++:

```cpp
#include <jeltz.h>

Jeltz jeltz("fermentation_temps");

void setup() {
  Serial.begin(115200);

  jeltz.addTool("get_reading", "Get tank temperature",
    [](JsonObject params) {
      int tank = params["tank_index"];
      return jeltz.result(readProbe(tank), "celsius");
    });

  jeltz.begin();
}

void loop() {
  jeltz.poll();
}
```

The device self-describes to the gateway — no TOML profile needed. Just plug it in.

*(`jeltz-arduino` is coming in Phase 2)*

## Built-in profiles (planned)

Jeltz will ship with profiles for common hardware:

| Sensor | Protocol | Description |
|--------|----------|-------------|
| DS18B20 | Serial | Dallas 1-Wire temperature sensor |
| DHT22 | Serial | Temperature + humidity |
| BME280 | Serial/I2C | Temperature + humidity + barometric pressure |
| HX711 | Serial | Load cell / weight sensor |
| Generic ESP32 | Serial | Any ESP32 with a text command protocol |
| RPi Pico W | MQTT | Pico W publishing sensor data over MQTT |

## Installation

> Not yet published to PyPI. For now, install from source:

```bash
git clone https://github.com/hhheath/jeltz.git
cd jeltz
pip install -e .
```

## Quickstart (coming soon)

Once the gateway and CLI are implemented:

```bash
# Initialize a new Jeltz project
jeltz init my-sensors
cd my-sensors

# Add a device using a built-in profile
jeltz add-device ds18b20 --port /dev/ttyUSB0

# Test the device connection (uses mock if no hardware present)
jeltz test ds18b20

# Start the gateway
jeltz start

# Check status
jeltz status
```

Then add to your MCP client config:

```json
{
  "mcpServers": {
    "jeltz": {
      "url": "http://localhost:8420/mcp"
    }
  }
}
```

## Use cases

**Edge AI development loop.** Deploy a model to a dev board with Edge Impulse, then use an LLM of your choice to run inference batches across 50 test samples, generate a confusion matrix, pull raw sensor data for misclassifications, and identify what additional training data you need — all in one conversation instead of a half-day manual investigation.

**Small-scale industrial monitoring.** A brewery with 6 fermentation tanks and a glycol chiller. A machine shop with vibration sensors on 4 presses. A research lab with 20 environmental monitors. Too small for Azure IoT Hub, too important for a spreadsheet. The LLM correlates across devices, interprets drift patterns, and catches the stuck sensor you wouldn't notice on a dashboard for days.

**Natural language commissioning.** "I just installed 3 new pressure sensors on the coolant lines. Run a health check on all three, verify they're reading within spec, and set alert thresholds at 15% above their current baseline." One sentence drives a multi-tool workflow: discover, read, calculate, configure.

**Air-gapped / local-first deployments.** Pair Jeltz with a local LLM (llama.cpp on a Raspberry Pi 5 or Qualcomm IQ9) for fully offline, data-sovereign AI interaction with your equipment. No cloud, no internet, no data leaving the building. The LLM handles tool routing locally; the reasoning stays on-site.

**Home automation with brains.** Bridge your DIY sensors into any MCP-compatible AI assistant. Not just "turn on the lights" — "the humidity in the basement has been climbing for 3 days and the sump pump hasn't cycled. You might have a drainage issue."

## Roadmap

- **Phase 1 (in progress):** Core framework, serial + MQTT adapters, SQLite time-series storage, fleet-level tools (cross-device reads, history, anomaly search), CLI, built-in profiles, mock adapter for testing
- **Phase 2:** `jeltz-arduino` C++ library, USB adapter, community profile repository, device namespacing
- **Phase 3:** Edge Impulse integration, local LLM integration, web dashboard, alert system, event streaming
- **Phase 4:** Additional protocol adapters (Modbus, BLE, CAN bus), IQ9 reference deployment, additional ML platform integrations

## Development

```bash
hatch run test        # Run tests (mock adapter, no hardware needed)
hatch run lint        # Ruff linter
hatch run typecheck   # Mypy strict mode
```

Requires Python 3.11+.

## Contributing

Jeltz is built in public. Follow along and contribute:

- **Profiles:** The easiest way to contribute. Write a TOML profile for hardware you own, test it, submit a PR.
- **Adapters:** New protocol support. Implement the `BaseAdapter` interface for your protocol.
- **Core:** Gateway, aggregator, CLI improvements.

## Why "Jeltz"?

Named after Prostetnic Vogon Jeltz from *The Hitchhiker's Guide to the Galaxy* — the captain who processes everything, methodically, without exception. Every tool call, every sensor reading, every health check gets routed, validated, and delivered.

Your sensors will be processed.

## License

Apache 2.0
